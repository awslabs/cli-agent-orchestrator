"""The Muse ``/status`` panel, parsed as pre-task identity evidence.

Muse's managed-v2 readiness is observed from the provider's own ``/status``
panel rather than from a SessionStart hook (Claude) or a minting bootstrap
(Kimi/Codex).  The panel is the provider-owned surface that names the exact
running session, model, reasoning effort, agent profile, provider, cwd, and
pre-task run state — the coordinator no-prompt canary on 2026-08-10 rendered
exactly this panel for a launched ``muse resume <id>`` session (exact
session ``adcb742e-2ab5-4239-9fe2-b503005db341``, agent profile
``native-basic``, provider ``meta``, exact cwd, ``Run: idle``,
``0 tokens / 0 turns``).

The installed 0.1.0-R708.1 meta panel renders model and effort together in
one line::

    Model: muse-spark-1.2-contributor (reasoning high)

There is no separate Reasoning row on that build; the echo provider renders
a bare model with no effort at all.  The parser therefore splits an exact
trailing `` (reasoning <effort>)`` suffix off the Model value into canonical
``model`` and ``reasoning`` fields, and treats a separate
``Reasoning:``/``Reasoning effort:`` row (a separately-supported variant)
as an additional source that must converge with the suffix or be refused as
ambiguous.

The panel is *printed output*: Muse writes it into the output area and the
composer line stays rendered at the bottom, so no modal dismiss is required
after observation.  That is also why the parse is strict: a panel that is
missing, ambiguous, truncated, or naming anything other than the expected
(or a canonical provider-generated) session is not readiness, and nothing is
admitted on it.
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

#: Panel row labels for a *separate* reasoning row.  The installed
#: 0.1.0-R708.1 meta panel does NOT render one — it puts the effort inside
#: the Model line — but some builds do, and a duplicate source must be
#: handled strictly (identical values converge; conflicting values refuse
#: as ambiguous) rather than ignored.  Both spellings are accepted because
#: a panel that renders the value under either label is the same evidence.
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

#: The exact reasoning-effort vocabulary the installed build accepts for
#: ``--reasoning-effort`` (``muse --help``).  A suffix or separate value
#: outside this set is malformed evidence and is refused, never guessed.
_MUSE_EFFORT_VOCABULARY = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "ultra"})

#: The exact trailing `` (reasoning <effort>)`` suffix the installed meta
#: panel appends to the Model value.  Only this exact form is split off;
#: any other parenthetical remains part of the model value.
_REASONING_SUFFIX = re.compile(r"^(.*?)\s+\(reasoning\s+([^()]*)\)$")

#: Pre-task state required before any durable readiness may be published:
#: the panel's own statement that the session is idle with zero turns.
PRE_TASK_RUN_STATE = "idle"
PRE_TASK_TOKEN_USAGE = (0, 0)


def _split_model_reasoning(value: str) -> tuple[str, Optional[str]]:
    """Split an optional exact `` (reasoning <effort>)`` suffix off a Model.

    Only the exact installed form is split; any other parenthetical is part
    of the model value (never guessed at).  A suffix that looks like the
    form but carries an empty or unknown effort is refused rather than
    guessed, because binding a session on an effort nobody selected is the
    failure this parse exists to prevent.
    """
    value = value.strip()
    match = _REASONING_SUFFIX.fullmatch(value)
    if match is None:
        return value, None
    model, effort = match.group(1).strip(), match.group(2).strip()
    if not effort or effort not in _MUSE_EFFORT_VOCABULARY:
        raise MuseStatusParseError(
            f"the /status Model value carries a malformed reasoning suffix: {value!r}; "
            "refusing rather than guessing an effort from arbitrary parenthetical text"
        )
    return model, effort


def _converge_reasoning(sources: Sequence[Optional[str]]) -> Optional[str]:
    """Converge identical reasoning values, or refuse conflicting ones.

    The model-line suffix and a separate Reasoning row are two sources for
    the same fact.  Identical values agree and converge; conflicting values
    mean the capture cannot prove which effort the session runs, so it is
    refused as ambiguous rather than resolved by a guess.
    """
    present = [source for source in sources if source is not None]
    if not present:
        return None
    first = present[0]
    if any(source != first for source in present[1:]):
        raise MuseStatusParseError(
            "the /status panel reports conflicting reasoning values "
            f"{sorted(set(present))}; refusing rather than guessing which is the "
            "session's effort"
        )
    return first


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

    The Model value may carry the installed `` (reasoning <effort>)``
    suffix, which is split into the canonical ``model`` and ``reasoning``
    fields.  A separate ``Reasoning:``/``Reasoning effort:`` row is an
    additional source that must converge with the suffix or be refused as
    ambiguous.

    Raises:
        MuseStatusParseError: The capture is empty, has no session line,
            has more than one of any required singleton line (including
            the Session line — the capture cannot prove which session the
            pane runs), carries a malformed reasoning suffix or value, or
            lacks a required line.
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

    # Every required field is a singleton; a second one is ambiguity, never
    # a value to pick.  The Session line is the identity and the rest are
    # the route facts, and a capture that cannot prove any one of them
    # proves nothing.
    duplicates = [label for label, values in fields.items() if len(values) > 1]
    if duplicates:
        raise MuseStatusParseError(
            "the /status panel renders more than one "
            + ", ".join(sorted(duplicates))
            + " line, so it cannot prove the session it names; refusing rather than "
            "choosing a value"
        )
    missing = [label for label in _REQUIRED_LABELS if label not in fields]
    if missing:
        raise MuseStatusParseError(
            "the /status panel is incomplete: missing "
            + ", ".join(sorted(missing))
            + "; a truncated capture is not an observation"
        )

    # Model + optional exact reasoning suffix, then the separate-row
    # reasoning values (each validated against the effort vocabulary).
    model, model_reasoning = _split_model_reasoning(fields["Model:"][0])
    label_reasoning: list[Optional[str]] = []
    for value in reasoning_values:
        cleaned = value.strip()
        if not cleaned or cleaned not in _MUSE_EFFORT_VOCABULARY:
            raise MuseStatusParseError(
                f"the /status reasoning value is not a known Muse effort: {value!r}; "
                "refusing rather than guessing"
            )
        label_reasoning.append(cleaned)
    reasoning = _converge_reasoning([model_reasoning, *label_reasoning])

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
        "session_id": fields["Session:"][0].strip(),
        "model": model,
        "reasoning": reasoning,
        "agent_profile": fields["Agent profile:"][0].strip(),
        "model_provider": fields["Model provider:"][0].strip(),
        "directory": fields["Directory:"][0].strip(),
        "run": fields["Run:"][0].strip(),
        "tokens": tokens,
        "turns": turns,
    }


def validate_discovered_session_id(session_id: Any) -> str:
    """Return a provider-generated session id proven to be a canonical UUID.

    The fresh launch discovers the id from the panel; a session id that is
    not a canonical lowercase UUID cannot be a Muse session identity, so it
    is refused rather than bound.
    """
    if not isinstance(session_id, str) or not session_id:
        raise MuseStatusMismatch(
            f"the /status panel names a session id that is not a canonical UUID: " f"{session_id!r}"
        )
    import uuid as _uuid_module

    try:
        parsed = _uuid_module.UUID(session_id)
    except ValueError as exc:
        raise MuseStatusMismatch(
            f"the /status panel names a session id that is not a canonical UUID: " f"{session_id!r}"
        ) from exc
    if str(parsed) != session_id:
        raise MuseStatusMismatch(
            f"the /status panel names a session id that is not a canonical lowercase "
            f"UUID: {session_id!r}"
        )
    return session_id


def require_pre_task_status(
    parsed: Mapping[str, Any],
    *,
    session_id: Optional[str],
    expected_model: str,
    expected_effort: Optional[str],
    working_directory: str,
    expected_profile_identity: str,
) -> dict[str, Any]:
    """Require the parsed panel to name exactly the claimed pre-task session.

    ``session_id`` is ``None`` on a fresh launch, where the id is
    *discovered*: the panel's session id is validated as a canonical UUID
    and returned as the identity.  When a ``session_id`` is supplied (an
    exact restore), the panel must name exactly it.  Every mismatch raises
    :class:`MuseStatusMismatch` naming the exact field and the observed vs
    required values, so a blocked launch records *which* evidence was
    wrong.  ``expected_effort`` is the requested effort when the route
    selected one (the panel must render it) and ``None`` for a
    provider-default route (no effort line is required and none is claimed
    observed).
    """
    mismatches: list[str] = []

    observed_session = str(parsed.get("session_id") or "")
    if session_id is None:
        # Fresh launch: the provider generated the id and this panel names
        # it.  Requiring it to be a canonical UUID is the discovery proof.
        validate_discovered_session_id(observed_session)
        session_matches = True
    else:
        session_matches = observed_session == session_id
        if not session_matches:
            mismatches.append(
                f"session: the panel names {observed_session!r}, not the expected "
                f"{session_id!r}"
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
