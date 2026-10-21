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
    return f"  secret_ok:\n    - pattern: {pattern}\n      sha256: {sha256}\n"


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
       ``Memory`` constructor, and ``_redact_for_model`` calls the acceptance-aware
       cascade. Deleting either line fails this test.
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
    assert any(
        isinstance(node, ast.Call) and _call_name(node) == "redact_secrets_with_acceptances"
        for node in ast.walk(redactor)
    )

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
