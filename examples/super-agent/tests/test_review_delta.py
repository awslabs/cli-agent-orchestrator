"""Real Git destination probes and regressions for the delta review."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import BIN, KIT, base_env, run
from test_automerge_gate import ARM, DENY, expect, gate, pr, review
from test_check_mandate import REPO, URL, act, check, doc


def git(work, env, *args, optional=False):
    result = subprocess.run(["git", "-C", str(work), *map(str, args)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 or (optional and result.returncode == 1), result.stderr
    return result.stdout.strip()


@pytest.fixture
def destination_repo():
    # No dot before the space: that is the parser boundary in the reported bypass.
    with tempfile.TemporaryDirectory(prefix="destination-", dir=os.environ.get("TMPDIR")) as root:
        root = Path(root)
        env = base_env(root, GIT_CONFIG_NOSYSTEM="1", TMPDIR=str(root))
        work, good, evil = root / "work", root / "good.git", root / "evil.git"
        work.mkdir()
        for bare in (good, evil):
            subprocess.run(["git", "init", "--bare", str(bare)], env=env,
                           capture_output=True, check=True)
        git(work, env, "init")
        git(work, env, "config", "user.name", "Test")
        git(work, env, "config", "user.email", "test@example.invalid")
        git(work, env, "commit", "--allow-empty", "-m", "initial")
        old = git(work, env, "rev-parse", "HEAD")
        for bare in (good, evil):
            git(work, env, "push", bare.as_uri(), f"{old}:refs/heads/fix")
        git(work, env, "branch", "-M", "fix")
        git(work, env, "commit", "--allow-empty", "-m", "next")
        git(work, env, "remote", "add", "origin", good.as_uri())
        yield root, work, good, evil, old, env


def destination_check(root, work, want, old, env, dest=None, head_url=None):
    path = root / "mandates.json"
    path.write_text(json.dumps(act(doc(push_urls={REPO: want}))))
    # The same observations used by publisher.md step 4, from real Git configuration.
    fetch = git(work, env, "remote", "get-url", "--all", "origin")
    push = git(work, env, "remote", "get-url", "--push", "--all", "origin")
    rewrites = git(work, env, "config", "--get-regexp", r"^url\.", optional=True)
    remotes = git(work, env, "config", "--get-regexp", r"^remote\.")
    remote = git(work, env, "ls-remote", want if head_url is None else head_url,
                 "refs/heads/fix").split()[0]
    local = git(work, env, "rev-parse", "refs/heads/fix")
    assert remote == old
    return run("check_mandate.py", "--mandates", path, "--holder", "shepherd",
               "--action", "ff_fix_push", "--pr-author", "your-login", "--base", "main",
               "--branch", "fix", "--expect-head", old, "--remote-head", remote,
               "--new-head", local, "--local-head", local, "--push-dest", want if dest is None else dest,
               "--push-url", push, "--fetch-url", fetch, "--url-rewrites", rewrites,
               "--remote-config", remotes, env=env)


@pytest.mark.parametrize("source", ["pushurl", "url", "include", "global", "count"])
def test_delta_space_named_remote_denies(destination_repo, source):
    root, work, good, evil, old, env = destination_repo
    spaced = root / "my good.git"
    good.rename(spaced)
    want = spaced.as_uri().replace("%20", " ")
    assert "." not in want.split(" ")[0]
    git(work, env, "config", "remote.origin.url", want)
    key = f"remote.{want}.{'url' if source == 'url' else 'pushurl'}"
    if source == "include":
        included = root / "included.config"
        git(work, env, "config", "--file", included, key, evil.as_uri())
        git(work, env, "config", "include.path", included)
    elif source == "global":
        git(work, env, "config", "--global", key, evil.as_uri())
    elif source == "count":
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=key, GIT_CONFIG_VALUE_0=evil.as_uri())
    else:
        git(work, env, "config", key, evil.as_uri())
    result = destination_check(root, work, want, old, env)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY")


@pytest.mark.parametrize("key", ["remote.pushDefault", "remote.PUSHDEFAULT"])
def test_delta_pushdefault_allows_real_push(destination_repo, key):
    root, work, good, evil, old, env = destination_repo
    git(work, env, "remote", "add", "other", evil.as_uri())
    git(work, env, "config", key, "other")
    result = destination_check(root, work, good.as_uri(), old, env)
    assert result.returncode == 0
    assert result.stdout.startswith("ALLOW")
    new = git(work, env, "rev-parse", "refs/heads/fix")
    git(work, env, "push", good.as_uri(), f"{new}:refs/heads/fix")
    assert git(work, env, "ls-remote", good.as_uri(), "refs/heads/fix").split()[0] == new
    assert git(work, env, "ls-remote", evil.as_uri(), "refs/heads/fix").split()[0] == old


@pytest.mark.parametrize("key", ["remote.url", "remote.pushurl", "remote.receivepack",
                                 "remote.pushdefaults", "remote.xpushdefault", "remote.pushdefault2"])
def test_delta_other_two_level_remote_denies(destination_repo, key):
    root, work, good, evil, old, env = destination_repo
    git(work, env, "config", key, evil.as_uri())
    result = destination_check(root, work, good.as_uri(), old, env)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_remote_config_malformed")


@pytest.mark.parametrize("variable", ["url", "pushurl", "receivepack"])
def test_delta_dotless_url_named_section_denies(destination_repo, variable):
    root, work, good, evil, old, env = destination_repo
    dotless = root / "good"
    good.rename(dotless)
    want = dotless.as_uri()
    assert "." not in want
    git(work, env, "config", "remote.origin.url", want)
    git(work, env, "config", f"remote.{want}.{variable}", evil.as_uri())
    result = destination_check(root, work, want, old, env)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_remote_name")


@pytest.mark.parametrize("kind", ["remotes", "branches"])
@pytest.mark.parametrize("want", ["goodrepo", "host:repo", "../goodrepo"])
def test_delta_legacy_remote_denies_real_push(destination_repo, kind, want):
    root, work, good, evil, old, env = destination_repo
    good = good.rename(work / want)
    git(work, env, "config", "remote.origin.url", want)
    legacy = work / ".git" / kind
    legacy.mkdir()
    content = (f"URL: {evil.as_uri()}\nPush: refs/heads/*:refs/heads/*\n"
               if kind == "remotes" else f"{evil.as_uri()}\n")
    (legacy / want).write_text(content)
    result = destination_check(root, work, want, old, env, head_url=good.as_uri())
    # Bypass the gate only in this disposable probe: the legacy nickname redirects
    # a real push to the evil file:// repo, which the gate must never authorise.
    new = git(work, env, "rev-parse", "refs/heads/fix")
    if want == "host:repo" and kind == "branches":
        # Git refuses the legacy file's implicit colon-containing fetch refspec.
        pushed = subprocess.run(["git", "-C", str(work), "push", want,
                                 f"{new}:refs/heads/fix"], env=env,
                                capture_output=True, text=True, timeout=30)
        assert pushed.returncode != 0 and "invalid refspec" in pushed.stderr
        assert git(work, env, "ls-remote", evil.as_uri(), "refs/heads/fix").split()[0] == old
        assert git(work, env, "ls-remote", good.as_uri(), "refs/heads/fix").split()[0] == old
        assert result.returncode == 1
        assert result.stdout.startswith("DENY push_url_invalid")
        return
    git(work, env, "push", want, f"{new}:refs/heads/fix")
    redirected = "/" not in want
    assert git(work, env, "ls-remote", evil.as_uri(), "refs/heads/fix").split()[0] == (new if redirected else old)
    assert git(work, env, "ls-remote", good.as_uri(), "refs/heads/fix").split()[0] == (old if redirected else new)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_url_invalid")


@pytest.mark.parametrize("dest", ["goodrepo", "host:repo", "../goodrepo"])
def test_delta_schemeless_push_dest_cli_denies(destination_repo, dest):
    root, work, good, evil, old, env = destination_repo
    result = destination_check(root, work, good.as_uri(), old, env, dest=dest)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_dest_invalid")


@pytest.mark.parametrize("side", ["leading", "trailing"])
def test_delta_edge_whitespace_push_urls_denies(destination_repo, side):
    root, work, good, evil, old, env = destination_repo
    clean = good.as_uri()
    want = " " + clean if side == "leading" else clean + " "
    # Keep --push-dest clean so its independent refusal cannot mask the URL check.
    result = destination_check(root, work, want, old, env, dest=clean, head_url=clean)
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_url_invalid")


@pytest.mark.parametrize("want", ["goodrepo", "host:repo", "../goodrepo"])
def test_delta_schemeless_push_urls_refuse_go_add(tmp_path, want):
    from go_ledger import mandate_repo

    with pytest.raises(ValueError, match="^invalid push_urls URL$"):
        mandate_repo({"push_urls": {REPO: want}})
    kit = tmp_path / "kit"
    shutil.copytree(BIN, kit / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "mandates.json").write_text(json.dumps({"push_urls": {REPO: want}}))
    result = subprocess.run([sys.executable, "-I", str(kit / "bin/go_ledger.py"), "add",
                             "--writer-role", "conductor", "--id", "test-go", "--action", "factual_fix_reply",
                             "--pr", "101", "--head", "a" * 40, "--policy-version", "test-1",
                             "--granted-by", "conductor"],
                            env=base_env(tmp_path, CAO_TERMINAL_ID="test-conductor"),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout.startswith("ERROR unreadable or ambiguous mandates repository")
    assert not (tmp_path / "state/go.jsonl").exists()


@pytest.mark.parametrize("char", [" ", "\t", "\n", "\x01", "\x7f", "\x85", "\u00a0"])
@pytest.mark.parametrize("field", ["push_urls", "push_dest"])
def test_delta_unsafe_destination_denies(tmp_path, char, field):
    from check_mandate import check_destination

    unsafe = URL + char + "suffix"
    want = unsafe if field == "push_urls" else URL
    data = act(doc(push_urls={REPO: want}))
    extra = dict(push_dest=URL if field == "push_urls" else unsafe, push_url=want, fetch_url=want,
                 url_rewrites="", remote_config="")
    assert check_destination(data, REPO, extra) == (
        "push_url_invalid" if field == "push_urls" else "push_dest_invalid")


@pytest.mark.parametrize("char", [" ", "\t", "\n", "\x01", "\x7f", "\x85", "\u00a0"])
def test_delta_unsafe_push_dest_cli_denies(destination_repo, char):
    root, work, good, evil, old, env = destination_repo
    result = destination_check(root, work, good.as_uri(), old, env,
                               dest=good.as_uri() + char + "suffix")
    assert result.returncode == 1
    assert result.stdout.startswith("DENY push_dest_invalid")


@pytest.mark.parametrize("char", [" ", "\t", "\n", "\x00", "\x01", "\x7f", "\x85", "\u00a0"])
def test_delta_unsafe_push_urls_refuse_go_add(tmp_path, char):
    kit = tmp_path / "kit"
    shutil.copytree(BIN, kit / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "mandates.json").write_text(json.dumps({"push_urls": {REPO: URL + char + "suffix"}}))
    result = subprocess.run([sys.executable, "-I", str(kit / "bin/go_ledger.py"), "add",
                             "--writer-role", "conductor", "--id", "test-go", "--action", "factual_fix_reply",
                             "--pr", "101", "--head", "a" * 40, "--policy-version", "test-1",
                             "--granted-by", "conductor"],
                            env=base_env(tmp_path, CAO_TERMINAL_ID="test-conductor"),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout.startswith("ERROR")
    assert not (tmp_path / "state/go.jsonl").exists()


@pytest.mark.parametrize("field", ["author", "base"])
def test_delta_conductor_null_scope_denies(tmp_path, field):
    text = (KIT / "conductor-playbook.md").read_text().split("## 5. Reviewer plan approval", 1)[1]
    variable = "A" if field == "author" else "B"
    code = re.search(rf"{variable}=\$\(python3 -c '([^']+)'", text).group(1)
    scope = {"author": {"login": "your-login"}, "baseRefName": "main"}
    if field == "author":
        scope["author"]["login"] = None
    else:
        scope["baseRefName"] = None
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(scope))
    result = subprocess.run([sys.executable, "-I", "-c", code, str(path)],
                            env=base_env(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == "\n"
    rc, out = check(tmp_path, doc(), holder="conductor", action="approve_review_plan",
                    **{field: result.stdout.strip()})
    assert rc == 1
    assert out.startswith("DENY missing_pr_scope")


def test_delta_arm_ignores_revoked_cap(tmp_path):
    data = json.loads((KIT / "mandates.json").read_text())
    arm = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    arm["conditions"]["max_changed_lines"] = 800
    revoked = copy.deepcopy(arm)
    revoked.update(id="revoked-arm", revoked=True)
    revoked["conditions"]["max_changed_lines"] = 300
    data["mandates"].append(revoked)
    result = gate(tmp_path, pr(additions=800, deletions=0), [review()], mandates=data)
    assert expect(result, ARM, "ARM").endswith("lines=800")


@pytest.mark.parametrize("inactive", [dict(revoked=True), dict(revoked=None),
                                      dict(holder="conductor"), dict(granted_by="conductor")])
def test_delta_inactive_invalid_cap_denies_update(tmp_path, inactive):
    data = json.loads((KIT / "mandates.json").read_text())
    arm = next(m for m in data["mandates"] if m["action"] == "arm_automerge")
    arm.update(inactive)
    arm["conditions"]["max_changed_lines"] = None
    result = gate(tmp_path, pr(mergeStateStatus="BEHIND"), [review()],
                  mandates=data, action="update_branch")
    assert expect(result, DENY, "DENY").startswith("DENY malformed_input max_changed_lines")


def test_delta_rule_documents_update_cap():
    text = (KIT / "rules/auto-merge.md").read_text()
    assert "min(1500, every M6 cap, revoked ones included)" in text
