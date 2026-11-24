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
shared secret references (`envFrom`) and only the DB entries of `backend.env`
(`DATABASE_URL`, `SUPABASE_DB_URL`). Argo applies shared resources in
wave 0, waits for the migration Job in wave 1 to complete, then updates the
API, worker, beat and synthetic CronJob in wave 2. A failure or the ten-minute
deadline blocks new consumers; old pods may continue serving during migration.
Migrations must remain compatible with those old pods during rollout.

The Job name hashes the image and the DB settings (plus static pod settings
and secret reference names), not feature flags: the pod omits every non-DB
`backend.env` entry, so flipping a flag neither reruns Alembic nor changes the
Job. A new image or DB setting creates a new Job rather than modifying an
immutable completed pod template. Completed Jobs have no TTL: deleting them
automatically would make Argo self-heal rerun migrations. The next successful
rollout prunes the previous Job. The Job retries up to 2 times inside the
ten-minute deadline, which rides out a transient connection failure; diagnose
a Job that still fails before an explicitly authorized retry. Provider-side
secret content changes alone do not change Job identity; a new image/DB setting
or an authorized retry is needed.

There is no in-cluster alert for a failed Job. After the 20 min grace period,
the scheduled `verify-dev-health.yml` build check flags a stalled rollout only
if the serving SHA differs from the selected `sourceSha`. It names the
migration Job as a possible cause. A `SUPABASE_DB_URL` change can create a new
migration Job with the same `sourceSha`. If that Job fails while the old pods
still serve the matching SHA, the build check passes.

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
