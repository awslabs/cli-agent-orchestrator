"""#644 AC10: vault ``secret_gate: "warn"`` redacts every model-bound path.

Ruling (maintainer, 2026-10-21): under ``warn`` a secret-bearing note is indexed
and recallable, but every credential-shaped span is replaced by
``[REDACTED:<pattern>]`` before note text reaches an agent, by injection or by
recall. Raw bytes pass only through a per-finding acceptance in the note's own
frontmatter, ``cao.secret_ok: [{pattern, sha256}]``, bound to the pattern name
and the SHA-256 of the exact matched span. A boolean or pattern-only acceptance
is refused. Acceptances are reported in status so the record is never silent.

Fixtures are assembled at runtime so no credential-shaped literal sits in the
source (the repo's gitleaks gate scans tests too).
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import re
from datetime import datetime, timezone
from pathlib import Path
from test.services.vault.test_vault_injection_renderer import (
    _counter_values,
    _injectable_renderer,
)

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.clients.database import VaultFindingModel, VaultNoteModel
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.secret_gate import (
    fingerprint_secrets,
    normalize_line_endings,
    pattern_names,
    redact_secrets,
    redact_secrets_with_acceptances,
    span_sha256,
)
from cli_agent_orchestrator.services.vault.binding import VaultBinding, resolve
from cli_agent_orchestrator.services.vault.config import VaultConfig
from cli_agent_orchestrator.services.vault.parser import (
    frontmatter_boundary,
    parse_note,
    secret_acceptances,
    secret_scan_text,
    split_frontmatter,
)
from cli_agent_orchestrator.services.vault.reader import (
    NO_REQUESTER_IDENTITY,
    load_candidate,
    resolve_candidates,
)
from cli_agent_orchestrator.services.vault.reconcile import reconcile

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "cli_agent_orchestrator"
NOTE_RELPATH = "Projects/CAO Design/Design.md"

_AWS_DOC_SAMPLE = "wJalrXUtn" + "FEMI/K7MD" + "ENG/bPxRf" + "iCYEXAMPL" + "EKEY"
_JWT_SAMPLE = ".".join(
    [
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ]
)

# One sample per pattern: (note body, substring that must never reach an agent).
# ``test_sample_table_covers_every_pattern`` fails when a pattern is added to
# ``secret_gate._SECRET_PATTERNS`` without a row here, so coverage of the
# redaction path cannot silently lag the pattern table.
_SAMPLES: dict[str, tuple[str, str]] = {
    "aws_access_key": ("creds: AKIAIOSFODNN7EXAMPLE in config", "AKIAIOSFODNN7EXAMPLE"),
    "aws_secret_access_key": ("AWS_SECRET_ACCESS_KEY=" + _AWS_DOC_SAMPLE, _AWS_DOC_SAMPLE),
    "pem_private_key": (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "BEGIN RSA PRIVATE KEY",
    ),
    "anthropic_api_key": ("key sk-ant-api03-" + "Qz7" * 30, "sk-ant-api03-Qz7"),
    "openai_api_key": ("OPENAI sk-proj-" + "Ab9" * 20, "sk-proj-Ab9"),
    "github_fine_grained_pat": (
        "github_pat_" + "A" * 22 + "_" + "b" * 59,
        "github_pat_" + "A" * 22,
    ),
    "github_pat": ("ghp_" + "a" * 36, "ghp_" + "a" * 36),
    "gitlab_pat": ("glpat-" + "x" * 20, "glpat-" + "x" * 20),
    "slack_token": (
        "xox" + "b-1234567890-1234567890-AbCdEfGhIjKlMnOp",
        "AbCdEfGhIjKlMnOp",
    ),
    "jwt": (_JWT_SAMPLE, _JWT_SAMPLE),
    "bearer_token": (
        "Authorization: Bearer abcdef0123456789ABCDEF",
        "abcdef0123456789ABCDEF",
    ),
    "secret_assignment": ("password: hunter2sixteen", "hunter2sixteen"),
}

_SECRET_BODY = "password: hunter2sixteen"
_SECRET_VALUE = "hunter2sixteen"

# Credentials that two or three patterns match, innermost first: the vendor
# pattern on the value, then ``bearer_token`` or ``secret_assignment`` on the
# assignment around it. (note body, the innermost value that must never leak,
# the chain of pattern names in cascade order.)
_NESTED: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "github_token_env": (
        "GITHUB_TOKEN=ghp_" + "a" * 36,
        "ghp_" + "a" * 36,
        ("github_pat", "bearer_token"),
    ),
    "anthropic_key_env": (
        "ANTHROPIC_API_KEY=sk-ant-api03-" + "Qz7" * 30,
        "sk-ant-api03-" + "Qz7" * 30,
        ("anthropic_api_key", "bearer_token"),
    ),
    "gitlab_key_env": (
        "API_KEY=glpat-" + "x" * 20,
        "glpat-" + "x" * 20,
        ("gitlab_pat", "bearer_token"),
    ),
    "bearer_github_header": (
        "Authorization: Bearer ghp_" + "c" * 36,
        "ghp_" + "c" * 36,
        ("github_pat", "bearer_token"),
    ),
    "three_levels": (
        "secret: token=ghp_" + "b" * 36,
        "ghp_" + "b" * 36,
        ("github_pat", "bearer_token", "secret_assignment"),
    ),
}
_ENV_TOKEN_BODY, _ENV_TOKEN_VALUE, _ENV_TOKEN_CHAIN = _NESTED["github_token_env"]
# ``bearer_token`` allows a line break between key and value; scan hashes the
# LF-normalised note, the reader hashes the file as written.
_CRLF_SPAN_BODY = "token:\r\n  abcdef0123456789ABCDEF"
_FINGERPRINT_LINE = re.compile(r"pattern=(\S+) sha256=([0-9a-f]{64})(?: inside=(\S+))?")

_BEARER_VALUE = "abcdef0123456789ABCDEF"
# U+2028 LINE SEPARATOR: ``splitlines()`` treats it as a line break, splitting
# on ``"\n"`` does not, and ``.`` in ``aws_secret_access_key`` matches it, so a
# span can cross it.
_LINE_SEPARATOR = "\u2028"

# A match may begin on the leading H1 line and end on the next one, because
# ``bearer_token`` and ``secret_assignment`` allow a line break between the key
# and the value. (note body, pattern, the value that must never reach an agent,
# the content the agent is served.) Where the marker replaces the line break the
# value's line joins the H1 line and the H1 strip removes both; the agent is
# served less than the note holds, never the credential. The last row is the
# control: no H1, so nothing is stripped and the marker stands in place.
_H1_SPAN_ROWS: dict[str, tuple[str, str, str, str]] = {
    "h1_ends_in_token": (
        f"# Deploy token\n{_BEARER_VALUE}",
        "bearer_token",
        _BEARER_VALUE,
        "",
    ),
    "h1_ends_in_api_token": (
        f"# Staging API token\n{_BEARER_VALUE}",
        "bearer_token",
        _BEARER_VALUE,
        "",
    ),
    "h1_ends_in_password": (
        "# Password:\nhunter2hunter2",
        "secret_assignment",
        "hunter2hunter2",
        "",
    ),
    "line_separator_under_h1": (
        f"# Notes\naws{_LINE_SEPARATOR}access: {_AWS_DOC_SAMPLE}",
        "aws_secret_access_key",
        _AWS_DOC_SAMPLE,
        "[REDACTED:aws_secret_access_key]",
    ),
    "control_no_h1": (
        f"Deploy token\n{_BEARER_VALUE}",
        "bearer_token",
        _BEARER_VALUE,
        "Deploy [REDACTED:bearer_token]",
    ),
}


def _warn_renderer(tmp_path, monkeypatch):
    """The injectable renderer fixture with the project mapping switched to warn."""
    service, session_factory, vault_root, config = _injectable_renderer(tmp_path, monkeypatch)
    mappings = [
        (
            mapping.model_copy(update={"secret_gate": "warn"})
            if mapping.folder == "Projects/CAO Design"
            else mapping
        )
        for mapping in config.vaults[0].mappings
    ]
    vault = config.vaults[0].model_copy(update={"mappings": mappings})
    warn_config = VaultConfig(enabled=True, vaults=[vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: warn_config)
    return service, session_factory, vault_root, warn_config, vault


def _write_note(vault_root, body: str, *, cao_extra: str = "") -> Path:
    note = vault_root / "Projects" / "CAO Design" / "Design.md"
    note.write_text(f"---\ncao:\n  key: design\n{cao_extra}---\n{body}", encoding="utf-8")
    return note


def _acceptance_yaml(pattern: str, sha256: str) -> str:
    return _acceptances_yaml([(pattern, sha256)])


def _acceptances_yaml(pairs) -> str:
    return "  secret_ok:\n" + "".join(
        f"    - pattern: {pattern}\n      sha256: {sha256}\n" for pattern, sha256 in pairs
    )


def _printed_fingerprints(
    memory_cli, monkeypatch, warn_config, vault
) -> list[tuple[str, str, str]]:
    """``(pattern, sha256, inside)`` for every line ``vault scan --fingerprints`` prints."""
    monkeypatch.setattr(memory_cli, "_configured_vault", lambda: (warn_config, vault))
    result = CliRunner().invoke(memory_cli.vault_scan, ["--fingerprints"])
    assert result.exit_code == 0, result.output
    return [
        (m.group(1), m.group(2), m.group(3) or "")
        for m in map(_FINGERPRINT_LINE.search, result.output.splitlines())
        if m is not None
    ]


def _reconcile(vault, run_id: str) -> None:
    reconcile(
        vault,
        apply=True,
        run_id=run_id,
        run_started_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )


def _recall(config):
    binding = resolve("project", "fixture-project", vault_config=config)
    assert isinstance(binding, VaultBinding)
    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=NO_REQUESTER_IDENTITY,
        consumer="explicit_recall",
    )
    if not candidates:
        return None
    return load_candidate(candidates[0], max_body_chars=4096, require_injectable=False)


def _findings(session_factory) -> dict[str, list[str]]:
    """Finding details by code, unfolding reconcile's ``count=N; code=..; detail=..`` rows."""
    with session_factory() as db:
        rows = db.query(VaultFindingModel).all()
        out: dict[str, list[str]] = {}
        for row in rows:
            detail = row.detail
            count = 1
            if detail.startswith("count="):
                head, _sep, rest = detail.partition("; code=")
                count = int(head.removeprefix("count="))
                detail = rest.partition("; detail=")[2]
            out.setdefault(row.code, []).extend([detail] * count)
        return out


def _note_status(session_factory) -> str:
    with session_factory() as db:
        return db.query(VaultNoteModel).filter_by(vault_relpath=NOTE_RELPATH).one().status


# ---------------------------------------------------------------------------
# Every pattern redacts, on both paths
# ---------------------------------------------------------------------------


def test_sample_table_covers_every_pattern():
    """A new pattern in secret_gate must get a redaction sample here."""
    assert set(_SAMPLES) == set(pattern_names())
    assert len(pattern_names()) == 12


@pytest.mark.parametrize("pattern", sorted(_SAMPLES))
def test_every_pattern_is_redacted_on_recall_and_injection(pattern, tmp_path, monkeypatch):
    body, must_not_leak = _SAMPLES[pattern]
    service, session_factory, vault_root, warn_config, vault = _warn_renderer(tmp_path, monkeypatch)
    _write_note(vault_root, body)
    _reconcile(vault, f"warn-{pattern}")
    assert _note_status(session_factory) == "indexed"
    assert "secret_detected" in _findings(session_factory)

    recalled = _recall(warn_config)
    block = service.get_memory_context_for_terminal("worker")

    assert recalled is not None
    assert must_not_leak not in recalled.content
    assert f"[REDACTED:{pattern}]" in recalled.content
    assert pattern in recalled.redacted_patterns
    assert must_not_leak not in block
    assert f"[REDACTED:{pattern}]" in block


@pytest.mark.parametrize("pattern", sorted(_SAMPLES))
def test_empty_allowlist_matches_redact_secrets_exactly(pattern):
    """The acceptance-aware cascade with no acceptances is the export redactor."""
    body, _leak = _SAMPLES[pattern]
    text, matches = redact_secrets_with_acceptances(body, ())
    assert text == redact_secrets(body)[0]
    assert all(not m.accepted for m in matches)
    assert pattern in {m.pattern for m in matches}


def test_recall_through_memory_service_is_redacted(tmp_path, monkeypatch):
    """Explicit recall via MemoryService carries the marker, not the bytes."""
    service, _session_factory, vault_root, _warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    _write_note(vault_root, _SECRET_BODY)
    _reconcile(vault, "warn-recall")
    context = dict(service._get_terminal_context("worker"))

    results = asyncio.run(
        service.recall(scope="project", limit=10, terminal_context=context, search_mode="metadata")
    )

    vault_results = [m for m in results if m.source_kind == "vault"]
    assert vault_results, "the warn-mapped note must be recallable"
    for memory in vault_results:
        assert _SECRET_VALUE not in memory.content
        assert "[REDACTED:secret_assignment]" in memory.content


# ---------------------------------------------------------------------------
# Nested matches: fingerprints are independent of the acceptance set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_NESTED))
def test_nested_fingerprints_are_independent_of_the_acceptance_set(name):
    """The pairs ``--fingerprints`` prints are the pairs checked under every allowlist.

    Enumerates every subset of the printed pairs. The innermost value reaches
    the output exactly when every pair is accepted, and the fingerprints the
    cascade reports never change with the allowlist. Reverting the P2-2 fix
    (re-matching later patterns over partly unredacted text) fails the
    full-subset case: the outer hash drifts and the value stays redacted.
    """
    body, value, chain = _NESTED[name]
    text, matches = redact_secrets_with_acceptances(body, ())
    assert text == redact_secrets(body)[0]
    assert tuple(m.pattern for m in matches) == chain
    assert [m.enclosed_by for m in matches] == [*chain[1:], None]
    printed = [(m.pattern, m.sha256) for m in matches]
    assert fingerprint_secrets(body).fingerprints == tuple(printed)

    for size in range(len(printed) + 1):
        for subset in itertools.combinations(printed, size):
            rendered, again = redact_secrets_with_acceptances(body, subset)
            assert [(m.pattern, m.sha256) for m in again] == printed, subset
            assert [m.accepted for m in again] == [pair in subset for pair in printed]
            if size == len(printed):
                assert rendered == body
                assert all(m.passed for m in again)
            else:
                assert value not in rendered, subset
                assert not again[0].passed


def test_inner_acceptance_alone_is_blocked_and_names_the_outer_match():
    body, value, (inner, outer) = _NESTED["github_token_env"]
    printed = fingerprint_secrets(body).fingerprints

    text, matches = redact_secrets_with_acceptances(body, [printed[0]])

    assert text == f"GITHUB_[REDACTED:{outer}]"
    assert value not in text
    assert (matches[0].pattern, matches[0].accepted, matches[0].passed) == (inner, True, False)
    assert matches[0].blocked_by == outer
    assert (matches[1].accepted, matches[1].passed, matches[1].blocked_by) == (False, False, None)


def test_outer_acceptance_alone_serves_the_outer_span_as_fingerprinted():
    body, value, (inner, outer) = _NESTED["github_token_env"]
    printed = fingerprint_secrets(body).fingerprints

    text, matches = redact_secrets_with_acceptances(body, [printed[1]])

    assert text == f"GITHUB_TOKEN=[REDACTED:{inner}]"
    assert value not in text
    assert (matches[0].accepted, matches[0].passed) == (False, False)
    assert (matches[1].accepted, matches[1].passed) == (True, True)


def test_three_level_nesting_blames_the_nearest_unaccepted_match():
    body, _value, chain = _NESTED["three_levels"]
    printed = fingerprint_secrets(body).fingerprints

    _text, matches = redact_secrets_with_acceptances(body, printed[:2])

    assert [m.passed for m in matches] == [False, False, False]
    assert matches[0].blocked_by == chain[2]
    assert matches[1].blocked_by == chain[2]


def test_reader_parses_frontmatter_only_when_the_body_has_a_match(tmp_path, monkeypatch):
    """P3: a clean note costs one cascade; frontmatter is parsed only to apply acceptances."""
    from cli_agent_orchestrator.services.vault import reader as reader_module

    _service, _session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    calls: list[str] = []
    real_parse_note = reader_module.parse_note

    def _counting_parse_note(text, **kwargs):
        calls.append(text)
        return real_parse_note(text, **kwargs)

    monkeypatch.setattr(reader_module, "parse_note", _counting_parse_note)

    _write_note(vault_root, "nothing credential-shaped here")
    _reconcile(vault, "warn-clean")
    recalled = _recall(warn_config)
    assert recalled is not None and "credential-shaped" in recalled.content
    assert calls == []

    _write_note(vault_root, _SECRET_BODY)
    _reconcile(vault, "warn-secret")
    recalled = _recall(warn_config)
    assert recalled is not None and "[REDACTED:secret_assignment]" in recalled.content
    assert len(calls) == 1


def test_fingerprint_is_independent_of_line_endings_inside_the_span():
    lf = _CRLF_SPAN_BODY.replace("\r\n", "\n")
    assert span_sha256(_CRLF_SPAN_BODY) == span_sha256(lf)
    assert fingerprint_secrets(_CRLF_SPAN_BODY).fingerprints == fingerprint_secrets(lf).fingerprints
    text, matches = redact_secrets_with_acceptances(
        _CRLF_SPAN_BODY, fingerprint_secrets(lf).fingerprints
    )
    assert text == _CRLF_SPAN_BODY
    assert [m.passed for m in matches] == [True]


def test_crlf_note_accepts_the_fingerprint_scan_printed(tmp_path, monkeypatch):
    """A span that crosses a CRLF line break is served raw with the printed fingerprint."""
    from cli_agent_orchestrator.cli.commands import memory as memory_cli

    _service, session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    _write_note(vault_root, _CRLF_SPAN_BODY)
    printed = _printed_fingerprints(memory_cli, monkeypatch, warn_config, vault)
    assert [p for p, _h, _i in printed] == ["bearer_token"]
    _write_note(
        vault_root, _CRLF_SPAN_BODY, cao_extra=_acceptances_yaml([(p, h) for p, h, _i in printed])
    )
    _reconcile(vault, "warn-crlf")

    recalled = _recall(warn_config)

    assert recalled is not None
    assert "abcdef0123456789ABCDEF" in recalled.content
    assert "[REDACTED" not in recalled.content
    assert _findings(session_factory)["secret_accepted"] == ["bearer_token"]
    assert "secret_acceptance_unmatched" not in _findings(session_factory)
    assert _counter_values(session_factory)["secret_acceptance.spans_passed"] == 1


# ---------------------------------------------------------------------------
# One text for the scan and the reader: redact first, present second
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_H1_SPAN_ROWS))
def test_a_match_starting_in_the_h1_is_redacted_on_every_model_bound_path(
    name, tmp_path, monkeypatch
):
    """The reader redacts the text the scan hashed, leading H1 included.

    ``bearer_token`` and ``secret_assignment`` allow a line break between the
    key and the value, so a heading that ends in a key word and a value on the
    next line are one match for the scan. The reader used to strip the H1
    first, leaving the value alone with nothing to match it, so the note was
    indexed ``secret_detected`` while the agent was served the raw bytes.
    """
    from cli_agent_orchestrator.cli.commands import memory as memory_cli

    body, pattern, must_not_leak, served = _H1_SPAN_ROWS[name]
    service, session_factory, vault_root, warn_config, vault = _warn_renderer(tmp_path, monkeypatch)
    _write_note(vault_root, body)
    printed = _printed_fingerprints(memory_cli, monkeypatch, warn_config, vault)
    assert [p for p, _h, _i in printed] == [pattern]
    _reconcile(vault, f"warn-h1-{name}")
    assert _note_status(session_factory) == "indexed"
    assert _findings(session_factory)["secret_detected"] == [pattern]

    recalled = _recall(warn_config)
    block = service.get_memory_context_for_terminal("worker")
    context = dict(service._get_terminal_context("worker"))
    results = asyncio.run(
        service.recall(scope="project", limit=10, terminal_context=context, search_mode="metadata")
    )

    assert recalled is not None
    assert recalled.content == served
    assert must_not_leak not in recalled.content
    assert recalled.redacted_patterns == [pattern]
    assert must_not_leak not in block
    assert all(must_not_leak not in m.content for m in results if m.source_kind == "vault")
    assert _counter_values(session_factory)["secret_redaction.memories_redacted"] >= 1


def test_a_marker_spanning_the_h1_fails_safe_and_an_acceptance_restores_the_line(
    tmp_path, monkeypatch
):
    """The documented side effect of redacting before the strip, both ways round.

    Unaccepted, the marker stands where the line break was, so the value's line
    joins the H1 line and the strip removes both: the agent is served less than
    the note holds, never the credential. Accepting the printed fingerprint puts
    the line break back, so only the H1 line goes.
    """
    from cli_agent_orchestrator.cli.commands import memory as memory_cli

    body = f"# Deploy token\n{_BEARER_VALUE}"
    _service, _session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    _write_note(vault_root, body)
    printed = _printed_fingerprints(memory_cli, monkeypatch, warn_config, vault)
    _reconcile(vault, "warn-h1-failsafe")

    assert _recall(warn_config).content == ""

    _write_note(vault_root, body, cao_extra=_acceptances_yaml([(p, h) for p, h, _i in printed]))
    accepted = _recall(warn_config)

    assert accepted is not None
    assert accepted.content == _BEARER_VALUE
    assert accepted.redacted_patterns == []


def test_the_reader_scans_exactly_the_text_the_scanner_fingerprints():
    """One helper defines "the text we scan", so the two paths cannot drift.

    The scan LF-normalises the whole note before it parses; the reader parses
    the file as written. ``secret_scan_text`` is the single definition of the
    text the gate scans, fingerprints and redacts, so a note carrying CRLF, a
    U+2028 line separator and an H1 fingerprints identically on both paths.
    """
    body = (
        f"# Notes\r\nAPI token\r\n{_BEARER_VALUE}\r\n"
        f"aws{_LINE_SEPARATOR}access: {_AWS_DOC_SAMPLE}\r\n"
    )
    text = f"---\r\ncao:\r\n  key: design\r\n---\r\n{body}"

    scanned = secret_scan_text(split_frontmatter(normalize_line_endings(text), 4096))
    read = secret_scan_text(split_frontmatter(text, 4096))

    assert read == scanned
    assert "\r" not in read
    # A line separator is content the patterns match across, not a line ending.
    assert _LINE_SEPARATOR in read
    assert fingerprint_secrets(read).fingerprints == fingerprint_secrets(scanned).fingerprints
    assert {p for p, _h in fingerprint_secrets(read).fingerprints} == {
        "bearer_token",
        "aws_secret_access_key",
    }


def test_strip_leading_h1_leaves_every_byte_but_one_lf_delimited_line():
    """The H1 strip splits on LF only; it is not a line-separator rewrite.

    ``splitlines()`` treated U+2028, U+2029, U+000B, U+000C and U+0085 as line
    breaks and the rejoin turned each into an LF, rewriting body bytes the scan
    had fingerprinted. Splitting on LF keeps the transform to what it is for:
    dropping one leading heading line.
    """
    from cli_agent_orchestrator.services.vault.reader import _strip_leading_h1

    for separator in ("\u2028", "\u2029", "\v", "\f", "\x85"):
        assert _strip_leading_h1(f"# Title\nkept{separator}text") == f"kept{separator}text"
        # The separator inside the heading is not a line break, so the whole
        # first LF-delimited line goes.
        assert _strip_leading_h1(f"# Ti{separator}tle\nkept") == "kept"
    assert _strip_leading_h1("## Not an h1\nkept") == "## Not an h1\nkept"
    assert _strip_leading_h1("no heading\nkept") == "no heading\nkept"


def test_presentation_transforms_run_only_on_the_redacted_text():
    """Structural guard for the ordering defect class: scan text in, presentation out.

    Two properties, checked on the source so a new surface fails loudly:

    1. In ``load_candidate`` the redaction call takes ``secret_scan_text`` and
       the H1 strip comes after it. Swapping the two lines fails this test.
    2. Every vault surface that hands note text to the secret gate routes it
       through ``secret_scan_text``. That is what keeps the scanned, the
       fingerprinted and the redacted bytes equal: reading ``region.body``
       directly at any one of them reintroduces the drift.
    """
    reader_path = SOURCE_ROOT / "services" / "vault" / "reader.py"
    reader_tree = ast.parse(reader_path.read_text(encoding="utf-8"), filename=str(reader_path))
    load_definition = _function(reader_tree, "load_candidate")
    calls = [node for node in ast.walk(load_definition) if isinstance(node, ast.Call)]
    redact_call = next(node for node in calls if _call_name(node) == "_redact_for_model")
    strip_call = next(node for node in calls if _call_name(node) == "_strip_leading_h1")

    assert redact_call.lineno < strip_call.lineno, "redact before any presentation transform"
    assert isinstance(redact_call.args[2], ast.Call)
    assert _call_name(redact_call.args[2]) == "secret_scan_text"

    # Callee -> index of the positional argument carrying vault note text.
    note_text_argument = {
        "classify_secret": 0,
        "redact_secrets_with_acceptances": 0,
        "_redact_for_model": 2,
    }
    routed: set[tuple[str, str | None, str]] = set()
    unrouted: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            index = note_text_argument.get(_call_name(node) or "")
            if index is None or len(node.args) <= index:
                continue
            argument = node.args[index]
            where = (path.name, _enclosing_function(node, parents), _call_name(node) or "")
            if isinstance(argument, ast.Call) and _call_name(argument) == "secret_scan_text":
                routed.add(where)
            else:
                unrouted.append(f"{path.name}:{node.lineno} {_call_name(node)}")

    assert unrouted == []
    assert routed == {
        ("scan.py", "scan_vault", "classify_secret"),
        ("scan.py", "_acceptance_findings", "redact_secrets_with_acceptances"),
        ("memory.py", "vault_scan", "redact_secrets_with_acceptances"),
        ("reader.py", "load_candidate", "_redact_for_model"),
    }


# ---------------------------------------------------------------------------
# Per-finding acceptance
# ---------------------------------------------------------------------------


def test_acceptance_with_matching_pattern_and_hash_passes_raw_bytes(tmp_path, monkeypatch):
    service, session_factory, vault_root, warn_config, vault = _warn_renderer(tmp_path, monkeypatch)
    digest = span_sha256(_SECRET_BODY)
    _write_note(vault_root, _SECRET_BODY, cao_extra=_acceptance_yaml("secret_assignment", digest))
    _reconcile(vault, "warn-accept")

    recalled = _recall(warn_config)
    block = service.get_memory_context_for_terminal("worker")

    assert recalled is not None
    assert _SECRET_VALUE in recalled.content
    assert "[REDACTED" not in recalled.content
    assert recalled.redacted_patterns == []
    assert _SECRET_VALUE in block
    findings = _findings(session_factory)
    assert findings["secret_accepted"] == ["secret_assignment"]
    assert findings["secret_detected"] == ["secret_assignment"]
    assert "secret_acceptance_unmatched" not in findings
    counters = _counter_values(session_factory)
    assert counters["secret_acceptance.spans_passed"] == 2  # recall + injection loads
    assert "secret_redaction.memories_redacted" not in counters


def test_acceptance_with_wrong_hash_is_not_honoured(tmp_path, monkeypatch):
    _service, session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    wrong = span_sha256(_SECRET_BODY + "x")
    _write_note(vault_root, _SECRET_BODY, cao_extra=_acceptance_yaml("secret_assignment", wrong))
    _reconcile(vault, "warn-wrong-hash")

    recalled = _recall(warn_config)

    assert recalled is not None
    assert _SECRET_VALUE not in recalled.content
    assert "[REDACTED:secret_assignment]" in recalled.content
    findings = _findings(session_factory)
    assert "secret_accepted" not in findings
    assert findings["secret_acceptance_unmatched"] == ["secret_assignment: no current span matches"]


def test_acceptance_with_right_hash_but_wrong_pattern_is_not_honoured(tmp_path, monkeypatch):
    _service, _session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    digest = span_sha256(_SECRET_BODY)
    _write_note(vault_root, _SECRET_BODY, cao_extra=_acceptance_yaml("bearer_token", digest))
    _reconcile(vault, "warn-wrong-pattern")

    recalled = _recall(warn_config)

    assert recalled is not None
    assert _SECRET_VALUE not in recalled.content


def test_nested_credential_passes_raw_when_every_printed_fingerprint_is_accepted(
    tmp_path, monkeypatch
):
    """The P2-2 scenario end to end: accept exactly what ``--fingerprints`` printed."""
    from cli_agent_orchestrator.cli.commands import memory as memory_cli

    service, session_factory, vault_root, warn_config, vault = _warn_renderer(tmp_path, monkeypatch)
    _write_note(vault_root, _ENV_TOKEN_BODY)
    printed = _printed_fingerprints(memory_cli, monkeypatch, warn_config, vault)
    assert [(p, inside) for p, _h, inside in printed] == [
        ("github_pat", "bearer_token"),
        ("bearer_token", ""),
    ]
    _write_note(
        vault_root, _ENV_TOKEN_BODY, cao_extra=_acceptances_yaml([(p, h) for p, h, _i in printed])
    )
    _reconcile(vault, "warn-nested-accept-all")

    recalled = _recall(warn_config)
    block = service.get_memory_context_for_terminal("worker")

    assert recalled is not None
    assert _ENV_TOKEN_BODY in recalled.content
    assert "[REDACTED" not in recalled.content
    assert recalled.redacted_patterns == []
    assert _ENV_TOKEN_BODY in block
    findings = _findings(session_factory)
    # Reconcile folds same-code findings into one sorted, comma-joined detail.
    assert len(findings["secret_accepted"]) == 2
    assert set(findings["secret_accepted"]) == {"bearer_token,github_pat"}
    assert "secret_acceptance_unmatched" not in findings
    counters = _counter_values(session_factory)
    assert counters["secret_acceptance.spans_passed"] == 4  # two spans, recall + injection
    assert "secret_redaction.memories_redacted" not in counters


def test_nested_credential_with_only_the_inner_acceptance_is_redacted_and_reported(
    tmp_path, monkeypatch
):
    """An accepted span inside an unaccepted match is redacted, and status does not call it served."""
    service, session_factory, vault_root, warn_config, vault = _warn_renderer(tmp_path, monkeypatch)
    inner_pair = fingerprint_secrets(_ENV_TOKEN_BODY).fingerprints[0]
    _write_note(vault_root, _ENV_TOKEN_BODY, cao_extra=_acceptances_yaml([inner_pair]))
    _reconcile(vault, "warn-nested-inner-only")

    recalled = _recall(warn_config)
    block = service.get_memory_context_for_terminal("worker")

    assert recalled is not None
    assert _ENV_TOKEN_VALUE not in recalled.content
    assert "GITHUB_[REDACTED:bearer_token]" in recalled.content
    assert recalled.redacted_patterns == ["github_pat", "bearer_token"]
    assert _ENV_TOKEN_VALUE not in block
    findings = _findings(session_factory)
    assert "secret_accepted" not in findings
    assert findings["secret_acceptance_unmatched"] == [
        "github_pat: span is inside an unaccepted bearer_token match; accept that fingerprint too"
    ]
    counters = _counter_values(session_factory)
    assert "secret_acceptance.spans_passed" not in counters
    assert counters["secret_redaction.memories_redacted"] == 2


@pytest.mark.parametrize(
    "secret_ok",
    [
        "  secret_ok: true\n",
        "  secret_ok:\n    - pattern: secret_assignment\n",
        "  secret_ok:\n    - secret_assignment\n",
        "  secret_ok:\n    - pattern: secret_assignment\n      sha256: true\n",
        "  secret_ok:\n    - pattern: not_a_pattern\n      sha256: " + "0" * 64 + "\n",
        "  secret_ok:\n    - pattern: secret_assignment\n      sha256: " + "0" * 63 + "\n",
    ],
    ids=["boolean", "pattern_only", "bare_name", "sha_not_hex", "unknown_pattern", "short_hash"],
)
def test_malformed_acceptance_quarantines_the_note(secret_ok, tmp_path, monkeypatch):
    """Anything but a list of {pattern, sha256} is an invalid cao block, not a bypass."""
    _service, session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    _write_note(vault_root, _SECRET_BODY, cao_extra=secret_ok)
    _reconcile(vault, "warn-malformed")

    assert _note_status(session_factory) == "quarantined"
    assert "invalid_cao_block" in _findings(session_factory)
    assert _recall(warn_config) is None


def test_acceptance_is_inert_on_a_reject_mapping(tmp_path, monkeypatch):
    """reject is unchanged: the note is quarantined and the acceptance is reported inert."""
    _service, session_factory, vault_root, config = _injectable_renderer(tmp_path, monkeypatch)
    digest = span_sha256(_SECRET_BODY)
    _write_note(vault_root, _SECRET_BODY, cao_extra=_acceptance_yaml("secret_assignment", digest))
    _reconcile(config.vaults[0], "reject-accept")

    assert _note_status(session_factory) == "quarantined"
    findings = _findings(session_factory)
    assert findings["secret_detected"] == ["secret_assignment"]
    assert findings["secret_acceptance_unmatched"] == [
        "secret_assignment: mapping secret_gate is not warn"
    ]
    assert "secret_accepted" not in findings


def test_acceptance_is_read_from_the_note_so_a_changed_secret_is_redacted(tmp_path, monkeypatch):
    """Editing the credential after reconcile invalidates the acceptance at read time."""
    _service, _session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    digest = span_sha256(_SECRET_BODY)
    _write_note(vault_root, _SECRET_BODY, cao_extra=_acceptance_yaml("secret_assignment", digest))
    _reconcile(vault, "warn-drift")
    assert _recall(warn_config).content.strip() == _SECRET_BODY

    _write_note(
        vault_root,
        "password: rotatedvalue99",
        cao_extra=_acceptance_yaml("secret_assignment", digest),
    )
    recalled = _recall(warn_config)

    assert recalled is not None
    assert recalled.index_freshness == "stale"
    assert "rotatedvalue99" not in recalled.content
    assert "[REDACTED:secret_assignment]" in recalled.content


def test_secret_acceptances_parser_is_fail_closed():
    assert secret_acceptances({}) == ()
    assert secret_acceptances({"secret_ok": True}) == ()
    assert secret_acceptances({"secret_ok": [{"pattern": "secret_assignment"}]}) == ()
    digest = "a" * 64
    assert secret_acceptances(
        {"secret_ok": [{"pattern": "secret_assignment", "sha256": digest}]}
    ) == (("secret_assignment", digest),)
    parsed = parse_note(
        "---\ncao:\n  key: k\n  secret_ok: true\n---\nbody",
        max_frontmatter_bytes=4096,
        secret_gate="warn",
    )
    assert parsed.finding_code is not None
    assert parsed.finding_code.value == "invalid_cao_block"


# ---------------------------------------------------------------------------
# Reporting: status, fingerprints, config
# ---------------------------------------------------------------------------


def test_status_reports_acceptances(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import status as status_module
    from cli_agent_orchestrator.services.vault.status import get_vault_status

    _service, session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(status_module, "SessionLocal", session_factory)
    digest = span_sha256(_SECRET_BODY)
    _write_note(vault_root, _SECRET_BODY, cao_extra=_acceptance_yaml("secret_assignment", digest))
    _reconcile(vault, "warn-status")

    result = get_vault_status(warn_config)[0]

    assert result.secret_acceptances == ((NOTE_RELPATH, 1),)
    assert dict(result.finding_counts)["secret_accepted"] == 1
    assert not any("secret_gate='warn' with inject=true" in w for w in result.warnings)
    assert digest not in repr(result)
    assert _SECRET_VALUE not in repr(result)


def test_scan_fingerprints_shows_pattern_and_hash_without_persisting(tmp_path, monkeypatch):
    from cli_agent_orchestrator.cli.commands import memory as memory_cli

    _service, session_factory, vault_root, warn_config, vault = _warn_renderer(
        tmp_path, monkeypatch
    )
    _write_note(vault_root, _SECRET_BODY)
    monkeypatch.setattr(memory_cli, "_configured_vault", lambda: (warn_config, vault))

    result = CliRunner().invoke(memory_cli.vault_scan, ["--fingerprints"])

    assert result.exit_code == 0, result.output
    expected = (
        f"fingerprint: {NOTE_RELPATH} pattern=secret_assignment sha256={span_sha256(_SECRET_BODY)}"
    )
    assert expected in result.output
    assert _SECRET_VALUE not in result.output
    with session_factory() as db:
        assert db.query(VaultFindingModel).count() == 0


def test_writer_merge_retains_authored_secret_ok():
    from cli_agent_orchestrator.services.vault.writer import _merge_cao_fields

    existing = (
        "---\ncao:\n  key: design\n  managed: true\n  secret_ok:\n"
        "    - pattern: secret_assignment\n      sha256: " + "b" * 64 + "\n---\nbody"
    )
    merged = _merge_cao_fields(existing, {"type": "project"}, frontmatter_boundary(existing))

    assert merged["secret_ok"] == [{"pattern": "secret_assignment", "sha256": "b" * 64}]


# ---------------------------------------------------------------------------
# Structural guard: one chokepoint, enumerated surfaces
# ---------------------------------------------------------------------------


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    parent: ast.AST = node
    while parent in parents and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parent = parents[parent]
    return getattr(parent, "name", None)


def test_vault_note_text_reaches_agents_only_through_the_redaction_chokepoint():
    """Every agent-facing surface receives vault text from ``load_candidate``, which redacts.

    Three properties, each checked structurally so a new surface fails loudly:

    1. ``load_candidate`` binds ``body`` through ``_redact_for_model`` before the
       ``Memory`` constructor, and ``_redact_for_model`` runs the cascade
       (``fingerprint_secrets``) and renders it against the note's acceptances
       (``render``). Deleting either line fails this test.
    2. A vault-backed ``Memory`` is constructed in exactly two places: the
       chokepoint, and ``_store_vault_memory``, which echoes the caller's own
       input back to the writer and reads no vault bytes.
    3. The set of ``memory_service`` functions that consume ``load_candidate`` is
       the enumerated set of agent-facing surfaces. A new consumer must be added
       here, with a reviewer confirming it does not post-process the content
       into something unredacted. No second redaction layer exists in the
       service, so a bypass of the chokepoint cannot be masked.
    """
    reader_path = SOURCE_ROOT / "services" / "vault" / "reader.py"
    reader_tree = ast.parse(reader_path.read_text(encoding="utf-8"), filename=str(reader_path))
    load_definition = _function(reader_tree, "load_candidate")

    redaction_binds_body = any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == "_redact_for_model"
        and isinstance(node.targets[0], ast.Tuple)
        and isinstance(node.targets[0].elts[0], ast.Name)
        and node.targets[0].elts[0].id == "body"
        for node in ast.walk(load_definition)
    )
    assert redaction_binds_body, "load_candidate must bind body through _redact_for_model"
    memory_calls = [
        node
        for node in ast.walk(load_definition)
        if isinstance(node, ast.Call) and _call_name(node) == "Memory"
    ]
    assert len(memory_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in memory_calls[0].keywords}
    assert isinstance(keywords["content"], ast.Name) and keywords["content"].id == "body"
    assert "redacted_patterns" in keywords
    redactor = _function(reader_tree, "_redact_for_model")
    redactor_calls = {_call_name(node) for node in ast.walk(redactor) if isinstance(node, ast.Call)}
    assert {"fingerprint_secrets", "render"} <= redactor_calls

    vault_memory_constructors: set[tuple[str, str | None]] = set()
    load_consumers: set[tuple[str, str | None]] = set()
    second_layer: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name == "Memory" and any(
                keyword.arg == "source_kind"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "vault"
                for keyword in node.keywords
            ):
                vault_memory_constructors.add((path.name, _enclosing_function(node, parents)))
            if name == "load_candidate" and path.name != "reader.py":
                load_consumers.add((path.name, _enclosing_function(node, parents)))
            if path.name == "memory_service.py" and name in {
                "redact_secrets",
                "_redact_injected_vault_content",
            }:
                second_layer.append(f"{path.name}:{node.lineno}")

    assert vault_memory_constructors == {
        ("reader.py", "load_candidate"),
        ("memory_service.py", "_store_vault_memory"),
    }
    assert load_consumers == {
        ("memory_service.py", "get_memory_context"),
        ("memory_service.py", "_metadata_recall"),
        ("memory_service.py", "_bm25_relevance"),
        ("memory_service.py", "_bm25_search"),
        ("memory_service.py", "_load_related_vault_memory"),
    }
    assert second_layer == []
