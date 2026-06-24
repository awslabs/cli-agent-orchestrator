"""OMP CLI provider implementation.

This module provides the :class:`OmpCliProvider` for integrating with the OMP
CLI — a terminal-native AI agent invoked via the ``omp`` binary. OMP is treated
as a generic TUI agent: like Hermes, its concrete output format is not
hard-known at integration time, so the status-detection and extraction regexes
are **environment-overridable** (``CAO_OMP_*_REGEX``). The defaults below are
reasonable placeholders calibrated against representative TUI agents; the
develop / test stages refine them against real ``omp`` output by setting the
env vars rather than editing source constants.

Provider responsibilities (see :class:`BaseProvider`):

- ``initialize`` — probe for the ``omp`` binary on ``$PATH``, launch the TUI
  inside the tmux window, wait for IDLE / COMPLETED.
- ``get_status`` — classify the terminal buffer into IDLE / PROCESSING /
  COMPLETED / WAITING_USER_ANSWER / ERROR / UNKNOWN.
- ``extract_last_message_from_script`` — pull the agent's last response out of
  the scrollback.
- ``exit_cli`` / ``cleanup`` — tear the session down.

The provider intentionally takes the lowest-friction integration path: OMP has
no native agent-config format yet, so installation is context-file-only (see
``install_service``), tool restrictions are advisory (no tool_mapping entry),
and the CAO skill catalog is delivered via the context file rather than a
native launch flag. This mirrors the early-stage stance that ``claude_code``
and ``hermes`` took and keeps the change purely additive — no existing
provider's behaviour is touched.
"""

import logging
import os
import re
import shlex
import shutil
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for OMP CLI provider-specific errors."""

    pass


# =============================================================================
# Regex patterns — env-overridable so calibration against real `omp` output
# never requires editing these source constants (see docs/omp-cli.md).
# =============================================================================

# CSI / OSC escape sequences. Broader than the SGR-only pattern some providers
# use because OMP's real escape behaviour is unknown; strips cursor moves and
# OSC title updates so the structural checks below fire on rendered text.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"

# The idle prompt OMP renders when waiting for the next user message. Defaults
# to a generic ``omp>`` / ``OMP>`` prompt line; override with
# ``CAO_OMP_IDLE_PROMPT_REGEX`` once the real prompt glyph is observed.
IDLE_PROMPT_PATTERN = os.environ.get(
    "CAO_OMP_IDLE_PROMPT_REGEX", r"^\s*(?:omp|OMP)>\s*$"
)
IDLE_PROMPT_PATTERN_LOG = os.environ.get("CAO_OMP_IDLE_LOG_REGEX", r"(?:omp|OMP)>")

# Spinner / in-flight indicator shown while OMP is generating. Defaults cover
# the braille-spinner + ellipsis vocabulary shared by most TUI agents.
PROCESSING_PATTERN = os.environ.get(
    "CAO_OMP_PROCESSING_REGEX",
    r"(?:Thinking|Working|musing\.\.\.|interrupt|cancel|"
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✢✽✻✳][^\n]*…)",
)

# Approval / selection dialogs that block the agent pending operator input.
WAITING_PROMPT_PATTERN = os.environ.get(
    "CAO_OMP_WAITING_REGEX",
    r"(?:Approve|Allow|Proceed|Confirm|permission)[^\n]*(?:y/n|yes/no|\[y/N\])"
    r"|↑/↓\s*(?:to )?(?:select|navigate)",
)

ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|omp .*failed:)"

# User / assistant message markers used for response extraction. Defaults are
# generic; override once OMP's concrete markers are known.
USER_PREFIX_PATTERN = os.environ.get("CAO_OMP_USER_PREFIX_REGEX", r"^\s*(?:You|User|●)[:,]?\s")
ASSISTANT_HEADER_PATTERN = os.environ.get(
    "CAO_OMP_ASSISTANT_HEADER_REGEX", r"^\s*(?:Assistant|OMP|omp)[:,]\s"
)

# Lines that are TUI chrome (status bar, separators, prompt) and should be
# stripped from an extracted response region.
SEPARATOR_PATTERN = r"^[\s─━═-]{10,}$"


def _strip_ansi(text: str) -> str:
    return re.sub(ANSI_CODE_PATTERN, "", text)


def _is_idle_line(line: str) -> bool:
    return re.search(IDLE_PROMPT_PATTERN, line) is not None


def _is_chrome_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or _is_idle_line(stripped)
        or re.match(SEPARATOR_PATTERN, stripped) is not None
        or re.search(PROCESSING_PATTERN, stripped, re.IGNORECASE) is not None
        or re.search(ASSISTANT_HEADER_PATTERN, stripped) is not None
        or re.search(USER_PREFIX_PATTERN, stripped) is not None
    )


class OmpCliProvider(BaseProvider):
    """Provider for the OMP CLI (``omp``) — a generic TUI agent adapter.

    Manages the lifecycle of an OMP TUI session inside a tmux window:
    initialization, status detection, response extraction, and cleanup. Status
    detection uses env-overridable regexes so the patterns can be recalibrated
    against real ``omp`` output without a code change.

    Attributes:
        terminal_id: Unique identifier for this terminal instance.
        session_name: Name of the tmux session containing this terminal.
        window_name: Name of the tmux window for this terminal.
        _agent_profile: Optional CAO agent profile name (role context is
            delivered via the install-time context file; the profile is only
            consulted for optional model / binary overrides).
        _model: Optional model override forwarded as ``--model`` at launch.
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize the OMP CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal.
            session_name: Name of the tmux session.
            window_name: Name of the tmux window.
            agent_profile: Optional CAO agent profile name. The profile's role
                body reaches OMP via the context file; the profile is consulted
                here only for an optional model override.
            allowed_tools: Optional list of CAO tool names the agent is allowed
                to use. OMP has no native ``--disallowedTools`` flag, so
                restrictions are advisory (no tool_mapping entry).
            model: Optional model override (e.g. ``"gpt-5"``).
            skill_prompt: Optional skill catalog text. OMP does not inject a
                CAO runtime skill catalog at launch (skills reach the agent via
                the context file); the value is accepted for API symmetry and
                ignored with a log line.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._model = model
        # Turn counter: 0 (fresh spawn) → IDLE; ≥1 once input has been
        # delivered → COMPLETED when the agent returns to a non-processing
        # state. OMP's TUI cannot otherwise distinguish "just spawned" from
        # "turn delivered" from the buffer alone, mirroring cursor_cli.
        self._turns: int = 0

    @property
    def paste_enter_count(self) -> int:
        """OMP submits on a single Enter after bracketed paste."""
        return 1

    def mark_input_received(self) -> None:
        """Record that a turn has been delivered to the agent (see _turns)."""
        self._turns += 1

    def _build_launch_command(self) -> str:
        """Build the ``omp`` launch command.

        Resolves the ``omp`` binary on ``$PATH`` (raises :class:`ProviderError`
        if missing), then forwards an optional ``--model`` override. The CAO
        agent profile's role body is **not** passed on the command line — it is
        already materialised into the context file at install time, which OMP
        reads at startup.

        Returns:
            A properly shell-escaped launch command string.
        """
        binary = shutil.which("omp")
        if not binary:
            raise ProviderError(
                "OMP CLI not found: 'omp' is not on $PATH. Install OMP CLI before launching."
            )

        model = self._model
        if model is None and self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
                if profile and profile.model:
                    model = profile.model
            except Exception as exc:  # noqa: BLE001 — profile load is best-effort here
                logger.warning("Could not load agent profile %r for model override: %s", self._agent_profile, exc)

        command_parts = [binary]
        if model:
            command_parts.extend(["--model", model])

        if self._skill_prompt:
            logger.info(
                "OMP provider does not inject a CAO runtime skill catalog at launch; "
                "skills reach the agent via the install-time context file."
            )

        if self._allowed_tools and "*" not in self._allowed_tools:
            logger.info(
                "OMP provider has no CAO-native tool restriction flag; "
                "restrictions are advisory (delivered via the context file)."
            )

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Start the OMP TUI and wait for the idle / completed splash.

        Returns:
            True if initialization completed successfully.

        Raises:
            TimeoutError: If the shell or OMP TUI does not reach IDLE /
                COMPLETED in time.
            ProviderError: If the ``omp`` binary is not on ``$PATH``.
        """
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_launch_command()
        get_backend().send_keys(self.session_name, self.window_name, command)

        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
        ):
            raise TimeoutError("OMP CLI initialization timed out after 120 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Classify the terminal buffer into a terminal status.

        Priority order:

        1. Empty buffer → UNKNOWN.
        2. WAITING_USER_ANSWER — an approval / selection dialog is active and
           no idle prompt follows it (the user already dismissed it).
        3. ERROR — an error pattern in the recent tail.
        4. PROCESSING — a processing indicator in the tail with no following
           idle prompt.
        5. COMPLETED — idle prompt present and at least one turn delivered.
        6. IDLE — idle prompt present, fresh spawn.
        7. UNKNOWN — fallback.

        Args:
            output: Raw terminal output (rolling buffer, up to ~8KB).

        Returns:
            Current :class:`TerminalStatus`.
        """
        if not output:
            return TerminalStatus.UNKNOWN

        clean = _strip_ansi(strip_terminal_escapes(output))
        lines = clean.splitlines()
        tail_lines = lines[-30:]
        tail_output = "\n".join(tail_lines)
        bottom_lines = [line for line in tail_lines if line.strip()][-8:]
        bottom_output = "\n".join(bottom_lines)

        has_idle_prompt = any(_is_idle_line(line.strip()) for line in bottom_lines)

        # ── 2. WAITING_USER_ANSWER ───────────────────────────────────────────
        waiting_matches = list(re.finditer(WAITING_PROMPT_PATTERN, clean, re.IGNORECASE))
        if waiting_matches:
            last_waiting_end = waiting_matches[-1].end()
            if not re.search(IDLE_PROMPT_PATTERN, clean[last_waiting_end:], re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER

        # ── 3. ERROR ─────────────────────────────────────────────────────────
        if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
            return TerminalStatus.ERROR

        # ── 4. PROCESSING ────────────────────────────────────────────────────
        # Position guard: a processing indicator is only authoritative when no
        # idle prompt appears after it (otherwise it is stale scrollback from a
        # completed turn). ``re.MULTILINE`` is required so the line-anchored
        # idle pattern can match a prompt mid-buffer, not just at the very end.
        proc_matches = list(re.finditer(PROCESSING_PATTERN, clean, re.IGNORECASE))
        if proc_matches:
            last_proc_end = proc_matches[-1].end()
            if not re.search(IDLE_PROMPT_PATTERN, clean[last_proc_end:], re.MULTILINE):
                return TerminalStatus.PROCESSING

        # ── 5/6. IDLE / COMPLETED ────────────────────────────────────────────
        if has_idle_prompt:
            return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

        # ── 7. UNKNOWN ───────────────────────────────────────────────────────
        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        """Return the idle prompt pattern used for log file monitoring."""
        return IDLE_PROMPT_PATTERN_LOG

    def _extract_response(self, clean_output: str) -> str:
        """Extract the last OMP response from the cleaned buffer.

        Anchors on the last user message (or, failing that, the last assistant
        header) and returns the text up to the trailing idle prompt, with TUI
        chrome stripped. A leading assistant-header marker (``OMP:`` /
        ``Assistant:``) on a response line is stripped off, but any content on
        the same line is preserved — OMP may emit the marker and the response
        body on one line.
        """
        user_matches = list(re.finditer(USER_PREFIX_PATTERN, clean_output, re.MULTILINE))
        if user_matches:
            last_user = user_matches[-1]
            line_end = clean_output.find("\n", last_user.end())
            search_region = (
                clean_output[line_end + 1 :] if line_end != -1 else clean_output[last_user.end() :]
            )
        else:
            header_matches = list(re.finditer(ASSISTANT_HEADER_PATTERN, clean_output, re.MULTILINE))
            if not header_matches:
                raise ValueError("No OMP response found - no assistant header or user message detected")
            search_region = clean_output[header_matches[-1].end() :]

        end_match = re.search(IDLE_PROMPT_PATTERN, search_region, re.MULTILINE)
        candidate_text = search_region[: end_match.start()] if end_match else search_region

        response_lines: list[str] = []
        for raw_line in candidate_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            if _is_idle_line(stripped) or re.match(SEPARATOR_PATTERN, stripped):
                continue
            if re.search(PROCESSING_PATTERN, stripped, re.IGNORECASE):
                continue
            # Strip a leading assistant-header marker but keep any same-line
            # content that follows it.
            stripped = re.sub(r"^\s*(?:Assistant|OMP|omp)[:,]\s*", "", raw_line).rstrip()
            if stripped:
                response_lines.append(stripped)

        response = "\n".join(response_lines).strip()
        if not response:
            raise ValueError("Empty OMP response - no content found")
        return response

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last OMP assistant response from terminal output."""
        clean_output = _strip_ansi(strip_terminal_escapes(script_output))
        return self._extract_response(clean_output)

    def exit_cli(self) -> str:
        """Return the command to exit the OMP TUI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up OMP provider state."""
        self._initialized = False
