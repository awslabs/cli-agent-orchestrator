"""The pre-allocation checks every Kiro v3 create passes (#1167 Phase 1).

``create_terminal`` calls these before any window, registry row or FIFO
exists, in this order:

1. after the capability probe: the probed version must qualify for strict
   or operator opt-in observed admission, and the policy must not be
   unrestricted (``*``);
2. at Step 1c, from the final launch directory: no ``KIRO_HOME`` other than the
   default home may reach the launch; the directory ``cao install`` writes
   (``CAO_AGENTS_DIR``) must be ``~/.kiro/agents``, the one Kiro reads; the
   installed agent file on disk must
   pass the validator against the probed engine's schema, with its grant
   fitting this launch's allowlist; and the workspace's ``.kiro/`` must hold
   nothing v3 would load as configuration.

Each refusal is a ``KiroV3Refusal``, or #1074's ``fallback_agent``
(``KiroAgentIdentityError``) for a fallback agent name, naming its check and
the remedy. Nothing here runs kiro-cli or writes anything.
"""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities, KiroV3LaunchError
from cli_agent_orchestrator.providers.kiro_identity import (
    KIRO_FALLBACK_AGENTS,
    require_kiro_agent_defined,
)
from cli_agent_orchestrator.providers.kiro_v3_schema import (
    SUPPORTED_KIRO_CLI_VERSIONS,
    KiroV3EngineSchema,
    KiroV3ObservedRefusal,
    KiroV3UnverifiedVersionError,
    observed_kiro_v3_entry,
    require_launchable_kiro_v3_version,
)
from cli_agent_orchestrator.providers.kiro_workspace import check_kiro_workspace
from cli_agent_orchestrator.services.kiro_v3_validate import (
    KiroV3InvalidAgentError,
    validate_kiro_v3_agent_bytes,
)
from cli_agent_orchestrator.services.settings_service import (
    kiro_v3_allow_unverified_versions,
    kiro_v3_observed_admission,
)
from cli_agent_orchestrator.utils.path_validation import flatten_path_separators

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KiroV3Admission:
    """What the version and policy check admitted a v3 launch under."""

    schema: KiroV3EngineSchema
    #: Set when the operator's opt-in admitted a version still pending
    #: operator verification (#1167, provisional); None otherwise.
    unverified_version: Optional[str] = None
    observed_version: Optional[str] = None
    borrowed_schema_version: Optional[str] = None

    @property
    def launch_notes(self) -> Optional[List[str]]:
        """The note the launch response carries, or None when there is nothing to say."""
        notes = []
        if self.observed_version is not None:
            pending = " (pending operator verification)" if self.unverified_version else ""
            notes.append(
                f"kiro_v3_observed_admitted: kiro-cli {self.observed_version} borrowed "
                f"schema from kiro-cli {self.borrowed_schema_version}{pending} by operator opt-in "
                "kiro.v3_admission=observed; every live launch check still applies."
            )
        if self.unverified_version is not None:
            notes.append(
                f"kiro_v3_unverified_version_admitted: kiro-cli {self.unverified_version} is "
                "pending operator verification; this v3 launch was admitted only by the "
                "operator opt-in kiro.v3_allow_unverified_versions."
            )
        return notes or None


def reinstall_remedy(agent_profile: str, agent_file: Path) -> str:
    """The remedy every on-disk refusal names, with any Kiro upgrader backups beside it.

    Backups are named by file name only; their directory goes to the server log.
    The refusal is the API's 400 body, and in shared-server mode the caller is
    not the server's user, so no absolute server path goes in it (as #1074's
    ``require_kiro_agent_defined`` does).
    """
    backups = sorted(glob.glob(f"{glob.escape(str(agent_file))}.bak*"))
    note = ""
    if backups:
        logger.warning("Kiro v3 agent file %s has upgrader backups: %s", agent_file, backups)
        names = ", ".join(os.path.basename(backup) for backup in backups)
        note = f" Kiro's upgrader left {names} beside it (the server log names the directory)."
    return f"Reinstall it: `cao install {agent_profile} --provider kiro_cli --engine v3`.{note}"


def require_kiro_v3_admissible(
    capabilities: object, allowed_tools: Optional[List[str]], *, allow_observed: bool = False
) -> KiroV3Admission:
    """The probed engine's schema, or a refusal for its version or an unrestricted policy.

    Listed versions keep their pending-verification rule. Unlisted versions
    require the observed setting, v3 capability, and the newest same-major
    schema predecessor. Only the sealed-home helper path passes ``allow_observed``;
    other callers stay strict. Every later check still applies.
    """
    version = capabilities.version if isinstance(capabilities, KiroCapabilities) else None
    unverified: Optional[str] = None
    observed = (
        allow_observed
        and version not in SUPPORTED_KIRO_CLI_VERSIONS
        and kiro_v3_observed_admission()
    )
    if observed:
        if not isinstance(capabilities, KiroCapabilities) or "v3" not in capabilities.agent_engines:
            raise KiroV3ObservedRefusal(version, "--agent-engine does not advertise v3")
        entry = observed_kiro_v3_entry(
            version, allow_unverified=kiro_v3_allow_unverified_versions()
        )
        if entry.pending_operator_verification:
            unverified = entry.cli_version
    else:
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
    if observed:
        logger.warning(
            "Kiro v3 observed admission: kiro-cli %s borrowed schema from kiro-cli %s%s; "
            "operator opt-in kiro.v3_admission=observed; live checks remain required.",
            version,
            entry.cli_version,
            " (pending operator verification)" if unverified else "",
        )
    return KiroV3Admission(
        entry.schema,
        unverified,
        version if observed else None,
        entry.cli_version if observed else None,
    )


def _default_kiro_home() -> str:
    return os.path.realpath(os.path.join(str(Path.home()), ".kiro"))


def require_default_kiro_home(environments: Iterable[Optional[Mapping[str, str]]]) -> None:
    """Refuse a ``KIRO_HOME`` other than ``~/.kiro`` in any environment the launch inherits."""
    default = _default_kiro_home()
    for environment in environments:
        value = (environment or {}).get("KIRO_HOME")
        if value and os.path.realpath(os.path.expanduser(value)) != default:
            logger.warning("Kiro v3 launch refused: KIRO_HOME=%s is set for it", value)
            raise KiroV3LaunchError(
                "kiro_v3_home_unsupported",
                "KIRO_HOME is set to a directory other than ~/.kiro for this launch (the "
                "server log names it); Kiro engine 'v3' runs only with the default Kiro "
                "home in this release. Unset it, or retry with engine 'v2'.",
            )


def require_default_kiro_agents_dir(agents_dir: Path) -> None:
    """Refuse when ``cao install`` writes Kiro agents anywhere but ``~/.kiro/agents``.

    The v3 command unsets ``KIRO_HOME`` and ``KIRO_AGENT_CONFIG_DIR``, so
    kiro-cli loads ``~/.kiro/agents/<name>.json``. With ``CAO_AGENTS_DIR`` set
    elsewhere, the file Step 1c validates would not be the file Kiro loads,
    and an older file of the same name there would run unchecked. Compared by
    ``realpath``, so a link to the default directory passes.
    """
    default = os.path.realpath(os.path.join(_default_kiro_home(), "agents"))
    if os.path.realpath(agents_dir) != default:
        logger.warning(
            "Kiro v3 launch refused: cao install writes Kiro agents to %s, not %s",
            agents_dir,
            default,
        )
        raise KiroV3LaunchError(
            "kiro_v3_agents_dir_unsupported",
            "CAO_AGENTS_DIR makes `cao install` write Kiro agent files to a directory "
            "other than ~/.kiro/agents (the server log names it), but Kiro engine 'v3' "
            "loads agents only from ~/.kiro/agents, so the file CAO checks would not be "
            "the one Kiro runs. Unset CAO_AGENTS_DIR for cao-server and `cao install`, "
            "reinstall the agent with --engine v3, or retry with engine 'v2'.",
        )


def require_kiro_v3_launchable(
    agent_profile: str,
    working_directory: Optional[str],
    launch_env: Optional[Mapping[str, str]],
    allowed_tools: List[str],
    schema: KiroV3EngineSchema,
    agents_dir: Path,
) -> None:
    """Step 1c for a v3 create: the home, the agent file on disk, and the workspace.

    No refusal names an absolute server path: each one is the API's 400 body,
    so paths go to the server log, as #1074's ``require_kiro_agent_defined`` does.
    """
    stem = flatten_path_separators(agent_profile)
    if agent_profile in KIRO_FALLBACK_AGENTS:
        # Its own refusal (``fallback_agent``), before the file read: no v3 file
        # can be installed for a fallback name, so an install remedy would be
        # one the validator refuses.
        require_kiro_agent_defined(agent_profile, [])
    require_default_kiro_home([os.environ, launch_env])
    require_default_kiro_agents_dir(agents_dir)

    agent_file = Path(agents_dir) / f"{stem}.json"
    install = f"`cao install {agent_profile} --provider kiro_cli --engine v3`"
    try:
        raw = agent_file.read_bytes()
    except FileNotFoundError:
        logger.warning("Kiro v3 agent file for %r not found at %s", agent_profile, agent_file)
        raise KiroV3LaunchError(
            "kiro_v3_not_installed",
            f"no Kiro v3 agent file for '{agent_profile}' in the directory `cao install` "
            f"writes (the server log names it). Install it for v3: {install}.",
        ) from None
    except OSError as exc:
        # A directory, a symlink loop, no permission: a refusal, not a 500.
        logger.warning(
            "Kiro v3 agent file for %r at %s could not be read: %s", agent_profile, agent_file, exc
        )
        raise KiroV3LaunchError(
            "kiro_v3_agent_unreadable",
            f"the Kiro v3 agent file for '{agent_profile}' in the directory `cao install` "
            f"writes could not be read ({type(exc).__name__}; the server log names the "
            f"path). Remove whatever is there, then install it for v3: {install}.",
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
    "require_default_kiro_agents_dir",
    "require_default_kiro_home",
    "require_kiro_v3_admissible",
    "require_kiro_v3_launchable",
]
