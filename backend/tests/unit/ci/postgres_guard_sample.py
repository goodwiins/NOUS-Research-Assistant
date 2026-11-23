"""Sample run by ``test_shard_tests.py`` to exercise the GOO-702 skip guard.

Not collected on its own (it does not match ``test_*.py``). One PostgreSQL
test that skips at runtime (the database is missing), one PostgreSQL test a
skip marker skips (the database is not the cause, GOO-703), and one ordinary
skip the guard must leave alone.
"""

import pytest


@pytest.mark.requires_postgres
def test_postgres_sample_skips() -> None:
    pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")


@pytest.mark.requires_postgres
@pytest.mark.skipif(True, reason="a platform this test does not support")
def test_postgres_sample_skipped_by_a_marker() -> None:
    raise AssertionError("never runs")


def test_ordinary_sample_skips() -> None:
    pytest.skip("not a database test")
