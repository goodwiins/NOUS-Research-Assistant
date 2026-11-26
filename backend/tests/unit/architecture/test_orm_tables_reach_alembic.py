"""Every ORM table under ``src/models`` must reach Alembic (GOO-686, GOO-690).

Alembic sees exactly what ``import src.models`` puts on ``Base.metadata``.
``r6h3_model_baseline`` creates those tables with ``create_all`` and the
blocking ``alembic check`` drift gate compares them. A model module that
``src.models`` never imports is invisible to both. Its table is never created
on an alembic-built (managed) database, and CI stays green. That's how the
realtime status tables (GOO-686) and the analytics dashboard tables (GOO-690)
went missing.

The ratchet below imports every module under ``src/models`` in a child
process (a broken module must not poison this process's mapper registry). It
then requires every table they map to be one of:

* registered by ``import src.models``,
* migration-owned (``MIGRATION_OWNED_TABLE_REVISIONS`` in ``alembic/env.py``), or
* listed in ``KNOWN_UNREGISTERED``: pre-existing debt, each entry one of
  the dead or duplicate model families found by GOO-690. The list may only
  shrink. An entry that becomes registered (or is deleted) must be removed
  here, so the list never lies.

Every module under ``src/models`` must import, except the ones in
``KNOWN_IMPORT_FAILURES`` (shrink-only, matched exactly). A module that raises
at import would otherwise drop its tables from the inventory unnoticed.

The retired analytics dashboard family must stay gone.

Scope: this enforces *registration* on ``Base.metadata``. It does not prove a
create migration exists for a registered table: ``r6h3_model_baseline``
creates what is registered on fresh databases, and an already-migrated
database needs its own create-if-missing revision (see
``ws01_websocket_status_tables``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[3]

# GOO-690 inventory (2026-11-22): ORM-only tables whose module src.models never
# imports and that no migration creates. None of them backs a mounted route
# today. Retire them or register them plus add a create-if-missing revision,
# then delete the entry.
KNOWN_UNREGISTERED = frozenset(
    {
        # src/models/ab_testing_analytics.py
        "ab_aggregated_metrics",
        "ab_cohort_analysis",
        "ab_experiment_dashboards",
        "ab_funnel_analysis",
        "ab_statistical_significance",
        "ab_variant_comparisons",
        # src/models/analytics/graph_analytics.py
        "graph_analytics_results",
        "graph_community_metrics",
        "graph_edge_metrics",
        "graph_node_metrics",
        "graph_path_analytics",
        # src/models/analytics/realtime_models.py (unmounted /realtime router)
        "event_streams",
        "live_metrics",
        "realtime_subscriptions",
        # src/models/rag_query.py
        "rag_answer_versions",
        "rag_feedback_events",
        "rag_quality_metrics",
        "rag_queries",
        # Modules that cannot even be imported. Each redefines a table a
        # registered model already owns (InvalidRequestError "already defined
        # for this MetaData instance"), so nothing live can use them, and the
        # tables listed here are the ones they define before failing:
        # enhanced_document.py (document_quality_metrics),
        # evaluation_metrics.py (metric_aggregations),
        # knowledge_graph.py (entity_relationships).
        "enhanced_documents",
        "evaluation_runs",
        "metric_measurements",
        "knowledge_entities",
    }
)

# Model modules that raise at import today (GOO-690 inventory, 2026-11-22).
# Shrink-only: fix or delete a module, then remove it here.
KNOWN_IMPORT_FAILURES = frozenset(
    {
        # Redefine a table a registered model already owns
        # (InvalidRequestError "already defined for this MetaData instance").
        "src.models.ab_testing_models",
        "src.models.enhanced_document",
        "src.models.evaluation_metrics",
        "src.models.knowledge_graph",
        # `from sqlalchemy import Array`: no such name.
        "src.models.qa_system_models",
        # `import bleach`, which no requirements file installs.
        "src.models.validation_rules",
    }
)

RETIRED_ANALYTICS_TABLES = frozenset(
    {
        "analytics_metrics",
        "analytics_kpis",
        "analytics_metric_aggregations",
        "analytics_reports",
        "analytics_dashboards",
        "analytics_dashboard_widgets",
        "analytics_dashboard_layouts",
        "analytics_dashboard_permissions",
    }
)

_INVENTORY = r"""
import importlib, json, pkgutil, sys
sys.path.insert(0, "alembic")
import src.models
from src.models.base import Base
registered = set(Base.metadata.tables)
failed = []
for info in pkgutil.walk_packages(src.models.__path__, "src.models."):
    try:
        importlib.import_module(info.name)
    except Exception as exc:
        failed.append(f"{info.name}: {type(exc).__name__}: {exc}"[:300])
mapped = {}
for mapper in Base.registry.mappers:
    table = getattr(mapper, "local_table", None)
    if table is not None and getattr(table, "name", None):
        mapped.setdefault(table.name, mapper.class_.__module__)
for name in set(Base.metadata.tables) - set(mapped):
    mapped[name] = "<core Table>"
print(json.dumps({"registered": sorted(registered), "mapped": mapped, "failed": failed}))
"""


def _inventory() -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _INVENTORY],
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    data: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return data


def _migration_owned() -> set:
    env_py = (BACKEND_ROOT / "alembic" / "env.py").read_text()
    start = env_py.index("MIGRATION_OWNED_TABLE_REVISIONS = {")
    block = env_py[start : env_py.index("\n}\n", start)]
    owned = {
        line.split('"')[1]
        for line in block.splitlines()
        if line.strip().startswith('"')
    }
    from src.models.document_embedding import MIGRATION_OWNED_TABLES

    return owned | set(MIGRATION_OWNED_TABLES)


@pytest.fixture(scope="module")
def inventory() -> dict:
    return _inventory()


def test_every_model_table_reaches_alembic_or_is_known_debt(inventory: dict) -> None:
    registered = set(inventory["registered"])
    unregistered = set(inventory["mapped"]) - registered - _migration_owned()
    new = sorted(unregistered - KNOWN_UNREGISTERED)
    assert not new, (
        "ORM tables that `import src.models` does not register, so alembic "
        "never creates them on a managed database. Import the module in "
        "src/models/__init__.py and add a create-if-missing revision (see "
        f"ws01_websocket_status_tables): "
        + ", ".join(f"{t} ({inventory['mapped'][t]})" for t in new)
    )


def test_known_unregistered_list_only_shrinks(inventory: dict) -> None:
    still_unregistered = set(inventory["mapped"]) - set(inventory["registered"])
    stale = sorted(KNOWN_UNREGISTERED - still_unregistered)
    assert (
        not stale
    ), f"now registered or deleted; remove from KNOWN_UNREGISTERED: {stale}"


def test_model_modules_import_or_are_known_failures(inventory: dict) -> None:
    failures = {entry.split(":", 1)[0]: entry for entry in inventory["failed"]}
    new = sorted(set(failures) - KNOWN_IMPORT_FAILURES)
    assert not new, (
        "src/models modules that raise at import, so their tables silently "
        "drop out of this ratchet: " + "; ".join(failures[m] for m in new)
    )
    fixed = sorted(KNOWN_IMPORT_FAILURES - set(failures))
    assert not fixed, f"now import cleanly; remove from KNOWN_IMPORT_FAILURES: {fixed}"


def test_retired_analytics_dashboard_family_stays_gone(inventory: dict) -> None:
    assert not RETIRED_ANALYTICS_TABLES & set(inventory["mapped"])
    for path in (
        "src/models/analytics/analytics_models.py",
        "src/models/analytics/dashboard_models.py",
        "src/api/analytics/metrics.py",
        "src/api/analytics/dashboards.py",
        "src/api/analytics/reports.py",
        "src/services/analytics/metrics_service.py",
        "src/services/analytics/dashboard_service.py",
        "src/services/analytics/report_service.py",
    ):
        assert not (BACKEND_ROOT / path).exists(), path
