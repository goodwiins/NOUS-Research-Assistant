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
# - the emptiness probe (`psql ... -tAc`) prints $STUB_TABLE_COUNT, or
#   $STUB_COUNT_AFTER_LOAD (when set) once the load has run;
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
    *psql*-tAc*) fails probe && exit 1
        if [ -f "$STUB_PSQL_STDIN" ] && [ -n "${STUB_COUNT_AFTER_LOAD:-}" ]; then
            echo "$STUB_COUNT_AFTER_LOAD"
        else
            echo "${STUB_TABLE_COUNT:-0}"
        fi
        exit 0 ;;
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
            lockedredis) mkdir -p "$d/redis"; chmod 000 "$d/redis" ;;
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

# velero stub: logs its call and exits 1 when $STUB_FAIL has velero.
VELERO_STUB = """#!/bin/sh
echo "velero $*" >> "$STUB_LOG"
case " ${STUB_FAIL:-} " in *" velero "*) exit 1 ;; esac
exit 0
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
        ("velero", VELERO_STUB),
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


def test_unreadable_component_dir_fails_the_restore(tmp_path: Path) -> None:
    # find's own failure must stop the restore, not read as "MISSING".
    try:
        result, _calls, output = _restore(tmp_path, "postgres lockedredis")
    finally:
        for locked in tmp_path.rglob("redis"):
            locked.chmod(0o700)
    assert result.returncode != 0, output
    assert "Could not search" in output
    assert _psql_stdin(tmp_path) is None


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


def test_emptiness_probe_counts_tables_and_other_user_objects(tmp_path: Path) -> None:
    _result, calls, _output = _restore(tmp_path, "postgres")
    probe = next(line for line in calls.splitlines() if "-tAc" in line)
    # Views and extension-owned objects must not make a fresh DB look populated.
    assert "relkind IN ('r', 'p')" in probe and "deptype = 'e'" in probe
    # Sequences, composite/enum/domain types, functions and schemas collide
    # with the dump's CREATE statements too, so they count.
    assert "relkind IN ('S', 'c')" in probe
    # Extension-owned composite types record membership on pg_type.
    assert "d.objid = c.reltype" in probe
    assert "typtype IN ('e', 'd', 'r', 'm')" in probe
    assert "pg_catalog.pg_proc" in probe and "nspname <> 'public'" in probe


def test_failed_psql_load_fails_the_restore(tmp_path: Path) -> None:
    result, _calls, output = _restore(tmp_path, "postgres", STUB_FAIL="load")
    assert result.returncode != 0, output
    assert "PostgreSQL restore failed" in output
    assert "Restored 1 component" not in output


def test_failed_load_that_left_objects_says_partially_loaded(tmp_path: Path) -> None:
    result, _calls, output = _restore(
        tmp_path, "postgres", STUB_FAIL="load", STUB_COUNT_AFTER_LOAD="7"
    )
    assert result.returncode != 0, output
    assert "may be partially loaded" in output
    assert "rolled back" not in output


def test_failed_load_with_zero_count_still_says_to_inspect(tmp_path: Path) -> None:
    # The count skips views and extension objects, so 0 proves nothing.
    result, _calls, output = _restore(tmp_path, "postgres", STUB_FAIL="load")
    assert result.returncode != 0, output
    assert "shows no counted objects" in output
    assert "inspect it before retrying" in output
    assert "nothing was committed" not in output


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


def test_velero_failure_is_reported(tmp_path: Path) -> None:
    # Under set -e a bare failing command would exit before any message.
    log, env = _stubs(tmp_path)
    env["STUB_FAIL"] = "velero"
    result = subprocess.run(
        [_bash(), "-c", 'source "$1" && create_velero_backup', "_", str(SCRIPT)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Velero backup failed" in output


def test_help_states_the_velero_version(tmp_path: Path) -> None:
    log, env = _stubs(tmp_path)
    result = subprocess.run(
        [_bash(), str(SCRIPT), "help"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert "Velero >= 1.10" in result.stdout


# kubectl stub for the RUNBOOKS recovery block. It logs every call.
# - `get deployment postgres` fails when $RB_NO_PG=1;
# - `get crd externalsecrets...` prints the CRD unless $RB_NO_ESO=1;
# - `get externalsecret aws-database-credentials` prints its name when
#   $RB_RDS=1, prints nothing otherwise;
# - `get crd scaledobjects.keda.sh` prints the CRD when $RB_KEDA=1, prints
#   nothing otherwise (matching `--ignore-not-found`), and fails when $RB_FAIL
#   contains `keda`;
# - $RB_FAIL lists calls that exit 1 instead: deploy, crd, es, keda, drop.
RUNBOOK_KUBECTL_STUB = """#!/bin/sh
echo "kubectl $*" >> "$STUB_LOG"
fails() { case " ${RB_FAIL:-} " in *" $1 "*) return 0 ;; esac; return 1; }
case "$*" in
    *"get deployment postgres"*)
        fails deploy && exit 1; [ "${RB_NO_PG:-0}" != 1 ]; exit $? ;;
    *"get crd externalsecrets.external-secrets.io"*)
        fails crd && exit 1
        [ "${RB_NO_ESO:-0}" = 1 ] || echo crd/externalsecrets.external-secrets.io
        exit 0 ;;
    *"get externalsecret aws-database-credentials"*)
        fails es && exit 1
        [ "${RB_RDS:-0}" = 1 ] && echo externalsecret/aws-database-credentials
        exit 0 ;;
    *"get crd scaledobjects.keda.sh"*)
        fails keda && exit 1
        [ "${RB_KEDA:-0}" = 1 ] && echo crd/scaledobjects.keda.sh
        exit 0 ;;
    *"DROP DATABASE"*) fails drop && exit 1; exit 0 ;;
esac
exit 0
"""

DESTRUCTIVE = (
    "kubectl scale",
    "kubectl delete job",
    "kubectl annotate",
    "DROP DATABASE",
)


def _runbook_block() -> str:
    text = (REPO_ROOT / "docs" / "deployment" / "RUNBOOKS.md").read_text()
    start = text.index("\n(\n  set -eu\n") + 1
    end = text.index("\n)\n", start) + 3
    return text[start:end]


def _run_runbook(
    tmp_path: Path,
    backup_id: str = BACKUP_ID,
    namespace: str = "multimodal-rag-system",
    **extra: str,
) -> Tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(RUNBOOK_KUBECTL_STUB)
    kubectl.chmod(0o755)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    restore = repo / "scripts" / "backup-and-restore.sh"
    restore.write_text(
        '#!/bin/sh\necho "backup-and-restore.sh NAMESPACE=$NAMESPACE $*" >> "$STUB_LOG"\n'
    )
    restore.chmod(0o755)
    log = tmp_path / "stub.log"
    log.touch()
    block = _runbook_block().replace("  BACKUP_ID=  ", f"  BACKUP_ID={backup_id}  ", 1)
    if namespace != "multimodal-rag-system":
        assert block.count("  NS=multimodal-rag-system ") == 1
        block = block.replace("  NS=multimodal-rag-system ", f"  NS={namespace} ", 1)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    env["STUB_LOG"] = str(log)
    env.update(extra)
    result = subprocess.run(
        [_bash(), "-c", block],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=repo,
        timeout=60,
    )
    return result, log.read_text()


def test_runbook_is_one_compound_command() -> None:
    # A paste that is cut short or lands in another shell must run nothing,
    # so every step sits inside one subshell and nothing follows it.
    text = (REPO_ROOT / "docs" / "deployment" / "RUNBOOKS.md").read_text()
    block = _runbook_block()
    fence = text.rindex("```bash", 0, text.index(block))
    before = text[fence : text.index(block)]
    after = text[text.index(block) + len(block) :]
    for destructive in DESTRUCTIVE + ("backup-and-restore.sh restore",):
        assert destructive in block, destructive
        assert destructive not in before, destructive
        assert destructive not in after[: after.index("```")], destructive
    # `exit` inside the subshell never closes the operator's terminal.
    assert "exit 1" not in before


def test_runbook_runs_every_step_for_in_cluster_postgres(tmp_path: Path) -> None:
    result, calls = _run_runbook(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "kubectl scale deployment -n multimodal-rag-system --replicas=0" in calls
    assert "DROP DATABASE IF EXISTS ragdb WITH (FORCE)" in calls
    assert (
        f"backup-and-restore.sh NAMESPACE=multimodal-rag-system restore {BACKUP_ID}"
        in calls
    )


@pytest.mark.parametrize(
    ("case", "extra"),
    [
        ("rds", {"RB_RDS": "1"}),
        ("no postgres deployment", {"RB_NO_PG": "1"}),
        ("kubectl fails on the deployment", {"RB_FAIL": "deploy"}),
        ("kubectl fails on the CRD", {"RB_FAIL": "crd"}),
        ("kubectl fails on the ExternalSecret", {"RB_FAIL": "es"}),
        ("kubectl fails on the KEDA CRD", {"RB_FAIL": "keda"}),
    ],
)
def test_runbook_stops_before_touching_workloads(
    tmp_path: Path, case: str, extra: Dict[str, str]
) -> None:
    result, calls = _run_runbook(tmp_path, **extra)
    assert result.returncode != 0, case
    assert "STOPPED, nothing changed" in result.stderr, case
    assert "writers are still scaled down" not in result.stderr, case
    for destructive in DESTRUCTIVE + ("backup-and-restore.sh",):
        assert destructive not in calls, (case, destructive)


def test_runbook_pauses_and_resumes_keda_when_installed(tmp_path: Path) -> None:
    result, calls = _run_runbook(tmp_path, RB_KEDA="1")
    assert result.returncode == 0, result.stderr
    assert "autoscaling.keda.sh/paused-replicas=0 --overwrite" in calls
    assert "autoscaling.keda.sh/paused-replicas-" in calls
    assert "writers are still scaled down" not in result.stderr


def test_runbook_comments_have_no_apostrophes() -> None:
    # In default zsh (without interactivecomments), an unmatched apostrophe in a
    # comment line inside the pasted subshell leaves the prompt stuck at quote>.
    block = _runbook_block()
    for line in block.splitlines():
        if "#" not in line:
            continue
        comment = line.split("#", 1)[1]
        assert "'" not in comment, line


def test_runbook_refuses_without_a_backup_id(tmp_path: Path) -> None:
    result, calls = _run_runbook(tmp_path, backup_id="")
    assert result.returncode != 0
    assert "set BACKUP_ID" in result.stderr
    assert calls == ""


def test_runbook_refuses_an_empty_namespace(tmp_path: Path) -> None:
    result, calls = _run_runbook(tmp_path, namespace="")
    assert result.returncode != 0
    assert "NS is empty" in result.stderr
    assert calls == ""


def test_cluster_recovery_delegates_the_data_restore() -> None:
    # Steps 1-5 (Velero, Infisical, Helm) are valid on AWS. The data restore
    # is the RUNBOOKS block, which stops on RDS; it is not duplicated here.
    text = REPO_ROOT / "docs" / "deployment" / "PRODUCTION_DEPLOYMENT_GUIDE.md"
    guide = text.read_text()
    start = guide.index("#### Scenario 3: Full Cluster Recovery")
    block = guide[start : guide.index("**Secrets and Helm history.**", start)]
    assert "PostgreSQL Database Recovery" in block
    for destructive in DESTRUCTIVE + ("backup-and-restore.sh restore",):
        assert destructive not in block, destructive


def test_runbook_stops_when_a_step_fails(tmp_path: Path) -> None:
    # set -e: a failed drop must not go on to load the dump, and the operator
    # is reminded that writers remain scaled down.
    result, calls = _run_runbook(tmp_path, RB_FAIL="drop")
    assert result.returncode != 0
    assert "DROP DATABASE" in calls
    assert "backup-and-restore.sh" not in calls
    assert "writers are still scaled down" in result.stderr
