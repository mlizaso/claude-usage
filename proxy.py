"""Fixed-target TCP proxy used by the Docker launcher.

This process never mounts or reads Claude data. It is the only container with
a host-published port; the dashboard container stays on an internal network.
"""

import argparse
import select
import socket
import socketserver
import time


BUFFER_SIZE = 64 * 1024
# How long a connection with nothing outstanding may sit silent before it is
# reclaimed. Silence in both directions means the peer has gone away.
IDLE_TIMEOUT_SECONDS = 30
# How long to wait once bytes have gone client -> upstream and the answer has
# not come back. Silence then means "still working", not "gone": this is the
# only host-published port in the Docker deployment, so POST /api/rescan
# crosses it, and cli.py's own comment says a cold scan over a large
# ~/.claude/projects backlog "can take well over a minute". Cutting that off at
# the idle timeout dropped the response of a scan that had in fact succeeded.
RESPONSE_TIMEOUT_SECONDS = 300
UPSTREAM_RETRIES = 20


class FixedTargetProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, target):
        self.target = target
        super().__init__(server_address, ProxyHandler)


class ProxyHandler(socketserver.BaseRequestHandler):
    def _connect_upstream(self):
        for attempt in range(UPSTREAM_RETRIES):
            try:
                return socket.create_connection(self.server.target, timeout=2)
            except OSError:
                if attempt == UPSTREAM_RETRIES - 1:
                    raise
                time.sleep(0.1)
        raise RuntimeError("unreachable")

    def handle(self):
        try:
            # The select timeouts below only run when the loop reaches select,
            # and a thread parked in sendall() towards a client that stopped
            # reading never does. socketserver leaves the accepted socket
            # blocking, so without this nothing could reclaim the thread or its
            # descriptor. The upstream socket needs no equivalent: it keeps the
            # timeout=2 create_connection left on it.
            self.request.settimeout(RESPONSE_TIMEOUT_SECONDS)
            with self._connect_upstream() as upstream:
                peers = {
                    self.request: upstream,
                    upstream: self.request,
                }
                # A request that has been forwarded and not yet answered looks
                # exactly like an idle connection from here, so the two states
                # are tracked apart rather than sharing one timeout.
                awaiting_response = False
                while True:
                    readable, _, _ = select.select(
                        peers, [], [],
                        RESPONSE_TIMEOUT_SECONDS if awaiting_response
                        else IDLE_TIMEOUT_SECONDS,
                    )
                    if not readable:
                        return
                    for source in readable:
                        chunk = source.recv(BUFFER_SIZE)
                        if not chunk:
                            if source is not self.request:
                                return
                            # A client half-close ends the request, not the
                            # connection: the answer is still on its way back.
                            # Forwarded in this direction only — the upstream's
                            # own EOF means nothing more can arrive for the
                            # client, and returning closes its socket, which
                            # delivers the same FIN. Stop selecting on the
                            # client, or its EOF spins the loop.
                            try:
                                upstream.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
                            del peers[self.request]
                            continue
                        peers[source].sendall(chunk)
                        # Bytes towards the upstream open a request; the first
                        # byte back closes it.
                        awaiting_response = source is self.request
        except (OSError, TimeoutError):
            return


def main():
    parser = argparse.ArgumentParser(description="Fixed-target TCP proxy")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8081)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()

    with FixedTargetProxy(
        (args.listen_host, args.listen_port),
        (args.target_host, args.target_port),
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
