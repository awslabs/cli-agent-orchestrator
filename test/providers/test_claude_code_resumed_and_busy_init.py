"""Claude Code launch init on a resumed or already-busy supervisor (#1277).

Two provider-side causes made a resumed supervisor's ``cao launch`` fail while
the pane was visibly up and working:

* **Fix 3, the resumed ready screen.** Claude Code 2.1.322 draws a resumed
  conversation in the alternate screen, which has no scrollback. Once the
  conversation is longer than one screen the ``Claude Code v…`` banner has
  scrolled off it, so ``_handle_startup_prompts`` never matched and always ran to
  its ``provider_init_timeout`` outer cap. The fixtures are sanitised captures of
  a sandboxed live ``cao launch --resume-session-id`` on 2.1.322 (tmux backend):
  ``claude_code_1277_resumed_ready_long.txt`` (no banner, empty composer box at
  the bottom), ``claude_code_1277_resumed_ready_short.txt`` (banner still on
  screen) and ``claude_code_1277_resume_trust_dialog.txt`` (the trust dialog a
  resume in an untrusted folder draws first), all with default Claude settings;
  ``claude_code_1277_resumed_ready_long_vim_footer.txt`` is the same long resume
  under an operator configuration with ``tui: fullscreen`` and vim editor mode
  (an ``-- INSERT --`` footer and an agents indicator below the composer box).
* **Fix 2, a busy supervisor at the deadline.** A user typing into the pane
  during init made the supervisor PROCESSING, and the readiness wait accepts
  only IDLE/COMPLETED/WAITING_USER_ANSWER, so init "timed out" and the rollback
  tore the session down. PROCESSING is now accepted at the (unchanged) deadline
  only with evidence that real work started: the REPL was seen ready during this
  init, or the terminal made a CAO call recorded in the database.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from test.services.test_session_teardown_atomic import real_db  # noqa: F401 -- fixture
from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

FIXTURES = Path(__file__).parent / "fixtures"
RESUMED_LONG = (FIXTURES / "claude_code_1277_resumed_ready_long.txt").read_text()
RESUMED_LONG_VIM = (FIXTURES / "claude_code_1277_resumed_ready_long_vim_footer.txt").read_text()
RESUMED_SHORT = (FIXTURES / "claude_code_1277_resumed_ready_short.txt").read_text()
RESUME_TRUST = (FIXTURES / "claude_code_1277_resume_trust_dialog.txt").read_text()
RESUME_ID = "00000000-0000-4000-8000-000000001277"

# The launch command as the shell echoes it before Claude takes over the screen:
# a ``>`` at the start of a line, and no composer box. It must never read as ready.
ECHOED_COMMAND = (
    "user@host project % claude --dangerously-skip-permissions --resume "
    f"{RESUME_ID} --model haiku\n"
    "> `memory_store` is how you save a fact\n"
)


class _Clock:
    """A monotonic clock that advances one second per read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _provider(resume: bool = True) -> ClaudeCodeProvider:
    return ClaudeCodeProvider(
        "t1277", "sess", "win", resume_session_id=RESUME_ID if resume else None
    )


async def _run_handler(provider, frames, *, outer_timeout=60.0):
    """Run the real startup handler over ``frames`` (the last one repeats).

    Returns (backend mock, clock). Sleeps are instant; the clock moves one
    second per read, so reaching the outer cap takes ~``outer_timeout`` reads.
    """
    clock = _Clock()
    frames = list(frames)

    def _history(*_a, **_k):
        return frames.pop(0) if len(frames) > 1 else frames[0]

    with (
        patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        patch("cli_agent_orchestrator.providers.claude_code.time.monotonic", clock),
        patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep", AsyncMock()),
    ):
        backend.get_history.side_effect = _history
        await provider._handle_startup_prompts(idle_gap=20.0, outer_timeout=outer_timeout)
    return backend, clock


# --- Fix 3: the resumed ready screen ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("screen", [RESUMED_LONG, RESUMED_LONG_VIM], ids=["default", "vim_footer"])
async def test_resumed_ready_screen_without_banner_returns_without_the_outer_cap(caplog, screen):
    """Acceptance 4: a resumed REPL that is ready ends the handler at once."""
    provider = _provider()
    with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.providers.claude_code"):
        backend, clock = await _run_handler(provider, [screen])

    assert "outer cap" not in caplog.text
    assert clock.now < 10
    assert backend.get_history.call_count == 1
    backend.send_special_key.assert_not_called()
    backend.send_keys.assert_not_called()
    assert provider._startup_ready_seen is True


@pytest.mark.asyncio
async def test_resumed_ready_screen_with_banner_still_returns_early():
    """The short resume still shows the banner; that path keeps working."""
    provider = _provider()
    backend, clock = await _run_handler(provider, [RESUMED_SHORT])

    assert clock.now < 10
    assert provider._startup_ready_seen is True


@pytest.mark.asyncio
async def test_resume_answers_the_trust_dialog_before_accepting_the_composer():
    """The trust dialog is not a ready screen: it is answered first, then the REPL."""
    provider = _provider()
    backend, clock = await _run_handler(provider, [RESUME_TRUST, RESUMED_LONG])

    keys = [c.args[2] for c in backend.send_special_key.call_args_list]
    # "No, exit" is preselected in the captured dialog, so Down then Enter.
    assert keys == ["Down", "Enter"]
    # Two screens read, well inside the 60s cap (the clock ticks per read).
    assert backend.get_history.call_count == 2
    assert clock.now < 30
    assert provider._startup_ready_seen is True


# The live-captured Bedrock model-upgrade nudge (test_claude_code_unit.py).
_UPGRADE_NUDGE = (
    "Newer Opus model available\n"
    "Currently pinned: Opus 4.6\n"
    "Latest available: Opus 5 (au.anthropic.claude-opus-5)\n"
    "Update settings to use Opus 5? Claude Code will restart to apply.\n"
    "❯ 1. Yes\n"
    "  2. No\n"
    "Enter to confirm · Esc to cancel\n"
)


@pytest.mark.asyncio
async def test_a_dialog_on_the_same_screen_is_answered_before_the_composer_counts():
    """Contract: every known dialog is checked before the resumed composer.

    No capture shows a dialog and an empty composer together, so this frame is
    composed (the captured nudge above the captured resumed screen). It pins the
    order, so a dialog can never be left unanswered because the composer matched.
    """
    provider = _provider()
    backend, _clock = await _run_handler(provider, [_UPGRADE_NUDGE + RESUMED_LONG, RESUMED_LONG])

    backend.send_keys.assert_called_once_with("sess", "win", "2")
    assert provider._startup_ready_seen is True


@pytest.mark.asyncio
async def test_echoed_resume_command_is_not_a_ready_screen():
    """The echoed launch command (a line-start ``>``, no composer box) never matches."""
    provider = _provider()
    backend, clock = await _run_handler(provider, [ECHOED_COMMAND], outer_timeout=30.0)

    assert clock.now >= 30
    assert getattr(provider, "_startup_ready_seen", False) is False


@pytest.mark.asyncio
async def test_fresh_launch_does_not_use_the_resumed_composer_signal():
    """Only a ``--resume`` launch accepts the bannerless composer; a fresh one waits.

    A fresh launch always draws the banner, so the narrower rule is the safe one.
    """
    provider = _provider(resume=False)
    backend, clock = await _run_handler(provider, [RESUMED_LONG], outer_timeout=30.0)

    assert clock.now >= 30
    assert getattr(provider, "_startup_ready_seen", False) is False


# --- Fix 2: PROCESSING at the deadline with evidence of real work -----------


async def _initialize(provider, *, status_at_deadline, frames, during_init=None):
    """Run ``initialize()`` with the readiness wait timing out.

    ``frames`` feeds the real startup handler. ``during_init`` runs inside the
    readiness wait (the window in which a live pane makes its CAO calls). The
    status read after the wait returns ``status_at_deadline``.
    """
    clock = _Clock()
    frames = list(frames)

    async def _wait(*_a, **_k):
        if during_init is not None:
            during_init()
        return False

    def _history(*_a, **_k):
        return frames.pop(0) if len(frames) > 1 else frames[0]

    with (
        patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting"),
        patch.object(ClaudeCodeProvider, "_build_claude_command", return_value="claude"),
        patch(
            "cli_agent_orchestrator.providers.claude_code.wait_for_shell",
            AsyncMock(return_value=True),
        ),
        patch(
            "cli_agent_orchestrator.providers.claude_code.wait_until_status",
            AsyncMock(side_effect=_wait),
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            return_value=status_at_deadline,
        ),
        patch.object(provider, "wait_until_input_ready", AsyncMock(return_value=True)),
        patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        patch("cli_agent_orchestrator.providers.claude_code.time.monotonic", clock),
        patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep", AsyncMock()),
    ):
        backend.get_history.side_effect = _history
        return await provider.initialize()


def _record_call_from(caller_id):
    """Persist what an ``assign`` from ``caller_id`` leaves behind: a launch record."""
    database.create_launch_record(
        terminal_id="w1277",
        session_name="sess",
        provider="claude_code",
        target_name="developer",
        target_kind=None,
        launched_model=None,
        model_honored=None,
        caller_id=caller_id,
    )


def _own_ephemeral_profile(owner_id):
    """Persist what ``create_ephemeral_agent`` from ``owner_id`` leaves behind."""
    now = datetime.now(timezone.utc)
    with database.SessionLocal() as db:
        db.add(
            database.EphemeralAgentModel(
                name="Band-purpose-1277",
                owner_kind="terminal",
                owner_id=owner_id,
                session_name="sess",
                state="defined",
                provider="claude_code",
                effective_tools="[]",
                created_at=now,
                expires_at=now + timedelta(minutes=15),
                spec_sha256="0" * 64,
                profile_sha256="0" * 64,
                audit_path="audit.json",
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_busy_after_ready_screen_is_accepted_at_the_deadline(real_db, caplog):
    """Acceptance 3: input typed into a REPL seen ready is real work, not an init failure."""
    provider = _provider()
    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.providers.claude_code"):
        result = await _initialize(
            provider, status_at_deadline=TerminalStatus.PROCESSING, frames=[RESUMED_LONG]
        )

    assert result is True
    assert provider._initialized is True
    assert "PROCESSING" in caplog.text


@pytest.mark.asyncio
async def test_busy_with_a_recorded_cao_call_is_accepted_at_the_deadline(real_db):
    """Acceptance 3: a terminal that already assigned a worker is up, whatever the screen."""
    provider = _provider()
    result = await _initialize(
        provider,
        status_at_deadline=TerminalStatus.PROCESSING,
        frames=["Loading..."],
        during_init=lambda: _record_call_from("t1277"),
    )

    assert result is True


@pytest.mark.asyncio
async def test_a_call_recorded_before_this_init_is_not_evidence(real_db):
    """Terminal ids are reused: a record left by an earlier ``t1277`` does not count."""
    _record_call_from("t1277")
    provider = _provider()
    with pytest.raises(TimeoutError, match="Claude Code initialization timed out"):
        await _initialize(
            provider, status_at_deadline=TerminalStatus.PROCESSING, frames=["Loading..."]
        )


@pytest.mark.asyncio
async def test_busy_with_an_owned_ephemeral_profile_is_accepted_at_the_deadline(real_db):
    """``create_ephemeral_agent`` from the pane is recorded evidence too."""
    provider = _provider()
    result = await _initialize(
        provider,
        status_at_deadline=TerminalStatus.PROCESSING,
        frames=["Loading..."],
        during_init=lambda: _own_ephemeral_profile("t1277"),
    )

    assert result is True


@pytest.mark.asyncio
async def test_busy_without_evidence_still_times_out(real_db):
    """PROCESSING alone is not evidence: CAO's own launch keystrokes can latch it."""
    provider = _provider()
    with pytest.raises(TimeoutError, match="Claude Code initialization timed out"):
        await _initialize(
            provider,
            status_at_deadline=TerminalStatus.PROCESSING,
            frames=["Loading..."],
            during_init=lambda: _record_call_from("someone-else"),
        )


@pytest.mark.asyncio
async def test_ready_seen_but_not_busy_still_times_out(real_db):
    """Evidence only rescues a busy terminal; any other missed status is still a failure."""
    provider = _provider()
    with pytest.raises(TimeoutError, match="Claude Code initialization timed out"):
        await _initialize(
            provider, status_at_deadline=TerminalStatus.UNKNOWN, frames=[RESUMED_LONG]
        )
