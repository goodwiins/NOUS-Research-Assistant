"""Sample run by ``test_shard_tests.py`` to exercise the GOO-702 skip guard.

Not collected on its own (it does not match ``test_*.py``). One PostgreSQL
test that skips, and one ordinary skip the guard must leave alone.
"""

import pytest


@pytest.mark.requires_postgres
def test_postgres_sample_skips() -> None:
    pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")


def test_ordinary_sample_skips() -> None:
    pytest.skip("not a database test")
