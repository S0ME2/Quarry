from http.server import BaseHTTPRequestHandler
from urllib.request import urlopen

import pytest

from tender_system.testing.local_server import local_http_server, server_url


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "7")
        self.end_headers()
        self.wfile.write(b"fixture")


def response(url: str) -> bytes:
    with urlopen(url, timeout=2) as result:
        body = result.read()
        assert isinstance(body, bytes)
        return body


def test_conflict_falls_back_and_cleanup_preserves_existing_server() -> None:
    with local_http_server(FixtureHandler, port=0) as existing:
        existing_url = server_url(existing)
        with local_http_server(FixtureHandler, port=existing.server_port) as fixture:
            assert fixture.server_port != existing.server_port
            assert response(server_url(fixture)) == b"fixture"
            assert response(existing_url) == b"fixture"
        assert fixture.fileno() == -1
        assert response(existing_url) == b"fixture"


def test_debug_port_environment_conflict_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    with local_http_server(FixtureHandler, port=0) as existing:
        monkeypatch.setenv("E2E_SERVER_PORT", str(existing.server_port))
        with local_http_server(FixtureHandler) as fixture:
            assert fixture.server_port != existing.server_port
            assert response(server_url(fixture)) == b"fixture"
        assert response(server_url(existing)) == b"fixture"


def test_cleanup_on_failure_closes_only_owned_socket() -> None:
    with local_http_server(FixtureHandler, port=0) as existing:
        with pytest.raises(RuntimeError):
            with local_http_server(FixtureHandler, port=0) as fixture:
                raise RuntimeError("test failed")
        assert fixture.fileno() == -1
        assert response(server_url(existing)) == b"fixture"


@pytest.mark.parametrize("port", [-1, 65536])
def test_invalid_debug_port_rejected(port: int) -> None:
    with pytest.raises(ValueError):
        with local_http_server(FixtureHandler, port=port):
            pytest.fail("invalid port accepted")


def test_non_integer_environment_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2E_SERVER_PORT", "invalid")
    with pytest.raises(ValueError):
        with local_http_server(FixtureHandler):
            pytest.fail("invalid port accepted")
