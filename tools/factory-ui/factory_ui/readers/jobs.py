"""Jobs from `jobs/*/job.json`, read the way the helper's cached_jobs and
merge read them: the `cache` object is the last shared state this Mac read
from Linear, the top-level fields are this Mac's own."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from .. import config, paths
from . import as_dict, as_list, loads, reviews

# Never shown: a credential-like token, the session that holds a lease, and
# the Linear document links of the owner's workspace.
HIDDEN_TOP = ("write_token", "docs")
FINISHED_PHASES = ("merged", "closed", "abandoned")
STANDARD_KEYS = ("spec", "build", "decision", "pr-body", "related-tests")
# Cache fields the Jobs view prints as text. One that is an object or an
# array would make the page throw, so that job shows as unreadable instead.
SHOWN_FIELDS = (
    "phase",
    "round",
    "max_rounds",
    "pr",
    "home",
    "kind",
    "hold",
    "linear",
)
_ROUND_DIR = re.compile(r"^round-([1-9])$")


def _load(root: Path, job: str) -> tuple[dict[str, Any] | None, bool]:
    """(job.json as a dict, readable). A missing file reads as {}."""
    read = paths.safe_read(root, paths.jobs_dir(root) / job / "job.json")
    if read is None:
        return {}, True
    try:
        doc = loads(read.text)
    except ValueError:
        return None, False
    if not isinstance(doc, dict):
        return None, False
    return doc, True


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            t = float(value)
        except OverflowError:
            return None
        return t if math.isfinite(t) else None
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _age(now: float, value: Any) -> int | None:
    t = _epoch(value)
    return None if t is None else max(0, int(now - t))


def _title(cache: dict[str, Any]) -> str:
    if cache.get("pr_title"):
        return str(cache["pr_title"])
    return str(cache.get("request") or "")[: config.TITLE_CHARS]


def _row(job: str, doc: dict[str, Any] | None, readable: bool, now: float) -> dict:
    if not readable or doc is None:
        return {"id": job, "group": "unreadable"}
    cache = doc.get("cache")
    if not isinstance(cache, dict) or not cache:
        return {"id": job, "group": "no-cache"}
    lease = as_dict(cache.get("lease"))
    shown = [cache.get(k) for k in SHOWN_FIELDS] + [lease.get("host")]
    if any(isinstance(v, (dict, list)) for v in shown):
        return {"id": job, "group": "unreadable"}
    finished = cache.get("phase") in FINISHED_PHASES and cache.get("finished")
    return {
        "id": job,
        "phase": cache.get("phase"),
        "round": cache.get("round"),
        "max_rounds": cache.get("max_rounds"),
        "pr": cache.get("pr"),
        "home": cache.get("home"),
        "lease": (
            {"host": lease.get("host"), "beat": lease.get("beat")}
            if lease.get("host")
            else None
        ),
        "group": "cached",
        "title": _title(cache),
        "kind": cache.get("kind"),
        "hold": cache.get("hold"),
        "linear": cache.get("linear"),
        "age_s": _age(now, cache.get("finished") if finished else cache.get("created")),
        "cache_age_s": _age(now, doc.get("cache_at")),
        "lease_beat_age_s": _age(now, lease.get("beat")) if lease.get("host") else None,
        "verify_runs": _verify_entries(cache.get("verify_runs")),
        "verify_shadow": _verify_entries(cache.get("verify_shadow")),
    }


def _verify_entries(value: Any) -> dict[str, dict[str, Any]]:
    """Round entries of verify_runs or verify_shadow: verdict, why, mode and
    flags. Other keys (a plan, say) are skipped, as the helper skips them."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for rnd, e in value.items():
        if isinstance(e, dict) and str(rnd).isdigit():
            flags = as_list(e.get("flags"))
            out[str(rnd)] = {
                "verdict": e.get("verdict"),
                "why": e.get("why"),
                "mode": e.get("mode"),
                "flags": [str(f) for f in flags],
            }
    return out


def read_jobs(root: Path, now: float | None = None) -> list[dict[str, Any]]:
    """One row per listed job folder: cached, no-cache or unreadable."""
    now = time.time() if now is None else now
    rows = []
    for job in paths.listed_jobs(root):
        doc, readable = _load(root, job)
        try:
            rows.append(_row(job, doc, readable, now))
        except (ValueError, TypeError, OverflowError, AttributeError):
            # A job.json that parses but holds odd values is this job's
            # problem only: it shows as unreadable, the others still render.
            rows.append(_row(job, None, False, now))
    return rows


def _sanitize(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k not in HIDDEN_TOP and k != "cache"}
    if isinstance(out.get("lease"), dict):
        out["lease"] = {k: v for k, v in out["lease"].items() if k != "session"}
    return out


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _artifacts(folder: Path) -> list[dict[str, Any]]:
    """The artifact keys of a job folder: the standard ones always (present
    or not), the others when a regular file has their name."""
    present = set()
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        if name.endswith(".md") and paths.key_parts(name[:-3]):
            if _regular(folder / name):
                present.add(name[:-3])
        if _ROUND_DIR.match(name) and stat.S_ISDIR(_lmode(folder / name)):
            try:
                subs = os.listdir(folder / name)
            except OSError:
                continue
            for sub in subs:
                key = f"{name}/{sub[:-3]}"
                if sub.endswith(".md") and paths.key_parts(key):
                    if _regular(folder / name / sub):
                        present.add(key)
    keys = list(STANDARD_KEYS) + sorted(present - set(STANDARD_KEYS))
    return [{"key": k, "present": k in present} for k in keys]


def _lmode(path: Path) -> int:
    try:
        return os.lstat(path).st_mode
    except OSError:
        return 0


def _rounds(
    root: Path, folder: Path, cache: dict[str, Any], artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    verdicts = as_dict(cache.get("verdicts"))
    runs = _verify_entries(cache.get("verify_runs"))
    shadow = _verify_entries(cache.get("verify_shadow"))
    files = {a["key"] for a in artifacts if a["present"]}
    numbers = {str(n) for n in verdicts if str(n).isdigit()} | set(runs) | set(shadow)
    numbers |= {k.split("/")[0].removeprefix("round-") for k in files if "/" in k}
    out = []
    for n in sorted(numbers, key=int):
        state = as_dict(verdicts.get(n))
        lenses = set(state) | {
            k.split("/review-")[1] for k in files if k.startswith(f"round-{n}/review-")
        }
        rows = []
        for lens in sorted(lenses):
            entry = as_dict(state.get(lens))
            key = f"round-{n}/review-{lens}"
            fv = _file_verdict(root, folder, key) if key in files else None
            sv = entry.get("verdict")
            rows.append(
                {
                    "lens": lens,
                    "state": sv,
                    "file": fv,
                    "key": key if key in files else None,
                    "mismatch": sv is not None and fv is not None and sv != fv,
                }
            )
        out.append(
            {
                "round": n,
                "reviews": rows,
                "verify": _verify_round(root, folder, n, runs, shadow, files),
            }
        )
    return out


def _file_verdict(root: Path, folder: Path, key: str) -> str | None:
    parts = paths.key_parts(key)
    read = paths.safe_read(root, folder.joinpath(*parts)) if parts else None
    return reviews.file_verdict(read.text) if read else None


def _verify_round(
    root: Path,
    folder: Path,
    n: str,
    runs: dict[str, dict[str, Any]],
    shadow: dict[str, dict[str, Any]],
    files: set[str],
) -> dict[str, Any] | None:
    field, entry = (
        ("verify_runs", runs[n])
        if n in runs
        else ("verify_shadow", shadow[n]) if n in shadow else (None, None)
    )
    file_flags: list[str] = []
    header: dict[str, object] = {}
    key = None
    for cand in (f"round-{n}/review-verify", f"round-{n}/shadow-verify"):
        if cand in files:
            key = cand
            parts = paths.key_parts(cand)
            read = paths.safe_read(root, folder.joinpath(*parts)) if parts else None
            if read:
                header = reviews.verify_header(read.text)
                flags = header["FLAG"]
                file_flags += flags if isinstance(flags, list) else []
            break
    if entry is None and key is None:
        return None
    entry = entry or {}
    # The recorded entry first; the file's header fills what it lacks.
    return {
        "field": field,
        "verdict": entry.get("verdict") or header.get("VERDICT"),
        "why": entry.get("why") or header.get("WHY"),
        "mode": entry.get("mode") or header.get("MODE"),
        "key": key,
        "diff_finding": "diff-finding" in (entry.get("flags") or [])
        or "diff-finding" in file_flags,
    }


def job_detail(root: Path, job: str, now: float | None = None) -> dict[str, Any]:
    """Everything the job page shows except the artifact texts."""
    now = time.time() if now is None else now
    doc, readable = _load(root, job)
    folder = paths.jobs_dir(root) / job
    artifacts = _artifacts(folder)
    cache = as_dict((doc or {}).get("cache"))
    local = {k: v for k, v in (doc or {}).items() if k not in ("cache", "cache_at")}
    return {
        "id": job,
        "row": _row(job, doc, readable, now),
        "summary": {
            "local": _sanitize(local),
            "cache": _sanitize(cache),
            "cache_at": (doc or {}).get("cache_at"),
        },
        "artifacts": artifacts,
        "rounds": _rounds(root, folder, cache, artifacts),
    }


def artifact(root: Path, job: str, key: str) -> dict[str, Any] | None:
    """The text of one artifact with every `## Re-run` body hidden, or None."""
    parts = paths.key_parts(key)
    if parts is None:
        return None
    read = paths.safe_read(root, (paths.jobs_dir(root) / job).joinpath(*parts))
    if read is None:
        return None
    text = reviews.hide_rerun(read.text)
    if read.truncated:
        text += paths.CUT_NOTICE
    return {"key": key, "text": text, "truncated": read.truncated}
