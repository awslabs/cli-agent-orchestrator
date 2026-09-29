"""CI supply-chain guards for ``.github/workflows`` (#820).

Two properties a green CI run cannot attest on its own:

* every third-party action is referenced by a full commit SHA, not a tag a
  maintainer of that action can move;
* every workflow that runs ``uv`` executes against the committed ``uv.lock``
  and fails if ``pyproject.toml`` has drifted from it. ``uv sync --locked``
  covers the sync; ``UV_LOCKED`` at workflow level covers the ``uv run`` and
  ``uv export`` sites (and the ``uv run`` calls inside ``make`` targets) that
  carry no flag. Measured on uv 0.12: ``UV_FROZEN`` would install the OLD lock
  silently, and a flag beats the variable in either direction (uv warns
  "Ignoring UV_LOCKED because --frozen was provided" and proceeds), so the flag
  spelled at each command must be ``--locked`` too.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
UV_COMMAND = re.compile(r"\buv (sync|run|export|lock)\b")


def _workflows():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflows found"
    return files


def _steps(doc):
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            yield step


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    unpinned = []
    for step in _steps(doc):
        uses = step.get("uses")
        if not uses or uses.startswith("./") or uses.startswith("docker://"):
            continue  # local composite actions and images are not tag references
        if not SHA_PINNED.match(uses):
            unpinned.append(uses)
    assert unpinned == [], f"{path.name}: actions referenced by tag or branch: {unpinned}"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_uv_runs_against_the_committed_lock(path):
    text = path.read_text(encoding="utf-8")
    if not UV_COMMAND.search(text):
        pytest.skip("workflow does not run uv")
    doc = yaml.safe_load(text)
    env = doc.get("env") or {}
    assert str(env.get("UV_LOCKED")) == "1", f"{path.name}: workflow-level env.UV_LOCKED is not '1'"
    frozen = [
        line.strip() for line in text.splitlines() if UV_COMMAND.search(line) and "--frozen" in line
    ]
    assert frozen == [], f"{path.name}: --frozen overrides UV_LOCKED; use --locked: {frozen}"


def test_dependabot_moves_the_action_pins():
    """A SHA pin nobody updates is a pin to a growing set of known bugs."""
    config = yaml.safe_load((WORKFLOWS.parent / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {u["package-ecosystem"] for u in config["updates"]}
    assert "github-actions" in ecosystems
