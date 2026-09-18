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


def test_extract_relative_inline_markdown_md_link_ignores_label_fragment_and_code():
    result = extract_wikilinks(
        "See [a label that is not identity](Sub/Target.md#Section).\n"
        "```markdown\n[Fenced](Hidden.md)\n```\n"
        "and `[Inline](Also-Hidden.md)`."
    )

    assert result.links == ((False, "Sub/Target.md#Section"),)
    assert result.relative_paths == (True,)
    assert result.findings == ()


@pytest.mark.parametrize(
    "destination",
    [
        "./T.md",
        "../T.md",
        "Sub/../../T.md",
        "./Sub/./T.md",
    ],
)
def test_inline_markdown_extraction_retains_raw_dot_segment_destinations(destination):
    result = extract_wikilinks(f"[target]({destination})")

    assert result.links == ((False, destination),)
    assert result.relative_paths == (True,)


@pytest.mark.parametrize(
    "markdown",
    [
        "[absolute](/Target.md)",
        "[network path](//host/Target.md)",
        "[http](http://example.com/Target.md)",
        "[https](https://example.com/Target.md)",
        "[non-Markdown](Target.txt)",
        "[scheme](custom:Target.md)",
        "[empty component](Sub//Target.md)",
        "[fragment only](#Section)",
        "[trailing slash](Target.md/)",
        f"[oversize]({'x' * 257}.md)",
        r"\[escaped](Target.md)",
        r"[escaped destination]\(Target.md)",
        r"[escaped path](Sub/Target\.md)",
    ],
)
def test_inline_markdown_extraction_refuses_unsupported_destinations(markdown):
    assert extract_wikilinks(markdown).links == ()


def test_relative_inline_resolution_requires_source_and_matches_exact_source_relative_path():
    candidates = (
        LinkCandidate(
            "root-lookalike",
            "Mapped/Sub/Target.md",
            aliases=("Mapped/Nested/Sub/Target.md",),
        ),
        LinkCandidate("nested-target", "Mapped/Nested/Sub/Target.md"),
    )

    without_source = resolve_wikilink(
        "Sub/Target.md",
        embed=False,
        candidates=candidates,
        relative_path=True,
    )
    nested = resolve_wikilink(
        "Sub/Target.md#Section",
        embed=False,
        candidates=candidates,
        relative_path=True,
        source_relpath="Mapped/Nested/Source.md",
    )

    assert without_source.outcome == "unsupported"
    assert without_source.finding_code == FindingCode.LINK_TARGET_INVALID
    assert nested.outcome == "resolved"
    assert nested.target_key == "nested-target"
    assert nested.attributes == {"fragment": "Section"}


@pytest.mark.parametrize(
    ("raw_target", "source_relpath", "target_relpath"),
    [
        ("./Target.md", "Mapped/Source.md", "Mapped/Target.md"),
        ("../Target.md", "Mapped/Nested/Source.md", "Mapped/Target.md"),
        ("Sub/../../Target.md", "Mapped/Nested/Source.md", "Mapped/Target.md"),
        ("./Sub/./Target.md", "Mapped/Source.md", "Mapped/Sub/Target.md"),
    ],
)
def test_relative_inline_resolution_collapses_contained_dot_segments(
    raw_target, source_relpath, target_relpath
):
    result = resolve_wikilink(
        raw_target,
        embed=False,
        candidates=(LinkCandidate("target", target_relpath),),
        relative_path=True,
        source_relpath=source_relpath,
    )

    assert result.outcome == "resolved"
    assert result.target_key == "target"


@pytest.mark.parametrize(
    ("raw_target", "source_relpath"),
    [
        ("../T.md", "Source.md"),
        ("../../../T.md", "A/B/Source.md"),
        ("../Sub/../T.md", "Source.md"),
    ],
)
def test_relative_inline_resolution_refuses_vault_root_escapes(raw_target, source_relpath):
    result = resolve_wikilink(
        raw_target,
        embed=False,
        candidates=(LinkCandidate("target", "T.md"),),
        relative_path=True,
        source_relpath=source_relpath,
    )

    assert result.outcome == "unsupported"
    assert result.finding_code == FindingCode.LINK_TARGET_INVALID


@pytest.mark.parametrize(
    "source_relpath",
    [
        "/Mapped/Source.md",
        "Mapped/./Source.md",
        "Mapped/../Source.md",
        "Mapped//Source.md",
        r"Mapped\Source.md",
        "Mapped/Source.txt",
    ],
)
def test_relative_inline_resolution_refuses_invalid_source_relpath(source_relpath):
    result = resolve_wikilink(
        "./T.md",
        embed=False,
        candidates=(LinkCandidate("target", "Mapped/T.md"),),
        relative_path=True,
        source_relpath=source_relpath,
    )

    assert result.outcome == "unsupported"
    assert result.finding_code == FindingCode.LINK_TARGET_INVALID


def test_relative_inline_resolution_does_not_decode_percent_escapes():
    result = resolve_wikilink(
        "%2e%2e/T.md",
        embed=False,
        candidates=(LinkCandidate("literal", "Mapped/%2e%2e/T.md"),),
        relative_path=True,
        source_relpath="Mapped/Source.md",
    )

    assert result.outcome == "resolved"
    assert result.target_key == "literal"


def test_relative_inline_dot_segment_fragment_preserves_heading_finding():
    result = resolve_wikilink(
        "./T.md#Section",
        embed=False,
        candidates=(LinkCandidate("target", "Mapped/T.md"),),
        relative_path=True,
        source_relpath="Mapped/Source.md",
    )

    assert result.outcome == "resolved"
    assert result.target_key == "target"
    assert result.finding_code == FindingCode.HEADING_FRAGMENT_IGNORED
    assert result.attributes == {"fragment": "Section"}


def test_relative_inline_block_reference_remains_unsupported():
    result = resolve_wikilink(
        "./T.md#^block",
        embed=False,
        candidates=(LinkCandidate("target", "Mapped/T.md"),),
        relative_path=True,
        source_relpath="Mapped/Source.md",
    )

    assert result.outcome == "unsupported"
    assert result.finding_code == FindingCode.BLOCK_REFERENCE_UNSUPPORTED


def test_qualified_wikilink_does_not_gain_relative_suffix_matching():
    result = resolve_wikilink(
        "Sub/Target",
        embed=False,
        candidates=(LinkCandidate("lookalike", "Mapped/Sub/Target.md"),),
    )

    assert result.outcome == "dangling"
    assert result.finding_code == FindingCode.LINK_DANGLING


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
