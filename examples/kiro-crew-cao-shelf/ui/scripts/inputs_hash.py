#!/usr/bin/env python3
"""The content hash of the page bundle's build inputs, so freshness never depends on file times.

A fresh clone writes files in index order, so ``ui/src`` is newer than ``ui/bundle`` on disk even
when the committed bundle was built from exactly those sources. The build records this hash in
``ui/bundle/.inputs-sha256``; ``install.sh`` recomputes it and rebuilds only when the two differ.

Inputs: the source files the build reads under ``ui/src`` (an extension allowlist, ``SOURCE_SUFFIXES``;
any path part that starts with ``.`` is skipped, so ``.DS_Store``, editor swap and lock files, and
backups such as ``App.tsx~`` or ``App.tsx.orig`` never count), plus ``package.json``,
``package-lock.json``, ``vite.config.ts`` and ``tsconfig.json``. It needs no git, so a copy from a
tarball hashes the same as a clone. Each contributes ``<path relative to ui/>\\0<sha256 of its
bytes>\\n``, in sorted path order; the hash is the sha256 of that text. Standard library only, so it
runs with any python3 and needs no Node.

Usage: inputs_hash.py [--write | --check] [UI_DIR]   (UI_DIR defaults to this script's ui/ folder)
  (no flag)  print the hash
  --write    write it to UI_DIR/bundle/.inputs-sha256 (the build does this after vite build)
  --check    exit 0 when the recorded hash equals the computed one, 1 when it differs, 2 when
             no hash is recorded
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

CONFIG_FILES = ("package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")
# what vite (with the React plugin) reads from ui/src; today only .ts and .tsx exist there
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".json", ".svg")
STAMP = Path("bundle") / ".inputs-sha256"


def is_source(rel: Path) -> bool:
    return rel.suffix in SOURCE_SUFFIXES and not any(part.startswith(".") for part in rel.parts)


def input_files(ui: Path) -> list[str]:
    names = [
        p.relative_to(ui).as_posix()
        for p in (ui / "src").rglob("*")
        if p.is_file() and is_source(p.relative_to(ui))
    ]
    names += [n for n in CONFIG_FILES if (ui / n).is_file()]
    return sorted(names)


def inputs_hash(ui: Path) -> str:
    outer = hashlib.sha256()
    for name in input_files(ui):
        inner = hashlib.sha256((ui / name).read_bytes()).hexdigest()
        outer.update(f"{name}\0{inner}\n".encode("utf-8"))
    return outer.hexdigest()


def main(argv: list[str]) -> int:
    flags = [a for a in argv if a.startswith("--")]
    rest = [a for a in argv if not a.startswith("--")]
    if len(flags) > 1 or len(rest) > 1 or (flags and flags[0] not in ("--write", "--check")):
        print(__doc__, file=sys.stderr)
        return 2
    ui = Path(rest[0]) if rest else Path(__file__).resolve().parents[1]
    digest = inputs_hash(ui)
    stamp = ui / STAMP
    if not flags:
        print(digest)
        return 0
    if flags[0] == "--write":
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(digest + "\n", encoding="utf-8")
        return 0
    if not stamp.is_file():
        return 2
    return 0 if stamp.read_text(encoding="utf-8").strip() == digest else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
