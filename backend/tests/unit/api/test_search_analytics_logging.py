"""A failed search-analytics write is logged with its traceback (GOO-785)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

import src.core.database as database
from src.api.search import search as search_api

pytestmark = pytest.mark.unit


def test_failed_analytics_write_logs_a_warning_with_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = MagicMock(name="session")
    session.commit.side_effect = RuntimeError("password=hunter2 host=db.internal")
    monkeypatch.setattr(database, "SessionLocal", MagicMock(return_value=session))

    with caplog.at_level(logging.WARNING, logger=search_api.logger.name):
        search_api.log_search_query("s-1", "u-1", "o-1", "q", 0, 1.0, "fulltext")

    session.rollback.assert_called_once()
    session.close.assert_called_once()
    [record] = [r for r in caplog.records if r.name == search_api.logger.name]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Error logging search query"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert "hunter2" not in record.getMessage()
