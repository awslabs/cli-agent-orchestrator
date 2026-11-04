"""SECURITY.md's supported-versions table must track the released major version.

The table said only 1.x was supported while 2.5.x was on PyPI (found by the
2026-10 CI review). A policy page that lags the release line tells reporters
the current version is unsupported and tells users an old line still gets
fixes. This test fails on the next major bump until the table is updated.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project_major() -> int:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.M)
    assert match, 'pyproject.toml has no version = "X.Y.Z" line'
    return int(match.group(1))


def _supported_versions_table() -> str:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    start = text.index("## Supported Versions")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_current_major_is_marked_supported():
    major = _project_major()
    table = _supported_versions_table()
    assert re.search(
        rf"^\|\s*{major}\.x\.x\s*\|\s*:white_check_mark:\s*\|", table, re.M
    ), f"SECURITY.md does not mark {major}.x.x as supported"


def test_previous_majors_are_marked_unsupported():
    major = _project_major()
    table = _supported_versions_table()
    assert re.search(
        rf"^\|\s*<\s*{major}\.0\s*\|\s*:x:\s*\|", table, re.M
    ), f"SECURITY.md does not mark < {major}.0 as unsupported"


def test_no_stale_major_row_is_marked_supported():
    major = _project_major()
    table = _supported_versions_table()
    for row_major in re.findall(r"^\|\s*(\d+)\.x\.x\s*\|\s*:white_check_mark:", table, re.M):
        assert int(row_major) == major, f"SECURITY.md still marks {row_major}.x.x as supported"
