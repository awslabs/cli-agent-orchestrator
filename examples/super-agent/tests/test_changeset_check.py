"""changeset_check.py compares paths git would C-quote as themselves.

Its own file so tests/mutate.py can run it against a kit copy (the cc_ mutants)."""
import os
import subprocess
import sys

import pytest

from conftest import KIT


def helper(name, *args):
    return subprocess.run([sys.executable, KIT / "bin" / name, *map(str, args)], text=True,
                          capture_output=True)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, errors="surrogateescape",
                          capture_output=True, check=True).stdout.strip()


# Without -z these names came back C-quoted, the per-path lookup matched nothing, and
# two ranges with the same line counts but different lines for that path compared SAME.
# ":(glob)z" is pathspec magic: it must be looked up as the literal name.
@pytest.mark.parametrize("path", [".github/\u00e9.yml", "a\tb", "a\nb", '".github/x"',
                                  os.fsdecode(b"\xff"), ":(glob)z"],
                         ids=["non_ascii", "tab", "newline", "literal_quotes", "non_utf8",
                              "pathspec_magic"])
def test_changeset_check_compares_a_path_git_would_quote_as_itself(tmp_path, path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"],
                 ["config", "user.name", "Test"]):
        _git(repo, *args)
    (repo / "base").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    heads = []
    for text in ("one\n", "two\n"):
        _git(repo, "checkout", "-q", "--detach", base)
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", text.strip())
        heads.append(_git(repo, "rev-parse", "HEAD"))
    one, two = heads
    same = helper("changeset_check.py", repo, f"{base}..{one}", f"{base}..{one}")
    assert (same.returncode, same.stdout.strip()) == (0, "SAME")
    # same numstat (1 added, 0 deleted, same path), different line: must be DIFF on that path
    diff = helper("changeset_check.py", repo, f"{base}..{one}", f"{base}..{two}")
    assert diff.returncode == 1
    shown = path if path.isascii() and path.isprintable() else ascii(path)
    assert diff.stdout.strip() == "DIFF: " + shown
