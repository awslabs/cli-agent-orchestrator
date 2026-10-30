"""The pre-allocation checks every Kiro v3 create passes (#1167 Phase 1).

``create_terminal`` calls these before any window, registry row or FIFO
exists, in this order:

1. after the capability probe: the probed kiro-cli version must be allow-listed
   and verified, or pending verification with the operator's
   ``kiro.v3_allow_unverified_versions`` opt-in on, and the policy must not be
   unrestricted (``*``);
2. at Step 1c, from the final launch directory: no ``KIRO_HOME`` other than the
   default home may reach the launch; the installed agent file on disk must
   pass the validator against the probed engine's schema, with its grant
   fitting this launch's allowlist; and the workspace's ``.kiro/`` must hold
   nothing v3 would load as configuration.

Each refusal is a ``KiroV3Refusal`` naming its check and the remedy. Nothing
here runs kiro-cli or writes anything.
"""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities, KiroV3LaunchError
from cli_agent_orchestrator.providers.kiro_v3_schema import (
    KiroV3EngineSchema,
    KiroV3UnverifiedVersionError,
    require_launchable_kiro_v3_version,
)
from cli_agent_orchestrator.providers.kiro_workspace import check_kiro_workspace
from cli_agent_orchestrator.services.kiro_v3_validate import (
    KiroV3InvalidAgentError,
    validate_kiro_v3_agent_bytes,
)
from cli_agent_orchestrator.services.settings_service import kiro_v3_allow_unverified_versions
from cli_agent_orchestrator.utils.path_validation import flatten_path_separators

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KiroV3Admission:
    """What the version and policy check admitted a v3 launch under."""

    schema: KiroV3EngineSchema
    #: Set when the operator's opt-in admitted a version still pending
    #: operator verification (#1167, provisional); None otherwise.
    unverified_version: Optional[str] = None

    @property
    def launch_notes(self) -> Optional[List[str]]:
        """The note the launch response carries, or None when there is nothing to say."""
        if self.unverified_version is None:
            return None
        return [
            f"kiro_v3_unverified_version_admitted: kiro-cli {self.unverified_version} is "
            "pending operator verification; this v3 launch was admitted only by the "
            "operator opt-in kiro.v3_allow_unverified_versions."
        ]


def reinstall_remedy(agent_profile: str, agent_file: Path) -> str:
    """The remedy every on-disk refusal names, with any Kiro upgrader backups beside it."""
    backups = sorted(glob.glob(f"{glob.escape(str(agent_file))}.bak*"))
    note = f" Kiro's upgrader left {', '.join(backups)} beside it." if backups else ""
    return f"Reinstall it: `cao install {agent_profile} --provider kiro_cli --engine v3`.{note}"


def require_kiro_v3_admissible(
    capabilities: object, allowed_tools: Optional[List[str]]
) -> KiroV3Admission:
    """The probed engine's schema, or a refusal for its version or an unrestricted policy.

    A version pending operator verification refuses unless the operator opted
    in (``kiro.v3_allow_unverified_versions``). The opt-in is read only for
    such a version, so it never admits an unknown one, and it widens nothing
    else: every later check still applies. Provisional (#1167).
    """
    version = capabilities.version if isinstance(capabilities, KiroCapabilities) else None
    unverified: Optional[str] = None
    try:
        entry = require_launchable_kiro_v3_version(version)
    except KiroV3UnverifiedVersionError:
        if not kiro_v3_allow_unverified_versions():
            raise
        entry = require_launchable_kiro_v3_version(version, allow_unverified=True)
        unverified = entry.cli_version
    if allowed_tools is None or "*" in allowed_tools:
        raise KiroV3LaunchError(
            "kiro_v3_unrestricted",
            "an unrestricted ('*') launch is not supported on Kiro engine 'v3'; restrict "
            "the agent's allowedTools, or retry with engine 'v2'.",
        )
    if unverified is not None:
        logger.warning(
            "Kiro v3 launch admitted on kiro-cli %s, which is pending operator "
            "verification, by the operator opt-in kiro.v3_allow_unverified_versions "
            "(#1167). Keep the opt-in off outside the operator checklist.",
            unverified,
        )
    return KiroV3Admission(entry.schema, unverified)


def _default_kiro_home() -> str:
    return os.path.realpath(os.path.join(str(Path.home()), ".kiro"))


def require_default_kiro_home(environments: Iterable[Optional[Mapping[str, str]]]) -> None:
    """Refuse a ``KIRO_HOME`` other than ``~/.kiro`` in any environment the launch inherits."""
    default = _default_kiro_home()
    for environment in environments:
        value = (environment or {}).get("KIRO_HOME")
        if value and os.path.realpath(os.path.expanduser(value)) != default:
            raise KiroV3LaunchError(
                "kiro_v3_home_unsupported",
                f"KIRO_HOME={value} is set for this launch; Kiro engine 'v3' runs only with "
                "the default Kiro home in this release. Unset it, or retry with engine 'v2'.",
            )


def require_kiro_v3_launchable(
    agent_profile: str,
    working_directory: Optional[str],
    launch_env: Optional[Mapping[str, str]],
    allowed_tools: List[str],
    schema: KiroV3EngineSchema,
    agents_dir: Path,
) -> None:
    """Step 1c for a v3 create: the home, the agent file on disk, and the workspace."""
    require_default_kiro_home([os.environ, launch_env])

    stem = flatten_path_separators(agent_profile)
    agent_file = Path(agents_dir) / f"{stem}.json"
    try:
        raw = agent_file.read_bytes()
    except FileNotFoundError:
        raise KiroV3LaunchError(
            "kiro_v3_not_installed",
            f"no Kiro agent file at {agent_file}. Install it for v3: "
            f"`cao install {agent_profile} --provider kiro_cli --engine v3`.",
        ) from None
    findings = validate_kiro_v3_agent_bytes(
        raw, schema, expected_name=stem, launch_allowlist=list(allowed_tools)
    )
    if findings:
        raise KiroV3InvalidAgentError(
            agent_profile, findings, reinstall_remedy(agent_profile, agent_file)
        )

    if not working_directory:
        raise KiroV3LaunchError(
            "kiro_v3_bad_cwd", "a Kiro v3 launch needs a working directory to check."
        )
    check_kiro_workspace(working_directory)


__all__ = [
    "KiroV3Admission",
    "reinstall_remedy",
    "require_default_kiro_home",
    "require_kiro_v3_admissible",
    "require_kiro_v3_launchable",
]
