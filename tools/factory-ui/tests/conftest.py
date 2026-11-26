"""Fixtures for the factory UI tests.

Every test builds its own factory tree under ``tmp_path``; nothing here reads
or writes the real factory directory. The data is invented and only copies
the shape of the real files.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from factory_ui import runner as runner_mod  # noqa: E402
from factory_ui import server as server_mod  # noqa: E402

ACTIVE = "2026-01-01-example-active"
PR_OPEN = "2026-01-02-example-pr-open"
ESCALATED = "2026-01-03-example-escalated"
MERGED = "2026-01-04-example-merged"
NO_CACHE = "2026-01-05-example-no-cache"
BROKEN = "2026-01-06-example-broken"
LINKED = "2026-01-07-example-linked"

WRITE_TOKEN = "wt-fixture-token-7c1e"
LEASE_SESSION = "lease-session-fixture-93ab"
DOCS_VALUE = "docs-fixture-value-41d2"
RERUN_MARKER = "RERUN-MARKER-ENV-VALUE-5f0a"
OUTSIDE_BYTES = "OUTSIDE-TARGET-BYTES-c4d9"
NEWEST_SESSION = "aaaaaaaa-fresh-0001"
SESSIONS = {
    "fresh": NEWEST_SESSION,
    "onewin": "bbbbbbbb-onewin-0002",
    "stale": "cccccccc-stale-0003",
    "old": "dddddddd-old-0004",
}
TMP_SESSION = "eeeeeeee-tmp-0005"

SHA_A = "1111111111111111111111111111111111111111"
SHA_B = "2222222222222222222222222222222222222222"

XSS_SPEC = """# Example spec

Plain paragraph with <script>alert(1)</script> inside.

<img src=x onerror=alert(2)>

A [bad link](javascript:alert(3)) and a [good link](https://example.org/x).

| a | b |
| --- | --- |
| <b>cell</b> | `code` |

```
<script>alert(4)</script>
```
"""

VERIFY_FLAGGED = f"""VERDICT: CHANGES
HEAD: {SHA_A}
BASE: {SHA_B}
MODE: gate
PROOF: pytest
REPRO-SHA: {SHA_A}
WHY: flip
CLAIMED: PASS
OBSERVES: the fixture behaviour
FLAG: diff-finding

## Findings

The diff changes a line the reproduction does not cover.

## Re-run

FIXTURE_ENV={RERUN_MARKER}
PATH=/opt/example/bin

## Notes

After the hidden section.
"""

SHADOW_CLEAN = f"""VERDICT: PASS
HEAD: {SHA_A}
BASE: {SHA_B}
MODE: shadow
PROOF: pytest
REPRO-SHA: {SHA_A}
WHY: flip
CLAIMED: PASS
OBSERVES: the fixture behaviour

## Notes

Nothing flagged.
"""


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def review(verdict: str) -> str:
    return f"VERDICT: {verdict}\nHEAD: {SHA_A}\nBASE: {SHA_B}\n\n## Checks\n\nok\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _job(root: Path, job: str, doc: Any, raw: str | None = None) -> Path:
    folder = root / "jobs" / job
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.json").write_text(raw if raw is not None else json.dumps(doc))
    return folder


def _verdict(v: str) -> dict:
    return {"verdict": v, "head": SHA_A, "base": SHA_B}


def build_jobs(root: Path, outside: Path, now: float) -> None:
    lease = {
        "host": "mac-a",
        "session": LEASE_SESSION,
        "since": int(now - 3600),
        "beat": int(now - 120),
        "expires": int(now + 3600),
    }
    active = {
        "id": ACTIVE,
        "phase": "review",
        "round": 1,
        "max_rounds": 3,
        "pr": None,
        "home": "mac-a",
        "linear": "GOO-1001",
        "request": "Example request text that is long enough " * 5,
        "pr_title": None,
        "created": iso(now - 2 * 3600),
        "finished": None,
        "lease": lease,
        "kind": None,
        "hold": None,
        "write_token": WRITE_TOKEN,
        "docs": {"spec": DOCS_VALUE},
        "verdicts": {
            "1": {
                "code": _verdict("PASS"),
                "security": _verdict("CHANGES"),
                "verify": _verdict("CHANGES"),
            }
        },
        "verify_runs": {
            "1": {
                "verdict": "CHANGES",
                "why": "flip",
                "mode": "gate",
                "flags": ["diff-finding"],
            }
        },
    }
    folder = _job(
        root,
        ACTIVE,
        {
            "id": ACTIVE,
            "worktree": "/srv/example/worktree",
            "port": 4100,
            "write_token": WRITE_TOKEN,
            "cache": active,
            "cache_at": int(now - 300),
        },
    )
    _write(folder / "spec.md", XSS_SPEC)
    _write(folder / "build.md", "STATUS: READY\nCOMMIT: example commit\n\n# Build\n")
    _write(folder / "related-tests.md", "RELATED: pass\nLINT: pass\n")
    _write(folder / "round-1" / "review-code.md", review("PASS"))
    # The state says CHANGES; the file says PASS: a mismatch.
    _write(folder / "round-1" / "review-security.md", review("PASS"))
    _write(folder / "round-1" / "review-xmodel.md", review("PASS"))
    _write(folder / "round-1" / "review-verify.md", VERIFY_FLAGGED)
    _write(folder / "scratch" / "x", "scratch content\n")
    _write(folder / "xmodel-raw.json", "{}\n")
    # A symlink inside the job folder that points outside the root.
    os.symlink(outside / "secret.md", folder / "pr-body.md")

    pr_open = {
        "id": PR_OPEN,
        "phase": "pr-open",
        "round": 2,
        "max_rounds": 3,
        "pr": 42,
        "home": "mac-b",
        "linear": "GOO-1002",
        "request": "Second example request",
        "pr_title": "Example PR title",
        "created": iso(now - 3 * 86400),
        "finished": None,
        "lease": {},
        "verdicts": {
            "1": {"code": _verdict("CHANGES")},
            "2": {"code": _verdict("PASS")},
        },
        "verify_shadow": {
            "1": {"verdict": "PASS", "why": "flip", "mode": "shadow", "flags": []}
        },
    }
    folder = _job(
        root, PR_OPEN, {"id": PR_OPEN, "cache": pr_open, "cache_at": int(now - 60)}
    )
    _write(folder / "spec.md", "# Second spec\n\nText.\n")
    _write(folder / "decision.md", "APPROVE\nKIND: none\n\n# Decision\n")
    _write(folder / "pr-body.md", "## Summary\n\nExample body.\n")
    _write(folder / "round-1" / "review-code.md", review("CHANGES"))
    _write(folder / "round-1" / "shadow-verify.md", SHADOW_CLEAN)
    _write(folder / "round-2" / "review-code.md", review("PASS"))

    escalated = {
        "id": ESCALATED,
        "phase": "escalated",
        "round": 1,
        "max_rounds": 3,
        "pr": None,
        "home": "mac-a",
        "linear": "GOO-1003",
        "request": "Third example request",
        "created": iso(now - 86400),
        "finished": None,
        "kind": "POLICY",
        "hold": "waiting on the owner",
        "lease": {},
    }
    folder = _job(
        root,
        ESCALATED,
        {"id": ESCALATED, "cache": escalated, "cache_at": int(now - 600)},
    )
    _write(folder / "decision-1.md", "ESCALATE\nKIND: POLICY\n\n# Why\n")

    merged = {
        "id": MERGED,
        "phase": "merged",
        "round": 1,
        "max_rounds": 3,
        "pr": 41,
        "home": "mac-b",
        "linear": "GOO-1004",
        "request": "Fourth example request",
        "pr_title": "Merged example",
        "created": iso(now - 10 * 86400),
        "finished": iso(now - 9 * 86400),
        "lease": {},
    }
    folder = _job(
        root, MERGED, {"id": MERGED, "cache": merged, "cache_at": int(now - 7200)}
    )
    # Over 2 MB: served cut, with the notice.
    _write(folder / "build.md", "STATUS: READY\n" + ("x" * 99 + "\n") * 21_000)

    _job(root, NO_CACHE, {"id": NO_CACHE, "port": 4103, "worktree": "/srv/x"})
    _job(root, BROKEN, None, raw="{not json")

    # A symlinked job folder pointing outside the root.
    os.symlink(outside / "job-dir", root / "jobs" / LINKED)


BACKTEST_ROWS: list[Any] = [
    {
        "job": "2026-01-10-bt-one",
        "verdict": "PASS",
        "why": "flip",
        "flip": True,
        "overclaim": None,
        "revised": False,
        "flags": [],
        "expect": "PASS",
        "match": True,
        "owner_verdict": "real",
        "owner_note": "looks right",
        "at": "2026-01-10T10:00:00",
        "proof": "pytest",
        "review": "verify-backtest/bt-one/review-verify.md",
    },
    {
        "job": "2026-01-11-bt-two",
        "verdict": "PASS",
        "why": "flip",
        "flip": True,
        "flags": ["diff-finding"],
        "expect": "PASS",
        "match": "flagged-pass",
        "at": "2026-01-11T10:00:00",
        "proof": "vitest",
    },
    {
        "job": "2026-01-12-bt-three",
        "verdict": "PASS",
        "why": "flip",
        "flip": True,
        "flags": ["diff-finding"],
        "expect": "CHANGES",
        "match": "flagged",
        "at": "2026-01-12T10:00:00",
        "proof": "pytest",
    },
    {
        "job": "2026-01-13-bt-four",
        "verdict": "INCONCLUSIVE",
        "why": "no flip",
        "flip": False,
        "overclaim": True,
        "flags": [],
        "expect": "PASS",
        "match": False,
        "at": "2026-01-13T10:00:00",
        "proof": "pytest",
    },
    # An older row: no `flags` key.
    {
        "job": "2026-01-14-bt-five",
        "verdict": "PASS",
        "why": "flip",
        "expect": "PASS",
        "match": None,
        "at": "2026-01-14T10:00:00",
        "proof": "playwright",
    },
    {
        "job": "2026-01-15-bt-six",
        "verdict": "INCONCLUSIVE",
        "why": "no runner",
        "revised": True,
        "at": "2026-01-15T10:00:00",
        "proof": "none",
    },
]

# Hand-counted from BACKTEST_ROWS, the way the helper counts.
EXPECTED_BLOCK = {
    "rows": 6,
    "verdict": {"PASS": 4, "INCONCLUSIVE": 2},
    "expected": 5,
    "match": 1,
    "flagged": 1,
    "flagged_on_expected_pass": 1,
    "no_match": 2,
    "owner_read": 1,
}


def build_verifier(root: Path) -> None:
    (root / "verify.mode").write_text("shadow\n")
    lines = [json.dumps(r) for r in BACKTEST_ROWS]
    lines.append(json.dumps([1, 2]))  # valid JSON, not an object
    lines.append('{"job": "2026-01-16-bt-broken", "verd')  # broken last line
    (root / "verify-backtest.jsonl").write_text("\n".join(lines) + "\n")


def build_usage(root: Path, now: float) -> None:
    usage = root / "usage"
    usage.mkdir(parents=True)

    def win(pct: float, resets: float) -> dict:
        return {"used_percentage": pct, "resets_at": int(resets)}

    readings = {
        SESSIONS["fresh"]: {
            "seen_at": int(now - 60),
            "rate_limits": {
                "five_hour": win(40, now + 3600),
                "seven_day": win(60, now + 86400),
            },
        },
        SESSIONS["onewin"]: {
            "seen_at": int(now - 600),
            "rate_limits": {"seven_day": win(61, now + 86400)},
        },
        SESSIONS["stale"]: {
            "seen_at": int(now - 2 * 3600),
            "rate_limits": {
                "five_hour": win(10, now - 60),
                "seven_day": win(55, now + 86400),
            },
        },
        SESSIONS["old"]: {
            "seen_at": int(now - 7 * 3600),
            "rate_limits": {"five_hour": win(5, now - 3600)},
        },
    }
    for sid, data in readings.items():
        (usage / f"{sid}.json").write_text(json.dumps(data))
    # A reading being written: newer than all, and ignored.
    (usage / f"{TMP_SESSION}.json.tmp").write_text(
        json.dumps({"seen_at": int(now), "rate_limits": {}})
    )


PARALLEL_LOG = [
    {"at": "2026-12-01T10:00:00", "host": "mac-a", "mode": "on", "kind": "slot"},
    {
        "at": "2026-12-01T10:01:00",
        "host": "mac-a",
        "mode": "on",
        "kind": "claim",
        "job": ACTIVE,
        "sid": "s-1",
    },
    {
        "at": "2026-12-01T10:02:00",
        "host": "mac-b",
        "mode": "on",
        "kind": "brand-new-kind",
        "job": PR_OPEN,
        "extra_field": {"paths": ["a.py"]},
    },
]


def build_parallel(root: Path) -> None:
    (root / "parallel.mode").write_text("on 2 2026-12-01\n")
    lines = [json.dumps(r) for r in PARALLEL_LOG] + ["{broken"]
    (root / "parallel-log.jsonl").write_text("\n".join(lines) + "\n")


DOCTOR_OK = {
    "host": "mac-a",
    "key_file": "service.key",
    "key": "present (0600)",
    "linear": "ok",
    "jobs_in_linear": 5,
    "verify": {
        "mode": "shadow",
        "since": None,
        "tools": {"uv": False, "node": "24.1.0", "node_24": True},
        "problems": ["uv is not on PATH (venv builds)"],
        "inbound_blocked": True,
        "residuals": {"procargs": "a test can read the environment"},
        "leftovers": {
            "worktrees": ["verify-wt-1"],
            "env_folders": [],
            "private_env_folders": [],
            "open_envs": [],
            "live_groups": 0,
            "sandboxed_orphans": [],
        },
    },
}
DOCTOR_LINEAR_DOWN = dict(
    DOCTOR_OK, linear="Linear: HTTP 503 Service Unavailable", key="missing"
)
DOCTOR_LINEAR_DOWN.pop("jobs_in_linear")

BUDGET_GO = {
    "go": True,
    "why": "under limits",
    "usage": {
        "five_hour": {"used_percentage": 40, "resets_at": 1900000000},
        "seven_day": {"used_percentage": 60, "resets_at": 1900500000},
    },
    "reading": "own",
}
BUDGET_WAIT = {
    "go": False,
    "why": "five_hour 80% >= 75%",
    "wake_at": 1900000000,
    "stale": False,
    "reading": "own",
}
BUDGET_NO_DATA = {
    "go": True,
    "why": "no usage data; starting",
    "reading": "no session id",
}

DEFAULT_CONTROL = {
    "doctor": {"out": DOCTOR_OK, "exit": 0, "sleep": 0},
    "budget": {"out": BUDGET_GO, "exit": 0, "sleep": 0},
}

FAKE_F = """#!{python}
import json, os, sys, time
LOG = {log!r}
CONTROL = {control!r}
name = sys.argv[1] if len(sys.argv) > 1 else ""
with open(LOG, "a") as fh:
    fh.write(json.dumps({{
        "argv": sys.argv,
        "env": sorted(os.environ),
        "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "stdin_tty": sys.stdin.isatty(),
    }}) + "\\n")
with open(CONTROL) as fh:
    spec = json.load(fh).get(name, {{"out": {{"error": "unknown"}}, "exit": 2}})
if spec.get("sleep"):
    time.sleep(spec["sleep"])
if "raw" in spec:
    sys.stdout.write(spec["raw"])
else:
    print(json.dumps(spec["out"]))
sys.exit(spec.get("exit", 0))
"""


@dataclass
class Factory:
    root: Path
    outside: Path
    log: Path
    control: Path
    now: float

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines() if x]

    def set_control(self, name: str, **spec: Any) -> None:
        data = json.loads(self.control.read_text())
        data[name] = {"out": {}, "exit": 0, "sleep": 0, **spec}
        self.control.write_text(json.dumps(data))


def build_factory(tmp_path: Path) -> Factory:
    now = time.time()
    root = tmp_path / "factory"
    outside = tmp_path / "outside"
    (outside / "job-dir").mkdir(parents=True)
    (outside / "secret.md").write_text(OUTSIDE_BYTES + "\n")
    (outside / "job-dir" / "spec.md").write_text(OUTSIDE_BYTES + "\n")
    (outside / "job-dir" / "job.json").write_text(
        json.dumps({"id": LINKED, "cache": {"id": LINKED, "phase": OUTSIDE_BYTES}})
    )
    root.mkdir()
    build_jobs(root, outside, now)
    build_verifier(root)
    build_usage(root, now)
    build_parallel(root)
    log = outside / "f-calls.jsonl"
    control = outside / "f-control.json"
    control.write_text(json.dumps(DEFAULT_CONTROL))
    fake = root / "bin" / "factory"
    fake.parent.mkdir()
    fake.write_text(
        FAKE_F.format(python=sys.executable, log=str(log), control=str(control))
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return Factory(root=root, outside=outside, log=log, control=control, now=now)


@pytest.fixture
def factory(tmp_path: Path) -> Factory:
    return build_factory(tmp_path)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    header_list: list[tuple[str, str]]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def raw_request(
    port: int,
    path: str | bytes,
    method: str = "GET",
    host: str | None = None,
) -> Response:
    """Send one request byte for byte, so encoded and odd paths reach the
    server unchanged."""
    target = path if isinstance(path, bytes) else path.encode("latin-1")
    host_line = (host if host is not None else f"127.0.0.1:{port}").encode()
    req = method.encode() + b" " + target + b" HTTP/1.0\r\nHost: " + host_line
    req += b"\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(req)
        chunks = []
        while True:
            data = s.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    header_list = []
    for line in lines[1:]:
        k, _, v = line.partition(":")
        header_list.append((k.strip(), v.strip()))
    headers = {k.lower(): v for k, v in header_list}
    return Response(status, headers, header_list, body)


@dataclass
class Running:
    server: server_mod.FactoryServer
    port: int
    factory: Factory

    def get(self, path: str | bytes, **kw: Any) -> Response:
        return raw_request(self.port, path, **kw)


def start(
    factory: Factory, runner: runner_mod.Runner | None = None
) -> Iterator[Running]:
    srv = server_mod.make_server(factory.root, 0, runner=runner)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield Running(srv, srv.server_address[1], factory)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def running(factory: Factory) -> Iterator[Running]:
    yield from start(factory)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def clocked(factory: Factory, clock: FakeClock) -> Iterator[Running]:
    yield from start(factory, runner_mod.Runner(factory.root, clock=clock))


def job_ids_listed(factory: Factory) -> list[str]:
    return sorted(
        n
        for n in os.listdir(factory.root / "jobs")
        if not os.path.islink(factory.root / "jobs" / n)
    )


ARTIFACT_KEYS = {
    ACTIVE: [
        "spec",
        "build",
        "related-tests",
        "round-1/review-code",
        "round-1/review-security",
        "round-1/review-xmodel",
        "round-1/review-verify",
    ],
    PR_OPEN: [
        "spec",
        "decision",
        "pr-body",
        "round-1/review-code",
        "round-1/shadow-verify",
        "round-2/review-code",
    ],
    ESCALATED: ["decision-1"],
    MERGED: ["build"],
}

ALL_ROUTES = [
    "/",
    "/static/index.html",
    "/static/app.js",
    "/static/render.js",
    "/static/app.css",
    "/static/tokens.css",
    "/api/jobs",
    "/api/verifier",
    "/api/budget",
    "/api/health",
    "/api/parallel",
    *[f"/api/jobs/{j}" for j in (ACTIVE, PR_OPEN, ESCALATED, MERGED, NO_CACHE)],
    *[f"/api/jobs/{j}/artifact/{k}" for j, ks in ARTIFACT_KEYS.items() for k in ks],
]
