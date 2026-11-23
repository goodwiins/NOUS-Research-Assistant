#!/usr/bin/env python3
"""
Disaster Recovery Script for Multimodal RAG System.

Manual, destructive operator tool for the docker-compose / systemd layout. It
restores the PostgreSQL database and the uploads directory from local backups
written by ``scripts/backup/backup_database.sh``.

Safety contract:

* ``--components`` is required; there is no "recover everything" default.
* Nothing destructive runs until the backup has been validated
  (``pg_restore --list`` must read it and list at least one public table).
* A step that did not happen is reported as FAILED, never as SUCCESS. The
  database is only reported restored when it has at least as many public
  tables as the backup's table of contents.
* Neo4j restore is not implemented. ``knowledge_graph`` recovery always fails
  and leaves the graph untouched; restore it by hand with ``neo4j-admin``.
"""

import argparse
import asyncio
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import asyncpg

logger = logging.getLogger(__name__)

COMPONENTS = ("database", "knowledge_graph", "uploads", "services")

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# One `pg_restore --list` entry for a table in the public schema, e.g.
# "215; 1259 16386 TABLE public users postgres".
_TOC_PUBLIC_TABLE = re.compile(r"^\d+;\s+\d+\s+\d+\s+TABLE\s+public\s+\S+", re.M)


class RecoveryAbort(Exception):
    """Stop recovery. The message says what state was left behind."""


def configure_logging() -> None:
    """Log to stderr, plus $DR_LOG_FILE when set. Never configured at import."""
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    log_file = os.getenv("DR_LOG_FILE")
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )


def safe_identifier(name: str) -> str:
    """Validate a SQL identifier before interpolation into DDL (asyncpg cannot parameterize DDL)."""
    if not _VALID_IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


async def _run_subprocess(
    cmd: Sequence[str], env: Optional[dict[str, str]] = None
) -> subprocess.CompletedProcess:
    """Run a subprocess without blocking the asyncio event loop."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    return subprocess.CompletedProcess(
        args=list(cmd),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode(errors="replace") if stdout else "",
        stderr=stderr.decode(errors="replace") if stderr else "",
    )


def _decompress(source: Path, dest_dir: Path) -> Path:
    """Return a pg_restore-readable archive path; gunzip ``*.gz`` into dest_dir."""
    if source.suffix != ".gz":
        return source
    target = dest_dir / source.with_suffix("").name
    with gzip.open(source, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return target


class RecoveryStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


def _failed(error: str) -> Dict[str, Any]:
    return {"status": RecoveryStatus.FAILED.value, "error": error}


class DisasterRecovery:
    """Disaster recovery coordinator for the multimodal RAG system."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.recovery_log: List[Dict[str, Any]] = []
        self.start_time = datetime.now()

    async def run_recovery(self, components: Sequence[str]) -> Dict[str, Any]:
        """Run disaster recovery for the explicitly named components, in order."""
        if not components:
            raise ValueError("components must be named explicitly")
        unknown = [c for c in components if c not in COMPONENTS]
        if unknown:
            raise ValueError(f"unknown components: {unknown}")

        logger.info("Starting disaster recovery for: %s", ", ".join(components))

        handlers = {
            "database": self.recover_database,
            "knowledge_graph": self.recover_knowledge_graph,
            "uploads": self.recover_uploads,
            "services": self.recover_services,
        }
        recovery_results: Dict[str, Any] = {}
        for component in COMPONENTS:  # database first, services last
            if component not in components:
                continue
            logger.info("Starting recovery for %s", component)
            try:
                result = await handlers[component]()
            except Exception as e:
                logger.error("Recovery failed for %s: %s", component, e)
                result = _failed(str(e))
            recovery_results[component] = result
            self._log_recovery_step(component, result)

        report = self._generate_recovery_report(recovery_results)
        self._report_notification_status()
        return report

    async def recover_database(self) -> Dict[str, Any]:
        """Recover PostgreSQL from the latest backup, validating it first."""
        logger.info("Starting database recovery...")

        backup_file = await self._find_latest_backup("postgresql")
        if not backup_file:
            return _failed("No database backup found; database untouched")
        logger.info("Using backup: %s", backup_file)

        try:
            with tempfile.TemporaryDirectory(prefix="dr_restore_") as tmp:
                archive = await asyncio.to_thread(_decompress, backup_file, Path(tmp))
                expected_tables = await self._validate_pg_archive(archive)

                stopped = await self._stop_services(
                    ["backend", "celery-worker", "celery-beat"]
                )
                if not stopped:
                    raise RecoveryAbort(
                        "Could not stop application services; database untouched"
                    )

                # From here on the live database is gone; failures are reported
                # as such so the operator knows it needs a manual restore.
                await self._drop_database()
                await self._create_database()
                if not await self._restore_database(archive):
                    raise RecoveryAbort(
                        "pg_restore failed after the database was dropped; "
                        "restore manually from " + str(backup_file)
                    )

            restored_tables = await self._count_restored_tables()
        except RecoveryAbort as e:
            logger.error("Database recovery aborted: %s", e)
            return _failed(str(e))
        except Exception as e:
            logger.error("Database recovery failed: %s", e)
            return _failed(str(e))

        if restored_tables < expected_tables:
            error = (
                f"Restored database has {restored_tables} public tables, "
                f"backup lists {expected_tables}"
            )
            logger.error(error)
            return _failed(error)

        logger.info("Database recovery completed: %d tables", restored_tables)
        return {
            "status": RecoveryStatus.SUCCESS.value,
            "backup_file": str(backup_file),
            "tables": restored_tables,
            "restored_at": datetime.now().isoformat(),
        }

    async def recover_knowledge_graph(self) -> Dict[str, Any]:
        """Neo4j restore is not implemented; never touch the live graph."""
        error = (
            "Neo4j restore is not implemented by this script; the graph was "
            "left untouched. Restore it manually with neo4j-admin."
        )
        logger.error(error)
        return _failed(error)

    async def recover_uploads(self) -> Dict[str, Any]:
        """Recover uploaded files from backup."""
        logger.info("Starting uploads recovery...")

        backup_file = await self._find_latest_backup("uploads")
        if not backup_file:
            logger.warning("No uploads backup found, skipping files recovery")
            return {
                "status": RecoveryStatus.SKIPPED.value,
                "reason": "No backup found",
            }
        logger.info("Using backup: %s", backup_file)

        listing = await _run_subprocess(["tar", "tzf", str(backup_file)])
        if listing.returncode != 0:
            return _failed(f"Uploads backup is unreadable: {listing.stderr.strip()}")

        upload_dir = Path(self.config.get("upload_dir", "/app/uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        result = await _run_subprocess(
            ["tar", "xzf", str(backup_file), "-C", str(upload_dir.parent)]
        )
        if result.returncode != 0:
            return _failed(f"Uploads extract failed: {result.stderr.strip()}")

        permissions_ok = await self._fix_upload_permissions(upload_dir)
        logger.info("Restored uploads from %s", backup_file)
        return {
            "status": (
                RecoveryStatus.SUCCESS if permissions_ok else RecoveryStatus.PARTIAL
            ).value,
            "backup_file": str(backup_file),
            "restored_at": datetime.now().isoformat(),
        }

    async def recover_services(self) -> Dict[str, Any]:
        """Start application services. Health is not checked by this script."""
        logger.info("Starting services recovery...")

        services_to_start = [
            "postgres",
            "redis",
            "neo4j",
            "backend",
            "celery-worker",
            "celery-beat",
            "frontend",
        ]
        started_services = []
        failed_services = []
        for service in services_to_start:
            if await self._start_service(service):
                started_services.append(service)
                logger.info("Started service: %s", service)
            else:
                failed_services.append(service)
                logger.error("Failed to start service: %s", service)

        await asyncio.sleep(30)

        if not started_services:
            status = RecoveryStatus.FAILED
        elif failed_services:
            status = RecoveryStatus.PARTIAL
        else:
            status = RecoveryStatus.SUCCESS

        return {
            "status": status.value,
            "started_services": started_services,
            "failed_services": failed_services,
            "health_checks": {
                "status": "not_run",
                "note": "Health checks are not implemented; verify services manually.",
            },
            "recovered_at": datetime.now().isoformat(),
        }

    async def _find_latest_backup(self, component: str) -> Optional[Path]:
        """Find the latest backup for a component."""
        patterns = {
            "postgresql": "multimodal_rag_backup_*.sql.gz",
            "neo4j": "neo4j_backup_*.tar.gz",
            "uploads": "uploads_backup_*.tar.gz",
        }
        pattern = patterns.get(component)
        if pattern is None:
            return None

        backup_dirs = [
            Path(self.config.get("backup_dir", "/backups")),
            Path("/backups"),
            Path("./backups"),
        ]
        for backup_dir in backup_dirs:
            if not backup_dir.exists():
                continue
            backups = list(backup_dir.glob(pattern))
            if backups:
                latest = max(backups, key=lambda x: x.stat().st_mtime)
                logger.info("Found latest backup for %s: %s", component, latest)
                return latest
        return None

    async def _validate_pg_archive(self, archive: Path) -> int:
        """Return the number of public tables in the archive, or abort."""
        result = await _run_subprocess(["pg_restore", "--list", str(archive)])
        if result.returncode != 0:
            raise RecoveryAbort(
                f"Backup {archive.name} is not a readable pg_restore archive "
                f"({result.stderr.strip()}); database untouched"
            )
        tables = len(_TOC_PUBLIC_TABLE.findall(result.stdout))
        if tables == 0:
            raise RecoveryAbort(
                f"Backup {archive.name} lists no public tables; database untouched"
            )
        logger.info("Backup validated: %d public tables", tables)
        return tables

    async def _stop_services(self, services: List[str]) -> bool:
        """Stop the services; True only if every stop succeeded."""
        results = [await self._stop_service(service) for service in services]
        return all(results)

    def _service_cmd(self, action: str, service: str) -> List[str]:
        if self.config.get("use_docker", True):
            return ["docker", action, service]
        return ["systemctl", action, service]

    async def _stop_service(self, service: str) -> bool:
        result = await _run_subprocess(self._service_cmd("stop", service))
        if result.returncode != 0:
            logger.error("Failed to stop %s: %s", service, result.stderr.strip())
            return False
        logger.info("Stopped service: %s", service)
        return True

    async def _start_service(self, service: str) -> bool:
        result = await _run_subprocess(self._service_cmd("start", service))
        if result.returncode != 0:
            logger.error("Failed to start %s: %s", service, result.stderr.strip())
            return False
        return True

    async def _admin_connection(self) -> Any:
        return await asyncpg.connect(
            host=self.config.get("db_host", "localhost"),
            port=self.config.get("db_port", 5432),
            user=self.config.get("db_user", "postgres"),
            password=self.config.get("db_password"),
            database="postgres",
        )

    async def _drop_database(self) -> None:
        """Drop the target database. Raises on failure."""
        db_name = safe_identifier(self.config.get("db_name", "multimodal_rag"))
        conn = await self._admin_connection()
        try:
            await conn.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = $1
                """,
                db_name,
            )
            # Identifier validated above; asyncpg cannot parameterize DDL.
            await conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
            logger.info("Dropped existing database")
        finally:
            await conn.close()

    async def _create_database(self) -> None:
        """Create the target database. Raises on failure."""
        db_name = safe_identifier(self.config.get("db_name", "multimodal_rag"))
        conn = await self._admin_connection()
        try:
            await conn.execute(f"CREATE DATABASE {db_name}")
            logger.info("Created new database")
        finally:
            await conn.close()

    async def _restore_database(self, archive: Path) -> bool:
        """Restore the database from a pg_restore archive."""
        cmd = [
            "pg_restore",
            "--host",
            self.config.get("db_host", "localhost"),
            "--port",
            str(self.config.get("db_port", 5432)),
            "--username",
            self.config.get("db_user", "postgres"),
            "--dbname",
            self.config.get("db_name", "multimodal_rag"),
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            str(archive),
        ]
        env = os.environ.copy()
        db_password = self.config.get("db_password")
        if db_password:
            env["PGPASSWORD"] = db_password

        result = await _run_subprocess(cmd, env=env)
        if result.returncode != 0:
            logger.error("Database restore failed: %s", result.stderr.strip())
            return False
        logger.info("Database restore completed")
        return True

    async def _count_restored_tables(self) -> int:
        """Count public tables in the restored database."""
        conn = await asyncpg.connect(
            host=self.config.get("db_host", "localhost"),
            port=self.config.get("db_port", 5432),
            user=self.config.get("db_user", "postgres"),
            password=self.config.get("db_password"),
            database=self.config.get("db_name", "multimodal_rag"),
        )
        try:
            count = await conn.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            )
            return int(count or 0)
        finally:
            await conn.close()

    async def _fix_upload_permissions(self, upload_dir: Path) -> bool:
        """Fix permissions for uploaded files; True when both commands succeed."""
        ok = True
        for cmd in (
            ["chown", "-R", "www-data:www-data", str(upload_dir)],
            ["chmod", "-R", "755", str(upload_dir)],
        ):
            result = await _run_subprocess(cmd)
            if result.returncode != 0:
                logger.error("%s failed: %s", cmd[0], result.stderr.strip())
                ok = False
        return ok

    def _log_recovery_step(self, component: str, result: Dict[str, Any]) -> None:
        self.recovery_log.append(
            {
                "component": component,
                "status": result.get("status", "unknown"),
                "timestamp": datetime.now().isoformat(),
                "details": result,
            }
        )

    def _generate_recovery_report(
        self, recovery_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate a recovery report. Any failed component fails the run."""
        end_time = datetime.now()
        statuses = [r.get("status") for r in recovery_results.values()]
        counts = {s.value: statuses.count(s.value) for s in RecoveryStatus}

        overall_status = RecoveryStatus.SUCCESS
        if counts[RecoveryStatus.FAILED.value] or not statuses:
            overall_status = RecoveryStatus.FAILED
        elif counts[RecoveryStatus.PARTIAL.value]:
            overall_status = RecoveryStatus.PARTIAL

        return {
            "recovery_id": f"recovery_{self.start_time.strftime('%Y%m%d_%H%M%S')}",
            "start_time": self.start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_seconds": (end_time - self.start_time).total_seconds(),
            "overall_status": overall_status.value,
            "summary": {
                "total_components": len(statuses),
                "successful": counts[RecoveryStatus.SUCCESS.value],
                "failed": counts[RecoveryStatus.FAILED.value],
                "partial": counts[RecoveryStatus.PARTIAL.value],
                "skipped": counts[RecoveryStatus.SKIPPED.value],
            },
            "component_results": recovery_results,
            "recovery_log": self.recovery_log,
        }

    def _report_notification_status(self) -> None:
        logger.warning("No recovery notification channel is configured; none sent")


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables."""
    return {
        "backup_dir": os.getenv("BACKUP_DIR", "/backups"),
        "use_docker": os.getenv("USE_DOCKER", "true").lower() == "true",
        "db_host": os.getenv("DB_HOST", "localhost"),
        "db_port": int(os.getenv("DB_PORT", "5432")),
        "db_user": os.getenv("DB_USER", "postgres"),
        "db_password": os.getenv("DB_PASSWORD"),
        "db_name": os.getenv("DB_NAME", "multimodal_rag"),
        "upload_dir": os.getenv("UPLOAD_DIR", "/app/uploads"),
        "report_dir": os.getenv("DR_REPORT_DIR", "."),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Disaster recovery for Multimodal RAG system. DESTRUCTIVE: "
            "'database' drops and recreates the target database."
        )
    )
    parser.add_argument(
        "--components",
        nargs="+",
        required=True,
        choices=COMPONENTS,
        help="Components to recover. Required; there is no default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be recovered and exit",
    )
    parser.add_argument(
        "--yes",
        "--force",
        dest="yes",
        action="store_true",
        help="Skip the interactive confirmation",
    )
    return parser.parse_args()


async def main() -> None:
    configure_logging()
    args = parse_args()
    config = load_config()

    if args.dry_run:
        print("DRY RUN: would recover components:", ", ".join(args.components))
        return

    if not args.yes:
        response = input(
            f"This will DROP and restore: {', '.join(args.components)}. "
            "Type 'yes' to continue: "
        )
        if response.strip().lower() != "yes":
            print("Recovery cancelled")
            return

    try:
        report = await DisasterRecovery(config).run_recovery(args.components)
    except Exception as e:
        logger.error("Disaster recovery failed: %s", e)
        sys.exit(1)

    report_file = Path(config["report_dir"]) / (
        f"recovery_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    report_file.write_text(json.dumps(report, indent=2))
    print(f"Recovery finished. Report saved to: {report_file}")
    print(f"Overall status: {report['overall_status']}")

    if report["overall_status"] != RecoveryStatus.SUCCESS.value:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
