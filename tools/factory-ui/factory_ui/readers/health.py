"""The `F doctor` answer, field by field, with the problems first."""

from __future__ import annotations

from typing import Any

from . import as_dict, as_list

TOP_FIELDS = ("host", "key_file", "key", "linear", "jobs_in_linear")
VERIFY_FIELDS = (
    "mode",
    "since",
    "tools",
    "inbound_blocked",
    "residuals",
    "leftovers",
)


def doctor(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    out: dict[str, Any] = {k: data[k] for k in TOP_FIELDS if k in data}
    verify = data.get("verify")
    if isinstance(verify, dict):
        out["verify"] = {k: verify[k] for k in VERIFY_FIELDS if k in verify}
    return out


def problems(data: dict[str, Any] | None) -> list[str]:
    """`verify.problems`, then a Linear status other than ok, then a key
    that is not present."""
    if data is None:
        return []
    verify = as_dict(data.get("verify"))
    out = [str(p) for p in as_list(verify.get("problems")) if p]
    linear = data.get("linear")
    if linear is not None and linear != "ok":
        out.append(str(linear))
    key = data.get("key")
    if key is not None and not str(key).startswith("present"):
        out.append(f"key: {key}")
    return out
