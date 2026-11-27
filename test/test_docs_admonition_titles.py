"""Reject legacy titled admonitions that Docusaurus renders as raw text."""

import os
import re
from pathlib import Path

DOCUSAURUS = Path(__file__).resolve().parents[1] / "docusaurus"
LEGACY_TITLE = re.compile(r"^:::[a-z]+ +\S")


def find_legacy_titles(root: Path) -> list[str]:
    offenders = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(set(subdirectories) - {"build", "node_modules", ".docusaurus"})
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix not in {".md", ".mdx"}:
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if LEGACY_TITLE.match(line):
                    offenders.append(f"{path.relative_to(root)}:{line_number}: {line}")
    return offenders


def test_no_legacy_admonition_titles() -> None:
    offenders = find_legacy_titles(DOCUSAURUS)
    assert not offenders, "Legacy admonition titles:\n" + "\n".join(offenders)
