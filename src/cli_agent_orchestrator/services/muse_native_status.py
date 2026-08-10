"""The Muse ``/status`` panel, parsed as pre-task identity evidence.

Muse's managed-v2 readiness is observed from the provider's own ``/status``
panel rather than from a SessionStart hook (Claude) or a minting bootstrap
(Kimi/Codex).  The panel is the provider-owned surface that names the exact
running session, model, reasoning effort, agent profile, provider, cwd, and
pre-task run state — the coordinator no-prompt canary on 2026-08-10 rendered
exactly this panel for a launched ``muse resume <id>`` session (exact
session ``adcb742e-2ab5-4239-9fe2-b503005db341``, model
``muse-spark-1.2-contributor``, reasoning ``high``, agent profile
``native-basic``, provider ``meta``, exact cwd, ``Run: idle``,
``0 tokens / 0 turns``).

The panel is *printed output*: Muse writes it into the output area and the
composer line stays rendered at the bottom, so no modal dismiss is required
after observation.  That is also why the parse is strict: a panel that is
missing, ambiguous, truncated, or naming anything other than the minted
session is not readiness, and nothing is admitted on it.

The Reasoning line is present on the meta provider (the canary read it back)
and absent on the echo provider, so the parser treats it as
present-when-rendered and the caller requires it exactly when an effort was
requested.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from cli_agent_orchestrator.services import provider_contracts

#: The exact command typed into the pane to render the status panel.
STATUS_COMMAND = "/status"

#: The agent-profile identity the installed build renders when launched
#: without ``--preset`` — the built-in default preset.  The launch never
#: passes ``--preset`` (only ``native-basic`` and ``miniswe`` exist and
#: neither is a CAO profile), so this is the profile identity the panel
#: must name.  It is a Muse-side fact, distinct from the CAO profile
#: family recorded in the roster and from the profile material digests
#: carried through the launch.
DEFAULT_AGENT_PROFILE = "native-basic"

#: Schema of the parsed, required panel evidence recorded in the
#: bootstrap and the readiness receipt.
STATUS_PANEL_SCHEMA = "cao-muse-status-panel-v1"

#: Panel row labels.  The Reasoning label is the meta provider's rendering
#: of the requested effort; the alternate spelling is accepted because the
#: coordinator canary recorded the value ("reasoning high") without a
#: machine capture of the exact label, and a panel that renders the value
#: under either label is the same evidence.
_REASONING_LABELS = ("Reasoning:", "Reasoning effort:")

_REQUIRED_LABELS = (
    "Session:",
    "Model:",
    "Agent profile:",
    "Model provider:",
    "Directory:",
    "Run:",
    "Token usage:",
)

#: Pre-task state required before any durable readiness may be published:
#: the panel's own statement that the session is idle with zero turns.
PRE_TASK_RUN_STATE = "idle"
PRE_TASK_TOKEN_USAGE = (0, 0)


class MuseStatusParseError(ValueError):
    """The captured screen is not a usable ``/status`` panel."""


class MuseStatusMismatch(ValueError):
    """The panel parsed, but it does not name the claimed pre-task session."""


def _strip_panel_row(row: str) -> str:
    """One composited row with its box-drawing furniture removed.

    The installed build renders the panel inside a box drawn with
    ``╭│╰╯─`` characters; captures return the composited viewport without
    escape sequences (``capture-pane -p``), so the furniture is literal.
    """
    cleaned = row.strip()
    for marker in ("│", "╭", "╰"):
        cleaned = cleaned.replace(marker, "")
    cleaned = cleaned.strip("─ ")
    return cleaned.strip()


def parse_status_panel(rows: Sequence[str]) -> dict[str, Any]:
    """Parse one ``/status`` capture into typed fields, or refuse.

    Raises:
        MuseStatusParseError: The capture is empty, has no session line,
            has more than one session line (ambiguous — the capture cannot
            prove which session the pane runs), or lacks a required line.
    """
    fields: dict[str, list[str]] = {}
    reasoning_values: list[str] = []
    for raw in rows:
        row = _strip_panel_row(raw)
        if not row:
            continue
        for label in _REASONING_LABELS:
            if row.startswith(label):
                reasoning_values.append(row[len(label) :].strip())
                break
        else:
            for label in _REQUIRED_LABELS:
                if row.startswith(label):
                    fields.setdefault(label, []).append(row[len(label) :].strip())
                    break

    session_lines = fields.get("Session:", [])
    if not session_lines:
        raise MuseStatusParseError(
            "the captured screen carries no /status Session line; the pane may still be "
            "booting or the capture may be empty"
        )
    if len(session_lines) > 1:
        raise MuseStatusParseError(
            "the captured screen carries more than one /status Session line, so it cannot "
            "prove which session the pane runs; refusing rather than guessing"
        )
    missing = [label for label in _REQUIRED_LABELS if label not in fields]
    if missing:
        raise MuseStatusParseError(
            "the /status panel is incomplete: missing "
            + ", ".join(sorted(missing))
            + "; a truncated capture is not an observation"
        )

    usage = fields["Token usage:"][0]
    match = re.fullmatch(r"(\d+)\s+tokens\s*/\s*(\d+)\s+turns", usage)
    if match is None:
        raise MuseStatusParseError(
            f"the /status Token usage line is not the expected 'N tokens / N turns' "
            f"shape: {usage!r}"
        )
    tokens, turns = int(match.group(1)), int(match.group(2))

    return {
        "schema": STATUS_PANEL_SCHEMA,
        "session_id": session_lines[0].strip(),
        "model": fields["Model:"][0].strip(),
        "reasoning": reasoning_values[0] if reasoning_values else None,
        "agent_profile": fields["Agent profile:"][0].strip(),
        "model_provider": fields["Model provider:"][0].strip(),
        "directory": fields["Directory:"][0].strip(),
        "run": fields["Run:"][0].strip(),
        "tokens": tokens,
        "turns": turns,
    }


def require_pre_task_status(
    parsed: Mapping[str, Any],
    *,
    session_id: str,
    expected_model: str,
    expected_effort: Optional[str],
    working_directory: str,
    expected_profile_identity: str,
) -> dict[str, Any]:
    """Require the parsed panel to name exactly the claimed pre-task session.

    Every mismatch raises :class:`MuseStatusMismatch` naming the exact
    field and the observed vs required values, so a blocked launch records
    *which* evidence was wrong.  ``expected_effort`` is the requested
    effort when the route selected one (the panel must render it) and
    ``None`` for a provider-default route (no effort line is required and
    none is claimed observed).
    """
    mismatches: list[str] = []

    observed_session = str(parsed.get("session_id") or "")
    session_matches = observed_session == session_id
    if not session_matches:
        mismatches.append(
            f"session: the panel names {observed_session!r}, not the minted " f"{session_id!r}"
        )

    observed_model = str(parsed.get("model") or "")
    model_matches = bool(expected_model) and observed_model == expected_model
    if not model_matches:
        mismatches.append(
            f"model: the panel names {observed_model!r}, not the requested " f"{expected_model!r}"
        )

    observed_effort = parsed.get("reasoning")
    effort_matches: bool
    if expected_effort and expected_effort != provider_contracts.EFFORT_PROVIDER_DEFAULT:
        effort_matches = bool(observed_effort) and str(observed_effort) == expected_effort
        if not effort_matches:
            mismatches.append(
                f"effort: the panel renders reasoning {observed_effort!r}, not the "
                f"requested {expected_effort!r}"
            )
    else:
        # A provider-default route requests no effort (the sentinel is not
        # an effort to observe); the panel may render the provider's own
        # default, which is not an observed request.
        effort_matches = True

    observed_profile = str(parsed.get("agent_profile") or "")
    profile_matches = observed_profile == expected_profile_identity
    if not profile_matches:
        mismatches.append(
            f"agent profile: the panel names {observed_profile!r}, not the expected "
            f"{expected_profile_identity!r}"
        )

    observed_provider = str(parsed.get("model_provider") or "")
    provider_matches = observed_provider == "meta"
    if not provider_matches:
        mismatches.append(f"provider: the panel names {observed_provider!r}, not 'meta'")

    observed_directory = str(parsed.get("directory") or "")
    directory_matches = observed_directory == working_directory
    if not directory_matches:
        mismatches.append(
            f"cwd: the panel names {observed_directory!r}, not the bound " f"{working_directory!r}"
        )

    run = str(parsed.get("run") or "")
    idle = run == PRE_TASK_RUN_STATE
    if not idle:
        mismatches.append(f"run state: the panel reads {run!r}, not {PRE_TASK_RUN_STATE!r}")

    tokens = parsed.get("tokens")
    turns = parsed.get("turns")
    zero_turns = tokens == 0 and turns == 0
    if not zero_turns:
        mismatches.append(
            f"pre-task usage: the panel reads {tokens} tokens / {turns} turns, not "
            f"{PRE_TASK_TOKEN_USAGE[0]} tokens / {PRE_TASK_TOKEN_USAGE[1]} turns"
        )

    if mismatches:
        raise MuseStatusMismatch(
            "the /status panel does not describe the claimed pre-task session: "
            + "; ".join(mismatches)
        )

    return {
        "schema": STATUS_PANEL_SCHEMA,
        "session_matches": True,
        "model_matches": True,
        "effort_matches": True,
        "profile_matches": True,
        "provider_matches": True,
        "directory_matches": True,
        "idle": True,
        "zero_turns": True,
        "observed": {
            "session_id": observed_session,
            "model": observed_model,
            "effort": observed_effort,
            "agent_profile": observed_profile,
            "model_provider": observed_provider,
            "directory": observed_directory,
            "run": run,
            "tokens": tokens,
            "turns": turns,
        },
    }
