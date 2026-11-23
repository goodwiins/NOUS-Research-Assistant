"""The frontend's project-refetch tool list must track the backend's (GOO-727).

``frontend/src/lib/agent-tools.ts`` declares ``PROJECT_MUTATING_TOOLS`` (tools
whose success makes the project rail refetch) and
``MUTATING_TOOLS_WITHOUT_PROJECT_DATA`` (reviewed exceptions). Together they
must cover ``identity_ledger._MUTATING_TOOLS`` exactly. The frontend test
``frontend/src/lib/__tests__/agent-tools.test.ts`` checks the same contract,
but backend-only PRs do not run frontend tests, so a new or renamed backend
mutating tool is caught here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.services.agent.identity_ledger import _MUTATING_TOOLS

pytestmark = pytest.mark.unit

AGENT_TOOLS_TS = Path(__file__).resolve().parents[5] / "frontend/src/lib/agent-tools.ts"


def _block(source: str, start_pattern: str, end: str) -> str:
    match = re.search(start_pattern, source)
    assert match, f"{start_pattern!r} not found in {AGENT_TOOLS_TS}"
    tail = source[match.end() :]
    stop = tail.find(end)
    assert stop >= 0, f"unterminated block after {start_pattern!r}"
    return tail[:stop]


def frontend_project_tools(source: str) -> set[str]:
    body = _block(source, r"PROJECT_MUTATING_TOOLS[^=]*=\s*new Set\(\[", "])")
    return set(re.findall(r"'([a-z_]+)'", body))


def frontend_allowlist(source: str) -> set[str]:
    body = _block(source, r"MUTATING_TOOLS_WITHOUT_PROJECT_DATA[^=]*=\s*\{", "};")
    return set(re.findall(r"^\s*([a-z_]+):", body, re.MULTILINE))


def test_reads_the_frontend_lists() -> None:
    source = AGENT_TOOLS_TS.read_text(encoding="utf-8")
    assert len(frontend_project_tools(source)) >= 5
    assert frontend_allowlist(source)


def test_frontend_lists_cover_the_backend_mutating_tools_exactly() -> None:
    source = AGENT_TOOLS_TS.read_text(encoding="utf-8")
    project = frontend_project_tools(source)
    allowlist = frontend_allowlist(source)

    assert not project & allowlist, "a tool cannot be both refetching and allowlisted"
    missing = set(_MUTATING_TOOLS) - project - allowlist
    assert not missing, (
        f"backend mutating tools {sorted(missing)} are not in the frontend's "
        "PROJECT_MUTATING_TOOLS or MUTATING_TOOLS_WITHOUT_PROJECT_DATA "
        "(frontend/src/lib/agent-tools.ts); add each to one of them"
    )
    unknown = (project | allowlist) - set(_MUTATING_TOOLS)
    assert not unknown, (
        f"frontend lists name {sorted(unknown)}, which identity_ledger._MUTATING_TOOLS "
        "does not; a backend tool was renamed or removed"
    )


def test_parsers_read_the_ts_shapes() -> None:
    source = (
        "export const PROJECT_MUTATING_TOOLS: ReadonlySet<string> = new Set([\n"
        "  'a_tool',\n  'b_tool',\n]);\n"
        "export const MUTATING_TOOLS_WITHOUT_PROJECT_DATA: Readonly<\n"
        "  Record<string, string>\n> = {\n"
        "  c_tool:\n    'reason',\n  d_tool: \"reason: with colon\",\n};\n"
    )
    assert frontend_project_tools(source) == {"a_tool", "b_tool"}
    assert frontend_allowlist(source) == {"c_tool", "d_tool"}
