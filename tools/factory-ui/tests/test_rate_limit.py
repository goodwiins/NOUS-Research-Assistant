"""Acceptance criterion 4: cache and rate limit, with an injected clock."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from conftest import Factory, FakeClock, start
from factory_ui import config, runner


def _runs(factory: Factory, name: str) -> int:
    return sum(1 for c in factory.calls() if c["argv"][1] == name)


def test_doctor_runs_once_per_minute_and_cache_holds(
    factory: Factory, clock: FakeClock
) -> None:
    factory.set_control("doctor", out={"linear": "ok", "host": "mac-a"}, sleep=0.3)
    for running in start(factory, runner.Runner(factory.root, clock=clock)):
        with ThreadPoolExecutor(20) as pool:
            codes = list(
                pool.map(
                    lambda i: running.get(
                        "/api/health?refresh=1" if i % 2 else "/api/health"
                    ).status,
                    range(20),
                )
            )
        assert codes == [200] * 20
        assert _runs(factory, "doctor") == 1
        for t in (1, 30, 59.9):
            clock.t = 1000 + t
            assert running.get("/api/health?refresh=1").status == 200
            assert running.get("/api/health").status == 200
        assert _runs(factory, "doctor") == 1
        clock.t = 1061
        assert running.get("/api/health?refresh=1").status == 200
        assert _runs(factory, "doctor") == 2
        clock.t = 1120
        body = running.get("/api/health").json()
        assert _runs(factory, "doctor") == 2
        assert body["command"]["status"] == "answer"
        assert body["command"]["as_of"] <= body["command"]["next_refresh_at"] + 60


def test_doctor_cache_expires_after_300s_and_serves_stale_while_running(
    factory: Factory, clock: FakeClock
) -> None:
    r = runner.Runner(factory.root, clock=clock)
    first = r.get("doctor")
    assert _runs(factory, "doctor") == 1
    clock.t += 301
    second = r.get("doctor")
    assert _runs(factory, "doctor") == 2
    assert second["as_of"] >= first["as_of"]


def test_timeout_is_cached_like_a_result(factory: Factory, clock: FakeClock) -> None:
    factory.set_control("doctor", out={"linear": "ok"}, sleep=5)
    policies = dict(config.COMMANDS)
    policies["doctor"] = config.Policy(
        ttl=300, min_interval=60, timeout=0.5, serve_stale=True
    )
    r = runner.Runner(factory.root, clock=clock, policies=policies)
    res = r.get("doctor")
    assert res["status"] == "timeout"
    assert _runs(factory, "doctor") == 1
    clock.t += 30
    assert r.get("doctor", refresh=True)["status"] == "timeout"
    assert r.get("doctor")["status"] == "timeout"
    assert _runs(factory, "doctor") == 1
    clock.t += 31
    factory.set_control("doctor", out={"linear": "ok"})
    assert r.get("doctor", refresh=True)["status"] == "answer"
    assert _runs(factory, "doctor") == 2


def test_budget_at_most_once_per_10s(factory: Factory, clock: FakeClock) -> None:
    for running in start(factory, runner.Runner(factory.root, clock=clock)):
        for _ in range(5):
            running.get("/api/budget?refresh=1")
        assert _runs(factory, "budget") == 1
        clock.t += 9.9
        running.get("/api/budget?refresh=1")
        assert _runs(factory, "budget") == 1
        clock.t += 0.2
        running.get("/api/budget?refresh=1")
        assert _runs(factory, "budget") == 2
        # Without refresh: the 15 s cache answers until it expires.
        clock.t += 14
        running.get("/api/budget")
        assert _runs(factory, "budget") == 2
        clock.t += 2
        running.get("/api/budget")
        assert _runs(factory, "budget") == 3


def test_policies_match_the_spec() -> None:
    d, b = config.COMMANDS["doctor"], config.COMMANDS["budget"]
    assert (d.ttl, d.min_interval, d.timeout, d.serve_stale) == (300, 60, 120, True)
    assert (b.ttl, b.min_interval, b.timeout) == (15, 10, 10)


def test_failures_are_cached(factory: Factory, clock: FakeClock) -> None:
    factory.set_control("doctor", raw="not json", exit=0)
    r = runner.Runner(factory.root, clock=clock)
    assert r.get("doctor")["status"] == "error"
    assert r.get("doctor", refresh=True)["status"] == "error"
    assert _runs(factory, "doctor") == 1
    factory.set_control("doctor", out={"x": 1}, exit=6)
    clock.t += 61
    assert r.get("doctor", refresh=True)["status"] == "helper bug"
    factory.set_control("doctor", out={"linear": "down"}, exit=1)
    clock.t += 61
    assert r.get("doctor", refresh=True)["status"] == "answer"
    assert _runs(factory, "doctor") == 3
