"""Readers: pure functions from files under the factory root to dicts."""

from __future__ import annotations

import json
import math
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """`value` when it is a JSON array, else an empty one."""
    return value if isinstance(value, list) else []


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


def loads(text: str) -> Any:
    """json.loads that refuses NaN, Infinity and numbers that overflow to
    them: Python accepts them, but they break age arithmetic and cannot be
    sent back as JSON."""
    return json.loads(text, parse_constant=_no_constant, parse_float=_finite)
