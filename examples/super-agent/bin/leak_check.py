#!/usr/bin/env python3
"""Find outward-text leak patterns without printing the matched value."""
import argparse
import re
import sys


# The single leak check for outward text (bodies and diffs); every hit blocks. Put the names that
# are private to you (your workplace folder, your local server, private agent names) in
# private_name: the shipped words are placeholders. `token` and `secret` match only in
# credential shapes (a token/secret name then = or : and a value of 8+ characters with a digit, or
# a known credential prefix), so prose such as "the token file" or "token: unavailable" stays CLEAN. An 8-hex word is an internal
# id, so bodies use full 40-character SHAs.
PATTERNS = {
    "private_path": re.compile(
        r"(?:/Users/(?!<you>(?:[^A-Za-z0-9_]|$))[^/\s]+|/home/[^/\s]+|/tmp/|/private/)"),
    "private_name": re.compile(r"my-workplace|my-local-server|my-decider-[\w-]*", re.I),
    "internal_id": re.compile(r"(?:\b[0-9a-f]{8}\b|brief\s+\d+)", re.I),
    "account_id": re.compile(r"\b\d{12}\b"),
    "instance_id": re.compile(r"\bi-(?!<instance-id>(?:[^A-Za-z0-9_]|$))[0-9a-f]+\b", re.I),
    "host_name": re.compile(r"\bip-[a-z0-9-]+\b", re.I),
    "html_comment": re.compile(r"<!--"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "credential": re.compile(
        r"(?:\b\w*(?i:token|secret)\w*[\"']?\s*[=:]\s*[\"']?(?=[\w\-./+]{8,})[A-Za-z_\-./+]*\d"
        r"|\bgh[opsur]_[A-Za-z0-9]{8,}|\bgithub_pat_\w{8,}"
        r"|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\bAKIA[0-9A-Z]{16}\b"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"),
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="input path, or - for stdin")
    args = ap.parse_args(argv)
    try:
        lines = sys.stdin.read().splitlines() if args.path == "-" else open(args.path, encoding="utf-8").read().splitlines()
    except OSError as exc:
        print(f"ERROR unreadable_input:{type(exc).__name__}", file=sys.stderr)
        return 2
    hits = [(kind, number) for number, line in enumerate(lines, 1)
            for kind, pattern in PATTERNS.items() if pattern.search(line)]
    if not hits:
        print("CLEAN")
        return 0
    for kind, number in hits:
        print(f"HIT {kind} line={number}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
