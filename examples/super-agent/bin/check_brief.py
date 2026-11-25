#!/usr/bin/env python3
"""Refuse a filled-in brief or assign message that still holds a template placeholder.

  check_brief.py [--brief | --message] FILE [FILE ...]        (- reads standard input)

--brief: FILE is a brief, and must say the worker stays in the foreground and leaves no job running in
the background (a line such as "Foreground only." or "Stay in the foreground"). A reviewer once went idle
on a background job and the workflow took that for done.

--message: FILE is a message passed on a shell command line (`cao launch "<msg>"`, or any CLI argument),
where a shell runs a backtick or `$(`. Either is refused; the brief goes in a <<'EOF' file and the message only points at it. A brief (a file) may
hold backticks.

A template placeholder is a lowercase name in single braces, such as {pr}, {kit} or {login}.
%{http_code} (curl), ${VAR} (shell) and JSON objects are not placeholders. Also refused: a
{{TOKEN}} left from a profile template, and a bare KIT/ or PB/ symbol, which a worker can't
resolve (the dispatcher writes the full path, from `workplace.py show`).

Prints `OK <file>` or `LEFT <file>: <what>` for each file. Exit 0 when every file is clean,
1 when any is not, 2 when a file can't be read. Reads only; stdlib only.
"""
import argparse
import re
import sys

PLACEHOLDER = re.compile(r"(?<![%$\w{])\{([a-z][a-z0-9_]*)\}(?!\})")
TOKEN = re.compile(r"\{\{[A-Za-z0-9_ ]*\}\}")
SYMBOL = re.compile(r"(?<![\w{/.$-])(KIT|PB)/")


def leftovers(text):
    """Sorted, de-duplicated things a worker can't act on: {name}, {{TOKEN}}, KIT/ and PB/."""
    found = {"{%s}" % m.group(1) for m in PLACEHOLDER.finditer(text)}
    found |= {m.group(0) for m in TOKEN.finditer(text)}
    found |= {m.group(0) for m in SYMBOL.finditer(text)}
    return sorted(found)


MESSAGE_FIX = "put the brief in a <<'EOF' file and point the message at it"


def shell_hazards(text):
    """(line number, what) for each backtick or $( in a message, which a shell runs on a command line."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for what in ("`", "$("):
            if what in line:
                found.append((n, what))
    return found


FOREGROUND = re.compile(r"\bforeground\b", re.I)
NEGATED = re.compile(r"(?:\b(?:not|never|no)\b|n't)[^.\n]{0,40}\bforeground\b", re.I)
FOREGROUND_FIX = ("add the line 'Foreground only. Never end your turn while a job you started is still running, "
                  "and never wait for a wakeup.'")


def has_foreground(text):
    """True when some line says foreground and doesn't deny it (\"not in the foreground\")."""
    return any(FOREGROUND.search(line) and not NEGATED.search(line) for line in text.splitlines())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", action="store_true", help="the files are briefs: they must carry the foreground sentence")
    ap.add_argument("--message", action="store_true", help="the files are messages passed on a shell command line (cao launch, any CLI argument): refuse a backtick or $(. Not needed for MCP assign or handoff messages")
    ap.add_argument("files", nargs="+", help="brief or message files; - for standard input")
    a = ap.parse_args(argv)
    if a.brief and a.message:
        ap.error("--brief and --message are different checks; run them on different files")
    rc = 0
    for name in a.files:
        try:
            text = sys.stdin.read() if name == "-" else open(name, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"UNREADABLE {name}: {type(exc).__name__}")
            return 2
        left = leftovers(text)
        hazards = shell_hazards(text) if a.message else []
        missing = a.brief and not has_foreground(text)
        if left or hazards or missing:
            if left:
                print(f"LEFT {name}: {', '.join(left)}")
            for n, what in hazards:
                print(f"LEFT {name}: line {n}: {what} in a message passed on a shell command line, where a shell runs it; {MESSAGE_FIX}")
            if missing:
                print(f"LEFT {name}: the foreground sentence is missing; {FOREGROUND_FIX}")
            rc = 1
        else:
            print(f"OK {name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
