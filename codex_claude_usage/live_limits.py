"""An OPTIONAL live query for plan limits, with the local cache as the fallback.

**This is the one part of this tool that talks to the network, and it is off
unless you turn it on.** Everything else reads files on this machine; the README
says so, and that stays true by default. Enabling this changes it, which is why
it takes a deliberate opt-in rather than appearing the moment a token exists.

    export CODEX_CLAUDE_USAGE_OAUTH_TOKEN="…"     # you supply it
    export CODEX_CLAUDE_USAGE_LIVE_LIMITS=1       # and you ask for it
    codex-claude-usage dashboard

The cached quota block can stop updating even when other account fields
change. An explicitly authorized live query can refresh windows missing from
that cache.

**Credential stores are never consulted automatically, and that is
deliberate.** Claude Code keeps its credential in platform storage, including
the macOS login Keychain. This module reads a static token only when the user
sets its environment variable, or runs the explicit command the user places in
`CODEX_CLAUDE_USAGE_TOKEN_COMMAND`; the shipped helper merely configures that command.
The decision and blast radius therefore stay with the user instead of silently
granting this process access to another application's credential.

The endpoint was read out of the Claude Code VS Code extension's own bundle
(`/api/oauth/usage` against `https://api.anthropic.com`) rather than guessed,
and it is overridable, because an endpoint discovered by inspection is a fact
about one version rather than a contract.

Nothing here raises. A refused, slow, offline or malformed response degrades to
"no live answer", and the caller falls back to the cache — which is exactly the
behaviour of the tool before this module existed.
"""

import hashlib
import json
import math
import os
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

# Discovered from the Claude Code extension bundle, not documented by Anthropic.
DEFAULT_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
ENDPOINT_ENV = "CODEX_CLAUDE_USAGE_LIMITS_URL"
TOKEN_ENV = "CODEX_CLAUDE_USAGE_OAUTH_TOKEN"
# A user-selected command can supply a current token for each query, avoiding
# a stale exported credential. Only run a command the user explicitly enabled;
# never discover or read an operating-system credential store automatically.
TOKEN_COMMAND_ENV = "CODEX_CLAUDE_USAGE_TOKEN_COMMAND"
# The command is a local credential lookup, not a network call. If it has not
# answered by now it is hung, and hanging the poll behind it is worse than
# falling back to the cache.
TOKEN_COMMAND_TIMEOUT = 5
ENABLE_ENV = "CODEX_CLAUDE_USAGE_LIVE_LIMITS"

# Short: the plan panel polls on a 30-second timer, and a request that outlives
# its own poll interval is a queue rather than a refresh.
TIMEOUT_SECONDS = 6
# A quota document is small. Anything larger is not one, and reading it would be
# the whole denial-of-service.
MAX_RESPONSE_BYTES = 512 * 1024
# OAuth bearer credentials use the HTTP b64token alphabet. Keep the transport
# check slightly broader (visible ASCII) while refusing control/non-ASCII data
# and pathological command output before it becomes a request header.
MAX_TOKEN_CHARS = 16 * 1024
# The reader keeps only the first non-blank line.  Two extra bytes allow the
# largest accepted token followed by one trailing space and a delimiter while
# still making a no-delimiter producer overflow immediately.
TOKEN_COMMAND_EXTRA_BYTES = 2
TOKEN_COMMAND_TERMINATE_GRACE = 0.25
TOKEN_COMMAND_POLL_INTERVAL = 0.01


class _WindowsProcessJob:
    """Kill-on-close Job Object containing one credential-command tree."""

    def __init__(self, kernel32, handle):
        self._kernel32 = kernel32
        self._handle = handle

    def attach(self, process):
        """Assign the just-spawned shell before reading any of its output."""
        import ctypes

        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            raise OSError("credential-command wrapper has no process id")
        # Use the public PID instead of CPython's private ``Popen._handle``.
        # PROCESS_TERMINATE | PROCESS_SET_QUOTA are the rights assignment needs.
        process_handle = self._kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(
                    self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate_all(self):
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            # KILL_ON_JOB_CLOSE is the final containment boundary. It also
            # catches a detached descendant after the shell leader has exited.
            self._kernel32.CloseHandle(handle)


def _create_windows_process_job():
    """Create a stdlib-only Windows Job Object with kill-on-close semantics."""
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return _WindowsProcessJob(kernel32, handle)


class _FetchTask:
    """One daemon-owned network operation and its eventual byte result."""

    def __init__(self, key, operation):
        self.key = key
        self.operation = operation
        self.done = threading.Event()
        self.value = None

    def run(self):
        try:
            self.value = self.operation()
        except Exception:
            # The public contract is cache fallback for every network failure,
            # including failures from an injected or changed response object.
            self.value = None
        finally:
            # The operation closes over the Request, including its bearer
            # header. A completed task remains in the single-flight slot until
            # the next poll, so retaining the callable would retain a token
            # fetched from a short-lived credential command indefinitely.
            self.operation = None
            self.done.set()


_FETCH_LOCK = threading.Lock()
_FETCH_IN_FLIGHT = None


def _run_fetch_with_deadline(key, operation, timeout):
    """Run at most one upstream fetch and wait no longer than ``timeout``.

    Socket timeouts are inactivity limits, not total deadlines: DNS, headers or
    a body that drips one byte per interval can keep renewing them forever. The
    network operation therefore lives in one daemon worker. Callers wait on its
    event for an absolute wall-clock budget and then fall back to the cache.

    There is intentionally only one in-flight operation per process. If it is
    permanently wedged, later requests do not create an unbounded population of
    stuck threads or sockets; they either share the same operation or fail fast
    when their endpoint/token/opener differs. A restart recovers live polling,
    while local cached limits remain available throughout.
    """
    global _FETCH_IN_FLIGHT
    try:
        budget = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(budget) or budget <= 0:
        return None

    with _FETCH_LOCK:
        task = _FETCH_IN_FLIGHT
        if task is not None and not task.done.is_set():
            if task.key != key:
                return None
        else:
            task = _FetchTask(key, operation)
            _FETCH_IN_FLIGHT = task
            worker = threading.Thread(
                target=task.run,
                name="codex-claude-usage-live-limits",
                daemon=True,
            )
            worker.start()
    if not task.done.wait(budget):
        return None
    return task.value


def _ssl_context():
    """A verifying TLS context that works on a python.org macOS build.

    Some python.org macOS installations need their certificate bundle
    configured before OpenSSL can validate HTTPS connections.

    `certifi` is used when it is importable, which is what that installer script
    wires up anyway. Verification is NEVER disabled: a quota figure is not worth
    a credential sent to whoever answered, and falling back to the local cache is
    the correct outcome when the chain cannot be checked.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of forwarding a bearer token to another URL.

    urllib's default redirect handler copies ``Authorization`` into the new
    request even when the origin changes. The quota endpoint has no redirect
    contract, and cached limits are already the safe fallback, so every 3xx is
    refused rather than trying to distinguish benign from hostile redirects.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_https_without_redirects(request, timeout):
    """Open one verified HTTPS request without installing redirect support."""
    transport = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        _RefuseRedirects(),
    )
    return transport.open(request, timeout=timeout)


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _valid_bearer_token(value):
    return (isinstance(value, str)
            and 0 < len(value) <= MAX_TOKEN_CHARS
            and value.isascii()
            and all(0x21 <= ord(char) <= 0x7e for char in value))


def _spawn_token_process(command, runner=None):
    """Start a bounded-reader-compatible token process.

    ``runner`` remains an injectable Popen factory for deterministic tests. It
    deliberately is NOT a ``subprocess.run`` seam: a completed-runner result has
    already buffered stdout and would recreate the unbounded-capture defect.
    """
    launch_command = command
    kwargs = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": False,
    }
    containment = None
    if os.name == "posix":
        # The shell and anything it starts share a private process group, so a
        # timeout/overflow can terminate descendants that inherited stdout.
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if flags:
            kwargs["creationflags"] = flags
        # A process group lets us request a graceful break, but unlike a Job
        # Object it does not terminate grandchildren after the shell exits.
        # Failing to create/assign the job fails this credential source closed;
        # resolve_token can still use its static fallback. The injectable Popen
        # factory follows this same path so a test seam cannot weaken cleanup.
        containment = _create_windows_process_job()
        # Popen cannot expose the primary thread handle needed for a safe
        # CREATE_SUSPENDED/Assign/Resume sequence. Launch a tiny Python wrapper
        # instead: it blocks on a private stdin gate, is assigned to the Job
        # Object, and only then starts the user's shell command. Descendants
        # therefore cannot race out before containment exists.
        wrapper = (
            "import subprocess,sys\n"
            "gate = sys.stdin.buffer.read(1)\n"
            "if gate != b'1': raise SystemExit(1)\n"
            "child = subprocess.Popen(sys.argv[1], shell=True)\n"
            "raise SystemExit(child.wait())\n"
        )
        launch_command = [sys.executable, "-I", "-S", "-c", wrapper, command]
        kwargs["shell"] = False
        kwargs["stdin"] = subprocess.PIPE

    factory = subprocess.Popen if runner is None else runner
    try:
        process = factory(launch_command, **kwargs)
    except BaseException:
        if containment is not None:
            containment.close()
        raise
    if os.name == "posix":
        pid = getattr(process, "pid", None)
        containment = pid if isinstance(pid, int) and pid > 0 else None
    if not all(hasattr(process, name) for name in ("poll", "wait")):
        _signal_token_process(process, containment, force=True)
        if containment is not None:
            _close_token_containment(containment)
        raise TypeError("token runner must return a Popen-like process")
    stdout = getattr(process, "stdout", None)
    if stdout is None or not hasattr(stdout, "read"):
        # On Windows the gated wrapper has not yet been attached to the Job or
        # released, so kill it directly before closing the unattached handle.
        # POSIX has already started the private group and uses normal cleanup.
        if os.name == "nt" and containment is not None:
            try:
                process.kill()
            except (AttributeError, OSError, ProcessLookupError):
                pass
            _wait_process(process, TOKEN_COMMAND_TERMINATE_GRACE)
            _close_token_containment(containment)
        else:
            _stop_token_process(process, containment)
        raise TypeError("token runner must expose a readable stdout pipe")
    if os.name == "nt" and containment is not None:
        try:
            containment.attach(process)
            gate = getattr(process, "stdin", None)
            if gate is None:
                raise OSError("credential-command wrapper gate is unavailable")
            gate.write(b"1")
            gate.flush()
            gate.close()
        except BaseException:
            try:
                if getattr(process, "stdin", None) is not None:
                    process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.kill()
            except (AttributeError, OSError, ProcessLookupError):
                pass
            _wait_process(process, TOKEN_COMMAND_TERMINATE_GRACE)
            containment.close()
            raise
    return process, containment


def _process_returncode(process):
    try:
        return process.poll()
    except (AttributeError, OSError):
        return getattr(process, "returncode", None)


def _wait_process(process, timeout):
    try:
        process.wait(timeout=timeout)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        return False
    return True


def _signal_token_process(process, containment=None, force=False):
    """Terminate and reap a token command without waiting indefinitely."""
    pid = getattr(process, "pid", None)
    if os.name == "posix" and isinstance(containment, int) and containment > 0:
        try:
            os.killpg(containment, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    elif os.name == "nt" and force and hasattr(containment, "terminate_all"):
        try:
            containment.terminate_all()
        except OSError:
            pass
        return
    elif os.name == "nt" and not force:
        # CTRL_BREAK reaches the private process group created above.  Fall
        # back to terminate for runners/hosts that do not expose the signal.
        event = getattr(signal, "CTRL_BREAK_EVENT", None)
        if event is not None:
            try:
                process.send_signal(event)
                return
            except (AttributeError, OSError):
                pass
    try:
        (process.kill if force else process.terminate)()
    except (AttributeError, OSError, ProcessLookupError):
        pass


def _close_token_containment(containment):
    close = getattr(containment, "close", None)
    if close is not None:
        try:
            close()
        except OSError:
            pass


def _stop_token_process(process, containment=None):
    """Stop the process group and make a bounded effort to reap it."""
    # Do not return early when the shell leader has exited: a child can retain
    # stdout after that point. The saved process-group id remains the one made
    # by ``start_new_session`` and is safe to signal until its descendants go
    # away; ``killpg`` simply reports ESRCH once it is already gone.
    _signal_token_process(process, containment)
    _wait_process(process, TOKEN_COMMAND_TERMINATE_GRACE)
    # A shell can be reaped while a descendant still owns stdout.  Force the
    # saved group even when ``process.poll()`` is already non-None; otherwise
    # that descendant survives every timeout/overflow cleanup.
    if containment is not None or _process_returncode(process) is None:
        _signal_token_process(process, containment, force=True)
    _wait_process(process, TOKEN_COMMAND_TERMINATE_GRACE)
    _close_token_containment(containment)


def _stream_token_line(process, deadline, containment=None):
    """Read only the bounded first non-blank line from ``process.stdout``."""
    stream = getattr(process, "stdout", None)
    if stream is None:
        return None, False
    captured = bytearray()
    finished = threading.Event()
    line_complete = threading.Event()
    overflow = threading.Event()
    leading = True
    consumed = 0
    limit = MAX_TOKEN_CHARS + TOKEN_COMMAND_EXTRA_BYTES
    whitespace = b" \t\r\n\v\f"

    def read_one():
        nonlocal leading, consumed
        try:
            while consumed < limit:
                chunk = stream.read(1)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                for byte in bytes(chunk):
                    consumed += 1
                    if leading and byte in whitespace:
                        continue
                    leading = False
                    if byte in (0x0a, 0x0d):
                        line_complete.set()
                        return
                    captured.append(byte)
                    if consumed >= limit:
                        overflow.set()
                        return
                    # A multi-byte fake stream chunk can contain more than the
                    # one byte requested. Never retain or inspect past the
                    # bounded prefix.
                    if consumed >= limit:
                        overflow.set()
                        return
        except (OSError, ValueError, TypeError, UnicodeError):
            return
        if consumed >= limit and not leading:
            overflow.set()

    def reader_main():
        try:
            read_one()
        finally:
            finished.set()

    reader = threading.Thread(
        target=reader_main,
        name="codex-claude-usage-token-reader",
        daemon=True,
    )
    reader.start()
    timed_out = False
    while True:
        if overflow.is_set():
            timed_out = True
            break
        if line_complete.is_set():
            # We intentionally stop reading at the first line, but give a
            # normal short-lived command a small grace period to publish its
            # return code. A producer that keeps writing (or leaves a child
            # holding the pipe) is terminated as a group instead of being
            # allowed to fill the pipe until the five-second command deadline.
            grace_deadline = min(
                deadline, time.monotonic() + TOKEN_COMMAND_TERMINATE_GRACE)
            while _process_returncode(process) is None \
                    and time.monotonic() < grace_deadline:
                time.sleep(TOKEN_COMMAND_POLL_INTERVAL)
            if _process_returncode(process) is None:
                timed_out = True
            break
        if finished.is_set() and _process_returncode(process) is not None:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(TOKEN_COMMAND_POLL_INTERVAL)

    if timed_out:
        _stop_token_process(process, containment)
        try:
            stream.close()
        except (OSError, ValueError):
            pass
        reader.join(TOKEN_COMMAND_TERMINATE_GRACE)
        return None, True

    # The process ended naturally and the reader observed EOF or a delimiter.
    # Capture the leader's status before cleanup, then immediately retire the
    # containment boundary.  EOF alone does not prove the group is empty: a
    # background descendant can redirect stdout, outlive its successful shell,
    # and retain access to the credential source.  Signalling the saved group
    # here closes that no-newline path just as the delimiter path above does.
    # A nonzero leader status retains the old fallback behavior.
    code = _process_returncode(process)
    if containment is not None:
        _stop_token_process(process, containment)
    else:
        _close_token_containment(containment)
    try:
        stream.close()
    except (OSError, ValueError):
        pass
    reader.join(TOKEN_COMMAND_TERMINATE_GRACE)
    if code != 0:
        return "", False
    text = bytes(captured).decode("utf-8", "replace")
    return text.strip().splitlines()[0].strip() if text.strip() else "", False


def _token_from_command(env=None, runner=None):
    """Run the reader's token command and return what it printed. Never raises.

    Shell evaluation is intentional: useful credential readers are pipelines
    and the command is supplied by the user, not parsed from request data. On
    Windows a gated Python wrapper joins a kill-on-close Job Object before it
    starts that shell. The shipped process path streams at most
    ``MAX_TOKEN_CHARS`` plus two delimiter bytes, discards stderr, uses a total
    deadline and cleans up the private POSIX process group or Windows Job Object
    on timeout or overproduction. An infinite producer therefore cannot grow
    memory or hold a poll forever.
    """
    environ = os.environ if env is None else env
    command = (environ.get(TOKEN_COMMAND_ENV) or "").strip()
    if not command:
        return ""
    try:
        process, containment = _spawn_token_process(command, runner=runner)
        try:
            return _stream_token_line(
                process, time.monotonic() + TOKEN_COMMAND_TIMEOUT,
                containment=containment)[0] or ""
        except BaseException:
            _stop_token_process(process, containment)
            raise
    except Exception:
        return ""


def resolve_token(env=None, runner=None):
    """The usable credential, or ``""``. A valid COMMAND value wins.

    Order matters: an access token expires in minutes, so a command that
    re-reads it is fresher than anything exported into a shell an hour ago. A
    reader who has set both meant the command.  A failed, oversized or
    header-unsafe command result is not a credential, however, and must not
    suppress an explicitly configured static fallback.
    """
    environ = os.environ if env is None else env
    from_command = _token_from_command(environ, runner=runner)
    if _valid_bearer_token(from_command):
        return from_command
    static = (environ.get(TOKEN_ENV) or "").strip()
    return static if _valid_bearer_token(static) else ""


def enabled(env=None):
    """Whether live querying is switched on with a credential source configured.

    Both are required. A source with no opt-in does nothing -- someone who
    exports a credential for another purpose has not asked this tool to start
    making requests -- and an opt-in with no source cannot do anything anyway.
    Token validity is checked once, later, by ``resolve_token``; running a
    configured command here would execute a credential lookup twice per fetch.
    """
    environ = os.environ if env is None else env
    if not _truthy(environ.get(ENABLE_ENV)):
        return False
    return bool((environ.get(TOKEN_ENV) or "").strip()
                or (environ.get(TOKEN_COMMAND_ENV) or "").strip())


def endpoint(env=None):
    """The URL to query. HTTPS only, refused rather than coerced.

    An `http://` override would send the credential in clear text, and silently
    upgrading it would hide that somebody asked for the wrong thing.
    """
    environ = os.environ if env is None else env
    url = (environ.get(ENDPOINT_ENV) or DEFAULT_ENDPOINT).strip()
    return url if url.lower().startswith("https://") else None


def fetch_utilization(env=None, timeout=TIMEOUT_SECONDS, opener=None, runner=None):
    """The live `cachedUsageUtilization`-shaped block, or None. Never raises.

    Returns the SAME shape the cache holds, so everything downstream --
    `account.limits_projection`, `limits_core.describe_windows`, the panel, the
    thresholds -- is unchanged and unaware. That is what keeps this module
    optional: delete it and the tool still works.

    `opener` exists for the tests, which must never make a real request.
    """
    environ = os.environ if env is None else env
    if not enabled(environ):
        return None
    url = endpoint(environ)
    if not url:
        return None
    token = resolve_token(environ, runner=runner)
    if not _valid_bearer_token(token):
        return None
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    # Named so a support engineer reading Anthropic's logs can tell this apart
    # from Claude Code itself; it is a different program with a different cadence.
    request.add_header("User-Agent", "codex-claude-usage-dashboard/limits")
    open_url = opener or _open_https_without_redirects

    def fetch_bytes():
        with open_url(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return None
            return response.read(MAX_RESPONSE_BYTES + 1)

    # Include the opener identity so unrelated injected/test endpoints never
    # share a result. A digest distinguishes credentials without retaining a
    # command-sourced bearer token in the process-global single-flight key.
    token_identity = hashlib.sha256(token.encode("ascii")).digest()
    try:
        raw = _run_fetch_with_deadline(
            (url, token_identity, id(open_url)), fetch_bytes, timeout
        )
    finally:
        # The worker may still be blocked after the caller's absolute deadline.
        # It closes over this Request, so remove the bearer before returning even
        # when the operation remains in the single-flight slot. If headers have
        # not gone out yet, the eventual operation may simply fail unauthenticated.
        request.remove_header("Authorization")
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    return _as_cached_block(payload)


def _as_cached_block(payload):
    """Normalise the response into the shape `~/.claude.json` stores.

    Accepts either the whole `cachedUsageUtilization` object or the bare
    `utilization` body, because an endpoint read out of a bundle is not a
    contract and the wrapper is exactly the sort of thing that differs between
    versions. Anything else answers None rather than a half-built block, so a
    changed response degrades to the cache instead of rendering nonsense.
    """
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("utilization"), dict):
        block = dict(payload)
    elif isinstance(payload.get("limits"), list) or "five_hour" in payload:
        block = {"utilization": payload}
    else:
        return None
    # Stamped NOW, because that is what it is: `account.limits_projection`
    # computes the age from this, and inheriting a server-side timestamp would
    # report a live reading as hours old.
    import time
    block["fetchedAtMs"] = int(time.time() * 1000)
    return block


def config_with_live_limits(config, env=None, opener=None, runner=None):
    """`config` with its quota block replaced by a live one, plus the source.

    Returns `(config, source)` where source is "live" or "cache". The config is
    COPIED rather than mutated: it is the caller's parsed `~/.claude.json`, and
    a live reading must never be written back to the file this tool only reads.
    """
    live = fetch_utilization(env=env, opener=opener, runner=runner)
    if not live:
        return config, "cache"
    merged = dict(config or {})
    merged["cachedUsageUtilization"] = live
    return merged, "live"
