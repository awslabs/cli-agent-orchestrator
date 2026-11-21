#!/usr/bin/env python3
"""Compare a posted body (a path or stdin) with a prepared source file."""
import argparse
import difflib
import sys


def normalise(text):
    return text.replace("\r\n", "\n").rstrip("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("posted", help="posted body path, or - for stdin")
    ap.add_argument("source")
    args = ap.parse_args(argv)
    try:
        posted = sys.stdin.read() if args.posted == "-" else open(args.posted, encoding="utf-8").read()
        source = open(args.source, encoding="utf-8").read()
    except OSError as exc:
        print(f"ERROR unreadable_input:{type(exc).__name__}", file=sys.stderr)
        return 2
    posted, source = normalise(posted), normalise(source)
    if posted == source:
        print("BODY-OK")
        return 0
    diff = list(difflib.unified_diff(source.splitlines(), posted.splitlines(),
                                     fromfile="source", tofile="posted", n=1))
    print("BODY-DIFF")
    print("\n".join(diff[:12]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
