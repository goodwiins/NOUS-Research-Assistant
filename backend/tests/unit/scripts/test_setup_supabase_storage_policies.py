"""Storage RLS policies must not trust user-editable metadata (GOO-632, MS-2).

``alembic/standalone/setup_supabase_storage.py`` used
``raw_user_meta_data->>'organization_id'`` to pick the org folder a user may
read and write. A signed-in user can rewrite their own user metadata, so they
could pick any organization's prefix. The org must come from the server-set
``raw_app_meta_data``, which is what the backend itself trusts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "standalone"
    / "setup_supabase_storage.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("setup_supabase_storage", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_never_authorizes_on_user_metadata_in_sql() -> None:
    module = _load()
    for bucket in module.BUCKETS:
        sql = module._bucket_policies_sql(bucket)
        assert "raw_user_meta_data" not in sql
        assert sql.count("raw_app_meta_data->>'organization_id'") == 2


def test_policies_are_replaced_not_skipped_on_rerun() -> None:
    """A rerun must replace a policy an older script version created."""
    sql = _load()._bucket_policies_sql("documents")
    for action in ("select", "insert"):
        drop = f'DROP POLICY IF EXISTS "storage_{action}_documents" ON storage.objects;'
        create = f'CREATE POLICY "storage_{action}_documents"'
        assert drop in sql and sql.index(drop) < sql.index(create)


def test_manual_sql_matches_applied_sql(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load()
    module._print_rls_sql()
    printed = capsys.readouterr().out
    assert "raw_user_meta_data" not in printed
    for bucket in module.BUCKETS:
        assert module._bucket_policies_sql(bucket) in printed
