"""Keep CSV cells a user opens in a spreadsheet from running as formulas.

A spreadsheet runs a cell that starts with ``=``, ``+``, ``-`` or ``@`` as a
formula, and Excel and others trim leading whitespace (spaces, tab, CR, LF)
before deciding, so a cell starting with a tab or a CR is a trigger too. Such
a cell is prefixed with ``'`` so it stays literal text.

A real number, or a string that is strictly a plain number (``-5``, ``+1.5``,
``-12%``), is left alone so signed numbers stay numbers (GOO-753). This is
the same rule as the frontend helper ``frontend/src/utils/csv.ts``.
"""

import re
from decimal import Decimal
from typing import Any

# Strictly a plain number: optional sign, ASCII digits, optional decimals,
# optional percent. Used with fullmatch, so "-5\n=cmd", " -5" and "-1e5"
# are not plain numbers.
_PLAIN_NUMBER = re.compile(r"[+-]?\d+(\.\d+)?%?", re.ASCII)
_FORMULA_START = re.compile(r"\s*[=+\-@]|[\t\r]")


def is_real_number(value: Any) -> bool:
    """An int, float or Decimal; a bool is not a number here."""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def needs_formula_quote(value: Any) -> bool:
    """True when ``value`` would run as a formula and needs a ``'`` prefix."""
    if is_real_number(value):
        return False
    text = value if isinstance(value, str) else str(value)
    if _PLAIN_NUMBER.fullmatch(text):
        return False
    return _FORMULA_START.match(text) is not None
