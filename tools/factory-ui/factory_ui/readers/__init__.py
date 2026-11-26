"""Readers: pure functions from files under the factory root to dicts."""

from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """`value` when it is a JSON array, else an empty one."""
    return value if isinstance(value, list) else []
