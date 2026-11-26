"""The HTTP server: GET and HEAD only, bound to 127.0.0.1, a closed route
table, and no request text ever reaching a path or a command."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import config, paths, sources
from .readers import budget, health, jobs, parallel, verifier
from .runner import Runner

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)
_JOB = re.compile(r"^/api/jobs/([^/]+)$")
_ARTIFACT = re.compile(r"^/api/jobs/([^/]+)/artifact/([a-z0-9/-]+)$")
_STATIC = re.compile(r"^/static/([a-z]+\.(?:html|js|css))$")


def _load_static() -> dict[str, tuple[bytes, str]]:
    """The static files, listed and read once at start-up."""
    out = {}
    for p in sorted(STATIC_DIR.iterdir()):
        if p.is_file() and p.suffix in CONTENT_TYPES:
            out[p.name] = (p.read_bytes(), CONTENT_TYPES[p.suffix])
    return out


class App:
    """Routes to views. Every view reads under `root` only."""

    def __init__(self, root: Path, runner: Runner | None = None) -> None:
        self.root = root
        self.runner = runner or Runner(root)
        self.static = _load_static()

    def route(self, path: str, query: str) -> tuple[int, Any]:
        """(status, body): body is a JSON-able value or (bytes, type)."""
        refresh = query == "refresh=1"
        if path == "/":
            return 200, self.static["index.html"]
        m = _STATIC.match(path)
        if m:
            found = self.static.get(m.group(1))
            return (200, found) if found else (404, {"error": "not found"})
        if path == "/api/jobs":
            return 200, {"rows": sources.jobs_rows(self.root), "now": time.time()}
        if path == "/api/verifier":
            return 200, self.verifier()
        if path == "/api/budget":
            return 200, self.budget(refresh)
        if path == "/api/health":
            return 200, self.health(refresh)
        if path == "/api/parallel":
            return 200, parallel.view(self.root)
        m = _JOB.match(path)
        if m:
            job = paths.listed_job(self.root, m.group(1))
            if job is None:
                return 404, {"error": "no such job"}
            return 200, jobs.job_detail(self.root, job)
        m = _ARTIFACT.match(path)
        if m:
            job = paths.listed_job(self.root, m.group(1))
            if job is None or paths.key_parts(m.group(2)) is None:
                return 404, {"error": "not found"}
            text = jobs.artifact(self.root, job, m.group(2))
            return (200, text) if text else (404, {"error": "not written yet"})
        return 404, {"error": "not found"}

    def verifier(self) -> dict[str, Any]:
        mode, since = verifier.verify_mode(self.root)
        block = dict(sources.backtests_block(self.root))
        listing = block.pop("listing", None) or {}
        return {
            "mode": {"mode": mode, "since": since},
            "backtests": block,
            "rows": listing.get("rows", []),
            "skipped": listing.get("skipped", 0),
            "truncated": listing.get("truncated", False),
            "rounds": verifier.round_rows(sources.jobs_rows(self.root)),
        }

    def budget(self, refresh: bool) -> dict[str, Any]:
        res = self.runner.get(
            "budget",
            refresh,
            session_id=lambda: budget.newest_session(self.root),
        )
        return {
            "command": _command(res),
            "answer": budget.answer(res.get("data"), res.get("session")),
            "session": budget.short(res["session"]) if res.get("session") else None,
            "limits": dict(config.LIMITS),
            "readings": budget.recent_readings(self.root, time.time()),
        }

    def health(self, refresh: bool) -> dict[str, Any]:
        res = self.runner.get("doctor", refresh)
        data = res.get("data")
        return {
            "command": _command(res),
            "doctor": health.doctor(data),
            "problems": health.problems(data),
        }


def _command(res: dict[str, Any]) -> dict[str, Any]:
    return {
        k: res.get(k)
        for k in ("status", "exit", "error", "stderr", "as_of", "next_refresh_at")
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "factory-ui"
    sys_version = ""
    server: FactoryServer

    def __getattr__(self, name: str) -> Callable[[], None]:
        # Any method other than GET and HEAD: 405, before routing.
        if name.startswith("do_"):
            return self._not_allowed
        raise AttributeError(name)

    def _not_allowed(self) -> None:
        self._send(
            405, {"error": "method not allowed"}, extra=(("Allow", "GET, HEAD"),)
        )

    def do_GET(self) -> None:
        self._handle(body=True)

    def do_HEAD(self) -> None:
        self._handle(body=False)

    def _handle(self, body: bool) -> None:
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send(403, {"error": "forbidden host"}, body=body)
            return
        path, _, query = self.path.partition("?")
        try:
            status, payload = self.server.app.route(path, query)
        except Exception as e:  # noqa: BLE001 - a view error is a 500, not a crash
            sys.stderr.write(f"factory-ui: {path}: {type(e).__name__}\n")
            status, payload = 500, {"error": "internal error"}
        self._send(status, payload, body=body)

    def _send(
        self,
        status: int,
        payload: Any,
        body: bool = True,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if isinstance(payload, tuple):
            data, ctype = payload
        else:
            data = json.dumps(payload).encode()
            ctype = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def end_headers(self) -> None:
        # Every response, the server's own error pages included.
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"factory-ui: {self.command} {self.path.partition('?')[0]}\n")


class FactoryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64  # several tabs refreshing at once

    def __init__(self, port: int, app: App) -> None:
        self.app = app
        super().__init__((config.HOST, port), Handler)


def make_server(root: Path, port: int, runner: Runner | None = None) -> FactoryServer:
    """A server on 127.0.0.1:`port` over the factory dir `root`."""
    return FactoryServer(port, App(root, runner))
