"""The Agent Plugins packages follow the CAO release (#1384).

``agent-plugin/*/plugin.json`` carried 2.5.3 while ``pyproject.toml`` was at 2.5.4. Two
separate things can cause that, and each has its own test here:

* the packages disagree with one another, or one surface of a package disagrees with
  the rest (the manifest, the Claude Code overlay, the ``mcp.json`` pin);
* the job that repins them after a publish never runs.

The second was the cause. ``regenerate-agent-plugin`` used the implicit ``success()``,
which is false when any ancestor job was skipped, and a ``pypi`` dispatch skips
``smoke-test``. So the publish succeeded and the repin was skipped (run 39339505140).

What this does NOT assert is ``committed == pyproject``. ``release.yml`` bumps
``pyproject.toml`` on ``main`` before the publish, so strict equality would redden
``main`` and every open PR for the whole release window; ``--check`` deliberately
tolerates that lag (``test_agent_plugin_pin_boundedness.py``, R4), and the repin job
enforces equality with ``--require-current-pin``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as bap  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"


def _json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mcp_pin(package: Path) -> str:
    args = _json(package / "mcp.json")["mcpServers"][bap.OPS_SERVER_KEY]["args"]
    prefix = f"{bap.PYPI_DISTRIBUTION}=="
    return next(a for a in args if isinstance(a, str) and a.startswith(prefix))[len(prefix) :]


def _versions_by_surface() -> Dict[str, str]:
    """Every place a committed package states the CAO version it was built at."""
    found: Dict[str, str] = {}
    for config in bap.PACKAGES:
        package = bap.PACKAGES_DIR / config.name
        found[f"{config.name}/plugin.json"] = _json(package / "plugin.json")["version"]
        found[f"{config.name}/.claude-plugin/plugin.json"] = _json(
            package / ".claude-plugin" / "plugin.json"
        )["version"]
        if config.ships_mcp:
            found[f"{config.name}/mcp.json pin"] = _mcp_pin(package)
    return found


class TestPackagesAgreeOnOneVersion:
    def test_every_surface_of_every_package_carries_one_version(self):
        surfaces = _versions_by_surface()
        # Not vacuous: all four packages, two manifests each, plus the operator's pin.
        assert len(surfaces) == 2 * len(bap.PACKAGES) + 1, surfaces
        assert (
            len(set(surfaces.values())) == 1
        ), f"the packages disagree on their version: {surfaces}"

    def test_the_committed_version_is_not_ahead_of_pyproject(self):
        """A package may lag the release it has not been repinned to; it may not lead it."""
        committed = next(iter(_versions_by_surface().values()))
        assert bap.pin_ordering_problems(committed, bap.package_version()) == []

    def test_a_package_that_disagrees_is_reported_by_the_build_check(self, monkeypatch, tmp_path):
        """The guard the build runs, shown to fire: one package a release behind the rest."""
        packages = tmp_path / "agent-plugin"
        for config in bap.PACKAGES:
            src = bap.PACKAGES_DIR / config.name
            dst = packages / config.name
            dst.mkdir(parents=True)
            (dst / "plugin.json").write_text((src / "plugin.json").read_text(encoding="utf-8"))
        victim = packages / bap.PACKAGES[-1].name / "plugin.json"
        manifest = json.loads(victim.read_text(encoding="utf-8"))
        manifest["version"] = "0.0.1"
        victim.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(bap, "PACKAGES_DIR", packages)

        version, problems = bap.committed_pin_report()
        assert version is None
        assert any("disagree on their version" in p for p in problems), problems


class TestTheRepinJobRunsAfterEveryPublish:
    """The cause of #1384, asserted on the parsed workflow like R3 in the boundedness tests."""

    @pytest.fixture
    def jobs(self) -> Dict[str, Any]:
        return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]

    EXPECTED_IF = "!cancelled() && needs.publish-pypi.result == 'success'"

    def test_the_condition_is_exactly_not_cancelled_and_a_successful_publish(self, jobs):
        """No ``if`` means the implicit ``success()``, false when an ancestor was skipped.

        ``!cancelled()`` rather than ``always()``: both run after a skipped ancestor, but
        ``always()`` also runs when the workflow is cancelled, and once ``publish-pypi`` has
        passed a cancel could then not stop the push to ``main``.

        Compared whole, after stripping the optional ``${{ }}`` wrapper and collapsing
        whitespace, so ``always() || ...`` or a negation cannot pass a substring check.
        """
        raw = str(jobs["regenerate-agent-plugin"].get("if", ""))
        condition = " ".join(raw.replace("${{", "").replace("}}", "").split())
        assert condition == self.EXPECTED_IF, raw

    def test_it_still_depends_only_on_the_publish_job(self, jobs):
        assert jobs["regenerate-agent-plugin"]["needs"] == "publish-pypi"

    def test_an_ancestor_is_skipped_on_the_path_that_failed(self, jobs):
        """Why the condition matters: publish-pypi itself runs with smoke-test skipped."""
        publish = jobs["publish-pypi"]
        assert "smoke-test" in publish["needs"]
        assert "needs.smoke-test.result == 'skipped'" in str(publish["if"])
