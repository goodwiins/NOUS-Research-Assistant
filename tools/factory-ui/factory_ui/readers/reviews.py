"""Review and verify file headers, and the hidden `## Re-run` section."""

from __future__ import annotations

import re

HIDDEN_NOTICE = "hidden: environment values; open the file on disk"

# A level-2 heading, as the helper's section() finds one: `##`, then a space
# or a tab. The Re-run redactor and the header parser share it.
_SECTION = r"##[ \t]"
# The body of a `## Re-run` section runs to the next level-2 heading or the
# end, like the helper's section(). A `###` heading inside stays in the body.
_RERUN = re.compile(
    rf"(?ms)^({_SECTION}[ \t]*Re-run\b[^\n]*)(?:\n|\Z)(.*?)(?=^{_SECTION}|\Z)"
)
_SECTION_START = re.compile(rf"^{_SECTION}")

VERIFY_KEYS = (
    "VERDICT",
    "HEAD",
    "BASE",
    "MODE",
    "PROOF",
    "REPRO-SHA",
    "WHY",
    "CLAIMED",
    "OBSERVES",
)
_HEADER_LINE = re.compile(r"^([A-Z][A-Z-]*):[ \t]*(.*)$")


def hide_rerun(text: str) -> str:
    """Replace the body of every `## Re-run` section with HIDDEN_NOTICE."""
    return _RERUN.sub(lambda m: f"{m.group(1)}\n\n{HIDDEN_NOTICE}\n", text)


def review_header(text: str) -> tuple[str, str, str]:
    """Line 1, and HEAD and BASE from lines 2 and 3, as the helper's
    review_header reads them."""
    lines = text.splitlines() + ["", "", ""]
    return (
        lines[0].strip(),
        lines[1].strip().removeprefix("HEAD:").strip(),
        lines[2].strip().removeprefix("BASE:").strip(),
    )


def file_verdict(text: str) -> str | None:
    """The verdict word of line 1 (`VERDICT: PASS` gives `PASS`)."""
    first = review_header(text)[0]
    if not first:
        return None
    return first.removeprefix("VERDICT:").strip() or None


def verify_header(text: str) -> dict[str, object]:
    """The header lines of a verify file, up to the first level-2 heading:
    each key of VERIFY_KEYS once, and every `FLAG:` value as a list. The
    `## Re-run` body is hidden first, so nothing from it can be returned."""
    out: dict[str, object] = {}
    flags: list[str] = []
    for line in hide_rerun(text).splitlines():
        if _SECTION_START.match(line):
            break
        m = _HEADER_LINE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if key == "FLAG":
            flags.append(value)
        elif key in VERIFY_KEYS and key not in out:
            out[key] = value
    out["FLAG"] = flags
    return out
