"""backup-and-restore.sh restore and Velero behaviour (GOO-676).

Restore used to sync `backups/<id>`, a prefix no upload uses (uploads are
`backups/postgres/postgres_backup_<id>.sql.gz` and so on). So it downloaded
nothing, restored nothing and still exited 0. With the prefix fixed, the
restore paths behind it run for the first time, so these tests pin what they
do:
- preconditions for every component are checked before any is applied;
- PostgreSQL loads only into an empty database, with ON_ERROR_STOP;
- the Redis snapshot is copied into the pod;
- components the script cannot restore are reported as skipped;
- a run that restores nothing fails;
- the plaintext dumps are removed afterwards.

The script runs with a minimal environment and stubs for kubectl, aws, helm
and velero. The stubs record their arguments and stdin. No cluster or bucket
is touched.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "backup-and-restore.sh"
BACKUP_ID = "20261121_120000"
DUMP_SQL = "SELECT 1;\n"

LOGGING_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
exit 0
"""

# kubectl stub:
# - `get pods -l app=redis` prints $STUB_REDIS_POD;
# - the emptiness probe (`psql ... -tAc`) prints $STUB_TABLE_COUNT;
# - any other psql call stores its stdin in $STUB_PSQL_STDIN;
# - `cp` copies its source to $STUB_CP_OUT.
# - `get externalsecret aws-database-credentials` succeeds only when $STUB_RDS=1;
# - `get deployment postgres` fails when $STUB_NO_PG_DEPLOY=1.
# $STUB_FAIL lists steps that exit 1 instead: probe, load, cp.
KUBECTL_STUB = """#!/bin/sh
echo "kubectl $*" >> "$STUB_LOG"
fails() { case " ${STUB_FAIL:-} " in *" $1 "*) return 0 ;; esac; return 1; }
case "$*" in
    *"get externalsecret aws-database-credentials"*) [ "${STUB_RDS:-0}" = 1 ]; exit $? ;;
    *"get deployment postgres"*) [ "${STUB_NO_PG_DEPLOY:-0}" != 1 ]; exit $? ;;
    *"get pods"*"app=redis"*) printf '%s' "${STUB_REDIS_POD:-}"; exit 0 ;;
    *psql*-tAc*) fails probe && exit 1; echo "${STUB_TABLE_COUNT:-0}"; exit 0 ;;
    *psql*) cat > "$STUB_PSQL_STDIN"; fails load && exit 1; exit 0 ;;
    cp\\ *) fails cp && exit 1; cp "$2" "$STUB_CP_OUT"; exit 0 ;;
esac
exit 0
"""

# aws stub: `s3 sync SRC DEST ...` creates the component files named in
# $STUB_FILES (postgres, badgz, redis, neo4j), like matching keys in a bucket.
AWS_STUB = """#!/bin/sh
echo "aws $*" >> "$STUB_LOG"
if [ "$1" = "s3" ] && [ "$2" = "sync" ]; then
    d="$4"
    for f in ${STUB_FILES:-}; do
        case "$f" in
            postgres) mkdir -p "$d/postgres"
                printf 'SELECT 1;\\n' | gzip > "$d/postgres/postgres_backup_${STUB_BACKUP_ID}.sql.gz" ;;
            badgz) mkdir -p "$d/postgres"
                printf 'not gzip' > "$d/postgres/postgres_backup_${STUB_BACKUP_ID}.sql.gz" ;;
            redis) mkdir -p "$d/redis"
                printf 'REDIS0011' | gzip > "$d/redis/redis_backup_${STUB_BACKUP_ID}.rdb.gz" ;;
            badredis) mkdir -p "$d/redis"
                printf 'not gzip' > "$d/redis/redis_backup_${STUB_BACKUP_ID}.rdb.gz" ;;
            neo4j) mkdir -p "$d/neo4j"
                printf 'dump' | gzip > "$d/neo4j/neo4j_backup_${STUB_BACKUP_ID}.dump.gz" ;;
        esac
    done
fi
exit 0
"""


# gunzip stub: fails on the Redis snapshot when $STUB_FAIL has gunzip_redis,
# otherwise defers to the real gunzip.
GUNZIP_STUB = """#!/bin/sh
case " ${STUB_FAIL:-} " in
    *" gunzip_redis "*) case "$*" in *redis_backup_*) exit 1 ;; esac ;;
esac
exec /usr/bin/gunzip "$@"
"""


def _bash() -> str:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    return bash


def _stubs(tmp_path: Path) -> Tuple[Path, Dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (
        ("kubectl", KUBECTL_STUB),
        ("helm", LOGGING_STUB),
        ("velero", LOGGING_STUB),
        ("aws", AWS_STUB),
        ("gunzip", GUNZIP_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "stub.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "STUB_LOG": str(log),
        "STUB_BACKUP_ID": BACKUP_ID,
        "STUB_PSQL_STDIN": str(tmp_path / "psql.stdin"),
        "STUB_CP_OUT": str(tmp_path / "cp.out"),
    }
    return log, env


def _run(
    tmp_path: Path, args: list[str], extra_env: Optional[Dict[str, str]] = None
) -> Tuple[subprocess.CompletedProcess, str]:
    log, env = _stubs(tmp_path)
    env.update(extra_env or {})
    result = subprocess.run(
        [_bash(), str(SCRIPT), *args],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    return result, log.read_text()


def _restore(
    tmp_path: Path, files: str, **extra: str
) -> Tuple[subprocess.CompletedProcess, str, str]:
    result, calls = _run(
        tmp_path, ["restore", BACKUP_ID], {"STUB_FILES": files, **extra}
    )
    return result, calls, result.stdout + result.stderr


def _psql_stdin(tmp_path: Path) -> Optional[str]:
    path = tmp_path / "psql.stdin"
    return path.read_text() if path.exists() else None


def test_restore_fails_when_no_backup_matches(tmp_path: Path) -> None:
    result, calls, output = _restore(tmp_path, "")
    assert result.returncode != 0, output
    assert "kubectl exec" not in calls


def test_restore_syncs_only_the_keys_for_the_id(tmp_path: Path) -> None:
    result, calls, output = _restore(tmp_path, "postgres")
    assert result.returncode == 0, output
    sync = next(line for line in calls.splitlines() if line.startswith("aws s3 sync"))
    assert f"--exclude * --include *_{BACKUP_ID}.*" in sync


def test_restore_pipes_the_dump_into_psql_with_on_error_stop(tmp_path: Path) -> None:
    result, calls, output = _restore(tmp_path, "postgres")
    assert result.returncode == 0, output
    assert _psql_stdin(tmp_path) == DUMP_SQL
    load = [
        line for line in calls.splitlines() if "psql" in line and "-tAc" not in line
    ]
    assert len(load) == 1, calls
    assert "ON_ERROR_STOP=1" in load[0] and "--single-transaction" in load[0]


def test_emptiness_probe_counts_only_base_tables(tmp_path: Path) -> None:
    _result, calls, _output = _restore(tmp_path, "postgres")
    probe = next(line for line in calls.splitlines() if "-tAc" in line)
    # Views and extension-owned tables must not make a fresh DB look populated.
    assert "relkind IN ('r', 'p')" in probe and "deptype = 'e'" in probe


def test_failed_psql_load_fails_the_restore(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres", STUB_FAIL="load")
    assert result.returncode != 0, output
    assert "PostgreSQL restore failed" in output
    assert "Restored 1 component" not in output


def test_failed_emptiness_probe_restores_nothing(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres", STUB_FAIL="probe")
    assert result.returncode != 0, output
    assert "Cannot query the target PostgreSQL database" in output
    # It stops at the probe, not later at the emptiness comparison.
    assert "not empty" not in output
    assert _psql_stdin(tmp_path) is None


def test_corrupt_redis_snapshot_leaves_postgres_untouched(tmp_path: Path) -> None:
    result, _calls, output = _restore(
        tmp_path, "postgres badredis", STUB_REDIS_POD="redis-0"
    )
    assert result.returncode != 0, output
    assert _psql_stdin(tmp_path) is None


def test_later_failure_says_postgres_was_already_restored(tmp_path: Path) -> None:
    result, _calls, output = _restore(
        tmp_path, "postgres redis", STUB_REDIS_POD="redis-0", STUB_FAIL="cp"
    )
    assert result.returncode != 0, output
    assert _psql_stdin(tmp_path) == DUMP_SQL
    assert "PostgreSQL WAS ALREADY RESTORED" in output


def test_redis_gunzip_failure_says_postgres_was_already_restored(
    tmp_path: Path,
) -> None:
    result, _calls, output = _restore(
        tmp_path, "postgres redis", STUB_REDIS_POD="redis-0", STUB_FAIL="gunzip_redis"
    )
    assert result.returncode != 0, output
    assert _psql_stdin(tmp_path) == DUMP_SQL
    assert "PostgreSQL WAS ALREADY RESTORED" in output


def test_restore_refuses_postgres_on_rds(tmp_path: Path) -> None:
    result, calls, output = _restore(tmp_path, "postgres", STUB_RDS="1")
    assert result.returncode != 0, output
    assert "RDS" in output
    assert _psql_stdin(tmp_path) is None
    assert "-tAc" not in calls, "no query may reach any database"


def test_restore_refuses_without_in_cluster_postgres(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres", STUB_NO_PG_DEPLOY="1")
    assert result.returncode != 0, output
    assert "deployment/postgres" in output
    assert _psql_stdin(tmp_path) is None


def test_success_line_mentions_the_infisical_bootstrap_secret(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres")
    assert result.returncode == 0, output
    last = [line for line in output.splitlines() if "Restored 1 component" in line]
    assert last and "infisical-universal-auth" in last[0], output


def test_neo4j_missing_text_does_not_promise_a_restore(tmp_path: Path) -> None:
    _result, _calls, output = _restore(tmp_path, "postgres")
    neo4j = [
        line for line in output.splitlines() if "MISSING" in line and "neo4j" in line
    ]
    assert neo4j, output
    assert "will not be restored" not in neo4j[0]
    assert "never restores" in neo4j[0]


def test_missing_expected_components_are_reported(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres")
    assert result.returncode == 0, output
    missing = [line for line in output.splitlines() if "MISSING" in line]
    assert any("redis" in line for line in missing), output
    assert any("neo4j" in line for line in missing), output
    assert not any("postgres" in line for line in missing), output


def test_restore_refuses_a_populated_database(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres", STUB_TABLE_COUNT="12")
    assert result.returncode != 0, output
    assert "not empty" in output
    assert _psql_stdin(tmp_path) is None


def test_restore_rejects_a_corrupt_dump_before_loading(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "badgz")
    assert result.returncode != 0, output
    assert _psql_stdin(tmp_path) is None


def test_restore_checks_every_component_before_applying_any(tmp_path: Path) -> None:
    # Redis cannot be restored (no pod), so Postgres must not be touched.
    result, _calls, output = _restore(tmp_path, "postgres redis", STUB_REDIS_POD="")
    assert result.returncode != 0, output
    assert _psql_stdin(tmp_path) is None


def test_redis_snapshot_is_copied_into_the_pod(tmp_path: Path) -> None:
    result, calls, output = _restore(
        tmp_path, "postgres redis", STUB_REDIS_POD="redis-0"
    )
    assert result.returncode == 0, output
    cp = next(line for line in calls.splitlines() if line.startswith("kubectl cp "))
    assert re.search(r" \S+/redis-0:/\S+$", cp), cp
    assert (tmp_path / "cp.out").read_bytes().startswith(b"REDIS")


def test_components_the_script_cannot_restore_are_reported(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres neo4j")
    assert result.returncode == 0, output
    assert "SKIPPED" in output and "neo4j" in output


def test_restore_that_restores_nothing_fails(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "neo4j")
    assert result.returncode != 0, output
    assert "kubectl exec" not in _calls


def test_restore_removes_the_plaintext_dumps(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres")
    assert result.returncode == 0, output
    assert not (tmp_path / "backups" / f"restore_{BACKUP_ID}").exists()


def test_restore_rejects_a_malformed_backup_id(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, ["restore", "../../etc"])
    assert result.returncode != 0
    assert "aws s3 sync" not in calls


def test_velero_backup_excludes_secrets(tmp_path: Path) -> None:
    log, env = _stubs(tmp_path)
    result = subprocess.run(
        [_bash(), "-c", 'source "$1" && create_velero_backup', "_", str(SCRIPT)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    create = [line for line in calls if line.startswith("velero create backup")]
    assert len(create) == 1, calls
    assert "--exclude-resources secrets" in create[0]
    # --namespace is Velero's own namespace; the app namespace is what it backs up.
    assert "--namespace velero " in create[0]
    assert "--include-namespaces multimodal-rag-system" in create[0]
    # Velero 1.10 renamed --default-volumes-to-restic.
    assert "--default-volumes-to-fs-backup=false" in create[0]
    assert "restic" not in create[0]


def _source_and_call(tmp_path: Path, function: str, **extra: str) -> list[str]:
    log, env = _stubs(tmp_path)
    env.update(extra)
    result = subprocess.run(
        [_bash(), "-c", f'source "$1" && {function}', "_", str(SCRIPT)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text().splitlines()


def test_velero_namespace_is_configurable(tmp_path: Path) -> None:
    calls = _source_and_call(
        tmp_path, "create_velero_backup", VELERO_NAMESPACE="ops-velero"
    )
    create = next(line for line in calls if line.startswith("velero create backup"))
    assert "--namespace ops-velero " in create


def test_listing_uses_the_velero_namespace(tmp_path: Path) -> None:
    calls = _source_and_call(tmp_path, "list_backups")
    assert "velero get backups --namespace velero" in calls, calls


def test_default_namespace_is_the_live_one(tmp_path: Path) -> None:
    # The RDS check must look in the namespace where the chart runs on AWS.
    result, calls, output = _restore(tmp_path, "postgres", STUB_RDS="1")
    assert result.returncode != 0, output
    assert (
        "get externalsecret aws-database-credentials -n multimodal-rag-system" in calls
    )


def test_backup_refuses_to_dump_rds(tmp_path: Path) -> None:
    log, env = _stubs(tmp_path)
    env["STUB_RDS"] = "1"
    result = subprocess.run(
        [_bash(), "-c", 'source "$1" && backup_postgresql', "_", str(SCRIPT)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "RDS" in output
    assert "pg_dump" not in log.read_text()
