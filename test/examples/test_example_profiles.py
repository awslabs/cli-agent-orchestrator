"""Every example agent profile must reference a provider that exists.

Regression: an example was once added with ``provider: gemini_cli`` after that provider had been removed
upstream (#353), making the profile dead-on-arrival configuration. This walks
every frontmattered example profile and validates the ``provider`` (and
worker overrides in ``agents`` blocks) against the live ``ProviderType`` enum
so docs and code can't drift apart again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Tuple

import frontmatter
import pytest

from cli_agent_orchestrator.models.provider import ProviderType

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
VALID_PROVIDERS = {p.value for p in ProviderType}


def _example_profiles() -> Iterator[Tuple[str, dict]]:
    for path in sorted(EXAMPLES_DIR.rglob("*.md")):
        if path.name == "README.md":
            continue
        post = frontmatter.load(path)
        if post.metadata:
            yield str(path.relative_to(EXAMPLES_DIR)), post.metadata


_PROFILES = list(_example_profiles())


def test_examples_directory_found() -> None:
    assert _PROFILES, f"no frontmattered example profiles under {EXAMPLES_DIR}"


@pytest.mark.parametrize("rel_path,meta", _PROFILES, ids=[p for p, _ in _PROFILES])
def test_example_profile_provider_exists(rel_path: str, meta: dict) -> None:
    provider = meta.get("provider")
    if provider is not None:
        assert provider in VALID_PROVIDERS, (
            f"{rel_path}: provider {provider!r} is not a known ProviderType "
            f"(valid: {sorted(VALID_PROVIDERS)})"
        )
    # Supervisor profiles can override worker providers inline.
    for agent in meta.get("agents", []) or []:
        if isinstance(agent, dict) and "provider" in agent:
            assert (
                agent["provider"] in VALID_PROVIDERS
            ), f"{rel_path}: worker provider {agent['provider']!r} is not a known ProviderType"
