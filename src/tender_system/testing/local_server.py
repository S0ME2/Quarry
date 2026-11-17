"""Owned localhost fixture servers. Bind atomically; never discover or kill other processes."""

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


@contextmanager
def local_http_server(
    handler: type[BaseHTTPRequestHandler], port: int | None = None
) -> Iterator[ThreadingHTTPServer]:
    """E2E_SERVER_PORT optionally requests a debug port; occupied ports fall back to port 0."""
    requested = int(os.environ.get("E2E_SERVER_PORT", "0")) if port is None else port
    if type(requested) is not int or not 0 <= requested <= 65535:
        raise ValueError("fixture server port must be between 0 and 65535")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", requested), handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE or requested == 0:
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    try:
        thread.start()
        yield server
    finally:
        if thread.ident is not None:
            server.shutdown()
        server.server_close()
        if thread.ident is not None:
            thread.join(timeout=5)


def server_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}"
