"""Claude native readiness: the provider says it started, or it did not.

A managed native launch may not submit a task byte until the provider is
genuinely ready. For the Kimi native path that proof is an ACP reply —
the transport answered, so the session exists. Claude's TUI has no such
channel, and every cheap substitute is a lie of a different shape:

* *the pane exists* proves a process was spawned, not that Claude
  initialised, authenticated, or adopted the session id it was given;
* *N seconds elapsed* proves nothing about anything, and turns a slow
  machine into a silent misdelivery;
* *a caller passed ready=True* proves only that a caller said so, which
  is the class of evidence this whole surface exists to eliminate;
* *the composer rendered* is closer, but a rendered composer belongs to
  whatever session Claude actually opened — including the wrong one, if
  a resume fell back to a picker.

The only artefact that names the exact session is Claude's own
``SessionStart`` hook, which carries the ``session_id`` in its payload.
This module arranges for that hook to be delivered somewhere this process
can read, and then waits for a record whose session id is the uuid that
was minted before launch.

The delivery is a generation-private append-only file rather than a
socket or a network listener. Three reasons: the hook is a short-lived
subprocess and appending a line is the least it can be asked to do; a
file has no accept loop to leak if the launch dies mid-way; and the
readiness record is then durable evidence a recovery can re-read rather
than an event that had to be caught live.

Nothing here writes to the pane or spends a provider turn. A launch that
never becomes ready costs the tokens Claude spent starting up and no
task bytes at all, which is the property that makes it safe to refuse.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

READINESS_SCHEMA = "cao-claude-native-readiness-v1"

#: The hook event that attests a session has started under a known id.
#: SessionStart, and only SessionStart: the later events prove activity,
#: which is a different claim and arrives too late to gate a first turn.
READINESS_HOOK_EVENT = "SessionStart"

#: The generation-private file the hook appends to, relative to the
#: generation's companion directory.
READINESS_FILENAME = "claude-session-start.jsonl"

#: How long a readiness wait may take before it is refused. Claude's
#: start-up includes authentication and workspace trust resolution, so
#: this is generous — but it is a *refusal* deadline, never a substitute
#: for the proof. Timing out means "no hook arrived", which is a fact
#: about the absence of evidence, not an inference from the clock.
READY_TIMEOUT_SECONDS = 90.0
_POLL_SECONDS = 0.1


class ClaudeNativeReadinessError(RuntimeError):
    """Readiness could not be arranged, or did not arrive."""


class ClaudeNativeNotReady(ClaudeNativeReadinessError):
    """No SessionStart hook named this session before the deadline."""


def readiness_path(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    """Where this generation's SessionStart records land.

    Keyed by generation as well as terminal, so a replacement launch
    cannot read the previous incarnation's readiness and conclude that it
    is itself ready. That is not a theoretical ordering concern: recovery
    exists precisely to replace a generation whose provider may still be
    running, and a shared path would let the corpse vouch for its
    successor.
    """
    return Path(companion_dir) / terminal_id / generation / READINESS_FILENAME


def hook_settings(readiness_file: Path) -> Dict[str, Any]:
    """The ``--settings`` payload installing a SessionStart hook.

    The hook body is deliberately the smallest thing that can work: it
    appends the hook's own stdin — the payload, which carries the
    ``session_id`` — to the readiness file. It runs no interpreter of
    ours, imports nothing, and cannot block Claude's startup on anything
    but a single append.
    """
    quoted = shlex.quote(str(readiness_file))
    return {
        "hooks": {
            READINESS_HOOK_EVENT: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            # `printf '\n'` terminates the record, because
                            # the payload itself has no trailing newline
                            # and two runs would otherwise concatenate
                            # into one unparseable line.
                            "command": f"cat >> {quoted} && printf '\\n' >> {quoted}",
                        }
                    ]
                }
            ]
        }
    }


def prepare(companion_dir: Path, terminal_id: str, generation: str) -> Dict[str, Any]:
    """Create the generation-private readiness file and its settings.

    Called *before* the launch, so the hook has somewhere to write the
    instant Claude starts. Returns the path and the settings payload the
    caller passes as ``--settings``.

    The file is created empty and owner-only. Creating it up front also
    makes "the hook never ran" distinguishable from "the directory was
    never prepared", which are different failures with different fixes.
    """
    path = readiness_path(companion_dir, terminal_id, generation)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
    except OSError as exc:
        raise ClaudeNativeReadinessError(
            f"could not prepare the readiness file for {terminal_id}/{generation}: {exc}"
        ) from exc
    return {"readiness_path": path, "settings": hook_settings(path)}


def _records(path: Path) -> List[Dict[str, Any]]:
    """Every well-formed record so far; malformed lines are skipped.

    A partially written line is expected — the hook may be appending as
    this reads — so an unparseable line is simply not yet evidence. It is
    skipped rather than raised on, and the next poll sees it complete.
    """
    try:
        raw = path.read_text()
    except OSError:
        return []
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


#: The key under which a readiness receipt publishes the session id the
#: provider itself reported. Named rather than spelled twice because it is
#: a cross-module contract: the consumer that assembles the bind proof
#: reads this exact key, and when the two were independent literals they
#: drifted -- the receipt said ``native_session_id`` while the consumer
#: asked for ``session_id``, so the field was silently always absent and
#: every real Claude native launch was refused as not-yet-ready forever.
#:
#: Deliberately NOT the raw hook payload's own spelling. Claude's
#: SessionStart record calls it ``session_id``; a receipt is a published
#: statement rather than a copy of the input, and the two namespaces
#: staying distinct is what lets the raw records be matched on one key
#: while the receipt publishes another.
SESSION_START_ID_KEY = "native_session_id"


def observed_session_ids(path: Path) -> List[str]:
    """The session ids that have started, in the order they were recorded."""
    return [
        str(record["session_id"])
        for record in _records(path)
        if isinstance(record.get("session_id"), str) and record["session_id"]
    ]


def await_session_start(
    path: Path,
    native_session_id: str,
    *,
    timeout: float = READY_TIMEOUT_SECONDS,
    poll: float = _POLL_SECONDS,
) -> Dict[str, Any]:
    """Block until Claude reports *this* session started, or refuse.

    Matching is on the exact session id. A record for a different id is
    not partial credit — it is the single most important thing this can
    catch, because it means Claude opened a session other than the one it
    was told to, which is exactly what a resume falling back to its
    interactive picker looks like from outside.

    Raises:
        ClaudeNativeNotReady: no matching record arrived in time. The
            message names any other ids seen, since "a session started,
            but not yours" and "nothing started" call for different
            recovery.
    """
    deadline = time.monotonic() + timeout
    seen: List[str] = []
    while True:
        seen = observed_session_ids(path)
        if native_session_id in seen:
            break
        if time.monotonic() >= deadline:
            others = [item for item in seen if item != native_session_id]
            detail = (
                f"other sessions started instead: {others}"
                if others
                else "no SessionStart hook was recorded at all"
            )
            raise ClaudeNativeNotReady(
                f"claude did not report SessionStart for {native_session_id} within "
                f"{timeout:.0f}s; {detail}. No task bytes were submitted."
            )
        time.sleep(poll)

    matching = [
        record for record in _records(path) if record.get("session_id") == native_session_id
    ]
    first = matching[0]
    return {
        "schema": READINESS_SCHEMA,
        "hook_event": READINESS_HOOK_EVENT,
        SESSION_START_ID_KEY: native_session_id,
        "readiness_path": str(path),
        # Corroboration only. The transcript path is Claude's own view of
        # where the session lives; it is recorded because a human
        # debugging a bad resume will want it, and it is never what the
        # match was made on.
        "transcript_path": first.get("transcript_path"),
        "source": first.get("source"),
        "cwd": first.get("cwd"),
        # The model the provider says this session is actually running,
        # forwarded verbatim under the provider's own key. This is the
        # only place the running route can be learned before a turn: the
        # launch argv states what was *asked for*, and the two differ
        # exactly when something went wrong, which is when it matters.
        #
        # Carried as an observation, not a route decision — nothing here
        # interprets it. A record that omits the key yields ``None``, and
        # the caller comparing against it must fail closed, because
        # "the provider did not say" is not agreement.
        "model": first.get("model"),
        # Every id this generation's hook file saw, so a receipt reader
        # can tell a clean start from one where Claude also opened
        # something else.
        "observed_session_ids": seen,
    }


def settings_argument(settings: Dict[str, Any]) -> str:
    """The ``--settings`` value for a prepared hook configuration.

    Passed as a JSON string rather than a file path. The installed build
    accepts either (``--settings <file-or-json>``), and inline leaves no
    settings file on disk for a later, unrelated Claude invocation in the
    same directory to pick up.
    """
    return json.dumps(settings, separators=(",", ":"), sort_keys=True)


def child_environment(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for the launched Claude, with inherited hooks removed.

    A managed launch must not inherit hook configuration from whatever
    shell spawned it. The readiness proof is only as good as the
    guarantee that the SessionStart hook writing to our file is the one
    we installed.
    """
    env = dict(os.environ if base is None else base)
    for name in ("CLAUDE_HOOKS", "CLAUDE_CODE_HOOKS"):
        env.pop(name, None)
    return env
