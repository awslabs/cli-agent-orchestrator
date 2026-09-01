"""No stale command count survives in ``catalog.rs`` (issue #583 Bolt 3, ``authoring-cli-verbs``).

WHY THIS EXISTS, stated precisely because the gap it closes was invisible for months.

``catalog.rs`` states its command count in **fourteen** places: the constant, two bare assertions,
three per-policy assertions, a test function NAME that spells the distribution in words, and seven
prose comments. Three mechanisms already guard some of them:

* ``catalog.rs``'s own variant-count test — the ``CommandId`` count equals ``COMMAND_COUNT``.
* ``catalog.rs``'s distinct-entries test — ``DISPLAY_ORDER`` holds ``COMMAND_COUNT`` distinct ids.
* ``test_command_catalog_matches_click.py`` — ``COMMAND_COUNT`` equals the live Click leaf count.

**None of them reads a comment.** When Bolt 3 came to add two verbs, the file carried FOUR stale
``69``s and one stale ``70`` in prose, plus a broken intra-doc link naming a test that had been
renamed — every one of them wrong while the whole suite was green. That is the same shape as the
failure the module's own comment describes: "every count here was internally consistent and every test
green, because nothing compared the table against the CLI."

So this test compares the prose against the constant. It is deliberately crude — a regex over the
source — because the thing being guarded is prose, and there is nothing structured to inspect.
"""

from __future__ import annotations

import re
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "tui" / "src" / "catalog.rs"

#: Numbers that legitimately appear in ``catalog.rs`` without being the current command count.
#: Kept as an explicit allowlist rather than a looser regex, so adding one is a deliberate act.
_HISTORICAL = {
    # The module records a real past failure — merges that added leaf commands this table did not
    # know about — and the account is the most useful thing in that comment. Rewriting its figures
    # to the current count would destroy it, so the past-tense narrative keeps its own numbers.
    "24/18/27 = 69",
    "24/18/28 = 70",
    "33/5/22",
    "33 IN-APP / 5 HANDOFF / 23 HIDE = 61",
    "22/16/23",
    "61, not the 60",
}


def _source() -> str:
    assert CATALOG.is_file(), f"expected the catalog at {CATALOG}"
    return CATALOG.read_text()


def _declared_count(source: str) -> int:
    m = re.search(r"const COMMAND_COUNT: usize = (\d+);", source)
    assert m is not None, "COMMAND_COUNT is no longer declared in the expected form"
    return int(m.group(1))


def _strip_historical(source: str) -> str:
    for phrase in _HISTORICAL:
        source = source.replace(phrase, "<historical>")
    return source


def _stale_prose_counts(source: str) -> list[tuple[str, str]]:
    current = _declared_count(source)
    scrubbed = _strip_historical(source)

    # Two-digit numbers adjacent to count vocabulary. Narrow on purpose: this must not fire on
    # `shell.resize(70, 20)` in renderer.rs (a terminal width, a different file entirely) nor on
    # issue numbers, line references, or version pins.
    patterns = [
        r"\*\*(\d{2,3}) of them\*\*",
        r"\*\*(\d{2,3}) as of this branch\*\*",
        r"all (\d{2,3})\*\*",
        r"re-listing (\d{2,3}) commands",
        r"totalling (\d{2,3})",
        r"account for all (\d{2,3}) leaf commands",
        r"must list (\d{2,3}) DISTINCT",
        r"summing to (\d{2,3})",
        r"distinct\.len\(\) == (\d{2,3})",
        r"\b\d{2,3}/\d{1,3}/\d{1,3}\s*=\s*(\d{2,3})\b",
    ]
    stale: list[tuple[str, str]] = []
    for pattern in patterns:
        for found in re.findall(pattern, scrubbed):
            if int(found) != current:
                stale.append((pattern, found))
    return stale


def test_the_guard_is_not_vacuous():
    """If the file stops parsing the way this test expects, fail loudly rather than pass silently.

    The same discipline ``test_the_catalog_parse_is_not_vacuous`` applies next door: a regex that
    stops matching would otherwise make every assertion below trivially true.
    """
    source = _source()
    count = _declared_count(source)
    assert 40 < count < 300, f"COMMAND_COUNT of {count} is outside any plausible range"
    assert str(count) in source, "the declared count must appear in the source it was read from"
    assert (
        "One row per leaf command" in source
    ), "the module doc-comment this test exists to police has moved or been reworded — re-point it"


def test_no_superseded_command_count_survives_in_prose():
    """Every count-shaped number in the file is either the current one or explicitly historical."""
    source = _source()
    current = _declared_count(source)
    stale = _stale_prose_counts(source)

    assert not stale, (
        f"COMMAND_COUNT is {current} but the prose still says {sorted({s for _, s in stale})}. "
        "Nothing else in the repo reads these comments, which is why they went stale for months "
        "before Bolt 3 found them. Update them, or add the phrase to _HISTORICAL if it is a "
        "deliberate account of a past state."
    )


def test_slash_separated_distribution_total_is_checked_for_staleness():
    """The live distribution total is accepted, while a stale replacement is reported."""
    source = _source()
    current = _declared_count(source)
    matches = re.finditer(r"\*\*\d{2,3}/\d{1,3}/\d{1,3}\s*=\s*(\d{2,3})\*\*", source)
    live = next((match for match in matches if int(match.group(1)) == current), None)
    assert live is not None, "expected the current slash-separated distribution total in catalog.rs"
    assert not _stale_prose_counts(source)

    stale_total = current - 1
    stale_source = source[: live.start(1)] + str(stale_total) + source[live.end(1) :]
    assert any(found == str(stale_total) for _, found in _stale_prose_counts(stale_source))


def test_the_distribution_test_name_matches_its_assertions():
    """The count in a TEST FUNCTION NAME is the one no compiler and no assertion can check."""
    source = _source()
    m = re.search(r"fn the_policy_distribution_is_(\w+)\(", source)
    assert m is not None, "the distribution test has been renamed beyond this pattern"
    spelled = m.group(1)

    words = {
        "eighteen": 18,
        "twentytwo": 22,
        "twentythree": 23,
        "twentyfour": 24,
        "twentyseven": 27,
        "twentyeight": 28,
        "twentynine": 29,
        "thirty": 30,
        "thirtyone": 31,
        "thirtytwo": 32,
        "thirtythree": 33,
        "five": 5,
        "sixteen": 16,
    }
    parts = [words[w] for w in re.findall("|".join(words), spelled)]
    assert len(parts) == 3, f"expected three numbers in {spelled!r}, parsed {parts}"

    asserted = [
        int(n) for n in re.findall(r"assert_eq!\((?:in_app|handoff|hidden), (\d+),", source)
    ]
    assert asserted == parts, (
        f"the test is named for {parts} but asserts {asserted}. A name is documentation that no "
        "mechanism checks, so it drifts silently — this test is the mechanism."
    )
    assert sum(parts) == _declared_count(source), (
        f"the name spells {parts} summing to {sum(parts)}, but COMMAND_COUNT is "
        f"{_declared_count(source)}"
    )


def test_no_intra_doc_link_names_a_missing_test():
    """The broken link this unit found: a rustdoc reference to a test renamed out of existence."""
    source = _source()
    referenced = set(re.findall(r"\[`(the_policy_distribution_is_\w+)`\]", source))
    defined = set(re.findall(r"fn (the_policy_distribution_is_\w+)\(", source))
    missing = referenced - defined
    assert not missing, (
        f"these doc links name tests that do not exist: {sorted(missing)}. `cargo doc` warns rather "
        "than fails on a broken intra-doc link, so nothing in CI catches it."
    )
