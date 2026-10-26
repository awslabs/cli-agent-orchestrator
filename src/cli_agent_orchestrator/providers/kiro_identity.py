"""Kiro agent identity: run the agent CAO asked for, or nothing (#1074).

kiro-cli does not refuse an ``--agent`` it cannot load. It prints ``agent "<name>"
not found, using "kiro_default"`` and starts its built-in default agent, which has
every built-in tool (shell, write, use_aws and more). Measured on kiro-cli 2.32.0;
the same fallback follows an agent file that does not parse. CAO therefore checks
identity twice, and both checks fail closed:

1. Before launch (``kiro_agent_defined``): one of the agent directories in
   ``kiro_agent_dirs`` must define the name. ``terminal_service.create_terminal``
   refuses otherwise, before any window exists.
2. After launch, before any task is typed (``require_header_identity``): the
   TUI header row, ``<agent> · <model> · ◔ N% · ...``, must name the agent.
   ``KiroCliProvider.initialize`` raises otherwise, and the creation path closes
   the terminal's window: a synchronous create rolls back, and a deferred create
   always reclaims the runtime, keeping only the failure record. Nothing is
   delivered.

Check 1 deliberately over-accepts: an agent file it counts that kiro-cli then
does not load still ends at check 2, which reads what kiro-cli actually started.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

from cli_agent_orchestrator.utils.path_validation import flatten_path_separators
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

# Agents a launch may never be identified as, whatever was requested: kiro-cli's
# built-in fallback, and the tool-less default agent proposed for CAO-owned Kiro
# homes. Provisional (#1074): an explicit request for either is refused too.
KIRO_FALLBACK_AGENTS = frozenset({"kiro_default", "cao-null"})

# The TUI header row: agent, model, then the context gauge and percentage. The
# gauge glyph is not pinned (``◔`` in every capture so far, at 0-14%), and the
# row continues with auth and cwd columns that vary by version. Matching the
# gauge keeps a wrapped cwd column (``~/project · (main)``) from reading as a
# header. Only rows of this shape are read: the requested name also appears in
# the echoed launch command and in kiro-cli's fallback warning, so searching
# the pane for it would accept the fallback.
_HEADER_ROW = re.compile(r"^(?P<agent>[^\s·]+)\s+·\s+(?P<model>[^\s·]+)\s+·\s+\S\s*\d{1,3}%")

# Agent files above this size are not parsed while looking for a ``name``.
_MAX_AGENT_FILE_BYTES = 1024 * 1024


class KiroAgentIdentityError(ValueError):
    """A Kiro launch whose agent identity is unknown or wrong; nothing was delivered.

    ``kind`` is one of ``agent_not_found`` and ``fallback_agent`` (refused
    before launch), or ``header_not_found`` and ``identity_mismatch`` (refused
    after kiro-cli started and before any task was typed). Provisional (#1074):
    these kinds and the 400 they map to are not yet settled public strings.
    """

    def __init__(
        self,
        kind: str,
        agent: str,
        message: str,
        *,
        observed: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.agent = agent
        self.observed = observed


def kiro_agent_dirs(
    working_directory: str,
    installed_dir: os.PathLike[str] | str,
    env_vars: Optional[Mapping[str, str]] = None,
) -> List[Path]:
    """Every directory a Kiro launch from ``working_directory`` may read agents from.

    The workspace directory (``<cwd>/.kiro/agents``), the directory ``cao
    install`` writes, ``~/.kiro/agents``, and ``$KIRO_HOME/agents`` for each
    absolute ``KIRO_HOME`` in the forwarded launch env or the server env
    (kiro-cli treats a relative ``KIRO_HOME`` as ``~/.kiro``). Duplicates are
    dropped and order is kept.

    Provisional (#1074): the workspace is the launch cwd only, not its parents,
    and a workspace agent counts for installed launches; refusing a workspace
    agent that shadows a global one is left to CAO-owned Kiro homes.
    """
    candidates = [
        Path(working_directory) / ".kiro" / "agents",
        Path(installed_dir),
        Path.home() / ".kiro" / "agents",
    ]
    for kiro_home in ((env_vars or {}).get("KIRO_HOME"), os.environ.get("KIRO_HOME")):
        if kiro_home and os.path.isabs(kiro_home):
            candidates.append(Path(kiro_home) / "agents")
    unique: List[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.realpath(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _declares_name(path: Path, agent: str) -> bool:
    """True when ``path`` is a JSON agent spec whose ``name`` is ``agent``."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_AGENT_FILE_BYTES:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("name") == agent


def kiro_agent_defined(agent: str, directories: Iterable[Path]) -> bool:
    """True when one of ``directories`` holds an agent spec kiro-cli could load as ``agent``.

    Counts ``<dir>/<agent>.json`` (what ``cao install`` writes), an
    ``<dir>/<agent>/`` directory holding a JSON spec (the layout AIM installs,
    see docs/kiro-cli.md), and any ``*.json`` one level down whose ``name`` is
    ``agent``. Whether kiro-cli then loads the file is not decided here; the
    header check after launch is what proves it. Kiro's built-in agents
    (``kiro_help``, ``kiro_planner``) have no file, so they never count:
    provisional (#1074), CAO launches installed agent files only.
    """
    stem = flatten_path_separators(agent)
    for directory in directories:
        try:
            if not directory.is_dir():
                continue
            if (directory / f"{stem}.json").is_file():
                return True
            agent_dir = directory / stem
            if agent_dir.is_dir() and any(p.is_file() for p in agent_dir.glob("*.json")):
                return True
            for pattern in ("*.json", "*/*.json"):
                if any(_declares_name(p, agent) for p in directory.glob(pattern)):
                    return True
        except OSError:
            continue
    return False


def require_kiro_agent_defined(agent: str, directories: List[Path]) -> None:
    """Refuse a Kiro launch, before it starts, whose agent kiro-cli would not load.

    Raises:
        KiroAgentIdentityError: ``fallback_agent`` for a name in
            ``KIRO_FALLBACK_AGENTS``, ``agent_not_found`` when no directory
            defines the name.
    """
    if agent in KIRO_FALLBACK_AGENTS:
        raise KiroAgentIdentityError(
            "fallback_agent",
            agent,
            f"Kiro agent '{agent}' is refused: it is the agent a Kiro launch falls back to "
            f"when the requested one cannot be loaded, so its header cannot show that "
            f"the request was honored. Launch an installed CAO profile instead.",
        )
    if not kiro_agent_defined(agent, directories):
        searched = ", ".join(str(d) for d in directories)
        raise KiroAgentIdentityError(
            "agent_not_found",
            agent,
            f"Kiro agent '{agent}' was not found in any agent directory kiro-cli reads "
            f"({searched}). kiro-cli would not refuse it: it would start its built-in "
            f"'kiro_default' agent, with every built-in tool, instead. Nothing was "
            f"launched. Remedy: `cao install {agent} --provider kiro_cli`, then retry.",
        )


def header_agent(pane: str) -> Optional[str]:
    """The agent named by the last Kiro TUI header row in ``pane``, or None.

    The live header is pinned at the bottom of the screen, so the last row of
    header shape is the current one.
    """
    agent: Optional[str] = None
    for row in strip_terminal_escapes(pane).splitlines():
        match = _HEADER_ROW.match(row.strip())
        if match:
            agent = match.group("agent")
    return agent


def require_header_identity(expected: str, observed: Optional[str]) -> None:
    """Refuse a started Kiro session unless its header named ``expected``.

    ``observed`` is ``header_agent`` of the pane. Called before any task is
    typed, so a refusal means nothing reached the wrong agent.

    Raises:
        KiroAgentIdentityError: ``header_not_found`` when no header was read,
            ``identity_mismatch`` when it names another agent or a fallback.
    """
    if observed is None:
        raise KiroAgentIdentityError(
            "header_not_found",
            expected,
            f"Kiro agent identity not confirmed for '{expected}': no TUI header row "
            f"('<agent> · <model> · ◔ N%') was found after kiro-cli started, so CAO "
            f"cannot tell which agent is running. No task was sent. A kiro-cli TUI "
            f"change can cause this; check the kiro-cli version.",
        )
    if observed != expected or observed in KIRO_FALLBACK_AGENTS:
        raise KiroAgentIdentityError(
            "identity_mismatch",
            expected,
            f"Kiro started agent '{observed}' instead of '{expected}'. kiro-cli falls "
            f"back to its built-in agent when the requested one is missing or does not "
            f"load. No task was sent. Remedy: `cao install {expected} --provider "
            f"kiro_cli`, check `kiro-cli agent list` for errors, then retry. If "
            f"CAO_AGENTS_DIR is set, kiro-cli reads it only when it is ~/.kiro/agents "
            f"or $KIRO_HOME/agents.",
            observed=observed,
        )
