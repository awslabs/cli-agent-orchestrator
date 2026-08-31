"""Tests for the optional pre-resolved memory block (issue #583 Bolt 2, unit ``terminal-frozen-memory``).

Two carry the unit's load:

* ``test_an_empty_frozen_block_prepends_nothing_and_never_consults_memoryservice`` — the ``is None`` vs
  truthiness distinction. An empty block is a SUPPLIED block meaning "the original run resolved no memory". Under
  a single truthiness test it would fall through to the live path and pick up memories written after the original
  run — the exact drift FR-9 exists to prevent, arriving through the one case nobody thinks to test.
* ``test_the_kill_switch_binds_the_frozen_path_too`` — skipping ``MemoryService`` also skips its
  ``is_memory_enabled()`` check, so without this a workflow run would paste memory into the context of an
  operator who turned memory off. A control bypass, not a determinism bug.

ONE TRAP THESE TESTS AVOID. The first-message guard (``_memory_injected_terminals``) also suppresses a
``MemoryService`` call on message two, so "MemoryService was not constructed" is NOT by itself evidence that the
frozen arm ran. Every assertion of that shape below is made on a FIRST message to a FRESH terminal id.
"""

import inspect

import pytest

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import inject_memory_context


@pytest.fixture(autouse=True)
def fresh_injection_state():
    """Every test starts from a terminal that has never been injected."""
    with terminal_service._memory_injected_lock:
        terminal_service._memory_injected_terminals.clear()
    yield
    with terminal_service._memory_injected_lock:
        terminal_service._memory_injected_terminals.clear()


@pytest.fixture()
def spy(monkeypatch):
    """Record whether ``MemoryService`` was constructed, and with what the curator was asked."""
    calls = {"constructed": 0, "task_description": None, "terminal_id": None}

    class _Spy:
        def __init__(self):
            calls["constructed"] += 1

        def get_curated_memory_context(self, terminal_id, task_description=None):
            calls["terminal_id"] = terminal_id
            calls["task_description"] = task_description
            return "<cao-memory>LIVE</cao-memory>"

    monkeypatch.setattr(terminal_service, "MemoryService", _Spy)
    return calls


@pytest.fixture()
def memory_disabled(monkeypatch):
    """Turn the operator's kill switch off at the module the frozen arm imports from."""
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: False)


# ---------------------------------------------------------------------------
# The two load-bearing properties
# ---------------------------------------------------------------------------


def test_an_empty_frozen_block_prepends_nothing_and_never_consults_memoryservice(spy):
    """`is None` decides the ARM; truthiness only decides the PREPEND. Collapsing them is the bug."""
    result = inject_memory_context("do the thing", "term-empty", frozen_memory="")

    assert result == "do the thing", "an empty frozen block must prepend nothing"
    assert spy["constructed"] == 0, (
        "an empty frozen block is a SUPPLIED block — it must NOT fall through to live memory, "
        "or a run that legitimately froze no memory would pick up memories written after it"
    )


def test_the_kill_switch_binds_the_frozen_path_too(spy, memory_disabled):
    """Skipping MemoryService skips its is_memory_enabled() check. That must not become a bypass."""
    result = inject_memory_context(
        "do the thing", "term-disabled", frozen_memory="<cao-memory>FROZEN</cao-memory>"
    )

    assert result == "do the thing", (
        "with memory disabled, a frozen block must not be injected — a workflow run must not "
        "re-enable memory for an operator who turned it off"
    )
    assert spy["constructed"] == 0


# ---------------------------------------------------------------------------
# The frozen path
# ---------------------------------------------------------------------------


def test_a_frozen_block_is_injected_verbatim_with_the_live_separator(spy):
    block = "<cao-memory>FROZEN</cao-memory>"
    result = inject_memory_context("do the thing", "term-verbatim", frozen_memory=block)

    assert result == block + "\n\n" + "do the thing"
    assert "LIVE" not in result


def test_a_supplied_block_does_not_construct_memoryservice_on_a_first_message(spy):
    """Asserted on a FIRST message to a FRESH id, so the first-message guard cannot be the cause."""
    inject_memory_context("do the thing", "term-fresh", frozen_memory="<cao-memory>F</cao-memory>")
    assert spy["constructed"] == 0, "the frozen arm must not consult the live store"


def test_the_first_message_guard_still_fires_once_per_terminal_on_the_frozen_path(spy):
    block = "<cao-memory>FROZEN</cao-memory>"
    first = inject_memory_context("one", "term-guard", frozen_memory=block)
    second = inject_memory_context("two", "term-guard", frozen_memory=block)

    assert first.startswith(block)
    assert second == "two", "a frozen block must be injected at most once per terminal"


# ---------------------------------------------------------------------------
# The default path — C-1
# ---------------------------------------------------------------------------


def test_an_absent_block_uses_the_live_path_unchanged(spy):
    long_message = "x" * 500
    result = inject_memory_context(long_message, "term-live")

    assert result == "<cao-memory>LIVE</cao-memory>" + "\n\n" + long_message
    assert spy["constructed"] == 1
    assert spy["terminal_id"] == "term-live"
    assert spy["task_description"] == long_message[:200], "the 200-char slice must be unchanged"


def test_the_live_path_is_unaffected_when_memory_is_disabled(spy, memory_disabled):
    """With no frozen block the switch is MemoryService's business, exactly as before this unit."""
    result = inject_memory_context("do the thing", "term-live-off")

    assert result == "<cao-memory>LIVE</cao-memory>" + "\n\n" + "do the thing"
    assert spy["constructed"] == 1, (
        "the frozen arm's switch check must sit inside that arm — the live path must reach "
        "MemoryService and let it apply its own check"
    )


# ---------------------------------------------------------------------------
# The signatures
# ---------------------------------------------------------------------------


def test_both_signatures_take_the_block_last_and_defaulted():
    """``agent_step.run_agent_step`` calls send_input positionally with two arguments."""
    for func in (inject_memory_context, terminal_service.send_input):
        params = list(inspect.signature(func).parameters.values())
        assert params[-1].name == "frozen_memory", f"{func.__name__}: block must come last"
        assert params[-1].default is None, f"{func.__name__}: block must default to None"


def test_send_input_forwards_the_block_unchanged(monkeypatch):
    """send_input is a conduit: it must not inspect, validate or alter the block."""
    seen = {}

    def _capture(message, terminal_id, frozen_memory=None):
        seen["message"] = message
        seen["terminal_id"] = terminal_id
        seen["frozen_memory"] = frozen_memory
        raise RuntimeError("stop here — forwarding is all this test needs to observe")

    monkeypatch.setattr(terminal_service, "inject_memory_context", _capture)
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _tid: {"session_name": "s", "window": "0", "provider": "claude_code"},
    )
    # No provider, so the status/blocked guards above the injection point are skipped.
    monkeypatch.setattr(
        terminal_service.provider_manager, "get_provider", lambda _tid: None, raising=False
    )

    block = "<cao-memory>FROZEN</cao-memory>"
    # send_input logs and RE-RAISES (terminal_service.py:1353-1355), so the sentinel
    # surfaces here. It stops execution immediately after forwarding — which is all this
    # test needs to observe, and it avoids mocking the tmux paste path that follows.
    with pytest.raises(RuntimeError, match="stop here"):
        terminal_service.send_input("term-fwd", "hello", frozen_memory=block)

    assert seen["frozen_memory"] == block, "the block must arrive unchanged"
    assert seen["message"] == "hello"
