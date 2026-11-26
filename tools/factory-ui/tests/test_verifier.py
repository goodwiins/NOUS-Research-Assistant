"""Acceptance criterion 12: the Verifier view."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ACTIVE, EXPECTED_BLOCK, PR_OPEN, Factory, Running
from dom import render
from factory_ui.readers import verifier


@pytest.mark.parametrize(
    "content,expected",
    [
        ("off\n", ("off", None)),
        ("shadow\n", ("shadow", None)),
        ("gate 2026-11-25\n", ("gate", "2026-11-25")),
        (None, ("off", None)),
        ("full speed ahead\n", ("off", None)),
        ("gate 2026-13-45\n", ("off", None)),
        ("gate\n", ("off", None)),
        ("gate 2026-11-25 extra\n", ("off", None)),
        ("shadow off\n", ("off", None)),
        ("  shadow  \n", ("shadow", None)),
    ],
)
def test_verify_mode_follows_the_helper(
    factory: Factory, content: str | None, expected: tuple
) -> None:
    f = factory.root / "verify.mode"
    f.unlink()
    if content is not None:
        f.write_text(content)
    assert verifier.verify_mode(factory.root) == expected


def test_backtests_block_counts(factory: Factory) -> None:
    assert verifier.count_backtests(factory.root) == EXPECTED_BLOCK


def test_backtests_block_without_file(factory: Factory) -> None:
    (factory.root / "verify-backtest.jsonl").unlink()
    block = verifier.count_backtests(factory.root)
    assert block["rows"] == 0 and block["verdict"] == {}


def test_api_verifier(running: Running) -> None:
    data = running.get("/api/verifier").json()
    assert data["mode"] == {"mode": "shadow", "since": None}
    assert data["backtests"] == EXPECTED_BLOCK
    assert len(data["rows"]) == 6
    assert data["skipped"] == 2
    first = data["rows"][0]
    for key in (
        "job",
        "verdict",
        "why",
        "flags",
        "flip",
        "overclaim",
        "revised",
        "expect",
        "match",
        "owner_verdict",
        "owner_note",
        "at",
        "proof",
    ):
        assert key in first
    assert "review" not in first
    assert data["rows"][4]["flags"] is None  # an older row without flags
    rounds = {(r["job"], r["round"]): r for r in data["rounds"]}
    assert rounds == {
        (ACTIVE, "1"): {
            "job": ACTIVE,
            "round": "1",
            "field": "verify_runs",
            "mode": "gate",
            "verdict": "CHANGES",
            "why": "flip",
            "diff_finding": True,
        },
        (PR_OPEN, "1"): {
            "job": PR_OPEN,
            "round": "1",
            "field": "verify_shadow",
            "mode": "shadow",
            "verdict": "PASS",
            "why": "flip",
            "diff_finding": False,
        },
    }


def test_rendered_verifier(running: Running, tmp_path: Path) -> None:
    data = running.get("/api/verifier").json()
    out = render("verifier", data, tmp_path)
    text = out["text"]
    assert "recomputed from this Mac's files" in text
    assert "flagged_on_expected_pass" in text
    assert "diff-finding" in text
    assert "flagged-pass" in text
    data["rounds"] = []
    assert "no verify runs recorded" in render("verifier", data, tmp_path)["text"]
