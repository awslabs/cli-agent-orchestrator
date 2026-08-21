"""Validate local Markdown links and GitHub-style heading fragments."""

from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
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
        (path.resolve() for path in (discover_markdown_files(root) if files is None else files)),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    parser = MarkdownIt("commonmark")
    headings = {path: _heading_slugs(path, parser) for path in markdown_files}
    errors: list[MarkdownLinkError] = []

    for source in markdown_files:
        for destination, line in _links(source, parser):
            error = _validate_destination(root, source, destination, line, headings, parser)
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
        scanner = _MarkdownLinkScanner(token.content, reference_labels)
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
                "href" if child.type == "link_open" else "src" if child.type == "image" else None
            )
            if attribute is None:
                continue
            destination = child.attrGet(attribute)
            if isinstance(destination, str):
                syntax_offset = scanner.next_link_offset(
                    inline_cursor,
                    autolink_text=child.content if child.markup == "autolink" else None,
                    image=child.type == "image",
                )
                yield (
                    destination,
                    inline_start_line + token.content.count("\n", 0, syntax_offset),
                )
                inline_cursor = syntax_offset + 1


def _token_start_line(token: Token) -> int:
    """Return the first one-based source line represented by a block token."""

    return token.map[0] + 1 if token.map is not None else 1


class _MarkdownLinkScanner:
    """Locate raw Markdown links with precomputed delimiter relationships.

    Parser destinations are normalized, so reference-style links and escaped
    direct destinations cannot be reliably located by searching for them.
    Tracking the syntax itself preserves the link's source line. Shortcut
    references need their parser-resolved labels to avoid treating ordinary
    bracket text as a link. Delimiter pairs are built once so a run of
    unmatched opening brackets cannot repeatedly rescan the same suffix.
    """

    def __init__(self, content: str, reference_labels: frozenset[str]) -> None:
        self._content = content
        self._reference_labels = reference_labels
        self._brackets = _matching_delimiters(content, "[", "]")
        self._bracket_delimiters = _unescaped_delimiter_prefix(content, "[", "]")
        self._parentheses = _matching_delimiters(content, "(", ")")

    def next_link_offset(self, cursor: int, *, autolink_text: str | None, image: bool) -> int:
        """Return the next parsed link's source offset at or after ``cursor``."""

        position = cursor
        while position < len(self._content):
            character = self._content[position]
            if character == "`":
                position = _skip_code_span(self._content, position)
                continue
            if autolink_text is not None and character == "<":
                autolink_end = _autolink_end(self._content, position)
                if (
                    autolink_end is not None
                    and self._content[position + 1 : autolink_end] == autolink_text
                ):
                    return position
            is_image = (
                character == "!"
                and position + 1 < len(self._content)
                and self._content[position + 1] == "["
            )
            is_link = character == "[" and (position == 0 or self._content[position - 1] != "!")
            if autolink_text is None and is_image == image and (is_link or is_image):
                link_start = position
                label_start = position + (2 if character == "!" else 1)
                label_end = self._brackets[label_start - 1]
                if (
                    label_end is not None
                    and self._link_syntax_end(label_start, label_end) is not None
                    and (image or not self._has_nested_link(label_start, label_end))
                ):
                    return link_start
            position += 1
        return cursor

    def _link_syntax_end(self, label_start: int, label_end: int) -> int | None:
        """Recognize direct, full/collapsed, and defined shortcut link forms."""

        position = label_end + 1
        while position < len(self._content) and self._content[position] in " \t":
            position += 1
        if position < len(self._content) and self._content[position] == "(":
            return self._parentheses[position]
        if position < len(self._content) and self._content[position] == "[":
            reference_end = self._brackets[position]
            if reference_end is None:
                return None
            reference_label = self._content[position + 1 : reference_end]
            if reference_label:
                label = self._normalized_reference_label(position + 1, reference_end)
            else:
                label = self._normalized_reference_label(label_start, label_end)
            if label is None:
                return None
            return reference_end if label in self._reference_labels else None
        label = self._normalized_reference_label(label_start, label_end)
        if label is None:
            return None
        return label_end if label in self._reference_labels else None

    def _normalized_reference_label(self, start: int, end: int) -> str | None:
        """Normalize a label only when it can be a CommonMark reference label."""

        if not self._reference_labels or self._has_unescaped_bracket(start, end):
            return None
        return normalizeReference(unescapeAll(self._content[start:end]))

    def _has_unescaped_bracket(self, start: int, end: int) -> bool:
        """Return whether a label contains an unescaped bracket delimiter."""

        return self._bracket_delimiters[end] != self._bracket_delimiters[start]

    def _has_nested_link(self, start: int, end: int) -> bool:
        """Return whether a non-image link candidate appears inside a label."""

        position = start
        while position < end:
            if self._content[position] == "`":
                position = _skip_code_span(self._content, position)
                continue
            if self._content[position] == "[" and (
                position == 0 or self._content[position - 1] != "!"
            ):
                nested_end = self._brackets[position]
                if (
                    nested_end is not None
                    and nested_end < end
                    and self._link_syntax_end(position + 1, nested_end) is not None
                ):
                    return True
            position += 1
        return False


def _skip_code_span(content: str, start: int) -> int:
    """Skip an inline code span so link-looking code is not treated as syntax."""

    delimiter_end = start
    while delimiter_end < len(content) and content[delimiter_end] == "`":
        delimiter_end += 1
    delimiter = content[start:delimiter_end]
    closing = content.find(delimiter, delimiter_end)
    return len(content) if closing < 0 else closing + len(delimiter)


def _autolink_end(content: str, start: int) -> int | None:
    """Return an autolink's closing angle bracket, without crossing lines."""

    position = start + 1
    while position < len(content) and content[position] not in ">\n":
        position += 1
    return position if position < len(content) and content[position] == ">" else None


def _matching_delimiters(content: str, opening: str, closing: str) -> list[int | None]:
    """Return each opening delimiter's matching close, ignoring escaped delimiters."""

    matches: list[int | None] = [None] * len(content)
    openings: list[int] = []
    position = 0
    while position < len(content):
        character = content[position]
        if character == "\\":
            position += 2
            continue
        if character == opening:
            openings.append(position)
        elif character == closing and openings:
            matches[openings.pop()] = position
        position += 1
    return matches


def _unescaped_delimiter_prefix(content: str, opening: str, closing: str) -> list[int]:
    """Count unescaped delimiters before each source position."""

    delimiters = [0]
    position = 0
    while position < len(content):
        character = content[position]
        if character == "\\":
            delimiters.append(delimiters[-1])
            position += 1
            if position < len(content):
                delimiters.append(delimiters[-1])
                position += 1
            continue
        delimiters.append(delimiters[-1] + (character == opening or character == closing))
        position += 1
    return delimiters


def _html_destinations(content: str, start_line: int) -> Iterable[tuple[str, int]]:
    """Yield href/src values from static HTML, retaining parser source lines."""

    parser = _HTMLLinkParser(start_line)
    parser.feed(content)
    parser.close()
    yield from parser.destinations


class _HTMLLinkParser(HTMLParser):
    """Extract static link attributes while HTMLParser handles raw-text elements."""

    def __init__(self, start_line: int) -> None:
        super().__init__(convert_charrefs=True)
        self._start_line = start_line
        self.destinations: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute_name = "href" if tag.lower() == "a" else "src" if tag.lower() == "img" else None
        if attribute_name is None:
            return
        attribute_occurrence = 0
        for name, value in attrs:
            if name.lower() != attribute_name or value is None:
                continue
            self.destinations.append(
                (
                    value,
                    self._start_line
                    + self.getpos()[0]
                    - 1
                    + _html_attribute_line_offset(
                        self.get_starttag_text() or "", attribute_name, attribute_occurrence
                    ),
                )
            )
            attribute_occurrence += 1


def _html_attribute_line_offset(tag_text: str, attribute_name: str, occurrence: int) -> int:
    """Return one matching attribute value's zero-based line within a start tag."""

    position = 1
    while (
        position < len(tag_text) and not tag_text[position].isspace() and tag_text[position] != ">"
    ):
        position += 1
    while position < len(tag_text):
        while position < len(tag_text) and tag_text[position].isspace():
            position += 1
        name_start = position
        while (
            position < len(tag_text)
            and not tag_text[position].isspace()
            and tag_text[position] not in "=>"
        ):
            position += 1
        name = tag_text[name_start:position]
        while position < len(tag_text) and tag_text[position].isspace():
            position += 1
        if position >= len(tag_text) or tag_text[position] != "=":
            position += 1
            continue
        position += 1
        while position < len(tag_text) and tag_text[position].isspace():
            position += 1
        value_start = position
        if position < len(tag_text) and tag_text[position] in "\"'":
            quote = tag_text[position]
            position += 1
            value_start = position
            while position < len(tag_text) and tag_text[position] != quote:
                position += 1
            position += 1
        else:
            while (
                position < len(tag_text)
                and not tag_text[position].isspace()
                and tag_text[position] != ">"
            ):
                position += 1
        if name.lower() == attribute_name:
            if occurrence == 0:
                return tag_text.count("\n", 0, value_start)
            occurrence -= 1
    return 0


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
        return MarkdownLinkError(source, line, destination, "path escapes the repository")
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
