"""Engine-aware Kiro profile adapters and atomic artifact rendering."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from pydantic_core import PydanticSerializationError

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.kiro_kas import KASAgentConfig
from cli_agent_orchestrator.utils.kiro_policy import (
    CompiledKiroPolicy,
    KiroPolicyError,
    compile_kiro_policy,
)

_SAFE_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def kiro_artifact_path(directory: Path, name: str, engine: KiroEngine) -> Path:
    """Return distinct deterministic artifact identities for v2 and KAS."""
    if not _SAFE_PROFILE_NAME_RE.fullmatch(name):
        raise ValueError("Kiro profile name must match [A-Za-z0-9_-]{1,64}")
    suffix = ".json" if engine == KiroEngine.V2 else ".kas.json"
    return directory / f"{name}{suffix}"


def render_kiro_v2(
    profile: AgentProfile,
    allowed_tools: list[str],
    resources: list[str],
    mcp_servers: Optional[dict[str, object]],
) -> str:
    """Render the existing v2 JSON shape without changing serialization."""
    raw_prompt = profile.prompt.strip() if profile.prompt and profile.prompt.strip() else None
    config = KiroAgentConfig(
        name=profile.name,
        description=profile.description,
        tools=profile.tools if profile.tools is not None else ["*"],
        allowedTools=allowed_tools,
        resources=resources,
        prompt=raw_prompt,
        mcpServers=mcp_servers,
        toolAliases=profile.toolAliases,
        toolsSettings=profile.toolsSettings,
        hooks=profile.hooks,
        model=profile.model,
    )
    return config.model_dump_json(indent=2, exclude_none=True)


def render_kiro_kas(
    profile: AgentProfile,
    resources: list[str],
    mcp_servers: Optional[dict[str, object]],
) -> tuple[str, CompiledKiroPolicy]:
    """Validate, compile, and render one KAS profile."""
    policy = compile_kiro_policy(profile)
    all_resources = resources + (profile.resources or [])
    if len(set(all_resources)) != len(all_resources):
        raise KiroPolicyError("contradictory-resource", "KAS resources contain duplicates")
    unsupported = [
        resource for resource in all_resources if not resource.startswith(("file://", "skill://"))
    ]
    if unsupported:
        raise KiroPolicyError(
            "unknown-resource",
            f"unsupported KAS resource identity {unsupported[0]!r}",
        )
    raw_prompt = profile.prompt.strip() if profile.prompt and profile.prompt.strip() else None
    config = KASAgentConfig(
        name=profile.name,
        description=profile.description,
        tools=list(policy.visible_tools),
        permissions=policy.permissions,
        resources=all_resources,
        prompt=raw_prompt,
        mcpServers=mcp_servers,
        hooks=profile.hooks,
        model=profile.model,
    )
    try:
        rendered = config.model_dump_json(indent=2, exclude_none=True)
    except (PydanticSerializationError, TypeError, ValueError) as exc:
        raise KiroPolicyError(
            "serialization-error", f"KAS profile serialization failed: {exc}"
        ) from exc
    return rendered, policy


def atomic_write_text(destination: Path, content: str) -> None:
    """Atomically replace one UTF-8 artifact after complete serialization."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
