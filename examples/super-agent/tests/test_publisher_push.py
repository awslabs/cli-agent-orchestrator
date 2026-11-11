"""the publisher's push publishes exactly the GO's new_head. it publishes only to the
repo's authorised URL. Runs the template's own PUSH commands (steps 1, 2, 4, 5, 6; step 3 is a
GitHub read) against scratch bare remotes under tmp_path, with a copy of the kit whose
mandates.json push_urls names the scratch "authorised" remote. Local git only: no network, no
real remote."""
import json
import re
import shutil
import subprocess

import pytest

from conftest import BIN, KIT, base_env, ledger

TEMPLATE = KIT / "templates" / "publisher.md"
BRANCH, PR = "fix/x", "101"


def steps():
    m = re.search(r"## Assign message\n```text\n(.*?)```", TEMPLATE.read_text(), re.S)
    assert m, "publisher.md has no Assign message block"
    block = m.group(1)
    return {m.group(1): m.group(2) for m in re.finditer(r"^(R?\d+)\. (.*)$", block, re.M)}


def command(step, values=None):
    cmd = steps()[step].split("  -> ")[0]
    if values is None:
        return cmd
    for k, v in values.items():
        cmd = cmd.replace("{" + k + "}", v)
    assert "{" not in cmd.replace("%{url_effective}", "").replace("%{http_code}", ""), cmd
    return cmd


def git_env(tmp_path):
    return base_env(tmp_path, GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL="/dev/null",
                    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(tmp_path, *args):
    r = subprocess.run(["git", *args], capture_output=True, text=True, env=git_env(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(tmp_path, wt, name):
    (wt / f"{name}.txt").write_text(name)
    git(tmp_path, "-C", wt, "add", f"{name}.txt")
    git(tmp_path, "-C", wt, "commit", "-q", "-m", f"fix: {name}")
    return git(tmp_path, "-C", wt, "rev-parse", "HEAD")


def kit_copy(tmp_path, url):
    """bin/ and mandates.json of the kit under test, authorising `url` for the repo."""
    dest = tmp_path / "kit"
    shutil.copytree(BIN, dest / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    doc = json.loads((BIN.parent / "mandates.json").read_text())
    doc["push_urls"] = {"your-org/your-repo": url}
    (dest / "mandates.json").write_text(json.dumps(doc))
    return dest / "bin"


@pytest.fixture
def repo(tmp_path):
    remote, other, wt = tmp_path / "remote.git", tmp_path / "other.git", tmp_path / "wt"
    git(tmp_path, "init", "-q", "--bare", remote)
    git(tmp_path, "init", "-q", "--bare", other)  # an unauthorised repository
    git(tmp_path, "init", "-q", "-b", BRANCH, wt)
    git(tmp_path, "-C", wt, "remote", "add", "origin", str(remote))
    a = commit(tmp_path, wt, "A")
    git(tmp_path, "-C", wt, "push", "-q", "origin", f"{BRANCH}:refs/heads/{BRANCH}")
    git(tmp_path, "-C", wt, "push", "-q", str(other), f"{BRANCH}:refs/heads/{BRANCH}")
    b = commit(tmp_path, wt, "B")  # the verified, approved commit
    return {"remote": remote, "other": other, "wt": wt, "a": a, "b": b,
            "bin": kit_copy(tmp_path, str(remote))}


def remote_head(tmp_path, remote):
    return git(tmp_path, "--git-dir", remote, "rev-parse", f"refs/heads/{BRANCH}")


def go(tmp_path, old, new):
    pv = json.loads((BIN.parent / "mandates.json").read_text())["policy_version"]
    r = ledger("add", "--id", "w-push", "--action", "ff_fix_push", "--pr", PR,
            "--branch", BRANCH, "--head", old, "--new-head", new, "--policy-version", pv,
            "--granted-by", "conductor test GO", env=base_env(tmp_path))
    assert r.returncode == 0, r.stdout
    return pv


def step(tmp_path, n, values):
    cmd = f"K={values['_bin']}; " + command(n, {k: v for k, v in values.items() if k != "_bin"})
    return subprocess.run(["/bin/bash", "-c", cmd], capture_output=True, text=True,
                          env=git_env(tmp_path), timeout=60)


def values(repo, pv):
    return {"worktree": str(repo["wt"]), "branch": BRANCH, "new": repo["b"], "old": repo["a"],
            "pr": PR, "push_action": "ff_fix_push", "policy_version": pv, "go_push": "--go-id w-push",
            "push_url": str(repo["remote"]), "_bin": str(repo["bin"])}


def test_template_push_never_names_a_bare_branch():
    pushes = [n for n, text in steps().items()  # `remote get-url --push` only reads the config
              if re.search(r"\bgit\b[^;|&]*\bpush\b", command(n).replace("get-url --push", "get-url"))]
    assert pushes == ["5"]
    assert command("5") == "git -C {worktree} push {push_url} {new}:refs/heads/{branch}"


def test_template_reads_and_pushes_the_verified_url_never_origin():
    """ls-remote (4, 6) and push (5) use {push_url}; origin is only inspected, in step 4."""
    for n in ("4", "5", "6"):
        assert not re.search(r"\b(?:ls-remote|push) origin\b", command(n)), n
    assert "ls-remote {push_url} refs/heads/{branch}" in command("4")
    assert command("6") == "git -C {worktree} ls-remote {push_url} refs/heads/{branch} | cut -f1"
    for part in ("remote get-url --all origin", "remote get-url --push --all origin",
                 "config --get-regexp '^url\\.'", '--push-dest {push_url} --push-url "$PU" '
                 '--fetch-url "$FU" --url-rewrites "$RW"'):
        assert part in command("4"), part


def test_real_kit_authorises_exactly_the_cao_url():
    doc = json.loads((BIN.parent / "mandates.json").read_text())
    assert doc["push_urls"] == {"your-org/your-repo":
                                "https://github.com/your-org/your-repo.git"}


def test_branch_extended_to_unapproved_c_is_refused_before_the_push(tmp_path, repo):
    """GO for B from remote A; the branch then moves to unapproved C."""
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    c = commit(tmp_path, repo["wt"], "C")
    r = step(tmp_path, "1", v)
    assert r.stdout.strip() == c != repo["b"]               # step 1 already shows the mismatch
    assert step(tmp_path, "2", v).stdout.strip() == "FF-OK"
    r = step(tmp_path, "4", v)                              # the final check refuses
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.strip() == f"DENY local_head_moved:new_head={repo['b'][:8]} local={c[:8]}"
    assert remote_head(tmp_path, repo["remote"]) == repo["a"]  # nothing was published


def test_approved_b_is_pushed_exactly_even_if_the_branch_moves_after_the_check(tmp_path, repo):
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    assert step(tmp_path, "1", v).stdout.strip() == repo["b"]
    assert step(tmp_path, "2", v).stdout.strip() == "FF-OK"
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (0, "ALLOW M1-ff-fix-push go=w-push"), r.stdout + r.stderr
    c = commit(tmp_path, repo["wt"], "C")                   # a race after the check
    assert step(tmp_path, "5", v).returncode == 0
    assert remote_head(tmp_path, repo["remote"]) == repo["b"] != c
    assert step(tmp_path, "6", v).stdout.strip() == repo["b"]


def test_go_for_another_commit_is_refused(tmp_path, repo):
    v = values(repo, go(tmp_path, repo["a"], repo["a"]))    # GO names A, the branch is at B
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (1, "DENY go_mismatch:new_head:w-push")
    assert remote_head(tmp_path, repo["remote"]) == repo["a"]


def test_remote_moved_is_refused_and_push_is_never_forced(tmp_path, repo):
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", BRANCH, str(repo["remote"]), other)
    x = commit(tmp_path, other, "X")
    git(tmp_path, "-C", other, "push", "-q", "origin", f"{BRANCH}:refs/heads/{BRANCH}")
    r = step(tmp_path, "4", v)
    assert r.returncode == 1 and r.stdout.startswith("DENY head_moved"), r.stdout
    assert step(tmp_path, "5", v).returncode != 0           # and the exact push is not a force
    assert remote_head(tmp_path, repo["remote"]) == x


# --- the push destination is bound to the authorised URL ---

def refused_before_push(tmp_path, repo, want):
    """Steps 1, 2 and 4 of the template; step 4 must refuse with `want`; both remotes stay at A."""
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    assert step(tmp_path, "1", v).stdout.strip() == repo["b"]
    assert step(tmp_path, "2", v).stdout.strip() == "FF-OK"
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (1, f"DENY {want}"), r.stdout + r.stderr
    assert remote_head(tmp_path, repo["remote"]) == repo["a"]
    assert remote_head(tmp_path, repo["other"]) == repo["a"]


def test_f1_wrong_pushurl_set_before_the_check_is_refused_before_the_push(tmp_path, repo):
    """two bare remotes at A; origin's pushurl points at the other one before the check."""
    git(tmp_path, "-C", repo["wt"], "config", "remote.origin.pushurl", str(repo["other"]))
    refused_before_push(tmp_path, repo, "push_url_mismatch (not the authorised URL for "
                                        "your-org/your-repo)")


def test_f1_two_push_urls_are_refused(tmp_path, repo):
    git(tmp_path, "-C", repo["wt"], "config", "remote.origin.pushurl", str(repo["remote"]))
    git(tmp_path, "-C", repo["wt"], "config", "--add", "remote.origin.pushurl", str(repo["other"]))
    refused_before_push(tmp_path, repo, "push_url_count:2 (want exactly one URL)")


def test_f1_two_fetch_urls_are_refused(tmp_path, repo):
    git(tmp_path, "-C", repo["wt"], "config", "--add", "remote.origin.url", str(repo["other"]))
    git(tmp_path, "-C", repo["wt"], "config", "remote.origin.pushurl", str(repo["remote"]))
    refused_before_push(tmp_path, repo, "fetch_url_count:2 (want exactly one URL)")


def test_f1_origin_pointing_elsewhere_is_refused(tmp_path, repo):
    git(tmp_path, "-C", repo["wt"], "remote", "set-url", "origin", str(repo["other"]))
    git(tmp_path, "-C", repo["wt"], "config", "remote.origin.pushurl", str(repo["remote"]))
    refused_before_push(tmp_path, repo, "fetch_url_mismatch (not the authorised URL for "
                                        "your-org/your-repo)")


@pytest.mark.parametrize("kind", ["pushInsteadOf", "insteadOf"])
def test_f1_url_rewrite_is_refused(tmp_path, repo, kind):
    """A rewrite also applies to an explicit URL, so any url.*.(push)insteadOf refuses."""
    git(tmp_path, "-C", repo["wt"], "config", f"url.{repo['other']}.{kind}", str(repo["remote"]))
    refused_before_push(tmp_path, repo, "push_url_rewrite (an insteadOf/pushInsteadOf rule is configured)")


def test_f1_wrong_push_url_placeholder_is_refused(tmp_path, repo):
    """The creator filled {push_url} with another repository: the check refuses before the push."""
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    v["push_url"] = str(repo["other"])
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (
        1, "DENY push_dest_mismatch (not the authorised URL for your-org/your-repo)"), r.stdout
    assert remote_head(tmp_path, repo["other"]) == repo["a"]


def test_f1_trailing_dot_git_is_normalised(tmp_path, repo):
    """origin names the remote with ".git", the push_urls entry and {push_url} without it."""
    bare = str(repo["remote"])
    assert bare.endswith(".git")
    repo["bin"] = kit_copy(tmp_path / "k2", bare[:-4])
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    v["push_url"] = bare[:-4]
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (0, "ALLOW M1-ff-fix-push go=w-push"), r.stdout + r.stderr
    assert step(tmp_path, "5", v).returncode == 0
    assert remote_head(tmp_path, repo["remote"]) == repo["b"]
    assert remote_head(tmp_path, repo["other"]) == repo["a"]


def test_flows_c_and_d_through_the_template_with_the_bound_destination(tmp_path, repo):
    """(c) OBSERVE without a GO -> conductor GO -> ALLOW -> exact push of B -> B read back;
    (d) a later pause after a GO -> exit 4 PAUSED before the push, the remote stays put."""
    pv = json.loads((BIN.parent / "mandates.json").read_text())["policy_version"]
    v = values(repo, pv)
    v["go_push"] = ""
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (3, "OBSERVE M1-ff-fix-push"), r.stdout + r.stderr
    v = values(repo, go(tmp_path, repo["a"], repo["b"]))
    assert step(tmp_path, "4", v).returncode == 0
    assert step(tmp_path, "5", v).returncode == 0
    assert step(tmp_path, "6", v).stdout.strip() == repo["b"]
    c = commit(tmp_path, repo["wt"], "C")
    r = ledger("add", "--id", "w-push2", "--action", "ff_fix_push", "--pr", PR, "--branch", BRANCH,
               "--head", repo["b"], "--new-head", c, "--policy-version", pv, "--granted-by", "conductor GO",
               env=base_env(tmp_path))
    assert r.returncode == 0, r.stdout
    m = repo["bin"].parent / "mandates.json"
    doc = json.loads(m.read_text())
    doc["paused"] = True
    m.write_text(json.dumps(doc))
    v.update({"old": repo["b"], "new": c, "go_push": "--go-id w-push2"})
    r = step(tmp_path, "4", v)
    assert (r.returncode, r.stdout.strip()) == (4, "PAUSED"), r.stdout + r.stderr
    assert remote_head(tmp_path, repo["remote"]) == repo["b"]
    assert remote_head(tmp_path, repo["other"]) == repo["a"]
