"""Shared fixtures for the Agent Plugins test package.

``constants.py`` derives every path from ``CAO_HOME_DIR`` at **import** time, so
these tests cannot relocate the store by setting an env var — the module is
long since imported by the time a test runs. Every entry point in
``agent_plugins`` therefore accepts an explicit store/skills-dir override, and
these fixtures supply tmp-path-backed ones. That is also why the production code
takes those parameters at all: an installer that could only ever write to the
real home directory would be untestable without monkeypatching module globals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable, Optional

import pytest

from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

#: The upstream canonical example package, used as the known-good positive
#: fixture in the conformance corpus (Requirement 23.4).
CANONICAL_EXAMPLE_DIR = Path(__file__).parent / "fixtures" / "canonical-example"


@pytest.fixture(autouse=True)
def _enable_agent_plugins_surface(monkeypatch):
    """Open Requirement 16.5's ship-gate for the suites that exercise the surface.

    ``CAO_AGENT_PLUGINS_ENABLED`` is default-off in production (review finding F1),
    so without this every CLI invocation and every ``/plugins*`` request in this
    package would be refused. ``test_ship_gate.py`` deliberately manages the
    variable itself so the default-off behaviour stays under test.
    """
    monkeypatch.setenv("CAO_AGENT_PLUGINS_ENABLED", "1")


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Default settings to "no extra dirs, symlink projection" for every test.

    Without this the suite would read the developer's real ``settings.json`` and
    a machine with ``skills.extra_dirs`` configured would see different
    collision outcomes than CI.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
        lambda: [],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
        lambda: "symlink",
    )


@pytest.fixture
def skills_dir(tmp_path) -> Path:
    """An isolated stand-in for the global ``SKILLS_DIR``."""
    path = tmp_path / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def store(tmp_path) -> InstalledPluginStore:
    """An installed-plugin store rooted under ``tmp_path``."""
    return InstalledPluginStore(
        plugins_dir=tmp_path / "agent-plugins",
        data_dir=tmp_path / "agent-plugin-data",
    )


def write_skill(
    directory: Path,
    name: str,
    description: str = "A test skill.",
    body: str = "Body.",
) -> Path:
    """Create a valid skill folder whose frontmatter name matches its folder.

    Scalars are **quoted** deliberately, via ``json.dumps`` (valid YAML for a
    string). Unquoted YAML applies implicit typing, so a skill legitimately named
    ``true``, ``no``, ``null``, ``on`` or ``1.0`` would parse as a bool, null or
    number and then compare unequal to its own folder name — this helper would be
    generating an *invalid* skill while every caller believed it was valid, and a
    test asserting the skill is reachable would fail for a reason that has
    nothing to do with the code under test.

    That is a live hazard here rather than a theoretical one: the collision
    properties draw plugin and skill names from sampled sets, and the next
    adversarial name added to one of those sets is as likely to be ``no`` as
    ``zzx``.

    The underlying hazard is real for plugin authors too — an unquoted
    ``name: true`` in a hand-written ``SKILL.md`` is silently rejected by CAO's
    existing skill loader — but it lives in that loader rather than in anything
    this feature introduces, so it is out of scope here beyond not tripping over
    it. See ``test_a_yaml_boolean_shaped_skill_name_survives_the_fixture``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return directory


def build_plugin(
    root: Path,
    name: str,
    *,
    skills: Optional[Iterable[str]] = None,
    version: str = "1.0.0",
    schema_id: Optional[str] = PLUGIN_SCHEMA_ID,
    extra_manifest: Optional[dict] = None,
    manifest_text: Optional[str] = None,
    with_mcp: bool = False,
    mcp_text: Optional[str] = None,
) -> Path:
    """Create a plugin directory at ``root``.

    Args:
        root: Directory to create (parents are created too).
        name: Manifest ``name``.
        skills: Skill names to create under ``skills/``.
        version: Manifest ``version``.
        schema_id: Manifest ``$schema``; ``None`` omits the field entirely.
        extra_manifest: Extra top-level manifest members, merged last so a test
            can override or add anything.
        manifest_text: Raw ``plugin.json`` text, bypassing JSON construction
            entirely (for invalid-JSON and non-object cases).
        with_mcp: Write a minimal valid ``mcp.json``.
        mcp_text: Raw ``mcp.json`` text, overriding ``with_mcp``.
    """
    root.mkdir(parents=True, exist_ok=True)

    if manifest_text is not None:
        (root / "plugin.json").write_text(manifest_text, encoding="utf-8")
    else:
        manifest: dict = {}
        if schema_id is not None:
            manifest["$schema"] = schema_id
        manifest["name"] = name
        if version is not None:
            manifest["version"] = version
        if extra_manifest:
            manifest.update(extra_manifest)
        (root / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    for skill_name in skills or ():
        write_skill(root / "skills" / skill_name, skill_name, f"Skill {skill_name}.")

    if mcp_text is not None:
        (root / "mcp.json").write_text(mcp_text, encoding="utf-8")
    elif with_mcp:
        (root / "mcp.json").write_text(
            json.dumps(
                {
                    "$schema": MCP_SCHEMA_ID,
                    "mcpServers": {
                        "demo": {"type": "stdio", "command": "demo-server"},
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return root


@pytest.fixture
def make_plugin(tmp_path) -> Callable[..., Path]:
    """Factory creating plugin source directories under ``tmp_path/sources``."""

    def _make(name: str, **kwargs) -> Path:
        return build_plugin(tmp_path / "sources" / name, name, **kwargs)

    return _make
