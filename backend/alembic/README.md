# Alembic migrations

`backend/alembic/` is the canonical schema migration system for the application database. Configuration is `backend/alembic.ini` (`script_location = alembic`) and `env.py`; revision scripts live in `versions/`; `script.py.mako` is the revision template. `standalone/` holds one-off data and storage helper scripts that are not part of the revision chain.

## Rules the CI enforces

`python scripts/ci/check_alembic.py` (run it as `(cd backend && python ../scripts/ci/check_alembic.py)`) runs offline, with no database, and fails on either of two things:

1. **More than one head.** Divergent branches cannot be applied by `alembic upgrade head`. This has recurred and left schemas partly migrated. Fix it with `alembic merge -m "merge heads" <head1> <head2>`, so the chain has exactly one head.
2. **A revision id longer than 32 characters.** `alembic_version.version_num` is `VARCHAR(32)`, and a 33-character id once crash-looped a deploy. Shorten the `revision` id and any `down_revision` that points at it.

## Everyday commands

```bash
cd backend
alembic revision --rev-id xx01_short_slug -m "describe_the_change"
alembic upgrade head
alembic downgrade -1
```

Revisions are written by hand. `alembic revision` without `--autogenerate` renders an empty script from `script.py.mako`; write `upgrade()` and `downgrade()` yourself, and pick a short `--rev-id` in the `xx01_slug` style of the existing chain so it stays within 32 characters. Do not use `--autogenerate`: only one revision in `versions/` carries Alembic's autogenerate marker, and that one (`f931599b6b5b`) emitted drops that would destroy model-owned schema and had to be disabled by hand. CI's blocking `alembic check` step (`.github/workflows/test-pipeline.yml`) compares the models with the migrated schema, so a hand-written revision that misses a model change still fails.

Before opening a PR, check which revision your branch builds on: rebase on a fresh `origin/develop` and re-run the checker, because another branch landing a revision after you branched creates a second head.

## Conventions and gotchas

- A new model file must be imported from `src/models/__init__.py` so the `alembic check` drift gate sees it.
- Write the model's `ondelete` rule into the migration by hand for any foreign key into `threads`, `chat_messages` or a table a thread delete cascades into; nothing checks a migration's rule (see `docs/engineering/gotchas.md`).
- `document_embeddings` is migration-owned and deliberately not on `Base.metadata`; only the `jd01_document_embeddings` revision creates it, and it skips itself when the `vector` extension is unavailable.
- Supabase has its own migration directory (`supabase/migrations/`); do not mix the two chains.
- Schema changes that change HTTP responses also need `backend/openapi.json` regenerated (`python scripts/ci/generate_openapi.py`).
