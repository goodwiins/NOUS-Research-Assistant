"""Acceptance criteria 6 and 7: methods, headers, Host, binding and port."""

from __future__ import annotations

import socket

import pytest
from conftest import ALL_ROUTES, Factory, Running
from factory_ui import __main__ as cli
from factory_ui import config, server

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'"
)
REQUIRED = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


def _check_headers(headers: list[tuple[str, str]]) -> None:
    got = {k.lower(): v for k, v in headers}
    for k, v in REQUIRED.items():
        assert got.get(k) == v, k
    assert not [k for k, _ in headers if k.lower().startswith("access-control-")]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_other_methods_answer_405(running: Running, method: str) -> None:
    for path in ("/", "/api/jobs", "/api/health", "/nowhere"):
        res = running.get(path, method=method)
        assert res.status == 405
        _check_headers(res.header_list)


def test_every_response_carries_the_headers(running: Running) -> None:
    paths = ALL_ROUTES + ["/nowhere", "/api/jobs/x", "/static/missing.js"]
    for path in paths:
        for method in ("GET", "HEAD"):
            res = running.get(path, method=method)
            _check_headers(res.header_list)
            if method == "HEAD":
                assert res.body == b""


def test_wrong_host_answers_403(running: Running) -> None:
    for host in ("evil.example:9417", "evil.example", f"127.0.0.1:{running.port + 1}"):
        res = running.get("/api/jobs", host=host)
        assert res.status == 403
        _check_headers(res.header_list)
    assert running.get("/", host=f"localhost:{running.port}").status == 200


def test_default_port_and_bind_address(factory: Factory) -> None:
    assert config.DEFAULT_PORT == 9417
    assert config.HOST == "127.0.0.1"
    srv = server.make_server(factory.root, 0)
    try:
        assert srv.socket.getsockname()[0] == "127.0.0.1"
    finally:
        srv.server_close()


@pytest.mark.parametrize("port", ["3000", "8000", "4625", "4800", "5432", "80"])
def test_refused_ports_exit_2(
    port: str, factory: Factory, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--port", port], root=factory.root)
    assert exc.value.code == 2
    assert "port" in capsys.readouterr().err


def test_port_rules() -> None:
    for p in (3005, 8010, 4100, 4899, 6379, 54321, 1023, 49152, 9000, 11434):
        assert config.port_refusal(p)
    for p in (9417, 4900, 1024, 49151, 5000):
        assert config.port_refusal(p) is None


def test_busy_port_exits_nonzero_without_trying_another(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    tried = []
    real = server.make_server

    def spy(root: object, p: int, **kw: object) -> server.FactoryServer:
        tried.append(p)
        return real(root, p, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(server, "make_server", spy)
    monkeypatch.setattr(config, "port_refusal", lambda p: None)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--port", str(port)], root=factory.root)
        assert exc.value.code not in (0, None)
        assert tried == [port]
    finally:
        busy.close()
