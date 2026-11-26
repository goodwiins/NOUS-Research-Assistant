"""Every failure escape in the Test Pipeline must say why it is there.

``continue-on-error: true`` and ``|| true`` make a failing step or command pass
silently, so a green run no longer proves the check ran. Each escape in
``test-pipeline.yml`` must carry a reason: a trailing comment on the same line,
or a comment line directly above it. A run of consecutive ``|| true`` lines
(the disk-cleanup blocks) may share one comment above the first line.

YAML parsing drops comments, so this test scans the raw workflow text.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml"

_CONTINUE_ON_ERROR = re.compile(r"continue-on-error:\s*true\b")
_OR_TRUE = re.compile(r"\|\|\s*true\b")
_TRAILING_COMMENT = re.compile(r"\s+#\s*\S")


def _has_trailing_comment(line: str, pattern: re.Pattern[str]) -> bool:
    return any(
        _TRAILING_COMMENT.match(line, match.end()) for match in pattern.finditer(line)
    )


def _is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("#") and bool(stripped[1:].strip())


def find_unjustified_escapes(text: str) -> list[int]:
    """Return the 1-based line numbers of escapes that carry no reason."""
    unjustified: list[int] = []
    previous = ""
    previous_justified_or_true = False
    for number, line in enumerate(text.splitlines(), start=1):
        justified_or_true = False
        if not line.strip().startswith("#"):
            patterns = [p for p in (_CONTINUE_ON_ERROR, _OR_TRUE) if p.search(line)]
            if patterns:
                justified = (
                    any(_has_trailing_comment(line, p) for p in patterns)
                    or _is_comment_line(previous)
                    or (patterns == [_OR_TRUE] and previous_justified_or_true)
                )
                if justified:
                    justified_or_true = _OR_TRUE in patterns
                else:
                    unjustified.append(number)
        previous = line
        previous_justified_or_true = justified_or_true
    return unjustified


def test_every_test_pipeline_escape_has_a_reason() -> None:
    lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
    offenders = find_unjustified_escapes("\n".join(lines))
    assert offenders == [], (
        "every continue-on-error: true / || true in test-pipeline.yml needs a "
        "same-line or directly-preceding comment saying why:\n"
        + "\n".join(f"  line {n}: {lines[n - 1].strip()}" for n in offenders)
    )


def test_uncommented_or_true_is_flagged() -> None:
    text = "run: |\n  echo start\n  rm -rf /tmp/x || true\n"
    assert find_unjustified_escapes(text) == [3]


def test_uncommented_continue_on_error_is_flagged() -> None:
    text = "- name: Step\n  run: make lint\n  continue-on-error: true\n"
    assert find_unjustified_escapes(text) == [3]


def test_trailing_comment_justifies_escape() -> None:
    text = (
        "- name: Step\n"
        "  run: make lint\n"
        "  continue-on-error: true # advisory until debt is gone\n"
        "  run: |\n"
        "    echo start\n"
        "    grep -c x file || true  # grep -c exits 1 on no match\n"
    )
    assert find_unjustified_escapes(text) == []


def test_comment_line_above_justifies_escape() -> None:
    text = (
        "- name: Step\n"
        "  # Advisory: replays the whole legacy chain\n"
        "  continue-on-error: true\n"
        "  run: |\n"
        "    # cleanup must not fail the job\n"
        "    rm -rf /tmp/x || true\n"
    )
    assert find_unjustified_escapes(text) == []


def test_one_comment_covers_a_run_of_or_true_lines() -> None:
    text = (
        "run: |\n"
        "  # best-effort cleanup\n"
        "  rm -rf /a || true\n"
        "  rm -rf /b || true\n"
        "  docker system prune -af || true\n"
    )
    assert find_unjustified_escapes(text) == []


def test_run_of_or_true_lines_without_comment_is_flagged() -> None:
    text = "run: |\n  echo start\n  rm -rf /a || true\n  rm -rf /b || true\n"
    assert find_unjustified_escapes(text) == [3, 4]


def test_continue_on_error_does_not_chain_from_an_or_true_run() -> None:
    text = "run: |\n  # cleanup\n  rm -rf /a || true\ncontinue-on-error: true\n"
    assert find_unjustified_escapes(text) == [4]


def test_continue_on_error_false_is_ignored() -> None:
    text = "- name: Step\n  run: make lint\n  continue-on-error: false\n"
    assert find_unjustified_escapes(text) == []


def test_escape_inside_a_comment_line_is_ignored() -> None:
    text = (
        "steps:\n"
        "  # with continue-on-error: true the gate could never fail\n"
        "  # never append || true to a blocking check\n"
    )
    assert find_unjustified_escapes(text) == []
