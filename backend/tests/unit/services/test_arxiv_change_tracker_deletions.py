"""A paper is "deleted" only when arXiv says it was withdrawn (IS2A-3).

The change scan asks arXiv for the newest N papers per category submitted in
the last ``days_back`` days. A stored paper is missing from that scan as soon
as it is older than the window, or slides below the top N, so missing from
the scan was being counted as deletion: after three runs every paper that
aged out of the window was marked deleted. The truncation guard existed but
the only caller never passed it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.arxiv import arxiv_change_tracker as tracker_module
from src.services.arxiv.arxiv_change_tracker import ArXivChangeTracker, ChangeRecord

pytestmark = pytest.mark.unit

ORG = "org-1"
NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _paper(paper_id: str, published: datetime, **extra: Any) -> Dict[str, Any]:
    return {
        "id": paper_id,
        "title": f"Title {paper_id}",
        "authors": ["Ada"],
        "categories": ["cs.AI"],
        "primary_category": "cs.AI",
        "published": _iso(published),
        **extra,
    }


@pytest.fixture
def tracker(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> ArXivChangeTracker:
    monkeypatch.chdir(tmp_path)
    t = ArXivChangeTracker()
    # Keep the tests off Redis; state lives in memory / the tmp state file.
    monkeypatch.setattr(t, "_redis_get_state", lambda: None)
    monkeypatch.setattr(t, "_redis_set_state", lambda: None)
    return t


def _miss(t: ArXivChangeTracker, window_start: Optional[datetime]) -> List[str]:
    """One scan that returns nothing; the change types it reports."""
    changes = t.detect_changes(
        [], ORG, tracked_categories={"cs.AI"}, window_start=window_start
    )
    return [c.change_type for c in changes]


# --- the date window ------------------------------------------------------


def test_paper_older_than_the_window_is_never_a_deletion_candidate(
    tracker: ArXivChangeTracker,
) -> None:
    tracker.detect_changes([_paper("old", NOW - timedelta(days=10))], ORG)
    window_start = NOW - timedelta(days=7)

    for _ in range(ArXivChangeTracker.DELETION_MISS_THRESHOLD + 2):
        assert _miss(tracker, window_start) == []
    data = tracker._org_state(ORG)["old"]
    assert "deleted" not in data
    assert "miss_count" not in data


def test_paper_inside_the_window_still_becomes_a_candidate(
    tracker: ArXivChangeTracker,
) -> None:
    tracker.detect_changes([_paper("recent", NOW - timedelta(days=1))], ORG)
    window_start = NOW - timedelta(days=7)

    results = [
        _miss(tracker, window_start)
        for _ in range(ArXivChangeTracker.DELETION_MISS_THRESHOLD)
    ]
    assert results[-1] == ["deleted"]


def test_paper_with_no_stored_date_is_not_a_candidate(
    tracker: ArXivChangeTracker,
) -> None:
    paper = _paper("undated", NOW)
    paper.pop("published")
    tracker.detect_changes([paper], ORG)

    for _ in range(ArXivChangeTracker.DELETION_MISS_THRESHOLD + 1):
        assert _miss(tracker, NOW - timedelta(days=7)) == []


def test_resighting_backfills_the_date_for_rows_stored_without_it(
    tracker: ArXivChangeTracker,
) -> None:
    paper = _paper("legacy", NOW - timedelta(days=1))
    tracker.detect_changes([paper], ORG)
    tracker._org_state(ORG)["legacy"]["paper_metadata"].pop("published")

    tracker.detect_changes([paper], ORG)  # same hash: no "updated" record
    stored = tracker._org_state(ORG)["legacy"]["paper_metadata"]
    assert stored["published"] == paper["published"]


# --- the truncation guard is wired in -------------------------------------


class _FakeArxiv:
    def __init__(self, pages: Dict[str, List[Dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeArxiv":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def search_papers(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.calls.append(kwargs)
        return self.pages.get(kwargs["query"], [])


async def _track(
    tracker: ArXivChangeTracker,
    monkeypatch: pytest.MonkeyPatch,
    pages: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    fake = _FakeArxiv(pages)
    monkeypatch.setattr(tracker_module, "ArXivIngestionService", lambda: fake)
    return await tracker.track_category_changes(
        ["cs.AI"], organization_id=ORG, days_back=7, update_db=False
    )


@pytest.mark.asyncio
async def test_a_full_page_counts_as_a_truncated_scan(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker.detect_changes([_paper("gone", NOW - timedelta(days=1))], ORG)
    tracker._org_state(ORG)["gone"]["miss_count"] = (
        ArXivChangeTracker.DELETION_MISS_THRESHOLD - 1
    )
    full_page = [
        _paper(f"p{i}", NOW - timedelta(hours=i))
        for i in range(tracker_module._SCAN_PAGE_SIZE)
    ]
    spy = MagicMock(wraps=tracker.detect_changes)
    monkeypatch.setattr(tracker, "detect_changes", spy)  # type: ignore[method-assign]

    result = await _track(tracker, monkeypatch, {"cat:cs.AI": full_page})

    assert spy.call_args.kwargs["scan_truncated"] is True
    assert result["summary"]["deleted"] == 0


@pytest.mark.asyncio
async def test_a_short_page_is_not_truncated_and_passes_the_window(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = MagicMock(wraps=tracker.detect_changes)
    monkeypatch.setattr(tracker, "detect_changes", spy)  # type: ignore[method-assign]

    await _track(tracker, monkeypatch, {"cat:cs.AI": [_paper("a", NOW)]})

    kwargs = spy.call_args.kwargs
    assert kwargs["scan_truncated"] is False
    window_start = kwargs["window_start"]
    assert abs((NOW - timedelta(days=7)) - window_start) < timedelta(minutes=1)


# --- withdrawal confirmation ----------------------------------------------


def _deleted(paper_id: str) -> ChangeRecord:
    return ChangeRecord(
        paper_id=paper_id,
        change_type="deleted",
        old_hash="h",
        new_hash="",
        change_date=NOW,
        fields_changed=[],
        metadata={},
    )


def _seed_deleted(tracker: ArXivChangeTracker, paper_id: str) -> None:
    tracker.detect_changes([_paper(paper_id, NOW)], ORG)
    data = tracker._org_state(ORG)[paper_id]
    data["miss_count"] = ArXivChangeTracker.DELETION_MISS_THRESHOLD
    data["deleted"] = NOW.isoformat()


@pytest.mark.asyncio
async def test_withdrawn_paper_is_kept_as_deleted(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_deleted(tracker, "w")
    details = _paper("w", NOW, comment="This paper has been withdrawn by the author")
    monkeypatch.setattr(
        tracker, "_fetch_paper_details", AsyncMock(return_value=details)
    )

    kept = await tracker._confirm_deletions([_deleted("w")], ORG, commit=True)

    assert [c.paper_id for c in kept] == ["w"]
    assert "deleted" in tracker._org_state(ORG)["w"]


@pytest.mark.asyncio
async def test_live_paper_is_not_deleted_and_its_misses_reset(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_deleted(tracker, "live")
    monkeypatch.setattr(
        tracker,
        "_fetch_paper_details",
        AsyncMock(return_value=_paper("live", NOW, comment="12 pages, 3 figures")),
    )

    kept = await tracker._confirm_deletions([_deleted("live")], ORG, commit=True)

    assert kept == []
    data = tracker._org_state(ORG)["live"]
    assert "deleted" not in data
    assert "miss_count" not in data
    # The correction is persisted, not just held in memory.
    reloaded = ArXivChangeTracker()
    assert "deleted" not in reloaded._org_state(ORG)["live"]


@pytest.mark.asyncio
async def test_failed_lookup_leaves_the_candidate_for_the_next_scan(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_deleted(tracker, "unknown")
    monkeypatch.setattr(tracker, "_fetch_paper_details", AsyncMock(return_value=None))

    kept = await tracker._confirm_deletions([_deleted("unknown")], ORG, commit=True)

    assert kept == []
    data = tracker._org_state(ORG)["unknown"]
    assert "deleted" not in data
    # Still at the threshold, so the next scan asks arXiv again.
    assert data["miss_count"] == ArXivChangeTracker.DELETION_MISS_THRESHOLD


@pytest.mark.asyncio
async def test_dry_run_confirmation_does_not_touch_state(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_deleted(tracker, "dry")
    monkeypatch.setattr(tracker, "_fetch_paper_details", AsyncMock(return_value=None))

    kept = await tracker._confirm_deletions([_deleted("dry")], ORG, commit=False)

    assert kept == []
    assert "deleted" in tracker._org_state(ORG)["dry"]


@pytest.mark.asyncio
async def test_other_change_types_pass_through_without_a_lookup(
    tracker: ArXivChangeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(tracker, "_fetch_paper_details", fetch)
    new = ChangeRecord(
        paper_id="n",
        change_type="new",
        old_hash=None,
        new_hash="h",
        change_date=NOW,
        fields_changed=["all"],
        metadata={},
    )

    assert await tracker._confirm_deletions([new], ORG, commit=True) == [new]
    fetch.assert_not_awaited()


@pytest.mark.parametrize(
    "comment,abstract,expected",
    [
        ("This paper has been withdrawn by the author.", "", True),
        ("Withdrawn.", "", True),
        (None, "This submission has been withdrawn due to an error.", True),
        ("12 pages; replaces a withdrawn draft", "", False),
        (None, "We study withdrawn bank deposits.", False),
        (None, None, False),
    ],
)
def test_reports_withdrawn(
    comment: Optional[str], abstract: Optional[str], expected: bool
) -> None:
    paper = {"comment": comment, "abstract": abstract}
    assert tracker_module._reports_withdrawn(paper) is expected
