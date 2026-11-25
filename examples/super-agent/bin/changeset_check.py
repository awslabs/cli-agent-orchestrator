#!/usr/bin/env python3
"""Compare the file changes represented by two git revision ranges.

Rename detection is off in every diff (--no-renames): a move is its deletion and its addition, so
no path, line count or line is hidden by a rename. Path listings are -z (NUL-separated, never
C-quoted), and each path is looked up as a literal pathspec, so a path with a non-ASCII byte, tab,
newline, quote or glob character is compared as itself."""
import argparse
import subprocess
import sys


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "--literal-pathspecs", *args], text=True,
                          errors="surrogateescape", capture_output=True, check=True).stdout


def nul_records(text):
    """The records of a -z listing; a non-empty one must end in NUL."""
    if not text:
        return []
    if not text.endswith("\0"):
        raise ValueError("not a -z listing")
    return [record for record in text[:-1].split("\0") if record]


def signature(repo, revision, path=None):
    args = ["diff", "--no-ext-diff", "--no-renames", revision]
    if path is not None:
        args += ["--", path]
    text = git(repo, *args)
    lines = [line for line in text.splitlines()
             if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    return sorted(lines)


def compare(repo, old, new):
    old_numstat = nul_records(git(repo, "diff", "--no-renames", "--numstat", "-z", old))
    new_numstat = nul_records(git(repo, "diff", "--no-renames", "--numstat", "-z", new))
    paths = set(nul_records(git(repo, "diff", "--no-renames", "--name-only", "-z", old)))
    paths.update(nul_records(git(repo, "diff", "--no-renames", "--name-only", "-z", new)))
    changed = []
    for path in sorted(paths):
        if signature(repo, old, path) != signature(repo, new, path):
            changed.append(path)
    return old_numstat == new_numstat and not changed, changed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo")
    ap.add_argument("old", help="OLD_BASE..OLD_HEAD")
    ap.add_argument("new", help="NEW_BASE..NEW_HEAD")
    args = ap.parse_args(argv)
    try:
        same, changed = compare(args.repo, args.old, args.new)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR git failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if same:
        print("SAME")
        return 0
    # a changed path with a newline, a tab or a non-ASCII byte prints as one escaped token
    shown = [p if p.isascii() and p.isprintable() else ascii(p) for p in changed]
    print("DIFF" + (": " + " ".join(shown) if changed else ": numstat"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
