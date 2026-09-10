import pytest

from cli_agent_orchestrator.services.vault.findings import FindingCode
from cli_agent_orchestrator.services.vault.links import (
    MAX_BODY_WIKILINKS,
    LinkCandidate,
    extract_wikilinks,
    resolve_wikilink,
)


@pytest.mark.parametrize(
    ("target", "embed", "candidates", "outcome", "finding", "attributes"),
    [
        (
            "Design",
            False,
            (LinkCandidate("design", "Design.md"),),
            "resolved",
            None,
            None,
        ),
        (
            "Design|Readable label",
            False,
            (LinkCandidate("design", "Design.md"),),
            "resolved",
            None,
            None,
        ),
        (
            "folder/Design",
            False,
            (
                LinkCandidate("qualified", "folder/Design.md"),
                LinkCandidate("other", "other/Design.md"),
            ),
            "resolved",
            None,
            None,
        ),
        (
            "Design#Read",
            False,
            (LinkCandidate("design", "Design.md"),),
            "resolved",
            FindingCode.HEADING_FRAGMENT_IGNORED,
            {"fragment": "Read"},
        ),
        (
            "Design#^block",
            False,
            (LinkCandidate("design", "Design.md"),),
            "unsupported",
            FindingCode.BLOCK_REFERENCE_UNSUPPORTED,
            None,
        ),
        (
            "Design",
            True,
            (LinkCandidate("design", "Design.md"),),
            "resolved",
            FindingCode.EMBED_NOT_INLINED,
            {"embed": True},
        ),
        ("image.png", True, (), "unsupported", FindingCode.ATTACHMENT_IGNORED, None),
        (
            "Architecture",
            False,
            (LinkCandidate("design", "Design.md", aliases=("Architecture",)),),
            "resolved",
            None,
            None,
        ),
        (
            "Architecture",
            False,
            (
                LinkCandidate("one", "One.md", aliases=("Architecture",)),
                LinkCandidate("two", "Two.md", aliases=("Architecture",)),
            ),
            "ambiguous",
            FindingCode.ALIAS_AMBIGUOUS,
            None,
        ),
        (
            "Design",
            False,
            (LinkCandidate("a", "A/Design.md"), LinkCandidate("b", "B/Design.md")),
            "ambiguous",
            FindingCode.LINK_AMBIGUOUS,
            None,
        ),
        (
            "Private",
            False,
            (LinkCandidate("private", "Private.md", excluded=True),),
            "excluded",
            FindingCode.LINK_EXCLUDED,
            None,
        ),
        ("Missing", False, (), "dangling", FindingCode.LINK_DANGLING, None),
    ],
)
def test_wikilink_boundary_outcomes(target, embed, candidates, outcome, finding, attributes):
    result = resolve_wikilink(target, embed=embed, candidates=candidates)

    assert result.outcome == outcome
    assert result.finding_code == finding
    assert result.attributes == attributes


def test_wikilink_extraction_preserves_embed_flag_and_raw_target():
    result = extract_wikilinks("See [[Design|display]] and ![[folder/Other#Part]].")

    assert result.links == ((False, "Design|display"), (True, "folder/Other#Part"))
    assert result.findings == ()


def test_matching_dotted_note_title_is_not_an_attachment():
    result = resolve_wikilink(
        "Node.js Notes",
        embed=False,
        candidates=(LinkCandidate("node-js", "Node.js Notes.md"),),
    )

    assert result.outcome == "resolved"
    assert result.finding_code is None


def test_excluded_and_available_matches_are_ambiguous_not_resolved():
    result = resolve_wikilink(
        "Design",
        embed=False,
        candidates=(
            LinkCandidate("available", "Public/Design.md"),
            LinkCandidate("excluded", "Private/Design.md", excluded=True),
        ),
    )

    assert result.outcome == "ambiguous"
    assert result.finding_code == FindingCode.LINK_AMBIGUOUS


def test_body_links_are_bounded_and_code_examples_are_not_extracted():
    result = extract_wikilinks(
        "```markdown\n[[Fenced]]\n```\n`[[Inline]]`\n"
        + " ".join(f"[[Target-{index}]]" for index in range(MAX_BODY_WIKILINKS + 1))
    )

    assert len(result.links) == MAX_BODY_WIKILINKS
    assert result.findings == (FindingCode.LINK_LIMIT_EXCEEDED,)
    assert all(target not in {"Fenced", "Inline"} for _, target in result.links)


@pytest.mark.parametrize("target", ["Heading\nSecond", "x" * 257])
def test_unsafe_link_attributes_are_refused(target):
    result = resolve_wikilink(
        f"Design#{target}",
        embed=False,
        candidates=(LinkCandidate("design", "Design.md"),),
    )

    assert result.outcome == "unsupported"
    assert result.finding_code == FindingCode.LINK_TARGET_INVALID
