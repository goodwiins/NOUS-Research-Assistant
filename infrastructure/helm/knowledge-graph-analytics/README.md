# knowledge-graph-analytics Helm chart

Backend (FastAPI) + frontend (Next.js) + celery + Neo4j for the NOUS Multimodal Intelligence Platform.

AWS is the active GitOps deployment. The API, worker, scheduler and synthetic
traffic share one immutable backend image; the frontend deploys through Vercel.

| File | Status | Cluster / namespace |
| --- | --- | --- |
| `values-aws.yaml` | Active, `nous-dev-aws` Argo application | EKS `nous-dev-cluster` / `multimodal-rag-system` |
| `values-dev.yaml` | Retired DOKS rollback configuration | `do-nyc3-rag-system-cluster` / `rag-dev` |
| `values-staging.yaml` | Retired render fixture | `rag-staging` |
| `values-production.yaml` | Retired render fixture | `rag-production` |

Always render `values.yaml` plus the selected overlay. The AWS source and
namespace are declared in [the Argo application](../../argocd/applications/aws-dev.yaml).

## Externally managed secrets

AWS application SQL uses RDS through `aws-database-credentials`; Redis uses
ElastiCache. `app-secrets` and `supabase-credentials` supply application and
auth settings. Shared `envFrom` references and `secretKeyRef` entries are the
contract; values files contain no plaintext credentials. AWS sets
`SUPABASE_DB_URL` blank so an auxiliary Supabase connection cannot supersede
RDS. Inspect the AWS overlay and [Infisical templates](templates/infisical-secrets.yaml)
for the exact references. Update secrets through their declared provider;
do not decode secret values into logs or patch generated Secrets as a routine
verification step. Secret changes and workload restarts are operational
mutations requiring explicit authorization.

`PLATFORM_OPERATOR_USER_IDS` (comma-separated user UUIDs that may see
host-wide metrics) is read from the optional `PLATFORM_OPERATOR_USER_IDS` key
of `app-secrets` for the backend, worker and beat. It is unset, which means
nobody, until the owner adds that key in Infisical for the environment. Never
put the ids in a values file: the repo is public, and an `env` literal (even
`""`) would override the `envFrom` secret. `tests/deployment_consistency_render_test.py`
asserts this wiring.

Adding or changing the key does not reach running pods by itself. Env vars are
read once, at container start. Reloader only watches the
`aws-database-credentials` secret, not `app-secrets`. Once the Infisical sync has
updated `app-secrets`, an operator must restart the backend, celery-worker and
celery-beat Deployments by hand, with `kubectl -n multimodal-rag-system rollout restart deployment/<name>`
for each one. That is an authorized operational mutation, like any restart. The
next release rollout also picks the key up. Until the restart, every pod keeps the
old value.

Only the `values-aws.yaml` entry reaches the live dev pods: Helm replaces lists
instead of merging them, so `values-aws.yaml`'s `backend.env` replaces the one in
`values.yaml`. The `values.yaml` copy covers renders that use the base file alone.

## Database migrations

AWS enables `backend.migrations.enabled` and disables
`backend.initContainers.runMigrations`. The chart rejects enabling both.
One ordinary, digest-pinned Job runs `alembic upgrade heads` with the backend's
shared secret references and environment. Argo applies shared resources in
wave 0, waits for the migration Job in wave 1 to complete, then updates the
API, worker, beat and synthetic CronJob in wave 2. A failure or the ten-minute
deadline blocks new consumers; old pods may continue serving during migration.
Migrations must remain compatible with those old pods during rollout.

The Job name hashes its image and pod configuration, so a new image/configuration
creates a new Job rather than modifying an immutable completed pod template.
Completed Jobs have no TTL: deleting them automatically would make Argo
self-heal rerun migrations. The next successful rollout prunes the previous
Job. The Job has no automatic retries; diagnose a failed migration before an
explicitly authorized retry. Provider-side secret content changes alone do not
change Job identity; a new image/configuration or an authorized retry is needed.

Use a full application Argo sync. Resource-selective sync and standalone Helm
upgrade do not enforce this wave barrier and must not be used to promote AWS
backend consumers. Render validation cannot prove live migration success.
The legacy dev/staging overlays retain their API init path; the retired
production overlay still opts out of automatic migration. Their rendering
remains covered without declaring them active.

Read-only rollout and environment checks (Helm 3.13.0, Python 3 + PyYAML):

```sh
python3 infrastructure/helm/knowledge-graph-analytics/tests/deployment_consistency_render_test.py
```

## Container hardening and Celery probes

Every backend-image container (API `backend`, `celery-worker`, `celery-beat`,
the migration Job's `run-migrations` and `synthetic-traffic`) renders its
container `securityContext` from the `knowledge-graph-analytics.containerSecurityContext`
helper: `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]`. The
pod-level `securityContext` (non-root UID/GID 1001) still comes from
`.Values.securityContext`. The helper adds no filesystem restriction; these
processes write only under `/app`, which the image's UID 1001 owns.

The worker's liveness and readiness probes both run
`celery -A src.tasks.celery_app inspect ping -d celery@$HOSTNAME`. The ping asks
only the pod's own node and imports only `celery_app`, not the task graph. A
stuck consumer stops answering. Readiness (`celeryWorker.readinessProbe.*`,
default 30 s ping timeout, every 60 s) gates rollout availability and the worker
PDB; liveness (`celeryWorker.livenessProbe.*`) restarts the container.

Beat runs with `--schedule=/app/celerybeat-schedule`, the same path as Celery's
implicit default. Its liveness probe fails when no `celerybeat-schedule*` file in
`/app` was modified in the last 10 minutes. The PersistentScheduler rewrites the
file about every 180 s while it ticks, so a stalled scheduler loop gets
restarted. Beat is PID 1, so a process check (`pgrep`) could never fail.
