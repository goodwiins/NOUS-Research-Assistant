# database/

What is left of the standalone database assets. None of these are the live
schema authority: backend schema changes go through Alembic in
`backend/alembic/`, and the Supabase CLI history lives in
[`supabase/`](../supabase/README.md). Check
[`docs/engineering/backend.md`](../docs/engineering/backend.md) and
[`AGENTS.md`](AGENTS.md) before running any of this against a shared database.

The legacy SQL that no runner applied (analytics, realtime-status, retention,
partitioning, RLS/security, views, procedures, triggers, the `init/`
bootstrap and `health_checks.py`) was deleted on 2026-11-21 (GOO-668, under
GOO-579). Recover it from git history if you need to read it; do not re-apply
it as written. The audit found cross-tenant SECURITY DEFINER functions,
viewer roles with write policies and destructive retention/partition code in it.

The other legacy `migrations/*.sql` files were deleted on 2026-11-22 (GOO-681):
nothing referenced them, and none applied to a clean Postgres. The
integration harness `tests/integration/run_tests.py` now builds its schema
with `alembic upgrade head`.

## What remains, and who uses it

| Path | Consumer |
| --- | --- |
| `ci-init/00_auth_schema.sql` | Init-script mount in `config/docker-compose/docker-compose.ci.yml`. |
| `performance_optimization.sql`, `neo4j_performance.cypher`, `redis_optimization.conf` | `scripts/deploy_performance_optimizations.sh` (manual; see GOO-655). |
| `redis_knowledge_graph_config.lua` | `scripts/init-redis-graph.sh` (manual). |
| `migrations/` | No runner. Five files kept only because a doc still tells operators to `psql -f` them: `005` (`docs/database/KNOWLEDGE_GRAPH_STARTUP_GUIDE.md`), `006_document_upload_schema` (`docs/deployment/document_upload_deployment.md`), `007_analytics_schema` (`docs/operations/monitoring/ANALYTICS_IMPLEMENTATION.md`), `007_knowledge_graph_analytics_dashboard` (`docs/api/analytics-implementation-guide.md`), `009_realtime_status_optimizations` (`docs/database/realtime_status_optimization_guide.md`). None applies to a clean database (checked 2026-11-22, GOO-681); they assume tables Alembic creates. |
