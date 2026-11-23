"""Contracts for the integration-test sharding used by the Test Pipeline.

The Integration Tests job runs one matrix leg per shard. The legs must cover
every test file exactly once, or a test silently stops running in CI.
"""

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

import pytest
import yaml  # type: ignore[import-untyped]  # CI lint gate installs no types-PyYAML

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "shard_tests.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml"

_spec = importlib.util.spec_from_file_location("shard_tests", SCRIPT)
assert _spec is not None and _spec.loader is not None
shard_tests = importlib.util.module_from_spec(_spec)
sys.modules["shard_tests"] = shard_tests
_spec.loader.exec_module(shard_tests)


def _tree(root: Path, sizes: dict[str, int]) -> None:
    for name, size in sizes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size, encoding="utf-8")


SIZES = {
    "test_big.py": 900,
    "test_mid.py": 400,
    "test_small.py": 100,
    "ai/test_nested.py": 300,
    "ai/test_tiny.py": 10,
    "conftest.py": 50,  # never collected as a test file
    "helpers.py": 50,
}


@pytest.mark.parametrize("total", [1, 2, 3, 5, 8])
def test_every_test_file_runs_in_exactly_one_shard(tmp_path: Path, total: int) -> None:
    _tree(tmp_path, SIZES)
    expected = sorted(tmp_path.rglob("test_*.py"))

    seen: list[Path] = []
    for shard in range(1, total + 1):
        seen.extend(shard_tests.shard_files(tmp_path, shard, total))

    assert sorted(seen) == expected
    assert len(seen) == len(set(seen))


def test_split_is_deterministic(tmp_path: Path) -> None:
    _tree(tmp_path, SIZES)
    first = shard_tests.shard_files(tmp_path, 2, 3)
    assert shard_tests.shard_files(tmp_path, 2, 3) == first


def test_heaviest_files_are_spread_across_shards(tmp_path: Path) -> None:
    _tree(tmp_path, SIZES)
    heaviest = {shard_tests.shard_files(tmp_path, shard, 2)[0].name for shard in (1, 2)}
    assert "test_big.py" in heaviest
    # the big file does not share a shard with the next-largest one
    big_shard = next(
        s
        for s in (1, 2)
        if tmp_path / "test_big.py" in shard_tests.shard_files(tmp_path, s, 2)
    )
    assert tmp_path / "test_mid.py" not in shard_tests.shard_files(
        tmp_path, big_shard, 2
    )


def test_more_shards_than_files_leaves_empty_shards(tmp_path: Path) -> None:
    _tree(tmp_path, {"test_only.py": 10})
    assert shard_tests.shard_files(tmp_path, 1, 3) == [tmp_path / "test_only.py"]
    assert shard_tests.shard_files(tmp_path, 2, 3) == []


@pytest.mark.parametrize("shard,total", [(0, 3), (4, 3), (1, 0), (-1, 2)])
def test_invalid_shard_arguments_are_rejected(
    tmp_path: Path, shard: int, total: int
) -> None:
    with pytest.raises(ValueError):
        shard_tests.shard_files(tmp_path, shard, total)


def test_cli_prints_one_path_per_line_and_rejects_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path, {"test_a.py": 10, "test_b.py": 20})
    assert shard_tests.main([str(tmp_path), "--shard", "1", "--of", "1"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == sorted(p.as_posix() for p in tmp_path.glob("test_*.py"))

    assert shard_tests.main([str(tmp_path), "--shard", "5", "--of", "2"]) == 2
    assert (
        shard_tests.main([str(tmp_path / "missing"), "--shard", "1", "--of", "2"]) == 2
    )


def _integration_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return cast(dict, workflow["jobs"]["integration-tests"])


def test_integration_job_enables_real_redis_cli_revocation_regression() -> None:
    job = _integration_job()
    step = next(
        step for step in job["steps"] if step.get("name") == "Run integration tests"
    )
    env = step["env"]
    assert env.get("CLI_REVOCATION_TEST_REDIS_URL") == env["REDIS_URL"]
    assert "job.services.redis.ports['6379']" in env["REDIS_URL"]


def test_integration_job_enrolls_orchestration_postgres_suites() -> None:
    """Suites gated on the shared orchestration variables run in CI.

    The Daily Research Brief lifecycle and the other PostgreSQL/Redis
    orchestration suites skip without these, which hid their regressions.
    """
    job = _integration_job()
    step = next(
        step for step in job["steps"] if step.get("name") == "Run integration tests"
    )
    env = step["env"]
    assert env.get("ORCHESTRATION_TEST_DATABASE_URL") == env["DATABASE_URL"]
    assert "job.services.postgres.ports['5432']" in env["DATABASE_URL"]
    assert "job.services.redis.ports['6379']" in env.get(
        "ORCHESTRATION_TEST_REDIS_URL", ""
    )


def test_integration_job_enrolls_retention_purge_regression() -> None:
    # HO-2: the thread-purge FK proof skips without a PostgreSQL URL.
    job = _integration_job()
    step = next(
        step for step in job["steps"] if step.get("name") == "Run integration tests"
    )
    env = step["env"]
    assert env.get("RETENTION_PURGE_TEST_DATABASE_URL") == env["DATABASE_URL"]
    # The proof must read the name CI sets, or a rename skips it silently.
    proof = REPO_ROOT / "backend" / "tests" / "integration"
    source = (proof / "test_retention_thread_purge_postgres.py").read_text(
        encoding="utf-8"
    )
    assert "RETENTION_PURGE_TEST_DATABASE_URL" in source


# ---- the PostgreSQL lane outside backend/tests/integration (GOO-702/731) ----

_POSTGRES_LANE = "Run ingestion recovery PostgreSQL tests"
_TESTS = REPO_ROOT / "backend" / "tests"
# Trees with PostgreSQL lanes of their own (integration shards), never
# collected (norecursedirs), or this directory, whose strings name the URLs.
_NOT_LANE_SCOPE = (
    "integration",
    "performance",
    "templates",
    "standalone",
    "scaffolding",
    "unit/ci",
)
# Names a PostgreSQL URL variable in a string: read directly, or through a
# constant (``_ENV = "..._TEST_DATABASE_URL"``, then ``os.getenv(_ENV)``).
_URL_NAME = r"[A-Z0-9_]*_TEST_DATABASE_URL"
_NAMES_PG_URL = re.compile(rf"""["']({_URL_NAME})["']""")
# Imports PostgreSQL fixtures: backend/tests/db/conftest.py or any *_postgres
# module (the stuck_recovery files import theirs from one, GOO-703).
_IMPORTS_PG_FIXTURES = re.compile(
    r"^\s*(?:from|import)\s+tests\.(?:db\.conftest|[\w.]*_postgres)\b", re.M
)
_CLEAN_ENV_DROPS = ("CI", "REQUIRE_POSTGRES_TESTS", "TEST_PG_ADMIN_DSN")
_CLEAN_ENV_DROP_SUFFIX = ("_TEST_DATABASE_URL", "_PG_ASYNC_URL")


def _in_lane_scope(path: Path) -> bool:
    rel = path.relative_to(_TESTS).as_posix()
    return not any(rel == p or rel.startswith(p + "/") for p in _NOT_LANE_SCOPE)


def _reaches_pg(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return bool(_NAMES_PG_URL.search(text) or _IMPORTS_PG_FIXTURES.search(text))


def _postgres_candidates() -> tuple[set[Path], set[Path]]:
    """Return (files that may hold a PostgreSQL test, the direct subset).

    Direct files are *_postgres.py files and test files that name a database
    URL or import PostgreSQL fixtures themselves. The rest reach a PostgreSQL
    fixture through a directory conftest (backend/tests/api/agent imports
    tests.db.conftest, for one). Collection then tells which tests in them
    really use one: backend/tests/conftest.py marks those requires_postgres.
    """
    fixture_dirs = {_TESTS / "db"} | {
        conftest.parent
        for conftest in _TESTS.rglob("conftest.py")
        if conftest.parent != _TESTS
        and _in_lane_scope(conftest)
        and _reaches_pg(conftest)
    }
    direct: set[Path] = set()
    reached: set[Path] = set()
    for path in _TESTS.rglob("test_*.py"):
        if not _in_lane_scope(path):
            continue
        if path.name.endswith("_postgres.py") or _reaches_pg(path):
            direct.add(path)
        if fixture_dirs.intersection(path.parents):
            reached.add(path)
    return direct | reached, direct


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CLEAN_ENV_DROPS and not k.endswith(_CLEAN_ENV_DROP_SUFFIX)
    }
    return {**env, "ENVIRONMENT": "testing", **extra}


def _pytest(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *args,
            "-c",
            "backend/pytest.ini",
            "--color=no",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _collect_postgres_tests(paths: set[Path]) -> set[str]:
    """Node ids (relative to backend/) the PostgreSQL mark selects."""
    result = _pytest(
        *sorted(p.relative_to(REPO_ROOT).as_posix() for p in paths),
        "--collect-only",
        "-qq",  # pytest.ini adds -v
        "-m",
        "requires_postgres",
        env=_clean_env(),
    )
    assert result.returncode in (0, 5), result.stdout[-4000:] + result.stderr
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def _postgres_lane_step() -> dict:
    return next(
        step
        for step in _integration_job()["steps"]
        if step.get("name") == _POSTGRES_LANE
    )


def _postgres_lane_files(step: dict) -> set[str]:
    """Expand the lane's ``postgres_tests`` array in the real repository."""
    lines = step["run"].splitlines()
    start = next(i for i, line in enumerate(lines) if "postgres_tests=(" in line)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == ")")
    script = "\n".join(
        lines[start : end + 1] + ['printf "%s\\n" "${postgres_tests[@]}"']
    )
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def _covered_by(node_file: str, lane: set[str]) -> bool:
    path = "backend/" + node_file
    return any(
        path == entry or path.startswith(entry.rstrip("/") + "/") for entry in lane
    )


def test_postgres_lane_runs_every_postgres_test() -> None:
    """Unit Tests has no database, so a PostgreSQL test the lane misses never
    runs anywhere: it skips there, or is deselected as integration (GOO-731:
    the 108 tests on the tests/db fixtures ran nowhere). Which tests are
    PostgreSQL tests comes from collection, not from reading source, so a
    fixture imported from a *_postgres module or a URL read through a
    constant counts too (GOO-703)."""
    step = _postgres_lane_step()
    lane = _postgres_lane_files(step)
    for name in lane:
        assert (REPO_ROOT / name).exists(), f"{name} is listed but does not exist"

    candidates, _ = _postgres_candidates()
    postgres_tests = _collect_postgres_tests(candidates)
    assert len(postgres_tests) > 250, "the scan found too few tests; it is stale"
    for expected in (
        "tests/unit/services/threads/test_thread_service.py::",
        "tests/api/agent/test_edit_resend_tombstone.py::",
        "tests/api/threads/test_messages_order_param.py::",
        "tests/db/test_chat_messages_client_uuid_uniqueness.py::",
        "tests/unit/tasks/test_stuck_processing_recovery.py::",
    ):
        assert any(n.startswith(expected) for n in postgres_tests), expected

    missing = sorted(
        n for n in postgres_tests if not _covered_by(n.split("::", 1)[0], lane)
    )
    assert not missing, (
        "PostgreSQL tests outside the lane's postgres_tests list in "
        f".github/workflows/test-pipeline.yml: {missing[:20]}"
    )


def test_postgres_lane_selects_by_the_postgres_mark() -> None:
    """The lane's -m must be the mark collection uses above, or a test the
    coverage check counts could still be deselected in CI."""
    step = _postgres_lane_step()
    assert step["env"]["REQUIRE_POSTGRES_TESTS"] == "1"
    assert re.search(r"-m\s+requires_postgres\s", step["run"]), step["run"]
    assert "-m " not in step["run"].replace("-m requires_postgres", "")


def test_no_unmarked_postgres_test_hides_in_a_postgres_file(tmp_path: Path) -> None:
    """GOO-703: the lane drops the tests of its files that are not marked
    requires_postgres. Run exactly those without a database: a PostgreSQL
    test among them would skip, and is reported here instead of vanishing.
    (The account suites' sqlite halves and pure-logic tests pass.)"""
    _, direct = _postgres_candidates()
    report = tmp_path / "unmarked.xml"
    result = _pytest(
        *sorted(p.relative_to(REPO_ROOT).as_posix() for p in direct),
        "-m",
        "not requires_postgres",
        "-q",
        f"--junitxml={report}",
        env=_clean_env(),
    )
    out = result.stdout[-4000:] + result.stderr[-2000:]
    if result.returncode == 5:  # every test in these files is marked
        return
    assert result.returncode == 0, out
    skipped = [
        f"{case.get('classname')}::{case.get('name')}"
        for case in ElementTree.parse(report).iter("testcase")
        if any(
            child.tag == "skipped" and child.get("type") != "pytest.xfail"
            for child in case
        )
    ]
    assert not skipped, (
        "Unmarked tests skip without a database; mark them requires_postgres "
        f"so the PostgreSQL lane runs them: {skipped}"
    )


_SKIP_CALLS = {"skip", "fail", "xfail", "exit", "importorskip"}


def _stops_the_test(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Raise):
        return True
    call = stmt.value if isinstance(stmt, ast.Expr) else None
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _SKIP_CALLS


def _is_test_or_fixture(func: ast.AST) -> bool:
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if func.name.startswith("test_"):
        return True
    return any("fixture" in ast.unparse(d) for d in func.decorator_list)


def _early_returns_without_a_database(path: Path) -> list[str]:
    """Tests or fixtures that `return` when the database URL is missing.

    Such a test passes having checked nothing, and no skip guard sees it
    (GOO-703). They must call pytest.skip (or fail) instead."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    url_constants = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and re.fullmatch(_URL_NAME, node.value.value)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }

    def reads_url(node: ast.AST, names: set[str]) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if re.fullmatch(_URL_NAME, sub.value):
                    return True
            if isinstance(sub, ast.Name) and sub.id in names:
                return True
        return False

    found = []
    for func in ast.walk(tree):
        if not _is_test_or_fixture(func):
            continue
        assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        names = set(url_constants)
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and reads_url(node.value, names):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if reads_url(node.value, names) and isinstance(node.target, ast.Name):
                    names.add(node.target.id)
        for node in ast.walk(func):
            if not isinstance(node, ast.If) or not reads_url(node.test, names):
                continue
            for stmt in node.body:
                if _stops_the_test(stmt):
                    break
                if isinstance(stmt, ast.Return):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{stmt.lineno}")
                    break
    return found


def test_postgres_tests_skip_rather_than_return_without_a_database() -> None:
    candidates, direct = _postgres_candidates()
    sources = direct | {
        conftest
        for conftest in _TESTS.rglob("conftest.py")
        if _in_lane_scope(conftest) and _reaches_pg(conftest)
    }
    sources.add(_TESTS / "db" / "conftest.py")
    found = [
        hit
        for path in sorted(sources)
        for hit in _early_returns_without_a_database(path)
    ]
    assert not found, (
        "These return early when the database URL is missing, so the test "
        f"passes having checked nothing; call pytest.skip instead: {found}"
    )


_GUARD_SAMPLE = Path(__file__).with_name("postgres_guard_sample.py")


def _run_guard_sample(**env: str) -> subprocess.CompletedProcess[str]:
    clean = {
        k: v for k, v in os.environ.items() if k not in {"CI", "REQUIRE_POSTGRES_TESTS"}
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_GUARD_SAMPLE),
            "-c",
            str(REPO_ROOT / "backend" / "pytest.ini"),
            "-q",
            "--color=no",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT / "backend",
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_ci_postgres_lane_fails_a_skipped_postgres_test() -> None:
    result = _run_guard_sample(CI="true", REQUIRE_POSTGRES_TESTS="1")

    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    # The marker skips during setup, so pytest counts that one as an error.
    summary = out.strip().splitlines()[-1]
    for count in ("1 failed", "1 error", "1 skipped"):
        assert count in summary, out
    # A runtime skip and a skip marker get different explanations (GOO-703):
    # only the first one points at the database.
    assert "PostgreSQL test skipped at runtime" in out, out
    assert "the database is most likely missing or unreachable" in out, out
    assert "PostgreSQL test skipped by a skip/skipif marker" in out, out
    assert "The database is not the cause" in out, out


@pytest.mark.parametrize(
    "env",
    [
        {"CI": "true"},  # the Unit Tests job: no database, skipping is right
        {"REQUIRE_POSTGRES_TESTS": "1"},  # a local run outside CI
    ],
)
def test_postgres_skips_stay_skips_outside_the_lane(env: dict[str, str]) -> None:
    result = _run_guard_sample(**env)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "3 skipped" in out, out


@pytest.mark.parametrize(
    ("case_xml", "pytest_exit", "accepted"),
    [
        ("<testcase name='passed'/>", 0, True),
        ("<testcase name='skipped'><skipped/></testcase>", 0, False),
        ("", 0, False),
        ("<testcase name='failed'><failure/></testcase>", 0, False),
        ("<testcase name='error'><error/></testcase>", 0, False),
        ("<testcase name='passed'/>", 1, False),
    ],
)
def test_recovery_postgres_step_requires_executed_tests(
    tmp_path: Path, case_xml: str, pytest_exit: int, accepted: bool
) -> None:
    job = _integration_job()
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Run ingestion recovery PostgreSQL tests"
    )
    assert step["if"] == "matrix.shard == 1"
    assert (
        "job.services.postgres.ports['5432']"
        in step["env"]["ORCHESTRATION_TEST_DATABASE_URL"]
    )
    # GOO-702: every *_postgres.py file under the unit tree, not a hand list
    # (the GOO-436 PDF-chat branch suites and ingestion stage guard included).
    collected = _postgres_lane_files(step)
    for module in (
        "tasks/test_stuck_processing_recovery.py",
        "tasks/test_stuck_recovery_terminal_retry.py",
        "tasks/test_ingestion_stage_guard_postgres.py",
        "services/test_message_branches_postgres.py",
        "services/test_alphaxiv_live_postgres.py",
        "services/test_native_pdf_branch_reads_postgres.py",
    ):
        assert f"backend/tests/unit/{module}" in collected

    # Execute the actual workflow shell against pytest's possible outcomes.
    # In particular, pytest exits zero when every selected test skips.
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)  # for find
    results = tmp_path / "test-results"
    results.mkdir()
    (results / "ingestion-recovery.xml").write_text(
        f"<testsuites><testsuite>{case_xml}</testsuite></testsuites>"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pytest_stub = bin_dir / "pytest"
    pytest_stub.write_text(f"#!/bin/sh\nexit {pytest_exit}\n")
    pytest_stub.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


_XFAIL = "<skipped type='pytest.xfail' message='tracked finding'/>"


@pytest.mark.parametrize(
    ("case_xml", "pytest_exit", "accepted"),
    [
        ("<testcase name='passed'/>", 0, True),
        (f"<testcase name='passed'/><testcase name='x'>{_XFAIL}</testcase>", 0, True),
        ("<testcase name='skipped'><skipped type='pytest.skip'/></testcase>", 0, False),
        ("<testcase name='skipped'><skipped/></testcase>", 0, False),
        ("", 0, False),
        ("<testcase name='failed'><failure/></testcase>", 0, False),
        ("<testcase name='error'><error/></testcase>", 0, False),
        ("<testcase name='passed'/>", 1, False),
    ],
)
def test_two_account_postgres_search_step_requires_executed_tests(
    tmp_path: Path, case_xml: str, pytest_exit: int, accepted: bool
) -> None:
    """Matrix row S2 (GOO-399): the suite skips without its database URL, so
    the step must reject an all-skipped or partly skipped green run."""
    job = _integration_job()
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Run two-account PostgreSQL search isolation tests"
    )
    # Pinned to the shortest shard so shard 1 (ingestion recovery lane) is not
    # also the longest leg; the lane must still run on exactly one shard.
    assert "matrix.shard == 3" in step["if"]
    assert (
        "job.services.postgres.ports['5432']"
        in step["env"]["TWO_ACCOUNT_PG_TEST_DATABASE_URL"]
    )
    assert (
        "backend/tests/integration/two_account/test_search_isolation_postgres.py"
        in step["run"]
    )
    # Q-P3: the search-vector repair suite rides the same no-skip lane.
    assert (
        "backend/tests/integration/test_search_vector_repair_postgres.py" in step["run"]
    )

    results = tmp_path / "test-results"
    results.mkdir()
    (results / "two-account-pg-search.xml").write_text(
        f"<testsuites><testsuite>{case_xml}</testsuite></testsuites>"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pytest_stub = bin_dir / "pytest"
    pytest_stub.write_text(f"#!/bin/sh\nexit {pytest_exit}\n")
    pytest_stub.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_integration_job_is_a_sharded_matrix_that_fails_closed() -> None:
    job = _integration_job()
    strategy = job["strategy"]
    shards = strategy["matrix"]["shard"]
    # fail-fast stays off so one red shard does not hide the others' results
    assert strategy["fail-fast"] is False
    assert shards == list(range(1, len(shards) + 1)) and len(shards) >= 2
    assert "matrix.shard" in job["name"]

    step = next(
        step
        for step in job["steps"]
        if "backend/tests/integration" in str(step.get("run", ""))
    )
    # the leg count comes from the matrix, never a second hard-coded number
    assert "strategy.job-total" in str(step["env"]["SHARDS"])
    assert "matrix.shard" in str(step["env"]["SHARD"])
    assert "shard_tests.py" in step["run"]
    assert "backend/tests/integration/ \\" not in step["run"]  # no whole-dir run
