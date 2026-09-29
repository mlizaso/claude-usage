"""Automatic container imports, using real archives and isolated usage DBs."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from claude_usage import docker_sources as docker, scanner
from tests.test_dashboard_js import requires_node, run_js


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", encoding="utf-8") as archive:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.type, member.linkname = value
                archive.addfile(member)
    return output.getvalue()


def claude_turn(identifier="message-1", tokens=100):
    return (json.dumps({
        "type": "assistant", "sessionId": "container-session", "uuid": identifier,
        "timestamp": "2026-09-05T10:00:00Z", "cwd": "/work/project",
        "message": {"id": identifier, "model": "claude-sonnet-4-5", "content": [],
                    "usage": {"input_tokens": tokens, "output_tokens": 10}},
    }) + "\n").encode()


class DockerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "usage.db"
        scanner.get_db(self.db).close()
        self.cache = self.root / "usage.db.docker-transcripts"
        self.container_id = "a" * 64
        self.endpoint = "unix:///test/docker.sock"
        self.metadata = {"id": self.container_id, "user": "1000", "env": [],
                         "workdir": "/work", "mounts": []}
        self.archives = {"/etc/passwd": archive_bytes({
            "passwd": b"root:x:0:0::/root:/bin/sh\ndev:x:1000:1000::/home/dev:/bin/sh\n"})}
        self.commands = []
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_USAGE_DOCKER": "1",
                                                       "CLAUDE_USAGE_PROJECTS_DIRS": ""}))
        self.enterContext(mock.patch.object(docker, "_docker_cli", return_value="docker"))
        self.enterContext(mock.patch.object(docker, "_read", side_effect=self.read))
        self.enterContext(mock.patch.object(docker, "_command", side_effect=self.command))
        self.addCleanup(docker._set_status, "pending")

    def read(self, cli, args, deadline, limit=0, **kwargs):
        self.commands.append(args)
        if args[0] == "context":
            return self.endpoint.encode()
        if "version" in args:
            return b"29.5.2"
        if "ps" in args:
            return self.container_id.encode()
        if "inspect" in args:
            return json.dumps(self.metadata).encode()
        raise AssertionError(args)

    @contextlib.contextmanager
    def command(self, cli, args, deadline, limit):
        self.commands.append(args)
        path = args[-2].split(":", 1)[1]
        if path not in self.archives:
            raise docker.MissingPath()
        yield io.BytesIO(self.archives[path])

    def scan(self, **kwargs):
        return scanner.scan(db_path=self.db, verbose=False, include_docker=True, **kwargs)

    def count(self):
        with contextlib.closing(scanner.get_db(self.db)) as conn:
            return tuple(conn.execute("SELECT COUNT(*), SUM(input_tokens) FROM turns").fetchone())


class TestAutomaticImports(DockerFixture):
    def test_configured_log_containers_are_checked_before_generic_home_probes(self):
        generic = self.metadata
        configured = {**generic, "id": "b" * 64, "env": ["CODEX_HOME=/state/codex"]}
        metadata = {item["id"]: item for item in (generic, configured)}
        visited = []

        def read(cli, args, deadline, limit=0, **kwargs):
            if "ps" in args:
                return "\n".join(metadata).encode()
            if args[0] != "context" and "inspect" in args:
                return "\n".join(json.dumps(metadata[key]) for key in args[5:]).encode()
            return self.read(cli, args, deadline, limit, **kwargs)

        def homes(cli, endpoint, container, deadline):
            visited.append(container["id"])
            return []

        with mock.patch.object(docker, "_read", side_effect=read), \
                mock.patch.object(docker, "_homes", side_effect=homes), \
                mock.patch.object(docker, "CONTAINER_WORKERS", 1):
            docker.collect(self.db)

        self.assertEqual(visited[0], configured["id"])
        self.assertEqual(set(visited), set(metadata))

    def test_metadata_timeouts_leave_time_to_import_healthy_containers(self):
        identifiers = [f"{index + 1:064x}" for index in range(48)]
        stalled = set(identifiers[7::8])
        elapsed = 0.0
        clock_lock = threading.Lock()
        self.archives["/root/.claude/projects"] = archive_bytes({
            "projects/log.jsonl": claude_turn(),
        })

        def now():
            with clock_lock:
                return elapsed

        def read(cli, args, deadline, limit=0, **kwargs):
            nonlocal elapsed
            if "ps" in args:
                return "\n".join(identifiers).encode()
            if args[0] == "context" or "version" in args:
                return self.read(cli, args, deadline, limit, **kwargs)
            self.assertIn("inspect", args)
            selected = args[5:]
            with clock_lock:
                remaining = deadline - elapsed
                if remaining <= 0:
                    raise docker.DockerReadError("collection deadline reached")
                if stalled.intersection(selected):
                    elapsed += min(kwargs["command_seconds"], remaining)
                    raise docker.DockerReadError("metadata timeout")
            return "\n".join(json.dumps({
                "id": identifier, "user": "", "env": [], "mounts": [],
            }) for identifier in selected).encode()

        @contextlib.contextmanager
        def command(cli, args, deadline, limit):
            if now() >= deadline:
                raise docker.DockerReadError("collection deadline reached")
            with self.command(cli, args, deadline, limit) as stream:
                yield stream

        with mock.patch.object(docker, "_read", side_effect=read), \
                mock.patch.object(docker, "_command", side_effect=command), \
                mock.patch.object(docker, "time", types.SimpleNamespace(monotonic=now)):
            result = self.scan()

        self.assertEqual(result["new"], 42)
        self.assertEqual(self.count(), (1, 100))
        self.assertEqual(docker.status(), {
            "state": "partial", "containers": 42, "sources": 42,
        })

    def test_one_stalled_container_does_not_hide_a_healthy_container(self):
        bad = "b" * 64
        good = "c" * 64
        self.archives["/root/.claude/projects"] = archive_bytes({
            "projects/log.jsonl": claude_turn(),
        })

        def read(_cli, args, _deadline, _limit=0, **_kwargs):
            self.commands.append(args)
            if args[0] == "context":
                return self.endpoint.encode()
            if "version" in args:
                return b"29.5.2"
            if "ps" in args:
                return f"{bad}\n{good}\n".encode()
            if "inspect" in args:
                identifiers = args[5:]
                if bad in identifiers:
                    raise docker.DockerReadError("Docker command timed out")
                return (json.dumps({"id": good, "user": "", "env": [], "mounts": []})
                        + "\n").encode()
            raise AssertionError(args)

        with mock.patch.object(docker, "_read", side_effect=read):
            result = self.scan()

        self.assertEqual(result["new"], 1)
        self.assertEqual(self.count(), (1, 100))
        self.assertEqual(docker.status(), {
            "state": "partial", "containers": 1, "sources": 1,
        })

    def test_container_only_usage_is_incremental_and_deduplicates_host_copies(self):
        raw = claude_turn()
        self.archives["/home/dev/.claude/projects"] = archive_bytes({"projects/work/log.jsonl": raw})
        first = self.scan()
        self.assertEqual(first["new"], 1)
        self.assertEqual(self.count(), (1, 100))
        self.assertEqual(docker.status(), {"state": "ready", "containers": 1, "sources": 1})
        cached = next(self.cache.rglob("*.jsonl"))
        identity = cached.stat()
        second = self.scan()
        self.assertEqual(second["skipped"], 1)
        self.assertEqual((cached.stat().st_ino, cached.stat().st_mtime_ns),
                         (identity.st_ino, identity.st_mtime_ns))
        local = self.root / "host"
        local.mkdir()
        (local / "same.jsonl").write_bytes(raw)
        self.scan(projects_dir=local)
        self.assertEqual(self.count(), (1, 100))
        self.archives["/home/dev/.claude/projects"] = archive_bytes({
            "projects/work/log.jsonl": raw + claude_turn("message-2", 200)})
        self.scan(projects_dir=local)
        self.assertEqual(self.count(), (2, 300))

    def test_offline_daemon_keeps_the_previous_import_available(self):
        self.archives["/root/.claude/projects"] = archive_bytes({"projects/log.jsonl": claude_turn()})
        self.scan()
        with mock.patch.object(docker, "_local_endpoint", side_effect=docker.DockerReadError):
            roots = docker.collect(self.db)
        self.assertEqual(roots, [self.cache])
        self.assertEqual(docker.status()["state"], "unavailable")
        self.assertEqual(self.count(), (1, 100))

    def test_headerless_codex_keeps_its_uuid_and_deduplicates_the_host_copy(self):
        from tests.test_codex_transcripts import _turn_context, _token_count
        name = "rollout-2026-09-05T10-00-00-abcdef12-3456-7890-abcd-123456789012.jsonl"
        raw = (_turn_context("gpt-6-astra") + "\n" + _token_count(220, 200, 20) + "\n").encode()
        self.archives["/root/.codex/sessions"] = archive_bytes({"sessions/2026/" + name: raw})
        local = self.root / "host"
        local.mkdir()
        (local / name).write_bytes(raw)
        self.scan(projects_dir=local)
        self.assertEqual(self.count(), (1, 200))
        self.assertEqual(next(self.cache.rglob("*.jsonl")).name, name)

    def test_interrupted_copy_and_size_refusal_preserve_the_complete_cache(self):
        path = "/root/.claude/projects"
        raw = claude_turn()
        self.archives[path] = archive_bytes({"projects/log.jsonl": raw})
        docker.collect(self.db)
        saved = next(self.cache.rglob("*.jsonl"))
        self.archives[path] = archive_bytes({"projects/log.jsonl": raw + claude_turn("m2")})[:600]
        docker.collect(self.db)
        self.assertEqual(saved.read_bytes(), raw)
        self.assertEqual(docker.status()["state"], "partial")
        self.archives[path] = archive_bytes({"projects/log.jsonl": raw + claude_turn("m2")})
        with mock.patch.object(docker, "MAX_FILE_BYTES", len(raw)):
            docker.collect(self.db)
        self.assertEqual(saved.read_bytes(), raw)
        self.assertEqual(docker.status()["state"], "partial")
        self.assertFalse(list(self.cache.rglob(".import-*")))

    def test_an_older_append_only_copy_cannot_erase_a_newer_import(self):
        path = "/root/.claude/projects"
        old = claude_turn()
        recent = old + claude_turn("m2")
        self.archives[path] = archive_bytes({"projects/log.jsonl": recent})
        docker.collect(self.db)
        self.archives[path] = archive_bytes({"projects/log.jsonl": old})
        docker.collect(self.db)
        self.assertEqual(next(self.cache.rglob("*.jsonl")).read_bytes(), recent)

    @unittest.skipUnless(os.name == "posix", "POSIX directory mode and link policies")
    def test_cache_links_and_public_permissions_are_refused(self):
        other = self.root / "elsewhere"
        other.mkdir()
        self.cache.symlink_to(other, target_is_directory=True)
        self.assertEqual(docker.collect(self.db), [])
        self.assertEqual(docker.status()["state"], "unavailable")
        self.assertEqual(list(other.iterdir()), [])
        self.cache.unlink()
        self.cache.mkdir(mode=0o755)
        self.cache.chmod(0o755)
        self.assertEqual(docker.collect(self.db), [])
        self.assertEqual(docker.status()["state"], "unavailable")

    def test_missing_directories_are_normal_and_do_not_create_a_cache(self):
        self.assertEqual(docker.collect(self.db), [])
        self.assertEqual(docker.status(), {"state": "ready", "containers": 0, "sources": 0})
        self.assertFalse(self.cache.exists())

    def test_bind_mounts_are_used_in_place_including_nested_overrides(self):
        local = self.root / "codex"
        (local / "sessions").mkdir(parents=True)
        self.metadata["mounts"] = [
            {"Type": "volume", "Destination": "/home/dev", "Source": "/daemon/volume"},
            {"Type": "bind", "Destination": "/home/dev/.codex", "Source": str(local)},
        ]
        roots = docker.collect(self.db, local_roots=[local / "sessions"])
        self.assertIn(local / "sessions", roots)
        self.assertFalse(any("cp" in cmd and cmd[-2].endswith("/.codex/sessions")
                             and "/home/dev/" in cmd[-2] for cmd in self.commands))

    def test_new_bind_sources_and_nested_mounts_use_the_container_view(self):
        local = self.root / "codex"
        self.metadata["mounts"] = [
            {"Type": "bind", "Destination": "/root/.codex", "Source": str(local)},
        ]
        root = PurePosixPath("/root/.codex/sessions")
        self.assertIsNone(docker._bind_root(self.metadata, root, []))
        self.assertEqual(docker._bind_root(self.metadata, root, [local / "sessions"]), local / "sessions")
        self.metadata["mounts"].append({"Type": "volume", "Destination": str(root / "2026"), "Source": "/daemon/volume"})
        self.assertIsNone(docker._bind_root(self.metadata, root, [local / "sessions"]))

    def test_custom_homes_and_source_directories_are_discovered(self):
        self.metadata["env"] = ["HOME=/workspace/user", "CODEX_HOME=/state/codex",
                                "CLAUDE_CONFIG_DIR=/state/claude"]
        docker.collect(self.db)
        copied = {cmd[-2] for cmd in self.commands if "cp" in cmd}
        for path in ("/workspace/user/.codex/sessions", "/state/codex/sessions", "/state/claude/projects"):
            self.assertIn(self.container_id + ":" + path, copied)

    def test_passwd_failure_keeps_configured_and_known_home_logs(self):
        self.metadata["env"] = ["HOME=/workspace/user", "CLAUDE_CONFIG_DIR=/state/claude"]
        self.archives["/etc/passwd"] = b"invalid archive"
        self.archives["/state/claude/projects"] = archive_bytes({
            "projects/configured.jsonl": claude_turn(),
        })
        self.archives["/workspace/user/.claude/projects"] = archive_bytes({
            "projects/home.jsonl": claude_turn("home-message", 200),
        })
        self.scan()
        self.assertEqual(self.count(), (2, 300))
        self.assertEqual(docker.status(), {"state": "partial", "containers": 1, "sources": 2})
        copied = [cmd[-2].split(":", 1)[1] for cmd in self.commands if "cp" in cmd]
        self.assertLess(copied.index("/state/claude/projects"), copied.index("/etc/passwd"))

    def test_a_passwd_command_failure_is_still_reported_after_importing_logs(self):
        self.metadata["env"] = ["CLAUDE_CONFIG_DIR=/state/claude"]
        self.archives["/state/claude/projects"] = archive_bytes({
            "projects/log.jsonl": claude_turn(),
        })

        @contextlib.contextmanager
        def command(cli, args, deadline, limit):
            if args[-2].endswith(":/etc/passwd"):
                raise docker.DockerReadError("permission denied")
            with self.command(cli, args, deadline, limit) as stream:
                yield stream

        with mock.patch.object(docker, "_command", side_effect=command):
            self.scan()
        self.assertEqual(self.count(), (1, 100))
        self.assertEqual(docker.status()["state"], "partial")

    def test_remote_context_is_never_contacted(self):
        self.endpoint = "ssh://remote-server"
        self.assertEqual(docker.collect(self.db), [])
        self.assertEqual(docker.status()["state"], "unavailable")
        self.assertEqual(len(self.commands), 1)

    def test_an_unsafe_or_unknown_engine_version_never_reads_archives(self):
        for version in (b"28.5.2", b"29.5.0", b"29.5.1-rc.1", b"unexpected"):
            with self.subTest(version=version), mock.patch.object(docker, "_read", return_value=version):
                self.assertFalse(docker._archive_reads_supported("docker", self.endpoint, time.monotonic() + 5))
        with mock.patch.object(docker, "_archive_reads_supported", return_value=False):
            self.assertEqual(docker.collect(self.db), [])
        self.assertEqual(docker.status()["state"], "upgrade_required")
        self.assertFalse(any("cp" in command for command in self.commands))

    def test_one_damaged_root_does_not_hide_another_assistant(self):
        self.archives["/root/.claude/projects"] = b"invalid archive"
        self.archives["/root/.codex/sessions"] = archive_bytes({"sessions/log.jsonl": claude_turn()})
        self.scan()
        self.assertEqual(self.count(), (1, 100))
        self.assertEqual(docker.status()["state"], "partial")

    def test_opt_out_never_invokes_docker(self):
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_DOCKER": "0"}):
            self.assertEqual(docker.collect(self.db), [])
        self.assertEqual(self.commands, [])
        self.assertEqual(docker.status()["state"], "disabled")

    def test_python_api_does_not_discover_containers_by_default(self):
        scanner.scan(projects_dir=self.root, db_path=self.db, verbose=False)
        self.assertEqual(self.commands, [])

    def test_archive_paths_links_and_special_files_cannot_escape_the_cache(self):
        attacks = {
            "projects/../../victim.jsonl": claude_turn(),
            "/projects/absolute.jsonl": claude_turn(),
            "projects/symlink.jsonl": (tarfile.SYMTYPE, str(self.root / "victim")),
            "projects/hardlink.jsonl": (tarfile.LNKTYPE, str(self.root / "victim")),
            "projects/pipe.jsonl": (tarfile.FIFOTYPE, ""),
        }
        victim = self.root / "victim"
        victim.write_bytes(b"unchanged")
        for name, payload in attacks.items():
            with self.subTest(name=name):
                self.archives["/root/.claude/projects"] = archive_bytes({name: payload})
                docker.collect(self.db)
                self.assertEqual(docker.status()["state"], "partial")
                self.assertEqual(victim.read_bytes(), b"unchanged")
                self.assertFalse(list(self.cache.rglob("*.jsonl")))

    def test_archive_import_ignores_non_transcripts_and_preserves_subagent_paths(self):
        self.archives["/root/.claude/projects"] = archive_bytes({
            "projects/work/subagents/agent-123.jsonl": claude_turn(),
            "projects/auth.json": b'{"token":"must-not-copy"}',
        })
        docker.collect(self.db)
        self.assertEqual(len(list(self.cache.rglob("*.jsonl"))), 1)
        self.assertEqual(next(self.cache.rglob("*.jsonl")).parent.name, "subagents")
        self.assertFalse(list(self.cache.rglob("auth.json")))
        if os.name == "posix":
            self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(next(self.cache.rglob("*.jsonl")).stat().st_mode & 0o777, 0o600)


class TestDockerCacheConcurrency(DockerFixture):
    COPY_SCRIPT = textwrap.dedent("""
        import contextlib, errno, os, sys, time
        from pathlib import Path, PurePosixPath
        import tests
        from claude_usage import docker_sources as docker

        cache, archive, control, role, seconds = sys.argv[1:]
        control = Path(control)
        if role == "older":
            replace = os.replace
            def paused_replace(source, target):
                (control / "ready").touch()
                deadline = time.monotonic() + 10
                while not (control / "release").exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("parent did not release the older copy")
                    time.sleep(0.01)
                replace(source, target)
            docker.os.replace = paused_replace
        else:
            if os.name == "posix":
                import fcntl
                lock_module, lock_name = fcntl, "flock"
            else:
                import msvcrt
                lock_module, lock_name = msvcrt, "locking"
            native_lock = getattr(lock_module, lock_name)
            def observed_lock(*args):
                try:
                    return native_lock(*args)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        (control / "waiting").touch()
                    raise
            setattr(lock_module, lock_name, observed_lock)

        @contextlib.contextmanager
        def command(*args, **kwargs):
            with open(archive, "rb") as stream:
                yield stream
        docker._command = command
        try:
            docker._import_root("unused", "unix:///test/docker.sock", "a" * 64,
                                PurePosixPath("/root/.claude/projects"), Path(cache),
                                time.monotonic() + float(seconds))
        except docker.DockerReadError:
            if role != "timeout":
                raise
            (control / "timed-out").touch()
    """)

    def setUp(self):
        super().setUp()
        self.original = claude_turn()
        self.older = self.original + claude_turn("m2")
        self.newer = self.older + claude_turn("m3")
        self.archives["/root/.claude/projects"] = archive_bytes({
            "projects/log.jsonl": self.original,
        })
        docker.collect(self.db)
        self.saved = next(self.cache.rglob("*.jsonl"))

    def start_copy(self, raw, role, seconds=5):
        archive = self.root / f"{role}.tar"
        archive.write_bytes(archive_bytes({"projects/log.jsonl": raw}))
        process = subprocess.Popen(
            [sys.executable, "-c", self.COPY_SCRIPT, str(self.cache), str(archive),
             str(self.root), role, str(seconds)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.addCleanup(self.stop_copy, process)
        return process

    @staticmethod
    def stop_copy(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def finish_copy(self, process):
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output + error)

    def wait_for(self, name, process):
        deadline = time.monotonic() + 5
        while not (self.root / name).exists():
            if process.poll() is not None:
                self.finish_copy(process)
                self.fail(f"copy exited before {name}")
            if time.monotonic() >= deadline:
                self.fail(f"copy did not reach {name}")
            time.sleep(0.01)

    def test_concurrent_older_copy_cannot_erase_a_newer_suffix(self):
        older = self.start_copy(self.older, "older")
        self.wait_for("ready", older)
        newer = self.start_copy(self.newer, "newer")
        deadline = time.monotonic() + 5
        while newer.poll() is None and not (self.root / "waiting").exists():
            if time.monotonic() >= deadline:
                self.fail("newer copy neither finished nor reached the native lock")
            time.sleep(0.01)
        (self.root / "release").touch()
        self.finish_copy(older)
        self.finish_copy(newer)
        self.assertEqual(self.saved.read_bytes(), self.newer)
        self.assertFalse(list(self.cache.rglob(".import-*")))

    def test_lock_timeout_preserves_cache_and_removes_the_temporary_copy(self):
        older = self.start_copy(self.older, "older")
        self.wait_for("ready", older)
        pending = set(self.cache.rglob(".import-*"))
        timed_out = self.start_copy(self.newer, "timeout", seconds=0.1)
        self.finish_copy(timed_out)
        self.assertTrue((self.root / "timed-out").exists())
        self.assertEqual(set(self.cache.rglob(".import-*")), pending)
        self.assertEqual(self.saved.read_bytes(), self.original)
        (self.root / "release").touch()
        self.finish_copy(older)
        self.assertEqual(self.saved.read_bytes(), self.older)

    @unittest.skipUnless(os.name == "posix", "construct POSIX links for the Windows guard")
    def test_windows_lock_refuses_linked_sidecars_without_writing_them(self):
        victim = self.root / "victim"
        victim.write_bytes(b"unchanged")
        lock = self.saved.parent / ".import.lock"
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    lock.symlink_to(victim)
                else:
                    os.link(victim, lock)
                try:
                    with mock.patch.object(docker.os, "name", "nt"), \
                            self.assertRaises((OSError, docker.DockerReadError)):
                        with docker._cache_update_lock(self.saved.parent, time.monotonic() + 1):
                            self.fail("unsafe lock was admitted")
                    self.assertEqual(victim.read_bytes(), b"unchanged")
                finally:
                    lock.unlink()

    @unittest.skipUnless(os.name == "posix", "native directory lock")
    def test_a_replaced_lock_directory_is_refused(self):
        directory = self.saved.parent
        moved = directory.with_name(directory.name + "-moved")
        original_open = os.open

        def swapped_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            directory.rename(moved)
            directory.mkdir(mode=0o700)
            return handle

        with mock.patch.object(docker.os, "open", side_effect=swapped_open), \
                self.assertRaises(docker.DockerReadError):
            with docker._cache_update_lock(directory, time.monotonic() + 1):
                self.fail("replacement directory was admitted")


class TestDockerCommandBounds(unittest.TestCase):
    def test_child_cannot_inherit_credentials_or_remote_context_overrides(self):
        forbidden = ["ANTHROPIC_API_KEY", "DOCKER_HOST", "DOCKER_CONTEXT", "PYTHONPATH", "PATH"]
        script = "import os,json; print(json.dumps(sorted(os.environ)))"
        with mock.patch.dict(os.environ, dict.fromkeys(forbidden, "must-not-inherit")):
            received = json.loads(docker._read(sys.executable, ["-c", script], time.monotonic() + 5))
        self.assertFalse(set(received) & set(forbidden))

    def test_real_process_output_and_error_are_bounded(self):
        deadline = time.monotonic() + 5
        for script, limit in (("print('x' * 20000)", 100),
                              ("import sys; sys.stderr.write('x' * 20000)", 100)):
            with self.subTest(script=script), self.assertRaises(docker.DockerReadError):
                docker._read(sys.executable, ["-c", script], deadline, limit)

    def test_a_stalled_child_is_killed_and_reaped(self):
        started = time.monotonic()
        with self.assertRaises(docker.DockerReadError):
            docker._read(sys.executable, ["-c", "import time; time.sleep(30)"], started + 0.2)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_missing_directory_is_distinguished_from_an_invalid_archive(self):
        script = "import sys; sys.stderr.write('Could not find the file /nope'); sys.exit(1)"
        with self.assertRaises(docker.MissingPath):
            with docker._command(sys.executable, ["-c", script], time.monotonic() + 5, 1024) as stream:
                tarfile.open(fileobj=stream, mode="r|", encoding="utf-8")

    def test_tar_metadata_cannot_request_an_unbounded_allocation(self):
        stream = docker._BoundedPipe(io.BytesIO(b"payload"), 100)
        with self.assertRaises(docker.DockerReadError):
            stream.read(2 * 1024 * 1024)


class TestDockerMetadataRecovery(unittest.TestCase):
    def test_individual_metadata_cannot_fill_the_work_queue_with_large_payloads(self):
        popen = subprocess.Popen
        script = ("import json; print(json.dumps({'id':'a'*64,"
                  "'env':['HOME=/'+'x'*262144], 'mounts':[]}))")

        def metadata_process(args, **kwargs):
            return popen([sys.executable, "-c", script], **kwargs)

        with mock.patch.object(docker.subprocess, "Popen", side_effect=metadata_process), \
                self.assertRaises(docker.DockerReadError):
            docker._inspect_container("docker", "unix:///fixture.sock", "a" * 64,
                                      time.monotonic() + 5)

    def test_metadata_identity_and_shape_are_validated(self):
        identifier = "a" * 64
        valid = {"id": identifier, "user": "", "env": [], "mounts": []}
        values = [None, [], {**valid, "id": "b" * 64},
                  {**valid, "env": None}, {**valid, "mounts": None},
                  {**valid, "mounts": ["invalid"]}]
        for value in values:
            with self.subTest(value=value), \
                    mock.patch.object(docker, "_read", return_value=json.dumps(value).encode()), \
                    self.assertRaises(docker.DockerReadError):
                docker._inspect_container("docker", "unix:///fixture.sock", identifier,
                                          time.monotonic() + 5)
        with mock.patch.object(docker, "_read", return_value=json.dumps(valid).encode()):
            self.assertEqual(docker._inspect_container(
                "docker", "unix:///fixture.sock", identifier, time.monotonic() + 5), valid)

    def test_parallel_collection_is_bounded_and_drained_before_returning(self):
        active = peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(docker.CONTAINER_WORKERS)

        def collect(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return [], 1, True, False
            finally:
                with lock:
                    active -= 1

        identifiers = [f"{index:064x}" for index in range(12)]
        with mock.patch.object(docker, "_collect_container", side_effect=collect), \
                mock.patch.object(docker, "_inspect_container", return_value={"env": [], "mounts": []}):
            results = list(docker._collect_containers(
                "docker", "unix:///fixture.sock", identifiers, (), Path("unused"),
                time.monotonic() + 10))
        self.assertEqual(len(results), len(identifiers))
        self.assertEqual(peak, docker.CONTAINER_WORKERS)
        self.assertEqual(active, 0)

    def test_explicit_roots_are_probed_before_generic_home_directories(self):
        container = {"id": "a" * 64, "env": ["CODEX_HOME=/state/codex"],
                     "mounts": [{"Destination": "/state/.claude/projects"}]}
        expected = {PurePosixPath("/state/codex/sessions"),
                    PurePosixPath("/state/.claude/projects")}
        with mock.patch.object(docker, "_command", side_effect=docker.MissingPath):
            roots = list(docker._homes("docker", "unix:///fixture.sock", container,
                                       time.monotonic() + 5))
        self.assertEqual(set(roots[:2]), expected)
        self.assertIn(PurePosixPath("/root/.codex/sessions"), roots)

    def test_an_expired_collection_never_starts_fallback_commands(self):
        container = {"id": "a" * 64, "env": ["CODEX_HOME=/state/codex"], "mounts": []}
        with mock.patch.object(docker, "_process") as process:
            result = docker._collect_container(
                "docker", "unix:///fixture.sock", container, (), Path("unused"),
                time.monotonic() - 1)
        process.assert_not_called()
        self.assertEqual(result, ([], 0, False, True))


class TestDockerWorkerShutdown(unittest.TestCase):
    def test_shutdown_terminates_our_clients_and_refuses_new_commands(self):
        with mock.patch.object(docker, "_SHUTTING_DOWN", False):
            with docker._process(sys.executable, ["-c", "import time; time.sleep(30)"]) as process:
                try:
                    self.assertIsNone(process.poll())
                    docker._shutdown_commands()
                    process.wait(timeout=3)
                    with self.assertRaises(docker.DockerReadError):
                        docker._read(sys.executable, ["-c", "print('unexpected')"],
                                     time.monotonic() + 5)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()

    def test_background_collection_does_not_delay_process_exit(self):
        script = textwrap.dedent("""
            import threading
            from unittest import mock
            from tests.test_docker_sources import DockerFixture
            from claude_usage import dashboard, docker_sources as docker

            fixture = DockerFixture()
            fixture.setUp()
            started = threading.Event()

            def homes(*args):
                started.set()
                threading.Event().wait(30)
                return []

            patcher = mock.patch.object(docker, '_homes', side_effect=homes)
            patcher.start()
            dashboard.start_scan_thread(lambda: docker.collect(fixture.db))
            if not started.wait(5):
                raise RuntimeError('Docker worker did not start')
            print('ready to exit', flush=True)
        """)
        with subprocess.Popen(
                [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready to exit")
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


@requires_node
class TestDockerStatusInDashboard(unittest.TestCase):
    def test_status_poll_reports_collection_failure_and_recovery(self):
        got = run_js(r"""
          const element = {hidden: true, textContent: '', classList: {
            toggle(name, value) { element.warning = value; }
          }};
          document.getElementById = () => element;
          const views = [];
          for (const state of ['scanning', 'partial', 'ready', 'not_installed']) {
            validatedScanStatus({state: 'idle', generation: 2,
              docker: {state, containers: 2, sources: 4}});
            views.push({text: element.textContent, hidden: element.hidden, warning: element.warning});
          }
          console.log(JSON.stringify(views));
        """)
        self.assertIn("Checking Docker", got[0]["text"])
        self.assertFalse(got[0]["hidden"])
        self.assertIn("could not be refreshed", got[1]["text"])
        self.assertTrue(got[1]["warning"])
        self.assertIn("2 containers", got[2]["text"])
        self.assertFalse(got[2]["warning"])
        self.assertTrue(got[3]["hidden"])


if __name__ == "__main__":
    unittest.main()
