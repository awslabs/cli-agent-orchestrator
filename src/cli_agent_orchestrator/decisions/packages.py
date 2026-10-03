"""Installed decider metadata and optional in-process operator commands."""

import re
from importlib.metadata import entry_points
from typing import Any, Callable, Sequence, cast

CONSENT_SAME_USER = (
    "Consent is a same-user local control, not a privilege boundary. CAO runs as you and verifies "
    "nothing about who typed the endpoint; granted_by is a provenance note, not an identity claim. "
    "Any process running as you, including an agent with shell access, can edit the consent file or "
    "settings.json and can read the key from the key store. What keeps agents out is that no "
    "agent-facing tool can grant consent, the consent command needs an interactive terminal, and "
    "each profile's tool restrictions."
)
EXTERNAL_DISCLOSURE = "sends data off this machine only after its own consent"


def installed_deciders() -> list[dict[str, Any]]:
    """Inspect distribution metadata without loading decider code."""
    result = []
    for entry in entry_points(group="cao.deciders"):
        dist = entry.dist
        distribution = (
            dist.metadata["Name"] if dist is not None and "Name" in dist.metadata else None
        )
        builtin = (
            distribution is not None
            and re.sub(r"[-_.]+", "-", distribution).lower() == "cli-agent-orchestrator"
        )
        item = {
            "name": entry.name,
            "distribution": distribution,
            "version": dist.version if dist is not None else None,
            "kind": "builtin" if builtin else "external",
        }
        if not builtin:
            item["disclosure"] = EXTERNAL_DISCLOSURE
        result.append(item)
    return sorted(result, key=lambda item: (item["name"], item["distribution"] or ""))


def dispatch(name: str, action: str, extra: Sequence[str] = ()) -> int:
    """Load only the console script owned by this decider's distribution."""
    matches = [entry for entry in entry_points(group="cao.deciders") if entry.name == name]
    if len(matches) != 1:
        raise ValueError(f"unknown or ambiguous decider: {name}")
    dist = matches[0].dist
    scripts = (
        [
            entry
            for entry in dist.entry_points
            if entry.group == "console_scripts" and entry.name == f"cao-decider-{name}"
        ]
        if dist is not None
        else []
    )
    if not scripts:
        raise ValueError(f"no operator commands for {name}")
    if len(scripts) != 1:
        raise ValueError(f"ambiguous operator commands for {name}")
    try:
        target = scripts[0].load()
        if not callable(target):
            raise TypeError("operator command must be callable")
        main = cast(Callable[[Sequence[str] | None], object], target)
        code = main([action, *extra])
    except Exception:
        raise RuntimeError(f"operator command failed for {name}") from None
    if not isinstance(code, int) or isinstance(code, bool):
        raise RuntimeError(f"operator command returned no integer exit code for {name}")
    return code
