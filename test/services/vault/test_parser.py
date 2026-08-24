import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.services.vault import parser
from cli_agent_orchestrator.services.vault.findings import FindingCode


def _parse(frontmatter: str):
    return parser.parse_note(
        f"---\n{frontmatter}\n---\nbody",
        max_frontmatter_bytes=8192,
        secret_gate="reject",
    )


def test_oversized_frontmatter_is_refused_before_yaml_parse(monkeypatch):
    safe_load = Mock()
    monkeypatch.setattr(parser.yaml, "safe_load", safe_load)

    result = parser.parse_note(
        "---\nkey: " + "x" * 32 + "\n---\nbody",
        max_frontmatter_bytes=8,
        secret_gate="reject",
    )

    assert result.finding_code == FindingCode.FRONTMATTER_TOO_LARGE
    assert result.finding_detail == "frontmatter byte cap"
    safe_load.assert_not_called()


def test_yaml_bomb_is_refused_before_yaml_parse(monkeypatch):
    safe_load = Mock()
    monkeypatch.setattr(parser.yaml, "safe_load", safe_load)

    result = _parse("a: &bomb [one, two]\nb: *bomb")

    assert result.finding_code == FindingCode.FRONTMATTER_UNSAFE
    assert result.finding_detail == "YAML anchor or alias"
    safe_load.assert_not_called()


@pytest.mark.parametrize(
    "frontmatter",
    [
        "a: &_anchor [one, two]\nb: *_anchor",
        "a: &1anchor [one, two]\nb: *1anchor",
        "a: &_base {name: note}\nb: {<<: *_base}",
    ],
)
def test_yaml_token_guard_refuses_all_anchor_and_alias_forms(frontmatter):
    result = _parse(frontmatter)

    assert result.finding_code == FindingCode.FRONTMATTER_UNSAFE
    assert result.finding_detail == "YAML anchor or alias"


@pytest.mark.parametrize(
    "frontmatter",
    [
        'title: "Foo &Bar"',
        "description: A *very* good note",
        "summary: see *Design Notes* for detail",
    ],
)
def test_yaml_token_guard_accepts_ordinary_prose(frontmatter):
    assert _parse(frontmatter).finding_code is None


def test_deeply_nested_yaml_is_quarantined_instead_of_raising():
    result = _parse("nested: " + "[" * 600 + "x" + "]" * 600)

    assert result.finding_code == FindingCode.FRONTMATTER_MALFORMED
    assert result.finding_detail == "invalid YAML"


@pytest.mark.parametrize(
    ("frontmatter", "code", "detail"),
    [
        ("a: &anchor value", FindingCode.FRONTMATTER_UNSAFE, "YAML anchor or alias"),
        ("cao: [", FindingCode.FRONTMATTER_MALFORMED, "invalid YAML"),
    ],
)
def test_unsafe_and_malformed_frontmatter_are_quarantined_with_specific_code(
    frontmatter, code, detail
):
    result = _parse(frontmatter)

    assert result.finding_code == code
    assert result.finding_detail == detail


def test_unterminated_frontmatter_is_malformed():
    result = parser.parse_note(
        "---\ncao:\n  key: valid\nbody",
        max_frontmatter_bytes=8192,
        secret_gate="reject",
    )

    assert result.finding_code == FindingCode.FRONTMATTER_MALFORMED
    assert result.finding_detail == "unterminated frontmatter"


@pytest.mark.parametrize(
    ("frontmatter", "code", "detail"),
    [
        ("cao: invalid", FindingCode.INVALID_CAO_BLOCK, "cao must be an object"),
        (
            "cao:\n  key: Bad Key",
            FindingCode.KEY_INVALID,
            "cao.key must match ^[a-z0-9-]{1,60}$",
        ),
        (
            "cao:\n  type: unsupported",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.type is invalid",
        ),
        (
            "cao:\n  managed: 'yes'",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.managed must be a boolean",
        ),
        (
            "cao:\n  links: not-a-list",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links must be a list of at most 64 objects",
        ),
        (
            "cao:\n  links:\n"
            + "\n".join(
                "    - {to: invalid, type: wrong}"
                for _ in range(parser.MAX_CAO_LINKS + 1)
            ),
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links must be a list of at most 64 objects",
        ),
        (
            "cao:\n  links:\n    - invalid",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links must contain only objects",
        ),
        (
            "cao:\n  links:\n    - {to: ''}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].to must be a non-empty string",
        ),
        (
            "cao:\n  links:\n    - {to: target, type: invalid}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].type is invalid",
        ),
        (
            "cao:\n  links:\n    - {to: target, status: invalid}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].status is invalid",
        ),
        (
            "cao:\n  links:\n    - {to: target, origin: invalid}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].origin is invalid",
        ),
        (
            "cao:\n  links:\n    - {to: target, confidence: 2}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].confidence must be between 0 and 1",
        ),
        (
            "cao:\n  links:\n    - {to: target, confidence: true}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].confidence must be between 0 and 1",
        ),
        (
            "cao:\n  kee: authored-key",
            FindingCode.INVALID_CAO_BLOCK,
            "cao contains unknown member",
        ),
        (
            "cao:\n  links:\n    - {to: target, injected: true}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links contains unknown member",
        ),
        (
            "cao:\n  links:\n    - {to: '" + "a" * 257 + "'}",
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].to contains unsupported characters or exceeds 256 characters",
        ),
        (
            'cao:\n  links:\n    - {to: "target\\nnext"}',
            FindingCode.INVALID_CAO_BLOCK,
            "cao.links[].to contains unsupported characters or exceeds 256 characters",
        ),
    ],
)
def test_invalid_cao_values_are_quarantined_without_defaulting(
    frontmatter, code, detail
):
    result = _parse(frontmatter)

    assert result.finding_code == code
    assert result.finding_detail == detail


def test_valid_cao_relationship_taxonomies_are_imported_and_preserved():
    result = _parse(
        "aliases: [display-only]\n"
        "cao:\n"
        "  type: reference\n"
        "  links:\n"
        "    - {to: target, type: contradiction, status: proposal, origin: human, confidence: 0.8}"
    )

    assert result.finding_code is None
    assert result.cao["type"] == "reference"
    assert result.cao["links"][0]["type"] == "contradiction"
    assert result.frontmatter["aliases"] == ["display-only"]


def test_frontmatter_region_preserves_original_bytes_and_body():
    text = "---\ntitle: Keep order\ncao:\n  key: stable\n---\nbody\n"
    result = parser.parse_note(text, max_frontmatter_bytes=8192, secret_gate="reject")

    assert result.region.raw == "title: Keep order\ncao:\n  key: stable"
    assert result.region.body == "body\n"
    assert text[result.region.start : result.region.end] == (
        "---\ntitle: Keep order\ncao:\n  key: stable\n---\n"
    )


def test_frontmatter_boundary_recognizes_bom_and_crlf_complete_fences():
    text = "\ufeff---\r\ntitle: Keep\r\n---\r\nbody\r\n"

    boundary = parser.frontmatter_boundary(text)

    assert boundary is not None
    region, newline = boundary
    assert newline == "\r\n"
    assert region.raw == "title: Keep"
    assert region.body == "body\r\n"
    assert region.start == 1


def test_bom_prefixed_frontmatter_is_parsed_without_a_finding():
    result = parser.parse_note(
        "\ufeff---\ncao:\n  key: stable\n---\nbody",
        max_frontmatter_bytes=8192,
        secret_gate="reject",
    )

    assert result.finding_code is None
    assert result.cao["key"] == "stable"
    assert result.region.start == 1


def test_closing_fence_must_be_a_complete_line():
    result = parser.parse_note(
        "---\ntitle: title\n---not-a-fence\ncao:\n  key: stable\n",
        max_frontmatter_bytes=8192,
        secret_gate="reject",
    )

    assert result.finding_code == FindingCode.FRONTMATTER_MALFORMED
    assert result.finding_detail == "unterminated frontmatter"


@pytest.mark.parametrize(
    ("raw", "spans", "indentation"),
    [
        ("cao:\n  key: stale\ntitle: keep\n", ((0, 18),), ""),
        ("? cao\n: \n  key: stale\ntitle: keep\n", ((0, 22),), ""),
        ("!!str cao:\n  key: stale\ntitle: keep\n", ((0, 24),), ""),
        ("  cao:\n    key: stale\n  title: keep\n", ((2, 24),), "  "),
        (
            "cao:\n  key: first\ncao:\n  key: second\ntitle: keep\n",
            ((0, 18), (18, 37)),
            "",
        ),
        ('desc: "one\\ncao: fake\\nend"\ntitle: keep\n', (), ""),
        ("items: [\ncao: not-really,\nother]\ntitle: keep\n", (), ""),
    ],
)
def test_locate_top_level_cao_blocks_uses_yaml_nodes(raw, spans, indentation):
    locations = parser.locate_top_level_cao_blocks(raw)

    assert locations.spans == spans
    assert locations.indentation == indentation


def test_locate_top_level_cao_blocks_refuses_anchors_before_composing():
    with pytest.raises(ValueError, match="^frontmatter_unsafe$"):
        parser.locate_top_level_cao_blocks("cao: &anchor {key: stale}\n")


def test_secret_classification_threads_mapping_gate():
    assert parser.classify_secret("password: hunter2", secret_gate="reject") == (
        FindingCode.SECRET_DETECTED,
        "error",
        "secret_assignment",
    )
    assert parser.classify_secret("password: hunter2", secret_gate="warn")[1] == "warn"


def test_secret_classification_covers_frontmatter_and_body():
    parsed = _parse("password: hunter2sixteen")
    note_text = parsed.region.raw + "\n" + parsed.region.body

    assert parser.classify_secret(note_text, secret_gate="reject") == (
        FindingCode.SECRET_DETECTED,
        "error",
        "secret_assignment",
    )


def test_u3_imports_are_side_effect_free(tmp_path):
    home = tmp_path / "new-cao-home"
    repository_root = Path(__file__).parents[3]
    env = {
        **os.environ,
        "CAO_HOME_DIR": str(home),
        "PYTHONPATH": str(repository_root / "src"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import cli_agent_orchestrator.services.vault.parser; "
                "import cli_agent_orchestrator.services.vault.identity; "
                "import cli_agent_orchestrator.services.vault.links; "
                "assert 'sqlalchemy' not in sys.modules"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not home.exists()


def test_relationship_service_reexports_leaf_taxonomies():
    from cli_agent_orchestrator.models import relationship
    from cli_agent_orchestrator.services import memory_relationship_service

    assert memory_relationship_service.VALID_TYPES is relationship.VALID_TYPES
    assert memory_relationship_service.VALID_STATUSES is relationship.VALID_STATUSES
    assert memory_relationship_service.VALID_ORIGINS is relationship.VALID_ORIGINS
