"""Validate local Markdown links and GitHub-style heading fragments."""

from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass
from html import unescape as unescape_html
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import normalizeReference, unescapeAll
from markdown_it.token import Token

_MARKDOWN_SUFFIX = ".md"
_EXCLUDED_PREFIXES = (
    Path("skills/vendor"),
    # These are package copies generated from the top-level skills/ source.
    Path("src/cli_agent_orchestrator/skills"),
    Path("test/fixtures"),
    Path("test/providers/fixtures"),
)
_EXCLUDED_FILES = (
    # This profile contains a literal, generic README template with deliberately
    # non-repository paths such as docs/guide.md.
    Path("examples/codex-basic/codex_documenter.md"),
)
_PUNCTUATION_RE = re.compile(r"[^\w\-\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s")
_HTML_TAG_RE = re.compile(
    r"<\s*(?P<tag>a|img)\b(?P<attributes>[^>]*)>", re.IGNORECASE | re.DOTALL
)
_HTML_ATTRIBUTE_RE = re.compile(
    r"""(?<![\w:-])(?P<name>href|src)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s"'=<>`]+))""",
    re.IGNORECASE,
)
_HTML_NON_TAG_CONTENT_RE = re.compile(
    r"<!--.*?-->|<(?:script|style|textarea|title)\b[^>]*>.*?</(?:script|style|textarea|title)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_LINK_SYNTAX_RE = re.compile(
    r"""(?<!\\)!?\[(?:\\.|[^\]\\])*\](?:\s*\((?:\\.|[^)])*\)|\[(?:\\.|[^\]\\])*\])""",
    re.DOTALL,
)
_MARKDOWN_SHORTCUT_REFERENCE_RE = re.compile(
    r"""(?<!\\)!?\[(?P<label>(?:\\.|[^\]\\])*)\](?!\s*(?:\(|\[))""",
    re.DOTALL,
)
_MARKDOWN_AUTOLINK_RE = re.compile(r"<[^>\n]+>")


@dataclass(frozen=True)
class MarkdownLinkError:
    """A local Markdown link that cannot be resolved."""

    source: Path
    line: int
    destination: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.destination!r}: {self.reason}"


def discover_markdown_files(repo_root: Path) -> list[Path]:
    """Return maintained Markdown files, sorted by repository-relative path.

    Git's tracked file list makes the result deterministic and avoids scanning
    build output, caches, and other untracked local artifacts.
    """

    completed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--", "*.md"],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    files: list[Path] = []
    for name in completed.stdout.splitlines():
        relative = Path(name)
        if relative in _EXCLUDED_FILES or any(
            relative.is_relative_to(prefix) for prefix in _EXCLUDED_PREFIXES
        ):
            continue
        files.append(repo_root / relative)
    return files


def validate_markdown_links(
    repo_root: Path, files: Iterable[Path] | None = None
) -> list[MarkdownLinkError]:
    """Validate local paths and heading fragments in Markdown files under ``repo_root``."""

    root = repo_root.resolve()
    markdown_files = sorted(
        (
            path.resolve()
            for path in (discover_markdown_files(root) if files is None else files)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    parser = MarkdownIt("commonmark")
    headings = {path: _heading_slugs(path, parser) for path in markdown_files}
    errors: list[MarkdownLinkError] = []

    for source in markdown_files:
        for destination, line in _links(source, parser):
            error = _validate_destination(
                root, source, destination, line, headings, parser
            )
            if error is not None:
                errors.append(error)
    return errors


def _links(path: Path, parser: MarkdownIt) -> Iterable[tuple[str, int]]:
    """Yield local destinations from parsed Markdown and static HTML tags."""

    environment: dict[str, object] = {}
    tokens = parser.parse(path.read_text(encoding="utf-8"), environment)
    references = environment.get("references")
    reference_labels = (
        frozenset(label for label in references if isinstance(label, str))
        if isinstance(references, dict)
        else frozenset()
    )

    for token in tokens:
        if token.type == "html_block":
            yield from _html_destinations(token.content, _token_start_line(token))
            continue
        if token.type != "inline" or token.children is None:
            continue

        inline_start_line = _token_start_line(token)
        inline_cursor = 0
        for child in token.children:
            if child.type == "html_inline":
                offset = token.content.find(child.content, inline_cursor)
                if offset < 0:
                    offset = inline_cursor
                yield from _html_destinations(
                    child.content,
                    inline_start_line + token.content.count("\n", 0, offset),
                )
                inline_cursor = offset + len(child.content)
                continue

            attribute = (
                "href"
                if child.type == "link_open"
                else "src"
                if child.type == "image"
                else None
            )
            if attribute is None:
                continue
            destination = child.attrGet(attribute)
            if isinstance(destination, str):
                syntax_offset = _markdown_link_syntax_offset(
                    token.content, inline_cursor, reference_labels
                )
                yield (
                    destination,
                    inline_start_line + token.content.count("\n", 0, syntax_offset),
                )
                inline_cursor = syntax_offset + 1


def _token_start_line(token: Token) -> int:
    """Return the first one-based source line represented by a block token."""

    return token.map[0] + 1 if token.map is not None else 1


def _markdown_link_syntax_offset(
    content: str, cursor: int, reference_labels: frozenset[str]
) -> int:
    """Locate the next raw Markdown link or image construct in inline source.

    Parser destinations are normalized, so reference-style links and escaped
    direct destinations cannot be reliably located by searching for them.
    Tracking the syntax itself preserves the link's source line. Shortcut
    references need their parser-resolved labels to avoid treating ordinary
    bracket text as a link.
    """

    matches = (
        match
        for expression in (_MARKDOWN_LINK_SYNTAX_RE, _MARKDOWN_AUTOLINK_RE)
        if (match := expression.search(content, cursor)) is not None
    )
    offsets = [match.start() for match in matches]
    shortcut_offset = _shortcut_reference_offset(content, cursor, reference_labels)
    if shortcut_offset is not None:
        offsets.append(shortcut_offset)
    return min(offsets, default=cursor)


def _shortcut_reference_offset(
    content: str, cursor: int, reference_labels: frozenset[str]
) -> int | None:
    """Return the first defined shortcut reference at or after ``cursor``."""

    for match in _MARKDOWN_SHORTCUT_REFERENCE_RE.finditer(content, cursor):
        label = normalizeReference(unescapeAll(match.group("label")))
        if label in reference_labels:
            return match.start()
    return None


def _html_destinations(content: str, start_line: int) -> Iterable[tuple[str, int]]:
    """Yield quoted or bare href/src values from static anchor and image tags."""

    static_content = _HTML_NON_TAG_CONTENT_RE.sub(_preserve_newlines, content)
    for tag_match in _HTML_TAG_RE.finditer(static_content):
        tag = tag_match.group("tag").lower()
        attribute_name = "href" if tag == "a" else "src"
        attributes = tag_match.group("attributes")
        for attribute_match in _HTML_ATTRIBUTE_RE.finditer(attributes):
            if attribute_match.group("name").lower() != attribute_name:
                continue
            destination, offset = _html_attribute_value(attribute_match)
            yield (
                unescape_html(destination),
                start_line
                + content.count("\n", 0, tag_match.start("attributes") + offset),
            )


def _html_attribute_value(match: re.Match[str]) -> tuple[str, int]:
    """Return an HTML attribute's value and its offset within the attributes."""

    for name in ("double", "single", "bare"):
        value = match.group(name)
        if value is not None:
            return value, match.start(name)
    raise ValueError("HTML attribute value is missing")


def _preserve_newlines(match: re.Match[str]) -> str:
    """Mask non-tag HTML content without changing subsequent source line offsets."""

    return re.sub(r"[^\n]", " ", match.group(0))


def _heading_slugs(path: Path, parser: MarkdownIt) -> set[str]:
    """Return GitHub-compatible heading IDs, preserving duplicate suffixes."""

    slugs: set[str] = set()
    counts: dict[str, int] = {}
    tokens = parser.parse(path.read_text(encoding="utf-8"))
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        slug = _github_slug(_rendered_heading_text(inline))
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def _rendered_heading_text(inline: Token) -> str:
    """Return the text GitHub renders from an inline heading token."""

    if inline.children is None:
        return inline.content
    return "".join(_rendered_token_text(child) for child in inline.children)


def _rendered_token_text(token: Token) -> str:
    """Return rendered text for a heading's visible inline token."""

    if token.type in ("text", "code_inline"):
        return token.content
    if token.type in ("softbreak", "hardbreak"):
        return " "
    if token.type == "image":
        if token.children is not None:
            return "".join(_rendered_token_text(child) for child in token.children)
        return token.content
    return ""


def _github_slug(heading: str) -> str:
    """Approximate GitHub's heading slugger for parsed Markdown heading text."""

    normalized = unicodedata.normalize("NFKC", heading).strip().lower()
    without_punctuation = _PUNCTUATION_RE.sub("", normalized)
    return _WHITESPACE_RE.sub("-", without_punctuation)


def _validate_destination(
    repo_root: Path,
    source: Path,
    destination: str,
    line: int,
    headings: dict[Path, set[str]],
    parser: MarkdownIt,
) -> MarkdownLinkError | None:
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc:
        return None

    decoded_path = unquote(parsed.path)
    decoded_fragment = unquote(parsed.fragment)
    if decoded_path:
        candidate = (
            repo_root / decoded_path.lstrip("/")
            if decoded_path.startswith("/")
            else source.parent / decoded_path
        )
    else:
        candidate = source
    resolved = candidate.resolve()

    if not resolved.is_relative_to(repo_root):
        return MarkdownLinkError(
            source, line, destination, "path escapes the repository"
        )
    if not resolved.exists():
        return MarkdownLinkError(source, line, destination, "target does not exist")
    if resolved.is_dir():
        directory_document = _directory_document(resolved)
        if directory_document is None:
            if decoded_fragment:
                return MarkdownLinkError(
                    source,
                    line,
                    destination,
                    "directory has no README.md or index.md for heading fragments",
                )
            return None
        resolved = directory_document

    if decoded_fragment:
        target_headings = headings.get(resolved)
        if target_headings is None and resolved.suffix == _MARKDOWN_SUFFIX:
            target_headings = _heading_slugs(resolved, parser)
        if target_headings is None or decoded_fragment not in target_headings:
            return MarkdownLinkError(
                source,
                line,
                destination,
                f"heading fragment #{decoded_fragment} does not exist",
            )
    return None


def _directory_document(directory: Path) -> Path | None:
    """Resolve directory links using GitHub's README-first document behavior."""

    for name in ("README.md", "index.md"):
        document = directory / name
        if document.is_file():
            return document
    return None


def format_errors(errors: Sequence[MarkdownLinkError]) -> str:
    """Format failures for the command-line wrapper and CI logs."""

    return "\n".join(str(error) for error in errors)
