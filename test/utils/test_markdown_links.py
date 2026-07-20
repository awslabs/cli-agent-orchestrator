from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.utils import markdown_links
from cli_agent_orchestrator.utils.markdown_links import (
    discover_markdown_files,
    validate_markdown_links,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_paths_fragments_duplicate_headings_and_directory_indexes(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "README.md",
        "\n".join(
            (
                "[guide](docs/guide%20file.md#repeated-heading-1)",
                "[root guide](/docs/guide%20file.md#repeated-heading)",
                "[directory](docs/)",
                "[self](#repeated-heading)",
                "",
                "## Repeated Heading",
            )
        ),
    )
    _write(
        tmp_path / "docs" / "guide file.md",
        "# Repeated Heading\n\n## Repeated Heading\n",
    )
    _write(tmp_path / "docs" / "README.md", "# Index\n")

    assert validate_markdown_links(tmp_path, [source, tmp_path / "docs" / "guide file.md"]) == []


def test_reports_missing_target_and_heading_fragment(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "[missing](missing.md)\n[missing heading](guide.md#not-there)\n",
    )
    guide = _write(tmp_path / "guide.md", "# Present\n")

    errors = validate_markdown_links(tmp_path, [source, guide])

    assert [error.reason for error in errors] == [
        "target does not exist",
        "heading fragment #not-there does not exist",
    ]
    assert all(error.source == source.resolve() for error in errors)


def test_reports_repository_escape_after_percent_decoding(tmp_path: Path) -> None:
    source = _write(tmp_path / "README.md", "[escape](%2E%2E/outside.md)\n")

    errors = validate_markdown_links(tmp_path, [source])

    assert len(errors) == 1
    assert errors[0].destination == "%2E%2E/outside.md"
    assert errors[0].reason == "path escapes the repository"


def test_reports_broken_markdown_images_with_path_diagnostics(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "![missing image](images/missing.png)\n![escape](%2E%2E/outside.png)\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.destination, error.reason) for error in errors] == [
        ("images/missing.png", "target does not exist"),
        ("%2E%2E/outside.png", "path escapes the repository"),
    ]


def test_reports_static_html_anchor_and_image_destinations(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "\n".join(
            (
                '<a class="link" href="missing.html">Missing</a>',
                '<img alt="Missing" src="images/missing.png">',
                "<div>",
                '  <a href="../outside.html">Outside</a>',
                "</div>",
                "<script>const ignored = '<a href=\"not-a-link.html\">';</script>",
            )
        ),
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination, error.reason) for error in errors] == [
        (1, "missing.html", "target does not exist"),
        (2, "images/missing.png", "target does not exist"),
        (4, "../outside.html", "path escapes the repository"),
    ]


def test_ignores_links_inside_malformed_html_raw_text_end_tag(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "<script>const fake = '<a href=\"not-a-link.html\">';</script\t\n bar>\n",
    )

    assert validate_markdown_links(tmp_path, [source]) == []


@pytest.mark.parametrize(
    ("tag", "attribute", "first_destination", "second_destination"),
    (
        ("a", "href", "first-anchor.html", "second-anchor.html"),
        ("img", "src", "first-image.png", "second-image.png"),
    ),
)
def test_reports_multiline_duplicate_html_attribute_lines(
    tmp_path: Path,
    tag: str,
    attribute: str,
    first_destination: str,
    second_destination: str,
) -> None:
    source = _write(
        tmp_path / "README.md",
        (
            f'<{tag} {attribute}="{first_destination}"\n'
            f'  {attribute}="{second_destination}">'
            f'{"content</a>" if tag == "a" else "content"}\n'
        ),
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [
        (1, first_destination),
        (2, second_destination),
    ]


def test_heading_fragments_use_rendered_heading_text(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "[heading](guide.md#linked-code-alt-image--entity)\n"
        "[multiline heading](guide.md#rendered-heading)",
    )
    guide = _write(
        tmp_path / "guide.md",
        "# [Linked](target.md) `Code` ![Alt *Image*](image.png) &amp; Entity\n\n"
        "Rendered\n"
        "Heading\n"
        "=======\n",
    )
    _write(tmp_path / "target.md", "# Target\n")
    _write(tmp_path / "image.png", "image\n")

    assert validate_markdown_links(tmp_path, [source, guide]) == []


def test_reports_destination_line_inside_multiline_paragraph(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "An introductory line\nfollowed by [a missing link](missing.md).\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert len(errors) == 1
    assert errors[0].line == 2
    assert errors[0].destination == "missing.md"


def test_reports_reference_link_line_inside_multiline_paragraph(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "Paragraph start\n[moved link][missing]\n\n[missing]: target.md\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert len(errors) == 1
    assert errors[0].line == 2
    assert errors[0].destination == "target.md"


def test_reports_shortcut_reference_link_line_inside_multiline_paragraph(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "README.md",
        (
            "Paragraph start\n[ordinary bracket text]\n[missing]\n\n"
            + "[missing]: missing-link.md\n"
        ),
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert len(errors) == 1
    assert errors[0].line == 3
    assert errors[0].destination == "missing-link.md"


def test_reports_shortcut_reference_image_line_inside_multiline_paragraph(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "README.md",
        (
            "Paragraph start\n![ordinary bracket text]\n![missing]\n\n"
            + "[missing]: missing-image.png\n"
        ),
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert len(errors) == 1
    assert errors[0].line == 3
    assert errors[0].destination == "missing-image.png"


def test_reports_escaped_destination_line_inside_multiline_paragraph(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "README.md",
        "Paragraph start\n[moved link](missing\\(file\\).md)\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert len(errors) == 1
    assert errors[0].line == 2
    assert errors[0].destination == "missing(file).md"


def test_reports_nested_link_at_the_parser_recognized_source_line(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "[outer label\n[inner](missing.md)](outer.md)\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [(2, "missing.md")]


def test_reports_nested_link_after_an_adjacent_image_at_its_source_line(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "![existing image](image.png)\n[outer label\n[inner](missing.md)](outer.md)\n",
    )
    _write(tmp_path / "image.png", "image\n")

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [(3, "missing.md")]


@pytest.mark.parametrize("autolink", ("<https://example.com>", "<docs@example.com>"))
def test_reports_collapsed_reference_and_autolink_source_lines(
    tmp_path: Path, autolink: str
) -> None:
    source = _write(
        tmp_path / "README.md",
        f"Paragraph start\n[missing][]\nx <foo@bar baz> {autolink}\n[local](missing.html)\n\n"
        "[missing]: missing.md\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [
        (2, "missing.md"),
        (4, "missing.html"),
    ]


def test_non_autolink_angle_text_does_not_shift_later_link_diagnostic(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "x <foo@bar baz>\n[real](missing.md)\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [(2, "missing.md")]


@pytest.mark.parametrize("reference", ("[unknown]", "[]"))
def test_undefined_reference_does_not_shift_later_link_diagnostic(
    tmp_path: Path, reference: str
) -> None:
    source = _write(
        tmp_path / "README.md",
        f"[not a link]{reference}\ncontinued paragraph\n[real](missing.md)\n",
    )

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [(3, "missing.md")]


def test_balanced_rejected_brackets_do_not_normalize_overlapping_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(
        tmp_path / "README.md",
        "[" * 10_000 + "]" * 10_000 + "\n[real](missing.md)\n\n[known]: existing.md\n",
    )
    normalized_labels: list[str] = []
    original = markdown_links.normalizeReference

    def counted_normalization(label: str) -> str:
        normalized_labels.append(label)
        return original(label)

    monkeypatch.setattr(markdown_links, "normalizeReference", counted_normalization)

    errors = validate_markdown_links(tmp_path, [source])

    assert [(error.line, error.destination) for error in errors] == [(2, "missing.md")]
    assert normalized_labels == [""]


def test_ignores_external_and_non_local_schemes(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "README.md",
        "\n".join(
            (
                "[web](https://example.com/a.md#missing)",
                "[mail](mailto:docs@example.com)",
                "[telephone](tel:+123456789)",
                "[protocol relative](//example.com/docs)",
            )
        ),
    )

    assert validate_markdown_links(tmp_path, [source]) == []


def test_accepts_existing_directory_without_a_landing_document(tmp_path: Path) -> None:
    source = _write(tmp_path / "README.md", "[directory](docs/)\n")
    (tmp_path / "docs").mkdir()

    assert validate_markdown_links(tmp_path, [source]) == []


def test_discovers_tracked_markdown_and_excludes_vendored_and_fixture_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "README.md", "# Maintained\n")
    _write(tmp_path / "skills" / "vendor" / "README.md", "# Vendored\n")
    _write(
        tmp_path / "src" / "cli_agent_orchestrator" / "skills" / "README.md",
        "# Generated\n",
    )
    _write(tmp_path / "test" / "fixtures" / "README.md", "# Fixture\n")
    _write(tmp_path / "test" / "providers" / "fixtures" / "README.md", "# Fixture\n")
    _write(tmp_path / "examples" / "codex-basic" / "codex_documenter.md", "# Template\n")
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.markdown_links.subprocess.run",
        lambda *args, **kwargs: type(
            "Completed",
            (),
            {
                "stdout": "README.md\nskills/vendor/README.md\nsrc/cli_agent_orchestrator/skills/README.md\ntest/fixtures/README.md\ntest/providers/fixtures/README.md\nexamples/codex-basic/codex_documenter.md\n"
            },
        )(),
    )

    assert discover_markdown_files(tmp_path) == [tmp_path / "README.md"]
