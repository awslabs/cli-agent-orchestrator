"""Follow-up regressions for the permission gates and their caller instructions."""
import json
import re
import shutil
import subprocess
import sys

import pytest

from conftest import BIN, KIT, base_env, run
from test_automerge_gate import ESCALATE, expect, gate, pr, review
from test_check_mandate import H1, REPO, URL, act, check, doc
from test_outbox import key, reserve


def push_check(tmp_path, remote_config=None):
    path = tmp_path / "mandates.json"
    path.write_text(json.dumps(act(doc())))
    args = ["--mandates", path, "--holder", "shepherd", "--action", "ff_fix_push",
            "--pr-author", "your-login", "--base", "main", "--expect-head", H1,
            "--remote-head", H1, "--branch", "fix", "--new-head", H1, "--local-head", H1,
            "--push-dest", URL, "--push-url", URL, "--fetch-url", URL, "--url-rewrites", ""]
    if remote_config is not None:
        args += ["--remote-config", remote_config]
    return run("check_mandate.py", *args, env=base_env(tmp_path))


def test_followup_missing_remote_config_denies_push(tmp_path):
    result = push_check(tmp_path)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_dest_needs:--remote-config")


def test_followup_remote_pushdefault_allows_push(tmp_path):
    config = tmp_path / "gitconfig"
    env = base_env(tmp_path)
    result = subprocess.run(["git", "config", "--file", str(config), "remote.pushDefault", "origin"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0
    result = subprocess.run(["git", "config", "--file", str(config), "--get-regexp", r"^remote\."],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0
    result = push_check(tmp_path, result.stdout)
    assert result.returncode == 0
    assert result.stdout.startswith("ALLOW")


@pytest.mark.parametrize("line", ["invalid", "url.origin.insteadOf https://example.invalid/",
                                  "", "remote."])
def test_followup_malformed_remote_config_denies(tmp_path, line):
    result = push_check(tmp_path, line + "\n")
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_remote_config_malformed")


def test_followup_two_repository_keys_deny_check(tmp_path):
    data = act(doc())
    data["push_urls"]["other/repo"] = "https://github.com/other/repo.git"
    assert check(tmp_path, data) == (1, "DENY unreadable_input:ValueError")


def test_followup_two_repository_keys_refuse_go_add(tmp_path):
    kit = tmp_path / "kit"
    shutil.copytree(BIN, kit / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "mandates.json").write_text(json.dumps({"push_urls": {
        REPO: URL, "other/repo": "https://github.com/other/repo.git"}}))
    result = subprocess.run([sys.executable, "-I", str(kit / "bin/go_ledger.py"), "add",
                             "--writer-role", "conductor", "--id", "test-go", "--action", "factual_fix_reply",
                             "--pr", "101", "--head", H1, "--policy-version", "test-1",
                             "--granted-by", "conductor"],
                            env=base_env(tmp_path, CAO_TERMINAL_ID="test-conductor"),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout.startswith("ERROR")
    assert not (tmp_path / "state/go.jsonl").exists()


@pytest.mark.parametrize("repo", ["owner/name/extra", "owner/name\n", "https://example.invalid/repo"])
def test_followup_invalid_repository_key_denies_wildcard_scope(tmp_path, repo):
    data = act(doc(push_urls={repo: URL}))
    data["mandates"][0]["scope"]["repo"] = "*"
    assert check(tmp_path, data) == (1, "DENY unreadable_input:ValueError")


@pytest.mark.parametrize("field", ["author", "base"])
def test_followup_whitespace_scope_denies_wildcard_mandate(tmp_path, field):
    data = doc()
    data["mandates"][1]["scope"].update(pr_author="*", base="*")
    args = {"holder": "conductor", "action": "approve_review_plan", field: "   "}
    rc, out = check(tmp_path, data, **args)
    assert rc == 1
    assert out.startswith("DENY missing_pr_scope")


@pytest.mark.parametrize("pending_pr", [101, 102])
def test_followup_damaged_pending_is_reported_and_blocks(tmp_path, pending_pr):
    old = key(tmp_path, pr=pending_pr, head="a" * 40)
    assert reserve(tmp_path, old, pr=pending_pr, head="a" * 40).returncode == 0
    (tmp_path / "state/outbox" / (old + ".pending")).write_text("{broken")
    new = key(tmp_path, head="b" * 40)
    result = reserve(tmp_path, new, head="b" * 40)
    assert result.returncode == 4
    assert result.stdout == "PENDING (a damaged reservation must be reconciled first)\n"
    assert not (tmp_path / "state/outbox" / (new + ".pending")).exists()


@pytest.mark.parametrize("revoked", [False, True])
def test_followup_update_cap_does_not_widen_on_revocation(tmp_path, revoked):
    data = json.loads((KIT / "mandates.json").read_text())
    arm = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    arm["conditions"]["max_changed_lines"] = 300
    arm["revoked"] = revoked
    result = gate(tmp_path, pr(mergeStateStatus="BEHIND", additions=301, deletions=0),
                  [review()], mandates=data, action="update_branch")
    assert expect(result, ESCALATE, "ESCALATE_SIZE") == "ESCALATE_SIZE lines=301 cap=300"


def test_followup_update_cap_never_exceeds_default(tmp_path):
    data = json.loads((KIT / "mandates.json").read_text())
    arm = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    arm["conditions"]["max_changed_lines"] = 5000
    result = gate(tmp_path, pr(mergeStateStatus="BEHIND", additions=1501, deletions=0),
                  [review()], mandates=data, action="update_branch")
    assert expect(result, ESCALATE, "ESCALATE_SIZE") == "ESCALATE_SIZE lines=1501 cap=1500"


@pytest.mark.parametrize("rel,command", [("shepherd-playbook.md", "check_mandate.py --holder"),
                                         ("shepherd-playbook.md", "holds.py clear"),
                                         ("bin/holds.py", "holds.py clear"),
                                         ("bin/check_mandate.py", "check_mandate.py --holder")])
def test_followup_usage_has_no_fixed_author_or_base(rel, command):
    text = (KIT / rel).read_text()
    if rel.endswith(".py"):
        text = text.split('"""', 2)[1]
    if rel == "shepherd-playbook.md":
        blocks = [block.split("```", 1)[0] for block in text.split("```sh\n")[1:]
                  if command in block.split("```", 1)[0]]
        assert blocks
        for block in blocks:
            assert "gh pr view <N>" in block
            assert "--json author,baseRefName" in block
            assert block.index("gh pr view") < block.index(command)
            assert '--pr-author "$A" --base "$B"' in block
    lines = [line for line in text.splitlines() if command in line]
    assert lines
    for line in lines:
        assert not re.search(r"--pr-author\s+(?:your-login|fanhongy)\b", line)
        assert not re.search(r"--base\s+main\b", line)


def test_followup_conductor_plan_check_uses_live_scope():
    text = (KIT / "conductor-playbook.md").read_text().split("## 5. Reviewer plan approval", 1)[1]
    step = text.split("3. The workflow", 1)[0]
    assert "gh pr view <N>" in step
    assert '--json author,baseRefName > "$D/review-scope.json"' in step
    assert step.index("gh pr view") < step.index("check_mandate.py --holder")
    assert '--pr-author "$A" --base "$B"' in step


def test_followup_prewrite_summary_includes_scope_and_remote_config():
    text = (KIT / "shepherd-playbook.md").read_text().split("8. **Every publisher", 1)[1]
    step = text.split("9. **Posts:", 1)[0]
    assert "fresh `gh pr view" in step
    assert '--pr-author "$A" --base "$B"' in step
    assert '--remote-config "$RC"' in step
    assert "URL-named remote sections" in step
