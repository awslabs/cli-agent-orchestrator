"""Regression probes for permission, destination and outbox review findings."""
import copy
import json
import shutil
import subprocess
import sys

import pytest

from conftest import BIN, KIT, base_env, run
from test_automerge_gate import ESCALATE, expect, gate, pr, review
from test_check_mandate import H1, REPO, URL, act, doc
from test_outbox import BODY, body_file, f4_release, key, reserve


@pytest.mark.parametrize("position", ["stock", 0, 1])
def test_review_revoked_limit_cannot_widen(tmp_path, position):
    data = json.loads((KIT / "mandates.json").read_text())
    active = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    revoked = copy.deepcopy(active)
    revoked.update(id="revoked-wide", revoked=True)
    revoked["conditions"]["max_changed_lines"] = 5000
    if position != "stock":
        data["mandates"].insert(data["mandates"].index(active) + position, revoked)
    out = expect(gate(tmp_path, pr(additions=3000, deletions=0), [review()], mandates=data),
                 ESCALATE, "ESCALATE_SIZE")
    assert out == "ESCALATE_SIZE lines=3000 cap=1500"


@pytest.mark.parametrize("order", [False, True])
def test_review_smallest_active_limit(tmp_path, order):
    data = json.loads((KIT / "mandates.json").read_text())
    active = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    small = copy.deepcopy(active)
    small["id"] = "small-cap"
    small["conditions"]["max_changed_lines"] = 30
    data["mandates"] = [small, active] if order else [active, small]
    assert "cap=30" in expect(gate(tmp_path, pr(), [review()], mandates=data),
                              ESCALATE, "ESCALATE_SIZE")


@pytest.mark.parametrize("field,value", [("holder", "conductor"), ("granted_by", "agent"),
                                          ("revoked", True)])
def test_review_inactive_scope_ignored(tmp_path, field, value):
    import automerge_gate
    data = json.loads((KIT / "mandates.json").read_text())
    active = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    invalid = copy.deepcopy(active)
    invalid[field] = value
    invalid["scope"] = {"repo": "other/repo", "pr_author": "other"}
    invalid["conditions"]["never_prs"] = [999]
    data["mandates"] = [active, invalid]
    path = tmp_path / "mandates.json"
    path.write_text(json.dumps(data))
    got = automerge_gate.load_limits(path, "arm")
    assert got["repo"] == active["scope"]["repo"]
    assert got["pr_author"] == active["scope"]["pr_author"]
    assert 999 in got["never_prs"]


@pytest.mark.parametrize("action", ["factual_fix_reply", "open_pr"])
@pytest.mark.parametrize("missing", ["--pr-author", "--base"])
def test_review_missing_scope_is_deny(tmp_path, action, missing):
    path = tmp_path / "mandates.json"
    path.write_text(json.dumps(act(doc())))
    args = ["--mandates", path, "--holder", "shepherd", "--action", action,
            "--expect-head", H1, "--remote-head", H1,
            "--pr-author", "your-login", "--base", "main"]
    i = args.index(missing)
    del args[i:i + 2]
    result = run("check_mandate.py", *args, env=base_env(tmp_path))
    assert result.returncode == 1 and result.stdout.startswith("DENY missing_pr_scope")


def test_review_casefold_login_finds_existing_post(tmp_path):
    rows = [{"user": {"login": "Your-Login"}, "body": BODY,
             "html_url": "https://example.invalid/comment/1"}]
    _, found, released, pending = f4_release(tmp_path, rows)
    assert found.returncode == 0 and found.stdout.startswith("FOUND")
    assert released.returncode == 1 and pending.returncode == 4
    next_head = "b" * 40
    another = key(tmp_path, head=next_head)
    assert reserve(tmp_path, another, head=next_head).returncode == 4


def test_review_pending_blocks_new_head_and_kind(tmp_path):
    first_head = "a" * 40
    first = key(tmp_path, head=first_head)
    assert reserve(tmp_path, first, head=first_head).returncode == 0
    for head, kind in (("b" * 40, "reply"), (first_head, "review")):
        other = key(tmp_path, head=head, kind=kind)
        result = reserve(tmp_path, other, head=head, kind=kind)
        assert result.returncode == 4 and result.stdout.startswith("PENDING")
        assert not (tmp_path / "state/outbox" / (other + ".pending")).exists()
    unrelated = key(tmp_path, pr=102)
    assert reserve(tmp_path, unrelated, pr=102).returncode == 0


@pytest.mark.parametrize("name", [URL, "https://example.invalid/mirror"])
def test_review_url_remote_section_refused(tmp_path, name):
    import check_mandate
    extra = {"push_dest": URL, "push_url": URL, "fetch_url": URL, "url_rewrites": "",
             "remote_config": f"remote.{name}.url https://example.invalid/other"}
    reason = check_mandate.check_destination(act(doc()), REPO, extra)
    assert reason is not None and reason.startswith("push_remote_name")


@pytest.mark.parametrize("login,word", [("alice", "ALLOW"), ("colleague", "DENY wrong_author")])
def test_review_readme_setup_then_template_allows(tmp_path, login, word):
    """Run the shipped setup substitution, then a template check with fake GitHub reads."""
    import re
    kit = tmp_path / "kit"
    shutil.copytree(KIT, kit, ignore=shutil.ignore_patterns("__pycache__"))
    readme = (kit / "README.md").read_text()
    start = readme.index("   grep -rl")
    end = readme.index("   cp pr-watch", start)
    setup = readme[start:end].replace("OWNER/REPO", "acme/widgets").replace("LOGIN", "alice")
    result = subprocess.run(["/bin/bash", "-c", setup], cwd=kit,
                            env=base_env(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads((kit / "mandates.json").read_text())
    data["shepherd_mode"] = "act"
    (kit / "mandates.json").write_text(json.dumps(data))
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    (fake / "gh").write_text("#!/bin/sh\nprintf '%s\\n' '"
                            + json.dumps({"author": {"login": login}, "baseRefName": "main"}) + "'\n")
    (fake / "git").write_text("#!/bin/sh\nprintf '%s\\n' '" + H1 + "'\n")
    for file in fake.iterdir():
        file.chmod(0o755)
    template = next(line for line in (kit / "templates/publisher.md").read_text().splitlines()
                    if line.startswith("8. ")).split("  -> ")[0]
    template = template[template.find("gh pr view") if "gh pr view" in template
                        else template.index("R=$(git"):]
    replacements = {"pr": "101", "post_head": H1, "policy_version": data["policy_version"],
                    "body_file": str(body_file(tmp_path)), "go_post": "",
                    "scope_file": str(tmp_path / "scope.json")}
    template = re.sub(r"\{([^{}]+)\}", lambda m: replacements[m.group(1)], template)
    env = base_env(tmp_path, PATH=str(fake) + ":/usr/bin:/bin")
    result = subprocess.run(["/bin/bash", "-c", "K=" + str(kit / "bin") + "; " + template],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == (0 if word == "ALLOW" else 1), result.stdout + result.stderr
    assert result.stdout.startswith(word), result.stdout + result.stderr
    result = subprocess.run([sys.executable, "-I", str(kit / "bin/go_ledger.py"), "add",
                             "--writer-role", "conductor", "--id", "setup-go", "--action", "factual_fix_reply",
                             "--pr", "101", "--head", H1, "--policy-version", data["policy_version"],
                             "--granted-by", "conductor"],
                            env=base_env(tmp_path, CAO_TERMINAL_ID="test-conductor"),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads((tmp_path / "state/go.jsonl").read_text())
    assert row["repo"] == "acme/widgets"


def test_review_go_repository_comes_from_mandates(tmp_path):
    kit = tmp_path / "kit"
    shutil.copytree(BIN, kit / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "mandates.json").write_text(json.dumps({"push_urls": {"acme/widgets": "https://github.com/acme/widgets.git"}}))
    result = subprocess.run([sys.executable, "-I", str(kit / "bin/go_ledger.py"), "add",
                             "--writer-role", "conductor", "--id", "setup-go", "--action", "factual_fix_reply",
                             "--pr", "101", "--head", H1, "--policy-version", "test-1",
                             "--granted-by", "conductor"],
                            env=base_env(tmp_path, CAO_TERMINAL_ID="test-conductor"),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "state/go.jsonl").read_text())["repo"] == "acme/widgets"


@pytest.mark.parametrize("rel", ["templates/publisher.md", "templates/automerge-armer.md"])
def test_review_every_existing_pr_check_uses_live_scope_file(rel):
    import re
    lines = (KIT / rel).read_text().splitlines()
    checks = [line for line in lines if re.match(r"\d+\.", line) and "check_mandate.py --holder" in line]
    for line in checks:
        if "--action open_pr" in line:
            assert "--pr-author your-login --base main" in line
            continue
        assert "gh pr view {pr}" in line
        assert "--json author,baseRefName > {scope_file}" in line
        assert line.index("gh pr view") < line.index("check_mandate.py")
        assert '--pr-author "$A" --base "$B"' in line
