"""Version-pinned, zero-prompt Claude route attestation.

The Kimi attestor can report a *resolved* model and effort because ACP
exposes them: a zero-prompt session reports its configuration and can be
set through structured protocol methods. Claude has no equivalent surface
before the first turn — which is why its resume status reports authority
unsupported by default — so this probe proves what is actually provable
without spending a turn, and says plainly which facts it did not observe.

The alternative shipped for a while and is worse than either: the route
attestation endpoint dispatched every non-Codex provider to the Kimi
probe, so a ``claude_code`` request ran the Kimi binary and returned a
Kimi receipt under a Claude label. A breaker would then have opened a
Claude route on evidence gathered from a different provider. A receipt
that names what it could not see is usable; one that quietly names the
wrong provider's facts is not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Optional

from cli_agent_orchestrator.services.provider_contracts import (
    PROVIDER_CLAUDE,
    SUPPORTED_VERSIONS,
    normalized_version,
)

#: Exact Claude builds this probe accepts — never a range. Read from the
#: one acceptance authority so the probe cannot drift from the contract it
#: is attesting against.
SUPPORTED_CLAUDE_VERSIONS = SUPPORTED_VERSIONS[PROVIDER_CLAUDE]
PROBE_VERSION = "claude-cli-route-v1"

#: Why this receipt states no observed model or effort. Carried in the
#: receipt rather than only in this docstring: the consumer is a recovery
#: breaker in another process, and an absent field it cannot explain is
#: indistinguishable from one the probe forgot to fill in.
UNOBSERVED_REASON = (
    "Claude exposes no pre-turn model or effort resolution surface: the values "
    "are settled by the first turn, and this probe sends none. The requested "
    "route is recorded as requested, never as resolved."
)


class ClaudeRouteProbeError(RuntimeError):
    pass


def attest_claude_route(
    project_root: str,
    *,
    expected_model: str,
    expected_effort: str,
    timeout: float = 10.0,
    claude_bin: str = "claude",
) -> dict[str, Any]:
    """Return a provider-native Claude route receipt without sending a prompt.

    What is proven: the requested working directory is a canonical
    existing directory, the pinned Claude binary is present and resolvable,
    and its own ``--version`` is one of the exact accepted builds. What is
    *not* proven is stated in the receipt with the reason, so a reader
    never has to infer it from a missing key.
    """
    if not os.path.isdir(project_root) or os.path.realpath(project_root) != project_root:
        raise ClaudeRouteProbeError("project_root must be an existing canonical directory")

    resolved: Optional[str] = shutil.which(claude_bin)
    if resolved is None:
        raise ClaudeRouteProbeError(f"Claude binary {claude_bin!r} is not on PATH")

    try:
        version_proc = subprocess.run(
            [resolved, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeRouteProbeError(f"could not execute Claude version probe: {exc}") from exc

    banner = version_proc.stdout.strip()
    version = normalized_version(banner)
    if version_proc.returncode != 0 or version not in SUPPORTED_CLAUDE_VERSIONS:
        raise ClaudeRouteProbeError(
            f"unsupported Claude version {banner!r}; expected one of "
            f"{list(SUPPORTED_CLAUDE_VERSIONS)!r}"
        )

    return {
        "probe_version": PROBE_VERSION,
        "claude_version": version,
        "claude_version_banner": banner,
        "claude_binary": resolved,
        "project_root": project_root,
        # Requested, not resolved — the two are different claims and the
        # field names say which one this is.
        "requested_model": expected_model,
        "requested_effort": expected_effort,
        "observed_model": None,
        "observed_effort": None,
        "route_source": "version-pinned-binary",
        "pre_turn_route_surface": False,
        "unobserved_reason": UNOBSERVED_REASON,
        "no_prompt_sent": True,
    }
