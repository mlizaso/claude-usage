import http.client
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import proxy
from proxy import FixedTargetProxy


class RecordingHandler(BaseHTTPRequestHandler):
    paths = []

    def do_GET(self):
        self.paths.append(self.path)
        body = b"local-upstream"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def live_relay_handlers():
    """How many threads are inside `ProxyHandler.handle` right now.

    Read from the live frames rather than inferred from anything the client can
    see, because every client-visible signal is delivered by a relay that
    returns and by one that lingers alike: the FIN a lingering handler forwards
    with `shutdown(SHUT_WR)` looks exactly like the FIN that closing the socket
    sends. The thread's own frame is the only thing that tells them apart.
    """
    code = proxy.ProxyHandler.handle.__code__
    return sum(1 for frame in sys._current_frames().values()
               if frame.f_code is code)


def wait_for_relay_handlers(target, timeout=2.0):
    """`live_relay_handlers()` once it has dropped to `target`, or at `timeout`."""
    deadline = time.monotonic() + timeout
    while live_relay_handlers() > target and time.monotonic() < deadline:
        time.sleep(0.02)
    return live_relay_handlers()


class HTTPUpstreamFixture:
    """A real `RecordingHandler` upstream with a real `FixedTargetProxy` in front.

    Shared rather than copied because every case below has to drive the actual
    relay: a mocked socket cannot show which side blocks, which is the only
    thing these tests are about.
    """

    def setUp(self):
        RecordingHandler.paths = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()

        self.proxy = FixedTargetProxy(
            ("127.0.0.1", 0), self.upstream.server_address
        )
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True
        )
        self.proxy_thread.start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.proxy_thread.join(timeout=2)
        self.upstream_thread.join(timeout=2)


class ProxyTests(HTTPUpstreamFixture, unittest.TestCase):
    def request(self, target):
        connection = http.client.HTTPConnection(
            *self.proxy.server_address, timeout=2
        )
        connection.request("GET", target)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_forwards_to_configured_target(self):
        status, body = self.request("/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"local-upstream")
        self.assertEqual(RecordingHandler.paths, ["/healthz"])

    def test_absolute_url_cannot_retarget_proxy(self):
        status, body = self.request("http://attacker.invalid/secret")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"local-upstream")
        self.assertEqual(
            RecordingHandler.paths, ["http://attacker.invalid/secret"]
        )


class BigBodyUpstream:
    """A raw upstream that answers with a body far larger than the buffers.

    Deliberately not an HTTP server: the point is to keep pushing bytes at a
    client that never reads, which is the only thing that parks the relay inside
    `sendall`. `disconnected` is set when the proxy hangs up on us, so the test
    observes the handler's real lifetime rather than a mocked exception.
    """

    BODY_SIZE = 64 * 1024 * 1024

    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.address = self.listener.getsockname()
        self.fully_sent = False
        self.disconnected = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        with connection:
            try:
                connection.recv(65536)  # the forwarded request
                connection.sendall(
                    b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n"
                    % self.BODY_SIZE
                )
                chunk = b"x" * 65536
                for _ in range(self.BODY_SIZE // len(chunk)):
                    connection.sendall(chunk)
                self.fully_sent = True
                connection.recv(1)  # blocks until the proxy hangs up
            except OSError:
                pass
        self.disconnected.set()

    def close(self):
        self.listener.close()
        self.thread.join(timeout=2)


class TestAClientThatStopsReadingIsReclaimed(unittest.TestCase):
    """A relay bounded only by `select` is not bounded at all.

    The IDLE/RESPONSE timeouts only run when the loop reaches `select`, and a
    thread parked in `sendall` towards a client that stopped reading never gets
    there. `socketserver.TCPServer` leaves the accepted socket blocking, so
    before `handle()` set its own bound the thread and its descriptor were
    retained for good — measured still parked at t+50 s against a 30 s idle
    timeout and a 300 s response timeout, while the dashboard behind it had
    already reclaimed its handler at 15 s.
    """

    RESPONSE_TIMEOUT = 2

    def setUp(self):
        self.upstream = BigBodyUpstream()
        self.addCleanup(self.upstream.close)
        patcher = mock.patch.object(
            proxy, "RESPONSE_TIMEOUT_SECONDS", self.RESPONSE_TIMEOUT
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proxy = FixedTargetProxy(("127.0.0.1", 0), self.upstream.address)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True
        )
        self.proxy_thread.start()
        self.addCleanup(self.proxy_thread.join, 2)
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)

    def test_a_client_that_stops_reading_does_not_pin_the_relay(self):
        client = socket.socket()
        # Ignored by macOS, which auto-tunes the receive buffer regardless; it
        # is what makes the stall cheap to reach where it is honoured.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        client.connect(self.proxy.server_address)
        self.addCleanup(client.close)
        client.sendall(b"GET /big HTTP/1.0\r\n\r\n")

        reclaimed = self.upstream.disconnected.wait(
            timeout=self.RESPONSE_TIMEOUT + 10
        )

        self.assertFalse(
            self.upstream.fully_sent,
            f"the whole {BigBodyUpstream.BODY_SIZE}-byte body fitted in the "
            "socket buffers, so the relay never blocked and this test proved "
            "nothing — raise BigBodyUpstream.BODY_SIZE",
        )
        self.assertTrue(
            reclaimed,
            "the handler is still parked in sendall() towards a client that "
            "stopped reading, past RESPONSE_TIMEOUT_SECONDS; nothing can "
            "reclaim the thread or its descriptor",
        )


class TestAHalfClosedClientStillGetsItsResponse(
    HTTPUpstreamFixture, unittest.TestCase
):
    """`shutdown(SHUT_WR)` ends the request, not the connection.

    The EOF it delivers used to tear the whole relay down, so the answer already
    on its way back was discarded and the upstream died with a BrokenPipeError
    mid-write. It is reachable with a stock tool: macOS's `nc` half-closes by
    default, so `printf 'GET /healthz HTTP/1.0\\r\\n\\r\\n' | nc 127.0.0.1 <port>`
    is the one-liner that reproduces it.
    """

    def _read_to_eof(self, client):
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def test_a_half_closed_request_is_still_answered(self):
        baseline = live_relay_handlers()
        client = socket.create_connection(self.proxy.server_address, timeout=5)
        self.addCleanup(client.close)
        client.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        client.shutdown(socket.SHUT_WR)

        received = self._read_to_eof(client)

        self.assertIn(b"200 OK", received)
        self.assertTrue(received.endswith(b"local-upstream"), received)
        self.assertEqual(RecordingHandler.paths, ["/healthz"])
        self.assertEqual(
            wait_for_relay_handlers(baseline), baseline,
            "the half-closed connection was answered and then parked instead "
            "of being torn down",
        )

    def test_forwarding_the_eof_is_one_directional(self):
        """Why only the client's EOF is forwarded, asserted rather than assumed.

        The dashboard answers HTTP/1.0 and closes after every response, so the
        upstream-EOF branch runs on every real connection. Forwarding that one
        too — the symmetric relay a reader is likely to reach for, and the shape
        this fix was first proposed in — leaves the handler selecting on a
        client that has no reason to close, for up to IDLE_TIMEOUT_SECONDS.
        Measured on this fixture with the symmetric form applied: 5 live
        handlers still relaying 5 s after 5 answered requests, against 0 here.
        """
        baseline = live_relay_handlers()
        client = socket.create_connection(self.proxy.server_address, timeout=5)
        self.addCleanup(client.close)
        client.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        received = b""
        while b"local-upstream" not in received:
            chunk = client.recv(4096)
            self.assertTrue(chunk, "the response was truncated")
            received += chunk

        # The client deliberately holds its own write side open, which is what a
        # browser does and what makes a symmetric relay linger.
        self.assertEqual(
            wait_for_relay_handlers(baseline), baseline,
            "the handler is still relaying after the upstream closed, so every "
            "ordinary request now holds a thread until IDLE_TIMEOUT_SECONDS",
        )


class WithheldAnswerUpstream:
    """A raw upstream that reads the request and then sits on its answer.

    Deliberately not an HTTP server: what has to be held still is the window
    after the client has half-closed and before the answer comes back, which is
    the whole of the time the relay must spend asleep. `eof_seen` is set when
    the proxy forwards that half-close, so the test can tell the branch under
    it actually ran rather than assuming it did; `release` is what finally lets
    the answer through.
    """

    ANSWER = b"HTTP/1.0 200 OK\r\nContent-Length: 6\r\n\r\nanswer"

    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.address = self.listener.getsockname()
        self.request_seen = threading.Event()
        self.eof_seen = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        with connection:
            try:
                connection.recv(65536)  # the forwarded request
                self.request_seen.set()
                if connection.recv(65536) == b"":  # the forwarded half-close
                    self.eof_seen.set()
                # Bounded so a failing assertion cannot leave this thread — and
                # the relay it is holding open — parked for good. The test
                # releases it itself, from the body and from cleanup.
                self.release.wait(timeout=30)
                connection.sendall(self.ANSWER)
            except OSError:
                pass

    def close(self):
        self.release.set()
        self.listener.close()
        self.thread.join(timeout=2)


class CountingClientSocket:
    """The accepted client socket, with its `recv` calls counted.

    Wrapped at `get_request` rather than mocked, because everything else about
    it has to stay real: `select` needs a live descriptor and `sendall` a live
    peer, and how often the relay asks a socket that has nothing left to say is
    the only thing the half-close branch leaves behind. Every other attribute —
    `sendall`, `settimeout`, `shutdown`, `close`, the ones socketserver itself
    calls on the way out — is the underlying socket's.
    """

    def __init__(self, sock):
        self._sock = sock
        self.recv_calls = 0

    def recv(self, *args):
        self.recv_calls += 1
        return self._sock.recv(*args)

    def fileno(self):
        return self._sock.fileno()

    def __getattr__(self, name):
        return getattr(self._sock, name)


class CountingProxy(FixedTargetProxy):
    """`FixedTargetProxy` handing its handler a counted client socket."""

    def __init__(self, *args, **kwargs):
        self.accepted = []
        super().__init__(*args, **kwargs)

    def get_request(self):
        sock, address = super().get_request()
        counted = CountingClientSocket(sock)
        self.accepted.append(counted)
        return counted, address


class TestAHalfClosedClientIsNotPolledWhileTheAnswerIsPending(unittest.TestCase):
    """The half-close branch ends by dropping the client from `peers`.

    Its comment says why — "Stop selecting on the client, or its EOF spins the
    loop" — and nothing in this file could see it: deleting that one line left
    all five tests above green. Both of the ones written for this branch watch
    the handler's *lifetime*, and the spin does not change that. It ends when
    the upstream answers and closes, exactly as it would have anyway, so the
    handler still exits on schedule while having burned a core to get there.

    A socket at EOF is readable forever, so leaving it in `peers` makes every
    `select` return immediately and every pass recv `b""` again. What separates
    the two is therefore not when the handler ends but what it does in between,
    and this counts it: how many times the relay asks the half-closed client
    for bytes during a window in which the answer is deliberately withheld.
    Measured here: 0 with the line, 112,305 without it.

    Counted rather than timed on purpose. `time.process_time` deltas are the
    other obvious observable and they are noisy on a loaded machine — the load
    average passed 100 while this suite was being written — and a timing
    assertion that flakes is worse than no assertion at all.
    """

    # Long enough that a spin is unmistakable (~10^5 polls) and short enough to
    # pay for on every run.
    WITHHOLD_SECONDS = 0.5
    # The relay may not ask a client that has already said goodbye anything at
    # all; 2 is slack for a request that arrived in more than one segment, not
    # a budget. Measured at 0 with four CPU-bound threads competing for the
    # machine.
    ALLOWED_POLLS = 2

    def setUp(self):
        self.upstream = WithheldAnswerUpstream()
        self.addCleanup(self.upstream.close)
        self.proxy = CountingProxy(("127.0.0.1", 0), self.upstream.address)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True
        )
        self.proxy_thread.start()
        self.addCleanup(self.proxy_thread.join, 2)
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        # Registered last so it runs first: a failed assertion leaves a spinning
        # handler behind, and releasing the answer is what lets it return.
        self.addCleanup(self.upstream.release.set)

    def test_the_relay_sleeps_while_a_half_closed_client_waits(self):
        baseline = live_relay_handlers()
        client = socket.create_connection(self.proxy.server_address, timeout=5)
        self.addCleanup(client.close)
        client.sendall(b"GET /slow HTTP/1.0\r\n\r\n")
        client.shutdown(socket.SHUT_WR)

        self.assertTrue(self.upstream.request_seen.wait(timeout=5),
                        "the request never reached the upstream")
        # Guard the guard: without this the count below is measured over a
        # window the branch may never have entered, and would read 0 for the
        # wrong reason.
        self.assertTrue(
            self.upstream.eof_seen.wait(timeout=5),
            "the client's half-close was never forwarded, so the branch this "
            "test is about never ran")
        self.assertEqual(len(self.proxy.accepted), 1)
        counted = self.proxy.accepted[0]

        before = counted.recv_calls
        client.settimeout(self.WITHHOLD_SECONDS)
        with self.assertRaises(TimeoutError):
            client.recv(4096)  # the upstream is still sitting on the answer
        polls = counted.recv_calls - before

        self.assertLessEqual(
            polls, self.ALLOWED_POLLS,
            f"the relay called recv() on the half-closed client {polls} times "
            f"in {self.WITHHOLD_SECONDS}s with the answer still outstanding. "
            f"A socket at EOF is readable forever, so a client left in `peers` "
            f"makes select return instantly on every pass and burns a core "
            f"until the upstream answers — which is what "
            f"`del peers[self.request]` is there to prevent.")

        # And it is still a relay: the answer it was asleep waiting for
        # arrives, and the handler leaves with it.
        self.upstream.release.set()
        client.settimeout(5)
        received = b""
        while b"answer" not in received:
            chunk = client.recv(4096)
            self.assertTrue(chunk, "the withheld answer never arrived")
            received += chunk
        self.assertEqual(
            wait_for_relay_handlers(baseline), baseline,
            "the half-closed connection was answered and then parked instead "
            "of being torn down")


if __name__ == "__main__":
    unittest.main()
