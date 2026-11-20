"""Marketplace catalogs for Claude Code and Codex (#1409).

The drift test reads the catalogs and each package's own ``plugin.json`` and
compares them directly. It does not call the renderer, so a renderer bug cannot
make the test agree with itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as builder  # noqa: E402

PACKAGES_DIR = REPO_ROOT / "agent-plugin"
CLAUDE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CODEX = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _packages() -> dict:
    return {
        d.name: _load(d / "plugin.json")
        for d in sorted(PACKAGES_DIR.iterdir())
        if d.is_dir() and (d / "plugin.json").is_file()
    }


def test_there_are_packages_to_compare():
    assert len(_packages()) >= 4


def test_claude_catalog_lists_every_package_once_and_matches_its_manifest():
    catalog = _load(CLAUDE)
    assert catalog["name"] == "cli-agent-orchestrator"
    assert catalog["owner"]["name"]
    assert catalog["metadata"]["description"]
    packages = _packages()
    names = [p["name"] for p in catalog["plugins"]]
    assert sorted(names) == sorted(packages), "each agent-plugin/* listed exactly once"
    for entry in catalog["plugins"]:
        manifest = packages[entry["name"]]
        assert entry["version"] == manifest["version"]
        assert entry["description"] == manifest["description"]
        assert entry["category"]
        assert entry["source"] == f"./agent-plugin/{entry['name']}"
        assert (REPO_ROOT / entry["source"]).is_dir()
    assert catalog["metadata"]["version"] == next(iter(packages.values()))["version"]


def test_codex_catalog_lists_every_package_once_and_matches_its_manifest():
    catalog = _load(CODEX)
    assert catalog["name"] == "cli-agent-orchestrator"
    assert catalog["interface"]["displayName"]
    packages = _packages()
    names = [p["name"] for p in catalog["plugins"]]
    assert sorted(names) == sorted(packages), "each agent-plugin/* listed exactly once"
    for entry in catalog["plugins"]:
        assert entry["source"]["source"] == "local"
        path = entry["source"]["path"]
        assert path == f"./agent-plugin/{entry['name']}"
        assert (REPO_ROOT / path / "plugin.json").is_file()
        assert entry["policy"]["installation"] in {
            "AVAILABLE",
            "INSTALLED_BY_DEFAULT",
            "NOT_AVAILABLE",
        }
        assert entry["policy"]["authentication"]
        assert entry["category"]


def test_builder_package_list_matches_directories():
    assert sorted(c.name for c in builder.PACKAGES) == sorted(_packages())


def test_build_redirected_at_another_packages_dir_leaves_the_checkout_alone(tmp_path, monkeypatch):
    before = (CLAUDE.read_bytes(), CODEX.read_bytes())
    monkeypatch.setattr(builder, "PACKAGES_DIR", tmp_path / "agent-plugin")
    builder.write_catalogs("9.9.9")
    assert (tmp_path / builder.CLAUDE_MARKETPLACE_REL).is_file()
    assert (CLAUDE.read_bytes(), CODEX.read_bytes()) == before


def test_check_passes_on_committed_catalogs():
    version = builder.committed_pin_report()[0]
    assert version
    assert builder.check_catalogs(version) == []


@pytest.mark.parametrize("which", ["claude", "codex"])
def test_check_fails_when_a_catalog_is_stale(tmp_path, monkeypatch, which):
    version = builder.committed_pin_report()[0]
    claude = tmp_path / builder.CLAUDE_MARKETPLACE_REL
    codex = tmp_path / builder.CODEX_MARKETPLACE_REL
    for src, dst in ((CLAUDE, claude), (CODEX, codex)):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
    stale = claude if which == "claude" else codex
    stale.write_text(stale.read_text().replace('"cao"', '"cao-renamed"', 1))
    monkeypatch.setattr(builder, "PACKAGES_DIR", tmp_path / "agent-plugin")
    problems = builder.check_catalogs(version)
    assert len(problems) == 1 and problems[0].startswith("CONTENT DIFFERS")


def test_check_fails_when_a_catalog_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "PACKAGES_DIR", tmp_path / "agent-plugin")
    assert [p.split()[0] for p in builder.check_catalogs("2.5.3")] == ["MISSING", "MISSING"]
