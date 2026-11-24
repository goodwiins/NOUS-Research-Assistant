"""Approval receipts are bound to a saved action, not a waiting thread.

Mutation proof: see docs/engineering/agent-approvals.md for the removed guards
and exact regression commands. The PostgreSQL suite uses this real graph.
"""

import importlib.metadata
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from packaging.requirements import Requirement

from src.services.agent.confirmation_service import (
    ApprovalExpired,
    PendingApproval,
    pending_approval,
    require_approval,
)

pytestmark = pytest.mark.unit

REQUIREMENTS_PATH = Path(__file__).resolve().parents[3] / "requirements.txt"


def langgraph_floor_problem(
    requirements: str, installed: str | None = None
) -> str | None:
    """Why the installed langgraph cannot pass the per-call interrupt id test.

    The floor comes from requirements.txt, so it moves with the pin. GOO-782:
    a local venv on langgraph 1.2.7 failed the identical-gates test 20/20 with
    an opaque `approval_id != approval_id` while CI (the lock, 1.2.24) passed
    20/20; the test was never clock- or order-dependent.
    """
    for raw in requirements.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement = Requirement(line)
        if requirement.name != "langgraph":
            continue
        if installed is None:
            installed = importlib.metadata.version("langgraph")
        if requirement.specifier.contains(installed, prereleases=True):
            return None
        return (
            f"installed langgraph {installed} does not satisfy requirements.txt "
            f"({requirement}): sequential identical interrupts share one id "
            "below the floor. Reinstall: pip install -r backend/requirements.txt"
        )
    return "requirements.txt no longer pins langgraph; update this guard"


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        values={"user_id": "user"},
        config={"configurable": {"checkpoint_id": "saved", "checkpoint_ns": ""}},
        tasks=(
            SimpleNamespace(
                interrupts=(
                    SimpleNamespace(
                        id="interrupt",
                        value={"tools": [{"name": "delete", "args": {"id": "a"}}]},
                    ),
                )
            ),
        ),
    )


def _approval(snapshot: Any, **changes: Any) -> PendingApproval:
    approval = pending_approval(
        snapshot,
        **{"thread_id": "thread", "run_id": "run", "user_id": "user", **changes},
    )

    assert approval is not None
    return approval


def test_receipt_is_stable_across_checkpoint_reloads() -> None:
    snapshot = _snapshot()
    first = _approval(snapshot)
    replay = _approval(deepcopy(snapshot))
    assert first == replay
    assert len(first.approval_id) == 64
    assert first.confirmation()["approval_id"] == first.approval_id
    assert first.resume(False) == {"interrupt": {"confirmed": False}}


@pytest.mark.parametrize("change", ["run", "thread", "checkpoint", "interrupt", "args"])
def test_any_change_to_the_authorized_action_rotates_receipt(change: str) -> None:
    snapshot = _snapshot()
    first = _approval(snapshot)
    context = {}
    if change in {"run", "thread"}:
        context[f"{change}_id"] = "replacement"
    elif change == "checkpoint":
        snapshot.config["configurable"]["checkpoint_id"] = "replacement"
    elif change == "interrupt":
        snapshot.tasks[0].interrupts[0].id = "replacement"
    else:
        snapshot.tasks[0].interrupts[0].value["tools"][0]["args"]["id"] = "b"
    assert _approval(snapshot, **context).approval_id != first.approval_id


@pytest.mark.parametrize("missing", ["owner", "checkpoint", "interrupt"])
def test_legacy_or_unowned_checkpoints_cannot_issue_receipts(missing: str) -> None:
    snapshot = _snapshot()
    if missing == "owner":
        snapshot.values = {"user_id": "someone-else"}
    elif missing == "checkpoint":
        snapshot.config = {}
    else:
        snapshot.tasks[0].interrupts[0].id = "placeholder-id"
    with pytest.raises(ApprovalExpired):
        _approval(snapshot)


class ApprovalState(TypedDict):
    user_id: str
    first: bool
    second: bool


def approval_graph(checkpointer: Any) -> Any:
    """Two real saved interrupts, including two gates within the same node."""

    def actions(state: ApprovalState) -> dict[str, bool]:
        first = interrupt({"tools": [{"name": "first", "args": {"id": "a"}}]})
        second = interrupt({"tools": [{"name": "second", "args": {"id": "b"}}]})
        return {"first": first["confirmed"], "second": second["confirmed"]}

    builder = StateGraph(ApprovalState)
    builder.add_node("actions", actions)
    builder.add_edge(START, "actions")
    builder.add_edge("actions", END)
    return builder.compile(checkpointer=checkpointer)


async def test_old_receipt_cannot_approve_next_interrupt_in_same_node() -> None:
    graph = approval_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke({"user_id": "user"}, config)
    first = _approval(await graph.aget_state(config))
    await graph.ainvoke(Command(resume=first.resume(True)), config)
    saved_second = await graph.aget_state(config)
    second = _approval(saved_second)
    # LangGraph <=1.2.19 reused the task interrupt id for sequential calls
    # inside one node; 1.2.20 issues a fresh id per call. Both gates share the
    # saved checkpoint_id, so here the differing action payload (and, on
    # >=1.2.20, the per-call interrupt id) is what gives the two gates
    # distinct approvals; see test_identical_sequential_gates_get_distinct_
    # receipts for the identical-payload case.
    assert first.approval_id != second.approval_id
    with pytest.raises(ApprovalExpired):
        require_approval(
            saved_second,
            approval_id=first.approval_id,
            thread_id="thread",
            run_id="run",
            user_id="user",
        )
    valid = require_approval(
        saved_second,
        approval_id=second.approval_id,
        thread_id="thread",
        run_id="run",
        user_id="user",
    )
    result = await graph.ainvoke(Command(resume=valid.resume(False)), config)
    assert result["first"] is True
    assert result["second"] is False
    with pytest.raises(ApprovalExpired):
        require_approval(
            await graph.aget_state(config),
            approval_id=second.approval_id,
            thread_id="thread",
            run_id="run",
            user_id="user",
        )


def identical_gate_graph(checkpointer: Any) -> Any:
    """Two sequential gates in one node with byte-identical payloads."""

    def actions(state: ApprovalState) -> dict[str, bool]:
        payload = {"tools": [{"name": "same", "args": {"id": "x"}}]}
        first = interrupt(payload)
        second = interrupt(payload)
        return {"first": first["confirmed"], "second": second["confirmed"]}

    builder = StateGraph(ApprovalState)
    builder.add_node("actions", actions)
    builder.add_edge(START, "actions")
    builder.add_edge("actions", END)
    return builder.compile(checkpointer=checkpointer)


async def test_identical_sequential_gates_get_distinct_receipts() -> None:
    # Both gates share one task and one saved checkpoint_id, and the action
    # digest is identical, so only the per-call interrupt id (LangGraph
    # >=1.2.20) separates them. requirements.txt pins that floor; on <=1.2.19
    # the two gates collapse to one approval id and the first receipt would
    # approve the second. A stale venv fails here, naming the cause (GOO-782).
    problem = langgraph_floor_problem(REQUIREMENTS_PATH.read_text(encoding="utf-8"))
    if problem:
        pytest.fail(problem)
    graph = identical_gate_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke({"user_id": "user"}, config)
    first_state = await graph.aget_state(config)
    first = _approval(first_state)
    await graph.ainvoke(Command(resume=first.resume(True)), config)
    saved_second = await graph.aget_state(config)
    second = _approval(saved_second)

    assert (
        first_state.config["configurable"]["checkpoint_id"]
        == saved_second.config["configurable"]["checkpoint_id"]
    )
    assert first.approval_id != second.approval_id
    with pytest.raises(ApprovalExpired):
        require_approval(
            saved_second,
            approval_id=first.approval_id,
            thread_id="thread",
            run_id="run",
            user_id="user",
        )


_PINNED = "fastapi>=0.1\nlanggraph>=1.2.20,<2.0  # per-call interrupt ids\n"


@pytest.mark.parametrize(
    "requirements, installed, expected",
    [
        (_PINNED, "1.2.7", "installed langgraph 1.2.7 does not satisfy"),
        (_PINNED, "1.2.19", "installed langgraph 1.2.19 does not satisfy"),
        (_PINNED, "1.2.20", None),
        (_PINNED, "1.2.24", None),
        ("fastapi>=0.1\nlanggraph-checkpoint>=4.3\n", "1.2.24", "no longer pins"),
    ],
)
def test_langgraph_floor_guard_names_a_stale_install(
    requirements: str, installed: str, expected: str | None
) -> None:
    """The guard reads the floor from requirements text and fails closed."""
    problem = langgraph_floor_problem(requirements, installed)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def test_the_real_requirements_pin_the_interrupt_id_floor() -> None:
    text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    assert langgraph_floor_problem(text, "1.2.19") is not None
    assert langgraph_floor_problem(text, "1.2.20") is None
