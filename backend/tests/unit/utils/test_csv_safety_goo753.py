"""CSV exports keep plain numbers as numbers and neutralise formulas (GOO-753).

The same table runs against both backend CSV writers: the behaviour-analytics
export and the research-report audit CSV. It matches the frontend helper's
cases (``frontend/src/utils/csv.ts``, GOO-666 FR-15).
"""

import csv
import io
from decimal import Decimal
from typing import Any, Callable

import pytest

from src.api.quality import user_behavior as ub
from src.services.research_engine.report_rendering import render_csv


def _behavior_cell(value: Any) -> str:
    rows = list(csv.DictReader(io.StringIO(ub._csv_table([{"cell": value}]))))
    return str(rows[0]["cell"])


def _report_cell(value: Any) -> str:
    rendered = render_csv([{"reason": value}], final_status="verified")
    rows = list(csv.DictReader(io.StringIO(rendered, newline="")))
    return str(rows[0]["reason"])


WRITERS = [
    pytest.param(_behavior_cell, id="user_behavior"),
    pytest.param(_report_cell, id="report_rendering"),
]

UNCHANGED: list[tuple[Any, str]] = [
    ("-5", "-5"),
    ("-3.2", "-3.2"),
    ("+1.5", "+1.5"),
    ("-12%", "-12%"),
    (-5, "-5"),
    (-3.2, "-3.2"),
    (Decimal("-7.25"), "-7.25"),
    # Real numbers whose text is not a plain number still stay numbers.
    (-1e-05, "-1e-05"),
    (-1e21, "-1e+21"),
    (Decimal("-1E+5"), "-1E+5"),
    ("a=b", "a=b"),
]

PREFIXED = [
    "-1e5",
    "-5.",
    "-.5",
    "--5",
    "-5%%",
    "-5,000",
    "- 5",
    " -5",
    "-5\n=cmd",
    "=1+1",
    "@SUM(A1)",
    "\t=x",
    "\r=x",
    "\tcmd",
    "  =HYPERLINK(1)",
    "\n+1",
]


@pytest.mark.parametrize("write", WRITERS)
@pytest.mark.parametrize(("value", "expected"), UNCHANGED)
def test_plain_numbers_and_plain_text_are_left_alone(
    write: Callable[[Any], str], value: Any, expected: str
) -> None:
    assert write(value) == expected


@pytest.mark.parametrize("write", WRITERS)
@pytest.mark.parametrize("value", PREFIXED)
def test_formula_starts_get_a_quote_prefix(
    write: Callable[[Any], str], value: str
) -> None:
    assert write(value) == "'" + value


def test_unicode_digits_are_not_a_plain_number() -> None:
    # "-٣" (Arabic-Indic three) is not an ASCII plain number, so it is quoted,
    # the same as the frontend's ASCII-only \d.
    assert _behavior_cell("-٣") == "'-٣"
    assert _report_cell("-٣") == "'-٣"
