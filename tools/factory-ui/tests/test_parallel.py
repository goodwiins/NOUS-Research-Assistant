"""Acceptance criterion 14: the Parallel placeholder view."""

from __future__ import annotations

from pathlib import Path

from conftest import Factory, start
from dom import render


def _get(factory: Factory) -> dict:
    data: dict = {}
    for running in start(factory):
        data = running.get("/api/parallel").json()
    return data


def test_dormant(factory: Factory, tmp_path: Path) -> None:
    (factory.root / "parallel.mode").unlink()
    (factory.root / "parallel-log.jsonl").unlink()
    data = _get(factory)
    assert data["state"] == "dormant"
    text = render("parallel", data, tmp_path)["text"]
    assert "Parallel jobs: off (no mode file on this Mac)" in text
    for panel in ("Slots", "Claims", "Waiting jobs"):
        assert panel in text


def test_mode_only(factory: Factory, tmp_path: Path) -> None:
    (factory.root / "parallel-log.jsonl").unlink()
    data = _get(factory)
    assert data["state"] == "mode"
    assert data["mode"] == {"mode": "on", "slots": 2, "since": "2026-12-01"}
    text = render("parallel", data, tmp_path)["text"]
    assert "2026-12-01" in text and "on" in text
    for bad in ("on two 2026-12-01", "on 2", "on 0 2026-12-01", "on 2 2026-02-30", "x"):
        (factory.root / "parallel.mode").write_text(bad + "\n")
        data = _get(factory)
        assert data["state"] == "mode"
        assert data["mode"] == {"mode": "off", "slots": None, "since": None}
    (factory.root / "parallel.mode").write_text("shadow\n")
    assert _get(factory)["mode"] == {"mode": "shadow", "slots": None, "since": None}


def test_lit(factory: Factory, tmp_path: Path) -> None:
    data = _get(factory)
    assert data["state"] == "lit"
    assert data["skipped"] == 1
    kinds = [r["kind"] for r in data["rows"]]
    assert kinds == ["slot", "claim", "brand-new-kind"]
    odd = data["rows"][2]
    assert odd["extra"] == {"extra_field": {"paths": ["a.py"]}}
    assert data["rows"][1]["extra"] == {"sid": "s-1"}
    text = render("parallel", data, tmp_path)["text"]
    assert "brand-new-kind" in text
    assert "extra_field" in text
    for panel in ("Slots", "Claims", "Waiting jobs"):
        assert panel in text


def test_lit_keeps_last_200(factory: Factory) -> None:
    lines = [f'{{"at": "t{i}", "kind": "slot"}}' for i in range(250)]
    (factory.root / "parallel-log.jsonl").write_text("\n".join(lines) + "\n")
    data = _get(factory)
    assert len(data["rows"]) == 200
    assert data["rows"][0]["at"] == "t50" and data["rows"][-1]["at"] == "t249"
