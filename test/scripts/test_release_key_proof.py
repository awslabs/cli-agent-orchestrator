"""The release-key proof workflow can only prove; it cannot release or push.

``release-key-proof.yml`` is the one workflow besides the release chain that
reads ``RELEASE_DEPLOY_KEY``, a key that can push to ``main`` past branch
protection. These tests pin the properties that keep it harmless: it starts
only by hand, it declares the ``release-deploy`` environment (so the key is
given only on a ref that environment allows), it holds no write permission,
and its only push is a dry run.

They also pin WHO reads the key, across every workflow file, so a new reader
has to be added here on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Set, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PROOF_PATH = WORKFLOWS / "release-key-proof.yml"
PROOF_TEXT = PROOF_PATH.read_text(encoding="utf-8")
PROOF: Dict[Any, Any] = yaml.safe_load(PROOF_TEXT)
KEY = "secrets.RELEASE_DEPLOY_KEY"


def _job() -> Dict[str, Any]:
    (job,) = PROOF["jobs"].values()
    return job


def _run_lines() -> list:
    lines = []
    for step in _job()["steps"]:
        lines.extend(str(step.get("run", "")).splitlines())
    return [line.strip() for line in lines if line.strip()]


class TestTheProofCannotDoHarm:
    def test_it_starts_only_by_hand(self):
        # PyYAML folds the key `on:` to True.
        assert PROOF[True] == {"workflow_dispatch": None}

    def test_the_job_declares_the_release_deploy_environment(self):
        assert _job()["environment"] == "release-deploy"

    def test_it_holds_no_write_permission(self):
        assert PROOF["permissions"] == {"contents": "read"}
        assert "permissions" not in _job()

    def test_it_runs_only_in_this_repository(self):
        assert _job()["if"] == "github.repository == 'awslabs/cli-agent-orchestrator'"

    def test_every_push_is_a_dry_run(self):
        pushes = [line for line in _run_lines() if re.search(r"\bgit\s+push\b", line)]
        assert pushes, "the write check is gone"
        for line in pushes:
            assert "--dry-run" in line, line

    def test_the_dry_run_does_not_target_a_real_branch(self):
        (push,) = [line for line in _run_lines() if re.search(r"\bgit\s+push\b", line)]
        assert push.endswith("HEAD:refs/heads/release-key-proof-dry-run")

    def test_it_reads_no_other_secret(self):
        assert set(re.findall(r"secrets\.(\w+)", PROOF_TEXT)) == {"RELEASE_DEPLOY_KEY"}

    def test_the_only_action_is_the_checkout_the_release_uses(self):
        used = {step["uses"] for step in _job()["steps"] if "uses" in step}
        release = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
        release_checkouts = {
            step["uses"]
            for step in release["jobs"]["release"]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        }
        assert used == release_checkouts


class TestTheProofCannotPassWithoutTheKey:
    """An empty secret makes actions/checkout fall back to the token over HTTPS."""

    def test_an_empty_key_fails_before_the_checkout(self):
        steps = _job()["steps"]
        guard = next(i for i, step in enumerate(steps) if "KEY_PRESENT" in str(step.get("env")))
        checkout = next(i for i, step in enumerate(steps) if "uses" in step)
        assert guard < checkout
        assert steps[guard]["env"] == {"KEY_PRESENT": "${{ secrets.RELEASE_DEPLOY_KEY != '' }}"}
        run = steps[guard]["run"]
        assert 'if [ "$KEY_PRESENT" != "true" ]; then' in run
        assert "exit 1" in run

    def test_the_key_value_is_passed_only_to_the_checkout(self):
        holders = [step for step in _job()["steps"] if "${{ " + KEY + " }}" in str(step)]
        assert [step.get("uses", "")[:17] for step in holders] == ["actions/checkout@"]

    def test_the_remote_must_be_ssh_before_the_key_is_trusted(self):
        text = "\n".join(_run_lines())
        assert "git remote get-url origin" in text
        assert "git@github.com:*)" in text
        ssh_check = text.index("git remote get-url origin")
        assert ssh_check < text.index("git ls-remote") < text.index("git push --dry-run")


class TestWhoReadsTheReleaseKey:
    """Every workflow file is walked, so a new reader cannot appear unnoticed."""

    READERS: Set[Tuple[str, str]] = {
        ("release.yml", "release"),
        ("publish-to-pypi.yml", "regenerate-agent-plugin"),
        ("release-key-proof.yml", "proof"),
    }

    @staticmethod
    def _files() -> list:
        files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        assert files
        return files

    def test_the_readers_are_exactly_the_known_three(self):
        found: Set[Tuple[str, str]] = set()
        for path in self._files():
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for name, job in (document.get("jobs") or {}).items():
                # The bare name, so `secrets['RELEASE_DEPLOY_KEY']` counts too.
                if "RELEASE_DEPLOY_KEY" in yaml.safe_dump(job):
                    found.add((path.name, name))
        assert found == self.READERS

    def test_the_key_is_named_only_inside_jobs(self):
        """Not in workflow-level ``env:`` or anywhere else the per-job walk skips."""
        for path in self._files():
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            outside_jobs = {key: value for key, value in document.items() if key != "jobs"}
            assert "RELEASE_DEPLOY_KEY" not in yaml.safe_dump(outside_jobs), path.name

    def test_no_workflow_reads_secrets_wholesale(self):
        """These reach the key without naming it, so the walk above would miss them."""
        for path in self._files():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"toJSON\(\s*secrets\s*\)", text, re.IGNORECASE), path.name
            assert not re.search(r"secrets\s*:\s*inherit", text), path.name
            for index in re.findall(r"secrets\s*\[([^\]]*)\]", text):
                assert re.fullmatch(r"\s*'[A-Za-z0-9_]+'\s*", index), (path.name, index)

    def test_no_composite_action_reads_it(self):
        actions = REPO_ROOT / ".github" / "actions"
        for path in sorted(actions.rglob("*.y*ml")):
            assert "RELEASE_DEPLOY_KEY" not in path.read_text(encoding="utf-8"), path
