"""Reject legacy titled admonitions that Docusaurus renders as raw text."""

import os
import re
from pathlib import Path

import pytest

DOCUSAURUS = Path(__file__).resolve().parents[1] / "docusaurus"
LEGACY_TITLE = re.compile(r"^[ \t>]*:{3,}[a-z]+[ \t]+\S")
FENCE_OPEN = re.compile(r"^[ \t>]*(`{3,}|~{3,})")


def find_legacy_titles(root: Path) -> list[str]:
    offenders = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(set(subdirectories) - {"build", "node_modules", ".docusaurus"})
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix not in {".md", ".mdx"}:
                continue
            fence_marker = ""
            fence_length = 0
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if fence_marker:
                    if re.fullmatch(
                        re.escape(fence_marker) + "{" + str(fence_length) + r",}[ \t]*",
                        line.lstrip(" \t>"),
                    ):
                        fence_marker = ""
                    continue
                fence = FENCE_OPEN.match(line)
                if fence:
                    fence_marker = fence.group(1)[0]
                    fence_length = len(fence.group(1))
                    continue
                if LEGACY_TITLE.match(line):
                    offenders.append(f"{path.relative_to(root)}:{line_number}: {line}")
    return offenders


def test_no_legacy_admonition_titles() -> None:
    offenders = find_legacy_titles(DOCUSAURUS)
    assert not offenders, "Legacy admonition titles:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "content, expected_line",
    [
        ("- List item\n   :::tip T", 2),
        ("::::tip T", 1),
        ("> :::tip T", 1),
        (":::tip\tT", 1),
    ],
)
def test_finds_legacy_titles_in_other_forms(
    tmp_path: Path, content: str, expected_line: int
) -> None:
    (tmp_path / "example.md").write_text(content + "\n", encoding="utf-8")

    assert find_legacy_titles(tmp_path) == [
        f"example.md:{expected_line}: {content.splitlines()[-1]}"
    ]


@pytest.mark.parametrize("fence", ["```", "```md", "~~~", "~~~md"])
def test_ignores_legacy_titles_inside_fences(tmp_path: Path, fence: str) -> None:
    closing_fence = fence[:3]
    (tmp_path / "example.md").write_text(
        f"{fence}\n:::tip Example\n{closing_fence}\n:::tip Outside\n",
        encoding="utf-8",
    )

    assert find_legacy_titles(tmp_path) == ["example.md:4: :::tip Outside"]


def test_ignores_valid_and_untitled_admonitions(tmp_path: Path) -> None:
    (tmp_path / "example.md").write_text(":::tip[Title]\n:::tip\n:::\n", encoding="utf-8")

    assert find_legacy_titles(tmp_path) == []
