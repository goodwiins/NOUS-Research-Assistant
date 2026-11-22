"""Guards for scripts/backup/disaster_recovery.py (audit finding TS-01, GOO-628).

The script used to wipe the whole Neo4j graph, "restore" it with a logged
no-op, pass an integrity check on 0 nodes and report SUCCESS. It also dropped
the live database before it knew the backup could be restored. Each test pins
one of those failure modes: nothing destructive may run until the backup has
been validated, and a step that did not happen must never be reported as a
success.
"""

from __future__ import annotations

import gzip
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Sequence

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "backup" / "disaster_recovery.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "disaster_recovery_under_test", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dr = _load_module()

TOC_TWO_TABLES = "\n".join(
    [
        ";",
        "; Archive created at 2026-11-21 00:00:00 UTC",
        "215; 1259 16386 TABLE public users postgres",
        "216; 1259 16390 TABLE public documents postgres",
        "217; 1259 16394 INDEX public ix_documents_org postgres",
    ]
)


class FakeShell:
    """Records argv lists and answers each external command."""

    def __init__(self, responses: dict[str, subprocess.CompletedProcess]):
        self.responses = responses
        self.calls: List[List[str]] = []

    async def __call__(
        self, cmd: Sequence[str], env: Any = None
    ) -> subprocess.CompletedProcess:
        argv = list(cmd)
        self.calls.append(argv)
        key = argv[0] if argv[0] != "pg_restore" or "--list" not in argv else "list"
        return self.responses.get(
            key, subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        )

    def ran(self, program: str) -> bool:
        return any(call[0] == program for call in self.calls)


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


def _write_backup(
    tmp_path: Path, name: str = "multimodal_rag_backup_20261121.sql.gz"
) -> Path:
    path = tmp_path / name
    with gzip.open(path, "wb") as handle:
        handle.write(b"PGDMP fake custom-format archive")
    return path


class Recorder:
    def __init__(self) -> None:
        self.events: List[str] = []


def _recovery(tmp_path: Path, recorder: Recorder, restored_tables: int = 2) -> Any:
    recovery = dr.DisasterRecovery({"backup_dir": str(tmp_path), "use_docker": True})

    async def drop() -> None:
        recorder.events.append("drop")

    async def create() -> None:
        recorder.events.append("create")

    async def count() -> int:
        recorder.events.append("count")
        return restored_tables

    recovery._drop_database = drop
    recovery._create_database = create
    recovery._count_restored_tables = count
    return recovery


@pytest.mark.asyncio
async def test_unreadable_backup_aborts_before_anything_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path)
    shell = FakeShell({"list": _fail("not a valid archive")})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recorder = Recorder()

    result = await _recovery(tmp_path, recorder).recover_database()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert recorder.events == []
    assert not shell.ran("docker"), "services must not be stopped for a bad backup"


@pytest.mark.asyncio
async def test_backup_with_no_tables_aborts_before_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path)
    shell = FakeShell({"list": _ok(";\n; empty archive\n")})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recorder = Recorder()

    result = await _recovery(tmp_path, recorder).recover_database()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert recorder.events == []


@pytest.mark.asyncio
async def test_gzipped_archive_is_decompressed_before_pg_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path)
    shell = FakeShell({"list": _ok(TOC_TWO_TABLES)})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recorder = Recorder()

    result = await _recovery(tmp_path, recorder).recover_database()

    assert result["status"] == dr.RecoveryStatus.SUCCESS.value
    restore_targets = [call[-1] for call in shell.calls if call[0] == "pg_restore"]
    assert restore_targets, "pg_restore never ran"
    assert all(not target.endswith(".gz") for target in restore_targets)
    assert recorder.events == ["drop", "create", "count"]


@pytest.mark.asyncio
async def test_service_stop_failure_leaves_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path)
    shell = FakeShell(
        {"list": _ok(TOC_TWO_TABLES), "docker": _fail("no such container")}
    )
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recorder = Recorder()

    result = await _recovery(tmp_path, recorder).recover_database()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert recorder.events == []


@pytest.mark.asyncio
async def test_restore_with_missing_tables_is_reported_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path)
    shell = FakeShell({"list": _ok(TOC_TWO_TABLES)})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recorder = Recorder()

    result = await _recovery(tmp_path, recorder, restored_tables=1).recover_database()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert "1" in result["error"] and "2" in result["error"]


@pytest.mark.asyncio
async def test_drop_failure_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backup(tmp_path)
    shell = FakeShell({"list": _ok(TOC_TWO_TABLES)})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recorder = Recorder()
    recovery = _recovery(tmp_path, recorder)

    async def broken_drop() -> None:
        raise RuntimeError("permission denied")

    recovery._drop_database = broken_drop

    result = await recovery.recover_database()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert "create" not in recorder.events
    assert not any(
        call[0] == "pg_restore" and "--list" not in call for call in shell.calls
    )


@pytest.mark.asyncio
async def test_knowledge_graph_recovery_never_wipes_and_never_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "neo4j_backup_20261121.tar.gz").write_bytes(b"x")
    shell = FakeShell({})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recovery = dr.DisasterRecovery({"backup_dir": str(tmp_path)})

    result = await recovery.recover_knowledge_graph()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert shell.calls == []
    assert not hasattr(recovery, "_clear_neo4j_data")
    assert "GraphDatabase" not in vars(dr)


@pytest.mark.asyncio
async def test_failed_uploads_extract_is_reported_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "uploads_backup_20261121.tar.gz").write_bytes(b"x")
    shell = FakeShell({"tar": _fail("corrupt archive")})
    monkeypatch.setattr(dr, "_run_subprocess", shell)
    recovery = dr.DisasterRecovery(
        {"backup_dir": str(tmp_path), "upload_dir": str(tmp_path / "uploads")}
    )

    result = await recovery.recover_uploads()

    assert result["status"] == dr.RecoveryStatus.FAILED.value
    assert not shell.ran("chown")


@pytest.mark.asyncio
async def test_services_recovery_does_not_claim_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = FakeShell({})
    monkeypatch.setattr(dr, "_run_subprocess", shell)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(dr.asyncio, "sleep", no_sleep)
    result = await dr.DisasterRecovery({}).recover_services()

    assert result["health_checks"]["status"] != "healthy"


@pytest.mark.asyncio
async def test_run_recovery_requires_explicit_components() -> None:
    with pytest.raises(ValueError):
        await dr.DisasterRecovery({}).run_recovery([])


def test_cli_refuses_to_run_without_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["disaster_recovery.py", "--force"])
    with pytest.raises(SystemExit) as excinfo:
        dr.parse_args()
    assert excinfo.value.code != 0


def test_import_does_not_open_a_log_file() -> None:
    assert not any(
        type(handler).__name__ == "FileHandler"
        for handler in dr.logging.getLogger().handlers
    )
