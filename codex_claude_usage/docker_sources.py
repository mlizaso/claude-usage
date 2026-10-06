"""Read local Docker transcripts without executing anything in containers.

The CLI supplies discovery and archive reads on all supported host platforms.
Only local daemon endpoints are accepted. Already-scanned bind mounts reuse
local logs; other JSONL files go into a private cache beside the database.
Container paths never become host paths during archive import.
"""

import atexit
import contextlib
import errno
import hashlib
import json
import os
import queue
import re
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

from .db import UnsafeDatabasePathError, secure_db_permissions
from .safefile import open_regular_file_descriptor

MAX_CONTAINERS = 256
MAX_MEMBERS = 100_000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
COLLECTION_SECONDS = 60
COMMAND_SECONDS = 15
INSPECT_COMMAND_SECONDS = 5
CONTAINER_WORKERS = 4

_STATUS = {"state": "pending", "containers": 0, "sources": 0}
_STATUS_LOCK = threading.Lock()
_COMMANDS_LOCK = threading.Lock()
_COMMANDS = set()
_SHUTTING_DOWN = False
_INSPECT_FORMAT = (
    '{"id":{{json .Id}},"user":{{json .Config.User}},'
    '"env":['
    '{{range .Config.Env}}{{$key := index (split . "=") 0}}'
    '{{if or (eq $key "HOME") (eq $key "CODEX_HOME") '
    '(eq $key "CLAUDE_CONFIG_DIR")}}{{json .}},{{end}}{{end}}null],'
    '"mounts":{{json .Mounts}}}'
)


class DockerReadError(Exception):
    """A bounded Docker read failed; its untrusted stderr is kept private."""


class MissingPath(DockerReadError):
    """A container has never used this transcript directory."""


def _shutdown_commands():
    """Stop our own Docker clients when daemon workers are abandoned at exit."""
    global _SHUTTING_DOWN
    with _COMMANDS_LOCK:
        _SHUTTING_DOWN = True
        processes = list(_COMMANDS)
    for process in processes:
        try:
            process.kill()
        except OSError:
            pass  # A command may already have exited and been reaped.


atexit.register(_shutdown_commands)


@contextlib.contextmanager
def _process(cli, args):
    # Serialize spawn/registration with shutdown, so no child can be started
    # after the exit hook has taken its snapshot of clients to terminate.
    with _COMMANDS_LOCK:
        if _SHUTTING_DOWN:
            raise DockerReadError("Docker collection is shutting down")
        process = subprocess.Popen(
            [cli, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=_docker_environment())
        _COMMANDS.add(process)
    try:
        with process:
            yield process
    finally:
        with _COMMANDS_LOCK:
            _COMMANDS.discard(process)


def status():
    with _STATUS_LOCK:
        return dict(_STATUS)


def _set_status(state, containers=0, sources=0):
    global _STATUS
    with _STATUS_LOCK:
        _STATUS = {"state": state, "containers": containers, "sources": sources}


def _docker_cli():
    # Do not execute a workspace's docker from PATH. These include Docker
    # Desktop's per-user installation and work with the VSIX's stripped PATH.
    if os.name == "nt":
        candidates = [Path(os.environ.get("ProgramFiles", "C:/Program Files"))
                      / "Docker/Docker/resources/bin/docker.exe"]
    else:
        candidates = [Path(p) for p in (
            "/usr/local/bin/docker", "/opt/homebrew/bin/docker", "/usr/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker")]
        candidates.append(Path.home() / ".docker/bin/docker")
    return next((str(p) for p in candidates
                 if p.is_file() and os.access(p, os.X_OK)), None)


def _docker_environment():
    # No API keys, credential commands, remote context overrides or proxy env.
    allowed = {"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
               "LOCALAPPDATA", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP"}
    result = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    result["LC_ALL"] = "C"
    return result


class _BoundedPipe:
    def __init__(self, pipe, limit):
        self.pipe = pipe
        self.remaining = limit

    def read(self, size=-1):
        # Tar metadata can ask for an attacker-sized PAX header in one read.
        if size < 0 or size > 1024 * 1024:
            raise DockerReadError("oversized Docker read")
        data = self.pipe.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise DockerReadError("Docker output limit reached")
        return data


@contextlib.contextmanager
def _command(cli, args, deadline, limit, command_seconds=COMMAND_SECONDS):
    timeout = min(command_seconds, deadline - time.monotonic())
    if timeout <= 0:
        raise DockerReadError("Docker collection deadline reached")
    with _process(cli, args) as process:
        expired = threading.Event()
        errors = []

        def stop():
            expired.set()
            try:
                process.kill()
            except OSError:
                pass

        def read_errors():
            errors.append(process.stderr.read(4097))
            if len(errors[0]) > 4096:
                stop()

        timer = threading.Timer(timeout, stop)
        timer.daemon = True
        reader = threading.Thread(target=read_errors, daemon=True)
        timer.start()
        reader.start()
        try:
            output = _BoundedPipe(process.stdout, limit)
            archive_error = None
            try:
                yield output
            except tarfile.TarError as exc:
                # An absent directory produces no tar bytes. Inspect Docker's
                # exit before treating that empty stream as archive corruption.
                archive_error = exc
            # Verify both completion and trailing bytes, even when tar stopped
            # at its end marker before the child finished producing output.
            while output.read(64 * 1024):
                pass
            code = process.wait()
            reader.join()
            if expired.is_set():
                raise DockerReadError("Docker command timed out or exceeded limits")
            if code:
                error = errors[0] if errors else b""
                if b"Could not find the file" in error:
                    raise MissingPath("no transcript directory")
                raise DockerReadError("Docker command failed")
            if archive_error is not None:
                raise archive_error
        finally:
            if process.poll() is None:
                stop()
            process.wait()
            reader.join()
            timer.cancel()


def _read(cli, args, deadline, limit=4 * 1024 * 1024, *, command_seconds=COMMAND_SECONDS):
    with _command(cli, args, deadline, limit, command_seconds=command_seconds) as stream:
        chunks = []
        while chunk := stream.read(64 * 1024):
            chunks.append(chunk)
    return b"".join(chunks)


def _inspect_container(cli, endpoint, identifier, deadline):
    """Inspect one container without retrying a stalled peer in a batch."""
    data = _read(
        cli,
        ["--host", endpoint, "inspect", "--format", _INSPECT_FORMAT, identifier],
        deadline,
        MAX_METADATA_BYTES,
        command_seconds=INSPECT_COMMAND_SECONDS,
    )
    container = json.loads(data)
    if (not isinstance(container, dict) or container.get("id") != identifier
            or not isinstance(container.get("env"), list)
            or not isinstance(container.get("mounts"), list)
            or not all(isinstance(m, dict) for m in container["mounts"])):
        raise DockerReadError("invalid Docker container metadata")
    return container


def _collect_container(cli, endpoint, container, local_roots, cache, deadline):
    """Collect one container's roots without affecting its neighbours."""
    roots = []
    sources = 0
    had_logs = False
    partial = False
    try:
        for root in _homes(cli, endpoint, container, deadline):
            bind = _bind_root(container, root, local_roots)
            if bind is not None:
                roots.append(bind)
                sources += 1
                had_logs = True
                continue
            try:
                if _import_root(cli, endpoint, container["id"], root, cache, deadline):
                    sources += 1
                    had_logs = True
            except MissingPath:
                pass
            except (DockerReadError, tarfile.TarError, OSError, ValueError):
                partial = True
    except (DockerReadError, tarfile.TarError, OSError, ValueError, TypeError,
            RecursionError):
        partial = True
    return roots, sources, had_logs, partial


def _collect_containers(cli, endpoint, identifiers, local_roots, cache, deadline):
    """Yield finished imports from a bounded group of daemon workers.

    Explicit log roots take priority over remaining inspections; inspections
    take priority over speculative home probes. This keeps known usage from
    waiting behind empty containers while bounding all Docker work together.
    Normal scans drain and join all workers before reading the cache; daemon
    ownership also preserves prompt exit of the dashboard's background scan.
    """
    pending = queue.PriorityQueue()
    results = queue.Queue()
    stopped = threading.Event()
    for index in range(len(identifiers)):
        pending.put((1, index, None))

    def run():
        try:
            while not stopped.is_set() and time.monotonic() < deadline:
                try:
                    priority, index, container = pending.get_nowait()
                except queue.Empty:
                    break
                try:
                    if container is None:
                        container = _inspect_container(
                            cli, endpoint, identifiers[index], deadline)
                        priority = 0 if _configured_roots(container) else 2
                        pending.put((priority, index, container))
                        continue
                    result = _collect_container(
                        cli, endpoint, container, local_roots, cache, deadline)
                except (DockerReadError, tarfile.TarError, OSError, ValueError,
                        TypeError, RecursionError):
                    result = ([], 0, False, True)
                except BaseException as exc:
                    stopped.set()
                    results.put(exc)
                    break
                results.put(result)
        finally:
            results.put(None)

    workers = []
    error = None
    try:
        for _ in range(min(CONTAINER_WORKERS, len(identifiers))):
            worker = threading.Thread(target=run, name="docker-usage", daemon=True)
            worker.start()
            workers.append(worker)
        remaining = len(workers)
        while remaining:
            result = results.get()
            if result is None:
                remaining -= 1
            elif isinstance(result, BaseException):
                error = error or result
            else:
                yield result
        for worker in workers:
            worker.join()
        if error is not None:
            raise error
    finally:
        stopped.set()


def _local_endpoint(cli, deadline):
    endpoint = _read(cli, ["context", "inspect", "--format",
                           "{{.Endpoints.docker.Host}}"], deadline, 4096).decode().strip()
    if (not endpoint.startswith(("unix:///", "npipe:////./pipe/"))
            or any(ord(c) < 32 for c in endpoint)):
        raise DockerReadError("automatic collection requires a local Docker context")
    return endpoint


def _archive_reads_supported(cli, endpoint, deadline):
    # docker cp first stats the source using HEAD /archive. Older engines have
    # a mount-redirection race in that operation (CVE-2026-42306).
    version = _read(cli, ["--host", endpoint, "version", "--format",
                          "{{.Server.Version}}"], deadline, 128).decode().strip()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[^\s]+)?", version)
    return bool(match and tuple(map(int, match.groups())) >= (29, 5, 1))


def _container_path(value):
    if (not isinstance(value, str) or len(value) > 4096
            or not value.startswith("/") or "\\" in value
            or any(ord(c) < 32 for c in value)):
        return None
    path = PurePosixPath(value)
    return None if ".." in path.parts else path


def _configured_roots(container):
    """Transcript roots explicitly revealed by environment or log mounts."""
    env = dict(item.split("=", 1) for item in container.get("env", [])
               if isinstance(item, str) and "=" in item)
    roots = set()
    for key, leaf in (("CLAUDE_CONFIG_DIR", "projects"), ("CODEX_HOME", "sessions")):
        if path := _container_path(env.get(key)):
            roots.add(path / leaf)
    for mount in container.get("mounts") or []:
        path = _container_path(mount.get("Destination"))
        if not path:
            continue
        if path.name in (".claude", ".codex"):
            roots.add(path / ("projects" if path.name == ".claude" else "sessions"))
        elif (path.parent.name, path.name) in ((".claude", "projects"), (".codex", "sessions")):
            roots.add(path)
    return roots


def _homes(cli, endpoint, container, deadline):
    """Yield known roots first; optional passwd discovery cannot hide them."""
    configured = _configured_roots(container)
    yield from sorted(configured, key=str)
    env = dict(item.split("=", 1) for item in container.get("env", [])
               if isinstance(item, str) and "=" in item)
    homes = {PurePosixPath("/root")}
    if home := _container_path(env.get("HOME")):
        homes.add(home)
    user = str(container.get("user", "")).split(":", 1)[0]
    discovery_error = None
    try:
        with _command(cli, ["--host", endpoint, "cp",
                            container["id"] + ":/etc/passwd", "-"],
                      deadline, 1024 * 1024) as stream:
            with tarfile.open(fileobj=stream, mode="r|", encoding="utf-8") as archive:
                member = archive.next()
                if (member and member.name == "passwd" and member.isfile()
                        and not member.issparse() and 0 <= member.size <= 128 * 1024):
                    handle = archive.extractfile(member)
                    for line in handle.read().decode("utf-8", "replace").splitlines():
                        fields = line.split(":")
                        if len(fields) != 7:
                            continue
                        home = _container_path(fields[5])
                        if home and (str(home).startswith("/home/")
                                     or user in (fields[0], fields[2])):
                            homes.add(home)
    except MissingPath:
        # Distroless images need not have passwd; explicit HOME still works.
        pass
    except (DockerReadError, tarfile.TarError, OSError, ValueError) as exc:
        discovery_error = exc
    if len(homes) > 32:
        raise DockerReadError("too many container home directories")
    roots = {home / part for home in homes
             for part in (".claude/projects", ".codex/sessions")}
    yield from sorted(roots - configured, key=str)
    if discovery_error is not None:
        # Preserve known logs but retain partial status for failed discovery.
        # Every fallback command still uses the original absolute deadline.
        raise discovery_error


def _bind_root(container, root, local_roots):
    mounts = sorted(container.get("mounts") or [],
                    key=lambda m: len(str(m.get("Destination", ""))), reverse=True)
    for mount in mounts:
        destination = _container_path(mount.get("Destination"))
        if destination and destination != root and destination.is_relative_to(root):
            return None  # A nested mount changes the view inside the container.
    for mount in mounts:
        destination = _container_path(mount.get("Destination"))
        if not destination or not root.is_relative_to(destination):
            continue
        if mount.get("Type") != "bind":
            return None
        source = str(mount.get("Source", ""))
        # Docker Desktop reports its VM's spelling of a macOS host bind.
        if source.startswith("/host_mnt/"):
            source = source[len("/host_mnt"):]
        path = Path(source).joinpath(*root.relative_to(destination).parts)
        # Reuse only a root the host scanner already trusts. Automatically
        # following a container-controlled descendant symlink on the host could
        # escape its bind mount. All newly discovered roots use archive reads.
        if path.is_absolute():
            for local in local_roots:
                if path == Path(os.path.abspath(local)):
                    return Path(local)
        return None
    return None


def _private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or (os.name == "posix"
                and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        raise DockerReadError("unsafe Docker cache directory")
    return path


def _digest(handle, size):
    value = hashlib.sha256()
    while size:
        chunk = handle.read(min(64 * 1024, size))
        if not chunk:
            return None
        value.update(chunk)
        size -= len(chunk)
    return value.digest()


def _validate_cache_lock(handle, path, *, directory):
    opened = os.fstat(handle)
    current = path.lstat()
    matches_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not matches_type(opened.st_mode) or not matches_type(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (not directory and (opened.st_nlink != 1 or current.st_nlink != 1))
            or getattr(current, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or (os.name == "posix"
                and (opened.st_uid != os.getuid() or opened.st_mode & 0o077))):
        raise DockerReadError("unsafe Docker cache lock")


@contextlib.contextmanager
def _cache_update_lock(directory, deadline):
    """Serialize the cache comparison and replacement across scanner processes.

    POSIX locks the private directory, whose identity survives file replacement.
    Windows uses a retained sidecar byte lock. Neither lock is unlinked after
    release, and both are released by the OS if a collector exits.
    """
    is_directory = os.name == "posix"
    path = directory if is_directory else directory / ".import.lock"
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) if is_directory
             else os.O_RDWR | os.O_CREAT)
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    handle = os.open(path, flags, 0o600)
    acquired = False
    try:
        _validate_cache_lock(handle, path, directory=is_directory)
        if is_directory:
            import fcntl
        else:
            import msvcrt
            if os.fstat(handle).st_size == 0:
                os.write(handle, b"\0")
            os.lseek(handle, 0, os.SEEK_SET)
        while True:
            if time.monotonic() >= deadline:
                raise DockerReadError("Docker cache update deadline reached")
            try:
                if is_directory:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        _validate_cache_lock(handle, path, directory=is_directory)
        yield
    finally:
        try:
            if acquired:
                if is_directory:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(handle)


def _copy_member(archive, member, cache, source_key, deadline):
    parts = PurePosixPath(member.name).parts
    # No tar paths are extracted. Hash the full source path and keep only a
    # portable basename, preserving Codex's UUID fallback for headerless logs.
    key = hashlib.sha256((source_key + "\0" + member.name).encode()).hexdigest()
    directory = _private_directory(_private_directory(cache) / key)
    if "subagents" in parts:
        directory = _private_directory(directory / "subagents")
    name = parts[-1]
    if (not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]{0,180}\.jsonl", name)
            or re.match(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])\.", name)):
        name = "transcript.jsonl"
    target = directory / name
    fd, temporary = tempfile.mkstemp(prefix=".import-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output, archive.extractfile(member) as source:
            digest = hashlib.sha256()
            remaining = member.size
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    raise DockerReadError("incomplete container transcript")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        with _cache_update_lock(directory, deadline):
            existing = open_regular_file_descriptor(
                target, follow_symlinks=False, single_link=True, owner_only=True, root=cache)
            if existing is not None:
                with os.fdopen(existing, "rb") as previous:
                    if os.fstat(previous.fileno()).st_size >= member.size:
                        if _digest(previous, member.size) == digest.digest():
                            # Keep the warm-scan identity and any newer suffix.
                            return
            # A last-moment final symlink is replaced, never opened for writing.
            os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _import_root(cli, endpoint, container_id, root, cache, deadline):
    key = hashlib.sha256((endpoint + "\0" + container_id + "\0" + str(root)).encode()).hexdigest()
    found = False
    deadline = min(deadline, time.monotonic() + COMMAND_SECONDS)
    with _command(cli, ["--host", endpoint, "cp", f"{container_id}:{root}", "-"],
                  deadline, MAX_ARCHIVE_BYTES) as stream:
        with tarfile.open(fileobj=stream, mode="r|", encoding="utf-8") as archive:
            for count, member in enumerate(archive):
                if count >= MAX_MEMBERS:
                    raise DockerReadError("too many container archive entries")
                parts = PurePosixPath(member.name).parts
                if (not parts or parts[0] != root.name or ".." in parts
                        or "\\" in member.name or len(member.name) > 4096):
                    raise DockerReadError("unsafe container archive path")
                if member.isdir():
                    continue
                if not member.isfile() or member.issparse():
                    raise DockerReadError("linked or special container transcript")
                if not member.name.endswith(".jsonl"):
                    continue
                if not 0 <= member.size <= MAX_FILE_BYTES:
                    raise DockerReadError("container transcript exceeds size limit")
                _copy_member(archive, member, cache, key, deadline)
                found = True
    return found


def collect(db_path, *, local_roots=()):
    """Return extra local scan roots; failure preserves previously copied logs.

    Called under scanner's lock. Atomic file replacement also makes concurrent
    CLI/VSIX processes safe: readers see a complete old or new transcript, and
    the scanner's existing identity/prefix checks detect concurrent replacement.
    """
    if os.environ.get("CODEX_CLAUDE_USAGE_DOCKER", "1").lower() in ("0", "false", "off"):
        _set_status("disabled")
        return []
    _set_status("scanning")
    roots = []
    containers = sources = 0
    try:
        db_path = secure_db_permissions(db_path)
        cache = db_path.with_name(db_path.name + ".docker-transcripts")
        if cache.exists() or cache.is_symlink():
            roots.append(_private_directory(cache))
        cli = _docker_cli()
        if cli is None:
            _set_status("unavailable" if roots else "not_installed")
            return roots
        deadline = time.monotonic() + COLLECTION_SECONDS
        endpoint = _local_endpoint(cli, deadline)
        if not _archive_reads_supported(cli, endpoint, deadline):
            _set_status("upgrade_required")
            return roots
        ids = _read(cli, ["--host", endpoint, "ps", "--all", "--no-trunc",
                          "--format", "{{.ID}}"], deadline, 128 * 1024).decode().splitlines()
        if not all(re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
            raise DockerReadError("invalid Docker container list")
        partial = len(ids) > MAX_CONTAINERS
        if ids:
            completed = 0
            for found_roots, found_sources, had_logs, failed in _collect_containers(
                    cli, endpoint, ids[:MAX_CONTAINERS], local_roots, cache, deadline):
                completed += 1
                roots.extend(found_roots)
                sources += found_sources
                containers += int(had_logs)
                partial |= failed
                _set_status("scanning", containers, sources)
            partial |= completed < min(len(ids), MAX_CONTAINERS)
            if cache not in roots and cache.is_dir():
                roots.append(_private_directory(cache))
        _set_status("partial" if partial else "ready", containers, sources)
    except (DockerReadError, tarfile.TarError, OSError, ValueError, TypeError,
            RecursionError, UnsafeDatabasePathError):
        _set_status("partial" if sources else "unavailable", containers, sources)
    return list(dict.fromkeys(roots))
