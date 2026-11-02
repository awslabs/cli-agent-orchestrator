"""The terminals registry row is not the agent: absence is not death (#708).

DEFECT CLASS: a subsystem treats the *absence or staleness of a registry row*
as proof that the thing the row records is gone -- so one subsystem deletes the
row on a weak signal while another silently drops every row that joins to it,
and a record in a non-terminal state ends up owned by no mechanism at all.

#708 was that class twice over, in two files that never mention each other:

1. ``cleanup_service.cleanup_old_data`` selected terminal rows on
   ``TerminalModel.last_active < cutoff``. ``last_active`` is written only by
   ``send_input`` / ``send_special_key``, so "nobody messaged this agent for
   RETENTION_DAYS" was read as "this agent is dead", and the row was deleted
   without the tmux window or provider process being touched.
2. ``list_pending_receiver_ids_older_than`` inner-joins ``terminals``, so a
   PENDING message whose receiver row is gone leaves the reconciliation sweep
   permanently -- while still reporting PENDING, with no log line and no
   status change.

Each half is locally defensible and was reviewed as such; the composition had
no owner. These are the structural guards, written so the next instance fails a
test rather than waiting for a reader to notice the seam.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome, TerminalCleanupResult
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    TerminalModel,
    fail_orphaned_pending_messages,
    list_pending_receiver_ids_older_than,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services.cleanup_service import stale_terminal_runtime_is_live

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"
GRACE = 30

#: Every ``clients/database.py`` helper whose PENDING-message behaviour depends
#: on the receiver's terminals row existing. Each one is a place where deleting
#: that row silently changes who owns the message. Adding a *sweep* here means
#: adding a cell to ``test_no_aged_pending_message_is_owned_by_nothing`` below;
#: this frozenset is what forces that.
REGISTRY_GATED_PENDING_SELECTORS = frozenset(
    {
        # Admission gate, not a sweep: rejects a receiver with no row, which is
        # why a message can never be re-queued once its receiver is deleted.
        "create_inbox_message",
        # Fast path, OpenCode only; narrower than the reconcile sweep by design.
        "list_pending_receiver_ids_by_provider",
        # The reconcile sweep. Inner join: orphans are deliberately not retried.
        "list_pending_receiver_ids_older_than",
        # The counterpart that gives those orphans a terminal state (#708).
        "fail_orphaned_pending_messages",
    }
)


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _module_functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _calls_made(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name):
            out.add(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            out.add(call.func.attr)
    return out


def _reads_terminal_attr(node: ast.AST, attr: str) -> bool:
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == attr
        and isinstance(n.value, ast.Name)
        and n.value.id == "TerminalModel"
        for n in ast.walk(node)
    )


def test_no_aged_pending_message_is_owned_by_nothing(test_db):
    """TOTALITY. Past the grace window, every PENDING message is claimed by
    exactly one mechanism -- whether or not its receiver's row still exists.

    This is the invariant #708 violated: the orphan cell was claimed by nobody,
    so the message stayed PENDING forever. Delete ``fail_orphaned_pending_messages``
    (or stop calling it) and the orphan cell's owner count drops to 0 here.
    """
    old = datetime.now(timezone.utc) - timedelta(seconds=GRACE * 4)
    with test_db() as seed:
        seed.add(
            TerminalModel(
                id="term-alive", tmux_session="cao-s", tmux_window="w", provider="kiro_cli"
            )
        )
        # The whole reachable state space for an aged PENDING message: the
        # receiver's registry row is either there or it is not.
        seed.add_all(
            [
                InboxModel(
                    sender_id="a",
                    receiver_id=receiver,
                    message=cell,
                    status=MessageStatus.PENDING.value,
                    created_at=old,
                )
                for cell, receiver in (
                    ("receiver-row-present", "term-alive"),
                    ("receiver-row-absent", "term-ghost"),
                )
            ]
        )
        seed.commit()

    with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
        retryable_receivers = set(list_pending_receiver_ids_older_than(GRACE))
        failed_ids = set(fail_orphaned_pending_messages(GRACE))

    with test_db() as check:
        messages = check.query(InboxModel).all()
        owners = {
            str(m.message): [
                name
                for name, claimed in (
                    ("reconcile-sweep: retry delivery", str(m.receiver_id) in retryable_receivers),
                    ("orphan-sweep: mark undeliverable", str(m.id) in failed_ids),
                )
                if claimed
            ]
            for m in messages
        }

    assert owners == {
        "receiver-row-present": ["reconcile-sweep: retry delivery"],
        "receiver-row-absent": ["orphan-sweep: mark undeliverable"],
    }, f"an aged PENDING message is owned by nothing or by two mechanisms: {owners}"


def test_every_registry_gated_pending_selector_is_declared():
    """ENUMERATION. A new sweep that reads PENDING through ``terminals`` is a new
    cell in the state space above, so it has to be declared here first."""
    found = {
        name
        for name, node in _module_functions(SRC / "clients" / "database.py").items()
        if "TerminalModel" in _names_used(node)
        and any(
            isinstance(n, ast.Attribute)
            and n.attr == "PENDING"
            and isinstance(n.value, ast.Name)
            and n.value.id == "MessageStatus"
            for n in ast.walk(node)
        )
    }
    assert found, "detector found no PENDING selectors at all — it has gone stale"
    assert found == set(REGISTRY_GATED_PENDING_SELECTORS), (
        "clients/database.py gained or lost a PENDING selector that joins the terminals "
        "registry. A deleted receiver row silently changes what such a sweep returns, so "
        "declare it in REGISTRY_GATED_PENDING_SELECTORS and give it a cell in "
        f"test_no_aged_pending_message_is_owned_by_nothing. diff={found ^ set(REGISTRY_GATED_PENDING_SELECTORS)}"
    )


def test_deleting_an_aged_terminal_row_is_gated_on_a_runtime_probe():
    """ENUMERATION. Any code that deletes a terminal row because ``last_active``
    is old must first ask the runtime whether the agent is still there."""
    offenders = []
    probed = []
    for module in sorted(SRC.rglob("*.py")):
        for name, node in _module_functions(module).items():
            if not _reads_terminal_attr(node, "last_active"):
                continue
            calls = _calls_made(node)
            if not calls & {"delete_terminal_row", "delete_terminal", "delete"}:
                continue  # a reader, not a deleter
            target = probed if "stale_terminal_runtime_is_live" in calls else offenders
            target.append(f"{module.relative_to(SRC)}::{name}")
    assert not offenders, (
        "deletes a terminal row selected on last_active without probing runtime liveness. "
        "last_active is written only by send_input/send_special_key, so age is not death, "
        f"and the row is the only thing making the agent addressable (#708): {offenders}"
    )
    assert probed == ["services/cleanup_service.py::cleanup_old_data"], (
        "the last_active-keyed retention path lost its liveness gate, moved, or was renamed; "
        f"this guard no longer sees it. gated={probed}"
    )


def _gate_says_dead(outcome: TerminalCleanupOutcome) -> bool:
    backend = MagicMock()
    backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(outcome=outcome)
    terminal = TerminalModel(id="t", tmux_session="cao-s", tmux_window="w", provider="kiro_cli")
    with patch("cli_agent_orchestrator.services.cleanup_service.get_backend", return_value=backend):
        is_live = stale_terminal_runtime_is_live(terminal)
    backend.cleanup_terminal_exact.assert_called_once_with("t", "cao-s", "w", close=False)
    return not is_live


@pytest.mark.parametrize("outcome", list(TerminalCleanupOutcome))
def test_liveness_gate_reads_dead_only_from_proof_of_absence(outcome):
    """EXHAUSTIVENESS. Every outcome that does not PROVE the runtime is gone --
    ``STILL_PRESENT``, ``UNKNOWN``, and anything added later -- must retain the
    row. A new ``TerminalCleanupOutcome`` member lands here automatically, and
    defaults to failing rather than to "gone".
    """
    if TerminalCleanupResult(outcome=outcome).reclaimed:
        pytest.skip(f"{outcome} proves absence; pinned by the dead-set test below")
    assert not _gate_says_dead(
        outcome
    ), f"{outcome} does not prove the runtime is gone, so the row must be retained"


def test_liveness_gate_dead_set_is_exactly_absent():
    """PINNED. ``ABSENT`` -- positive proof that no live object carries this
    terminal id -- is the only outcome that lets a row age out. Empty would
    make retention never reclaim anything; wider would reintroduce the class.
    """
    dead = {o.value for o in TerminalCleanupOutcome if _gate_says_dead(o)}
    assert dead == {TerminalCleanupOutcome.ABSENT.value}, dead


def test_liveness_gate_fails_closed_when_the_probe_raises():
    backend = MagicMock()
    backend.cleanup_terminal_exact.side_effect = RuntimeError("tmux unreachable")
    terminal = TerminalModel(id="t", tmux_session="cao-s", tmux_window="w", provider="kiro_cli")

    with patch("cli_agent_orchestrator.services.cleanup_service.get_backend", return_value=backend):
        assert stale_terminal_runtime_is_live(terminal) is True
