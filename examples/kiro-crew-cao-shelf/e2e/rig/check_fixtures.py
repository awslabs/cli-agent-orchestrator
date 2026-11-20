#!/usr/bin/env python3
"""Fail when the committed fixtures differ from what the generator writes, or break a schema.

    python3 e2e/rig/check_fixtures.py

The generator writes next to itself (``fixtures/_gen/make_fixtures.py`` -> ``fixtures/``). To keep the
checked-in files untouched, this copies ``fixtures/`` to a scratch directory, runs the COPY of the
generator there, and compares the two trees. Then it validates ``fixtures/expected/*.json`` against
``schemas/*.json`` (``jsonschema``, Draft 2020-12) when jsonschema is installed. Exit 0 when both pass.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXAMPLE = Path(os.environ.get("CHECK_FIXTURES_ROOT") or Path(__file__).resolve().parents[2])


def scratch_root() -> Path | None:
    base = os.environ.get("SHELF_SCRATCH")
    if base and Path(base).is_dir():
        path = Path(base) / "work-c"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return None  # CI: the default temporary directory is fine there


def tree_diff(left: Path, right: Path) -> list[str]:
    problems: list[str] = []

    def walk(cmp: filecmp.dircmp, prefix: str) -> None:
        problems.extend(
            f"only in committed: {prefix}{n}" for n in cmp.left_only if n != "__pycache__"
        )
        problems.extend(
            f"only generated: {prefix}{n}" for n in cmp.right_only if n != "__pycache__"
        )
        problems.extend(f"differs: {prefix}{n}" for n in cmp.diff_files)
        for name, sub in cmp.subdirs.items():
            if name != "__pycache__":
                walk(sub, f"{prefix}{name}/")

    walk(filecmp.dircmp(left, right), "")
    return problems


def main() -> int:
    fixtures = EXAMPLE / "fixtures"
    parent = scratch_root()
    with tempfile.TemporaryDirectory(dir=parent) as tmp:
        copy = Path(tmp) / "fixtures"
        shutil.copytree(fixtures, copy, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(
            [sys.executable, str(copy / "_gen" / "make_fixtures.py")],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        problems = tree_diff(fixtures, copy)
    if problems:
        print("fixtures drift (run: make fixtures):", *problems[:20], sep="\n  ")
        return 1
    try:
        import jsonschema
    except ImportError:
        print(
            "check-fixtures: generator output matches; jsonschema not installed, schema validation skipped"
        )
        return 0
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    schemas = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((EXAMPLE / "schemas").glob("*.schema.json"))
    }
    registry = Registry().with_resources(
        [
            (doc.get("$id", name), Resource.from_contents(doc, default_specification=DRAFT202012))
            for name, doc in schemas.items()
        ]
    )

    def check(doc_path: Path, schema_name: str, defn: str | None = None) -> None:
        schema = schemas[schema_name]
        if defn is not None:
            schema = {"$ref": f"{schema['$id']}#/$defs/{defn}"}
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
        validator.validate(json.loads(doc_path.read_text(encoding="utf-8")))

    for name in ("busy.shelf.json", "quiet.shelf.json"):
        check(fixtures / "expected" / name, "shelf.schema.json")
    for day in ("busy", "quiet"):
        for path in sorted((fixtures / "expected" / day).glob("history.*.json")):
            check(path, "history.schema.json")
        check(fixtures / "expected" / day / "all.history.json", "history.schema.json", "AllHistory")
    print("check-fixtures: fixtures match the generator and the expected files validate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
