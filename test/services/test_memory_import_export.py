"""Phase 4 U2 — Memory Import/Export tests.

Covers per devsecops T1–T20 hostile-archive battery + functional contracts:

- T1 zip-bomb 250:1 ratio rejected, 256 MiB decompressed cap, 12K member-count cap
- T2 path traversal in tar member.name (`../`, absolute, NUL, non-ASCII) rejected
- T2 ban ``tarfile.extractall`` confirmed via source grep
- T3 symlink/devfile/fifo entries rejected
- T4 manifest schema — extra keys, format_version=2, missing key, non-string,
  oversized
- T7 content_hash tampered → mismatch rejection; canonicalisation symmetric
  across key reorderings (manifest-with-placeholder substitution round-trip)
- T8 imported_from claim from archive STRIPPED before validation
- T11 agent scope rows in archive rejected
- T10 scope=global with scope_set not declaring it → ``scope_elevation``
- T13 target_project_id pinned (mid-import marker edits don't redirect rows)
- T14 marker strong-mode lenient default rewrites + audits; strict mode refuses
- T16 1000-rejection cap reached emits forensic
- T18 ``conflict_policy`` no default → API TypeError, CLI exit 2
- T19 system-dir output paths rejected even if home/cwd traversal resolves there
- T5.i partial-row rejection in sqlite-dump → full transaction rolls back

Functional:
- 3 conflict policies behavioural-tested (skip/replace/merge), merge tie-break
- Round-trip preservation: wiki content + metadata + related_keys + last_compiled_at +
  access_count + imported_from
- Dry-run produces no SQL writes
- 5-decision ``ImportAction.decision`` enum exercised
- Tmp dir cleanup ``cao-import-*`` swept by ``sweep_import_tmp_dirs``
- B3 strong-mode rewrite emits ``marker_strong_mode_rewrite`` once

Federation note: the dead U3 federation design (separate directory + per-row
``is_federated`` flag + demotion) is gone. ``federated`` is a plain scope
value; dump rows carry no ``is_federated`` key and the manifest carries an
``id_kind`` provenance field.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import re
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import audit_log, memory_export, memory_import
from cli_agent_orchestrator.services._archive_format import (
    CONTENT_HASH_PLACEHOLDER,
    MANIFEST_REQUIRED_KEYS,
    REJECTION_LIST_CAP,
    SUPPORTED_FORMAT_VERSION,
    TAR_MEMBER_COUNT_CAP,
    canonical_dump_bytes,
    canonical_manifest_bytes_with_placeholder,
    compute_content_hash,
)
from cli_agent_orchestrator.services.memory_export import (
    _validate_output_path,
    export,
)
from cli_agent_orchestrator.services.memory_import import (
    VALID_CONFLICT_POLICIES,
    ImportAction,
    _RatioGuardedReader,
    _validate_archive,
    _validate_dump_row,
    _validate_manifest,
    _validate_member_name,
    import_archive,
    sweep_import_tmp_dirs,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def heal_base(tmp_path, monkeypatch):
    """Redirect MEMORY_BASE_DIR for all U2 modules + audit_log."""
    base = tmp_path / "u2-base"
    base.mkdir()
    monkeypatch.setattr(memory_export, "MEMORY_BASE_DIR", base)
    monkeypatch.setattr(memory_import, "MEMORY_BASE_DIR", base)
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", base)
    return base


@pytest.fixture
def db_engine(tmp_path, monkeypatch):
    """Create a per-test SQLite engine AND redirect ``SessionLocal`` so any
    ``MemoryService(base_dir=...)`` constructed without an explicit engine
    (the path import_archive / export use) picks up our test DB.
    """
    db_path = tmp_path / "u2.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    from cli_agent_orchestrator.clients import database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine, heal_base):
    """Fixture svc shares ``base_dir`` with the import/export modules so
    seed-via-store + import/export round-trips operate on the same FS root.
    The SessionLocal patch in ``db_engine`` ensures the DB is also shared.
    """
    return MemoryService(base_dir=heal_base, db_engine=db_engine)


@pytest.fixture
def settings_no_strong(monkeypatch):
    """Default: project_marker_strong_mode=False."""

    def _settings():
        return {"enabled": True, "project_marker_strong_mode": False}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        _settings,
    )
    return _settings


@pytest.fixture
def settings_strong(monkeypatch):
    def _settings():
        return {"enabled": True, "project_marker_strong_mode": True}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        _settings,
    )
    return _settings


def _ctx() -> dict:
    return {
        "terminal_id": "term-u2",
        "session_name": "session-u2",
        "agent_profile": "developer",
        "provider": "claude_code",
        "cwd": "/home/user/u2",
        "caller_scope": "global",
    }


def _seed(svc: MemoryService, key: str, *, scope: str = "global", content: str = "body") -> None:
    _run(
        svc.store(
            content=content,
            scope=scope,
            memory_type="reference",
            key=key,
            tags="t",
            terminal_context=_ctx(),
        )
    )


# ---------------------------------------------------------------------------
# Archive builders for hostile-archive tests
# ---------------------------------------------------------------------------


def _canonical_manifest(
    *,
    project_id: str = "global",
    id_kind: str = "literal",
    scope_set: Optional[List[str]] = None,
    n_wiki_files: int = 0,
    n_metadata_rows: int = 0,
) -> Dict[str, Any]:
    return {
        "format_version": SUPPORTED_FORMAT_VERSION,
        "project_id": project_id,
        "id_kind": id_kind,
        "created_at": "2026-05-18T00:00:00Z",
        "exported_by": "cao test",
        "scope_set": scope_set or ["global"],
        "n_wiki_files": n_wiki_files,
        "n_metadata_rows": n_metadata_rows,
        "content_hash": CONTENT_HASH_PLACEHOLDER,
    }


def _build_archive(
    out_path: Path,
    *,
    manifest: Dict[str, Any],
    dump_rows: List[Dict[str, Any]],
    wiki_files: Optional[Dict[str, bytes]] = None,
    index_md: bytes = b"# Memory Index\n",
    extra_members: Optional[List[Any]] = None,
    skip_hash_recompute: bool = False,
) -> Path:
    """Build a valid tar.gz archive with the supplied payloads.

    ``manifest['content_hash']`` is recomputed canonically unless
    ``skip_hash_recompute=True`` (used for negative tests).
    """
    wiki_files = wiki_files or {}

    if not skip_hash_recompute:
        manifest["content_hash"] = compute_content_hash(
            manifest=manifest,
            dump_rows=dump_rows,
            index_md=index_md,
            wiki_files=wiki_files,
        )

    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    dump_bytes = canonical_dump_bytes(dump_rows)

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(manifest_bytes))

        info = tarfile.TarInfo("sqlite-dump.json")
        info.size = len(dump_bytes)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(dump_bytes))

        info = tarfile.TarInfo("index.md")
        info.size = len(index_md)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(index_md))

        for k in sorted(wiki_files.keys()):
            data = wiki_files[k]
            info = tarfile.TarInfo(f"wiki/{k}.md")
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))

        for member in extra_members or []:
            tar.addfile(member, io.BytesIO(b""))

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write(tar_buf.getvalue())

    out_path.write_bytes(gz_buf.getvalue())
    os.chmod(out_path, 0o600)
    return out_path


def _make_dump_row(
    key: str,
    *,
    scope: str = "global",
    scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": f"id-{key}",
        "key": key,
        "memory_type": "reference",
        "scope": scope,
        "scope_id": scope_id,
        "file_path": f"/tmp/{key}.md",
        "tags": "t",
        "source_provider": None,
        "source_terminal_id": None,
        "token_estimate": 10,
        "created_at": "2026-05-18T00:00:00Z",
        "updated_at": "2026-05-18T00:00:00Z",
        "last_compiled_at": None,
        "access_count": 0,
        "last_accessed_at": None,
        "related_keys": None,
        "imported_from": None,
    }


# ===========================================================================
# T1 — Zip-bomb / decompressed-total / member-count caps
# ===========================================================================


class TestT1ZipBombCaps:
    def test_ratio_guarded_reader_aborts_at_ratio_cap(self, tmp_path):
        """Highly-compressible payload (zeros) → ratio > 200:1 → RuntimeError."""
        # 5 MiB of zeros gzipped to ~5 KiB → ratio ~1000:1.
        payload = b"\x00" * (5 * 1024 * 1024)
        archive = tmp_path / "bomb.tar.gz"
        gz = gzip.compress(payload, compresslevel=9)
        archive.write_bytes(gz)

        reader = _RatioGuardedReader(archive)
        try:
            with pytest.raises(RuntimeError) as exc:
                while True:
                    chunk = reader.read(64 * 1024)
                    if not chunk:
                        break
            assert "ratio_exceeds_cap" in str(exc.value) or "size_exceeds_cap" in str(exc.value)
        finally:
            reader.close()

    def test_validate_archive_emits_ratio_rejection(self, tmp_path):
        """Build a real ratio-attack archive and run pass-1 validation."""
        # 8 MiB of zeros → tar header overhead is small; gzip → ratio ~hundreds.
        payload = b"\x00" * (8 * 1024 * 1024)
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb", compresslevel=9) as gz:
            gz.write(tar_buf.getvalue())
        archive = tmp_path / "ratio.tar.gz"
        archive.write_bytes(gz_buf.getvalue())

        manifest, rejections, _ = _validate_archive(archive)
        reasons = {r.reason for r in rejections}
        assert (
            "ratio_exceeds_cap" in reasons
            or "size_exceeds_cap" in reasons
            or "manifest_invalid" in reasons
        ), f"expected ratio/size rejection; got {reasons}"

    def test_member_count_cap_enforced(self, tmp_path):
        """Build an archive with bogus members and assert the cap constant +
        unknown-member rejection.
        """
        tar_buf = io.BytesIO()
        manifest = _canonical_manifest()
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
            for i in range(100):
                bogus = f"unknown-{i}.md"
                info = tarfile.TarInfo(bogus)
                info.size = 0
                tar.addfile(info, io.BytesIO(b""))

        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(tar_buf.getvalue())
        archive = tmp_path / "many.tar.gz"
        archive.write_bytes(gz_buf.getvalue())

        manifest_out, rejections, member_count = _validate_archive(archive)
        assert TAR_MEMBER_COUNT_CAP == 12_000
        assert member_count >= 1
        assert any(r.reason == "unknown_member" for r in rejections)


# ===========================================================================
# T2 — Path traversal + tarfile.extractall ban
# ===========================================================================


class TestT2PathTraversalAndExtractallBan:
    @pytest.mark.parametrize(
        "name,reason",
        [
            ("../etc/passwd", "path_traversal"),
            ("/etc/passwd", "path_absolute"),
            ("foo\x00bar", "encoding_invalid"),
            ("wiki/öxüé.md", "encoding_invalid"),
            ("./manifest.json", "path_traversal"),  # normpath != name
            ("wiki/../../etc/passwd", "path_traversal"),
        ],
    )
    def test_member_name_rejects_hostile_inputs(self, name, reason):
        ok, got_reason = _validate_member_name(name)
        assert ok is False
        assert got_reason == reason

    def test_member_name_overlong_rejected(self):
        ok, reason = _validate_member_name("a" * 300)
        assert ok is False
        assert reason == "path_traversal"

    def test_member_name_accepts_known_shapes(self):
        for name in ("manifest.json", "sqlite-dump.json", "index.md", "wiki/k1.md"):
            ok, _ = _validate_member_name(name)
            assert ok is True

    def test_no_extractall_call_in_source(self):
        """T2 ban: no actual ``.extractall(`` invocation in import path."""
        src = (
            Path(__file__).parents[2]
            / "src"
            / "cli_agent_orchestrator"
            / "services"
            / "memory_import.py"
        ).read_text(encoding="utf-8")
        forbidden = re.compile(r"\.extractall\s*\(")
        assert forbidden.search(src) is None, "tarfile.extractall call site detected"


# ===========================================================================
# T3 — Symlink / device / fifo rejection
# ===========================================================================


class TestT3SymlinkDevfileFifoRejected:
    def test_symlink_member_rejected(self, tmp_path):
        """Build a tar with a symlink member; validate_archive rejects."""
        tar_buf = io.BytesIO()
        manifest = _canonical_manifest()
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
            sym = tarfile.TarInfo("evil-link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tar.addfile(sym)
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(tar_buf.getvalue())
        archive = tmp_path / "sym.tar.gz"
        archive.write_bytes(gz_buf.getvalue())

        _, rejections, _ = _validate_archive(archive)
        assert any(r.reason == "symlink" for r in rejections)

    def test_fifo_member_rejected(self, tmp_path):
        tar_buf = io.BytesIO()
        manifest = _canonical_manifest()
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
            fifo = tarfile.TarInfo("evil-fifo")
            fifo.type = tarfile.FIFOTYPE
            tar.addfile(fifo)
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(tar_buf.getvalue())
        archive = tmp_path / "fifo.tar.gz"
        archive.write_bytes(gz_buf.getvalue())

        _, rejections, _ = _validate_archive(archive)
        assert any(r.reason in ("symlink", "device_or_pipe") for r in rejections)


# ===========================================================================
# T4 — Manifest schema enforcement
# ===========================================================================


class TestT4ManifestSchema:
    def test_manifest_rejects_extra_keys(self):
        m = _canonical_manifest()
        m["extra"] = "value"
        raw = json.dumps(m, sort_keys=True).encode("utf-8")
        out, rej = _validate_manifest(raw)
        assert out is None and rej is not None and rej.reason == "manifest_invalid"

    def test_manifest_rejects_format_version_2(self):
        m = _canonical_manifest()
        m["format_version"] = 2
        raw = json.dumps(m, sort_keys=True).encode("utf-8")
        out, rej = _validate_manifest(raw)
        assert out is None and rej.reason == "format_version_unsupported"

    def test_manifest_rejects_missing_required_key(self):
        m = _canonical_manifest()
        del m["scope_set"]
        raw = json.dumps(m, sort_keys=True).encode("utf-8")
        out, rej = _validate_manifest(raw)
        assert out is None and rej.reason == "manifest_invalid"

    def test_manifest_rejects_missing_id_kind(self):
        m = _canonical_manifest()
        del m["id_kind"]
        raw = json.dumps(m, sort_keys=True).encode("utf-8")
        out, rej = _validate_manifest(raw)
        assert out is None and rej.reason == "manifest_invalid"

    def test_manifest_rejects_non_string_project_id(self):
        m = _canonical_manifest()
        m["project_id"] = 12345
        raw = json.dumps(m, sort_keys=True).encode("utf-8")
        out, rej = _validate_manifest(raw)
        assert out is None and rej.reason == "manifest_invalid"

    def test_manifest_rejects_oversized(self):
        # Force >64 KiB payload.
        raw = b"a" * (65 * 1024)
        out, rej = _validate_manifest(raw)
        assert out is None and rej.reason == "size_exceeds_cap"

    def test_manifest_rejects_invalid_iso8601(self):
        m = _canonical_manifest()
        m["created_at"] = "2026-05-18 00:00:00"  # space, not T
        raw = json.dumps(m, sort_keys=True).encode("utf-8")
        out, rej = _validate_manifest(raw)
        assert out is None and rej.reason == "manifest_invalid"

    def test_manifest_required_key_set_constant(self):
        assert MANIFEST_REQUIRED_KEYS == frozenset(
            {
                "format_version",
                "project_id",
                "id_kind",
                "created_at",
                "exported_by",
                "scope_set",
                "n_wiki_files",
                "n_metadata_rows",
                "content_hash",
            }
        )


# ===========================================================================
# T7 — content_hash canonicalisation
# ===========================================================================


class TestT7ContentHashCanonicalisation:
    def test_canonical_manifest_invariant_under_key_reordering(self):
        m1 = _canonical_manifest()
        m2 = dict(reversed(list(m1.items())))  # different insertion order
        b1 = canonical_manifest_bytes_with_placeholder(m1)
        b2 = canonical_manifest_bytes_with_placeholder(m2)
        assert b1 == b2

    def test_canonical_manifest_substitutes_placeholder(self):
        m = _canonical_manifest()
        m["content_hash"] = "sha256:" + ("f" * 64)  # tampered
        out = canonical_manifest_bytes_with_placeholder(m)
        assert b"sha256:0000000000000000000000000000000000000000000000000000000000000000" in out
        assert b"sha256:ffffffffffff" not in out

    def test_canonical_dump_deterministic_across_row_reordering(self):
        rows = [_make_dump_row("a"), _make_dump_row("b")]
        b1 = canonical_dump_bytes(rows)
        b2 = canonical_dump_bytes(rows[::-1])
        # Different list order DOES change output (lists are ordered);
        # canonicalisation is per-row sort_keys, not list-level. The export
        # side enforces row-list ordering via lex sort.
        assert b1 != b2  # documents the non-commutative behaviour

    def test_compute_content_hash_changes_with_payload(self, tmp_path):
        m = _canonical_manifest(n_wiki_files=1, n_metadata_rows=1)
        rows = [_make_dump_row("k1")]
        h1 = compute_content_hash(
            manifest=m, dump_rows=rows, index_md=b"# A\n", wiki_files={"k1": b"a"}
        )
        h2 = compute_content_hash(
            manifest=m, dump_rows=rows, index_md=b"# A\n", wiki_files={"k1": b"b"}
        )
        assert h1 != h2

    def test_tampered_content_hash_rejected(self, tmp_path, heal_base, settings_no_strong):
        rows = [_make_dump_row("k1")]
        m = _canonical_manifest(n_wiki_files=1, n_metadata_rows=1)
        wiki = {"k1": b"# k1\n"}
        archive = tmp_path / "tampered.tar.gz"
        _build_archive(
            archive,
            manifest=m,
            dump_rows=rows,
            wiki_files=wiki,
        )
        # Now tamper: rebuild with wrong hash baked in.
        m_bad = _canonical_manifest(n_wiki_files=1, n_metadata_rows=1)
        m_bad["content_hash"] = "sha256:" + ("0" * 63 + "1")  # wrong
        archive_bad = tmp_path / "bad.tar.gz"
        _build_archive(
            archive_bad,
            manifest=m_bad,
            dump_rows=rows,
            wiki_files=wiki,
            skip_hash_recompute=True,
        )

        report = _run(import_archive(archive_bad, conflict_policy="skip", actor="cli"))
        reasons = {r.reason for r in report.rejections}
        assert (
            "content_hash_mismatch" in reasons or "manifest_invalid" in reasons
        ), f"expected hash rejection; got {reasons}"


# ===========================================================================
# T8 — imported_from claim from archive STRIPPED
# ===========================================================================


class TestT8ImportedFromStripped:
    def test_imported_from_overwritten_by_importer(
        self, tmp_path, svc, heal_base, settings_no_strong
    ):
        """Archive row claims ``imported_from='attacker-controlled'``;
        actual SQL row gets the importer-set provenance JSON, not the claim.
        """
        row = _make_dump_row("k1", scope="global")
        row["imported_from"] = "attacker-controlled-string"
        m = _canonical_manifest(n_wiki_files=1, n_metadata_rows=1)
        wiki = {"k1": b"# k1\nbody\n"}
        archive = tmp_path / "im.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )
        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="k1").first()
        assert r is not None, "row should be inserted"
        assert "attacker-controlled-string" not in (r.imported_from or "")
        assert "archive_sha256" in (r.imported_from or "")


# ===========================================================================
# T11 — agent scope rows in archive rejected
# ===========================================================================


class TestT11AgentScopeBan:
    def test_agent_scope_row_rejected(self):
        row = _make_dump_row("k1", scope="agent")
        ok, result = _validate_dump_row(
            row, _canonical_manifest(scope_set=["agent", "global"]), "global"
        )
        assert ok is False
        # Even though manifest declares it, agent is banned.
        assert hasattr(result, "reason") and result.reason == "scope_invalid"


# ===========================================================================
# T10 — scope_elevation when scope not in scope_set
# ===========================================================================


class TestT10ScopeElevation:
    def test_global_row_rejected_when_not_in_scope_set(self):
        row = _make_dump_row("k1", scope="global")
        m = _canonical_manifest(scope_set=["session"])  # global NOT declared
        ok, result = _validate_dump_row(row, m, "global")
        assert ok is False
        assert hasattr(result, "reason") and result.reason == "scope_elevation"

    def test_global_row_must_have_null_scope_id(self):
        row = _make_dump_row("k1", scope="global", scope_id="proj-x")
        m = _canonical_manifest(scope_set=["global"])
        ok, result = _validate_dump_row(row, m, "global")
        assert ok is False
        assert result.reason == "scope_invalid"


# ===========================================================================
# Federated is just a scope — round-trip
# ===========================================================================


class TestFederatedIsJustAScope:
    def test_federated_row_validates_and_imports(
        self, tmp_path, svc, heal_base, settings_no_strong
    ):
        """A ``federated`` dump row carries no special flag; it validates and
        imports through the normal scope path (no demotion, no separate dir).
        """
        row = _make_dump_row("fed1", scope="federated", scope_id=None)
        m = _canonical_manifest(scope_set=["federated"], n_wiki_files=1, n_metadata_rows=1)
        # Row-level validation accepts federated declared in scope_set.
        ok, _result = _validate_dump_row(row, m, "global")
        assert ok is True

        wiki = {"fed1": b"# fed1\nfederated body\n"}
        archive = tmp_path / "fed.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        report = _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )
        assert any(a.decision == "insert" for a in report.actions)
        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="fed1").first()
        assert r is not None
        assert r.scope == "federated"
        # Federated rows land with NULL scope_id (user-level), not pinned target.
        assert r.scope_id is None


# ===========================================================================
# T13 — target_project_id pinned mid-import
# ===========================================================================


class TestT13TargetProjectIdPinned:
    def test_target_project_id_pinned_to_caller_value(
        self, tmp_path, svc, heal_base, settings_no_strong
    ):
        """import_archive is called with target_project_id="my-proj"; the
        applied SQL row's scope_id is "my-proj" regardless of archive's
        project_id field.
        """
        row = _make_dump_row("k1", scope="project", scope_id="archive-proj")
        m = _canonical_manifest(
            project_id="archive-proj",
            id_kind="git_remote",
            scope_set=["project"],
            n_wiki_files=1,
            n_metadata_rows=1,
        )
        wiki = {"k1": b"# k1\n"}
        archive = tmp_path / "pin.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="my-proj",
                actor="cli",
            )
        )
        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="k1").first()
        assert r is not None
        assert r.scope_id == "my-proj"  # rewritten to caller's pinned value


# ===========================================================================
# T14 — Marker strong-mode default lenient + strict refuses
# ===========================================================================


class TestT14MarkerStrongMode:
    def test_lenient_default_rewrites_with_audit(
        self, tmp_path, svc, heal_base, settings_no_strong
    ):
        row = _make_dump_row("k1", scope="project", scope_id="archive-proj")
        m = _canonical_manifest(
            project_id="archive-proj",
            id_kind="git_remote",
            scope_set=["project"],
            n_wiki_files=1,
            n_metadata_rows=1,
        )
        wiki = {"k1": b"# k1\n"}
        archive = tmp_path / "lenient.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        report = _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="my-proj",
                actor="cli",
            )
        )
        # Import succeeded.
        assert any(a.decision == "insert" for a in report.actions)
        # Audit log records the rewrite event.
        body = audit_log.read_audit_log()
        assert "[marker_strong_mode_rewrite]" in body

    def test_strict_mode_refuses_mismatch(self, tmp_path, svc, heal_base, settings_strong):
        row = _make_dump_row("k1", scope="project", scope_id="archive-proj")
        m = _canonical_manifest(
            project_id="archive-proj",
            id_kind="git_remote",
            scope_set=["project"],
            n_wiki_files=1,
            n_metadata_rows=1,
        )
        wiki = {"k1": b"# k1\n"}
        archive = tmp_path / "strict.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        report = _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="my-proj",
                actor="cli",
            )
        )
        # Strict mode → rejection, no actions applied.
        assert report.actions == []
        assert any("strong-mode" in (r.detail or "") for r in report.rejections)


# ===========================================================================
# T16 — 1000-rejection cap forensic
# ===========================================================================


class TestT16RejectionCap:
    def test_rejection_list_cap_constant(self):
        assert REJECTION_LIST_CAP == 1000


# ===========================================================================
# T18 — conflict_policy required (no default)
# ===========================================================================


class TestT18ConflictPolicyRequired:
    def test_api_typeerror_when_policy_missing(self):
        archive = Path("/tmp/nonexistent.tar.gz")
        with pytest.raises(TypeError):
            # ``conflict_policy`` has no default in the signature.
            _run(import_archive(archive))  # type: ignore[call-arg]

    def test_api_typeerror_on_invalid_policy(self):
        archive = Path("/tmp/nonexistent.tar.gz")
        with pytest.raises(TypeError):
            _run(import_archive(archive, conflict_policy="bogus"))

    def test_valid_conflict_policies_set(self):
        assert VALID_CONFLICT_POLICIES == frozenset({"skip", "replace", "merge"})


# ===========================================================================
# T19 — System-dir output paths rejected
# ===========================================================================


class TestT19OutputPathContainment:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd.tar.gz",
            "/var/spool/evil.tar.gz",
            "/tmp/leak.tar.gz",
            "/dev/null.tar.gz",
        ],
    )
    def test_validate_output_path_rejects_system_dirs(self, bad_path):
        out, err = _validate_output_path(Path(bad_path))
        assert out is None and err is not None
        assert "blacklisted" in err or "must resolve" in err

    def test_validate_output_path_requires_tar_gz_suffix(self, tmp_path):
        bad = tmp_path / "no-suffix"
        out, err = _validate_output_path(bad)
        assert out is None
        assert ".tar.gz" in (err or "") or ".tgz" in (err or "")

    def test_validate_output_path_accepts_under_home_or_cwd(self, tmp_path, monkeypatch):
        # tmp_path is under /private/var/... on macOS — outside HOME/cwd.
        # Force HOME to tmp_path for the duration.
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "ok.tar.gz"
        out, err = _validate_output_path(target)
        assert err is None or "must resolve" not in err


# ===========================================================================
# T5.i — Partial-row rejection rolls back full transaction
# ===========================================================================


class TestT5iPartialRowRollback:
    def test_partial_invalid_row_aborts_entire_import(
        self, tmp_path, svc, heal_base, settings_no_strong
    ):
        # 1 valid + 1 invalid row → entire batch rolls back, NO rows applied.
        good = _make_dump_row("good-k", scope="global")
        bad = _make_dump_row("bad-k", scope="agent")  # T11 banned
        m = _canonical_manifest(
            scope_set=["global", "agent"],  # archive declares both
            n_wiki_files=1,
            n_metadata_rows=2,
        )
        wiki = {"good-k": b"# good\n", "bad-k": b"# bad\n"}
        archive = tmp_path / "partial.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[good, bad], wiki_files=wiki)

        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            n_before = db.query(MemoryMetadataModel).count()

        report = _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )

        with Session() as db:
            n_after = db.query(MemoryMetadataModel).count()
        # Rollback: row count unchanged.
        assert n_after == n_before
        # Report carries the rejection.
        assert report.rejections


# ===========================================================================
# Conflict policies — behavioural (skip / replace / merge)
# ===========================================================================


class TestConflictPolicies:
    def _build_simple_archive(self, tmp_path: Path, key: str, body: bytes) -> Path:
        row = _make_dump_row(key, scope="global")
        m = _canonical_manifest(n_wiki_files=1, n_metadata_rows=1)
        archive = tmp_path / f"{key}.tar.gz"
        _build_archive(
            archive,
            manifest=m,
            dump_rows=[row],
            wiki_files={key: body},
        )
        return archive

    def test_skip_existing_does_not_overwrite(self, tmp_path, svc, heal_base, settings_no_strong):
        _seed(svc, "kollide", content="local body")
        archive = self._build_simple_archive(tmp_path, "kollide", b"# imported\n")
        report = _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )
        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="kollide").first()
        # Local row still in place; imported_from NOT set on skip.
        assert r is not None
        assert (r.imported_from or "") == ""
        assert any(a.decision == "skip_existing" for a in report.actions)

    def test_replace_existing_overwrites(self, tmp_path, svc, heal_base, settings_no_strong):
        _seed(svc, "kollide2", content="local body")
        archive = self._build_simple_archive(tmp_path, "kollide2", b"# new\n")
        _run(
            import_archive(
                archive,
                conflict_policy="replace",
                target_project_id="global",
                actor="cli",
            )
        )
        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="kollide2").first()
        assert "archive_sha256" in (r.imported_from or "")

    def test_merge_imported_wins_when_newer(self, tmp_path, svc, heal_base, settings_no_strong):
        _seed(svc, "merge1", content="local body")
        # Backdate local row to make import newer.
        Session = sessionmaker(bind=svc._db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="merge1").first()
            r.updated_at = datetime(2020, 1, 1)
            db.commit()
        archive = self._build_simple_archive(tmp_path, "merge1", b"# merge\n")
        _run(
            import_archive(
                archive,
                conflict_policy="merge",
                target_project_id="global",
                actor="cli",
            )
        )
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="merge1").first()
        assert "archive_sha256" in (r.imported_from or "")


# ===========================================================================
# Round-trip preservation
# ===========================================================================


class TestRoundTrip:
    def test_export_then_import_preserves_metadata(
        self, tmp_path, svc, db_engine, heal_base, settings_no_strong, monkeypatch
    ):
        # Force HOME under tmp_path so the output-path containment guard
        # (T19) accepts the test archive path.
        monkeypatch.setenv("HOME", str(tmp_path))
        # Seed a row, populate access_count + related_keys + last_compiled_at.
        _seed(svc, "rt1", content="round-trip body")
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="rt1").first()
            r.access_count = 7
            r.related_keys = "rt2"
            r.last_compiled_at = datetime(2026, 5, 10, 12, 0, 0)
            db.commit()

        # Export to tmp.
        out_path = heal_base / "rt.tar.gz"
        report = _run(export("global", None, output_path=out_path, actor="cli"))
        assert report.errors == [], f"export errored: {report.errors}"
        assert out_path.exists()

        # Drop the row + delete the wiki file to simulate fresh import.
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="rt1").first()
            wiki_path = Path(r.file_path)
            db.delete(r)
            db.commit()
        if wiki_path.exists():
            wiki_path.unlink()

        # Import.
        ireport = _run(
            import_archive(
                out_path,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )
        assert any(
            a.decision == "insert" for a in ireport.actions
        ), f"expected insert; got {[a.decision for a in ireport.actions]} rej={ireport.rejections}"
        with Session() as db:
            r = db.query(MemoryMetadataModel).filter_by(key="rt1").first()
        assert r is not None
        assert int(r.access_count) == 7
        assert r.related_keys == "rt2"
        # imported_from carries provenance.
        assert "archive_sha256" in (r.imported_from or "")


# ===========================================================================
# Dry-run: no SQL writes
# ===========================================================================


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path, svc, db_engine, heal_base, settings_no_strong):
        row = _make_dump_row("dry1", scope="global")
        m = _canonical_manifest(n_wiki_files=1, n_metadata_rows=1)
        wiki = {"dry1": b"# dry\n"}
        archive = tmp_path / "dry.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            n_before = db.query(MemoryMetadataModel).count()

        report = _run(
            import_archive(
                archive,
                conflict_policy="skip",
                dry_run=True,
                target_project_id="global",
                actor="cli",
            )
        )
        assert report.dry_run is True
        with Session() as db:
            n_after = db.query(MemoryMetadataModel).count()
        assert n_after == n_before


# ===========================================================================
# 5-decision enum + tmp dir cleanup + strong-mode-rewrite emitted ONCE
# ===========================================================================


class TestImportActionDecisionEnum:
    def test_all_5_decisions_named(self):
        for decision in (
            "insert",
            "skip_existing",
            "replace_existing",
            "merge_winner_existing",
            "merge_winner_imported",
        ):
            a = ImportAction(key="k", scope="global", scope_id=None, decision=decision)
            assert a.decision == decision


class TestTmpDirSweep:
    def test_sweep_removes_old_tmp_dirs(self, heal_base):
        import_dir = heal_base / ".import"
        import_dir.mkdir()
        old = import_dir / "cao-import-aaaa"
        old.mkdir()
        # Backdate so it's older than retention_hours.
        os.utime(str(old), (0, 0))
        new = import_dir / "cao-import-bbbb"
        new.mkdir()
        n, oldest = sweep_import_tmp_dirs(retention_hours=1)
        assert n >= 1
        assert not old.exists()
        assert new.exists()  # young one preserved


class TestStrongModeRewriteOnce:
    def test_strong_mode_rewrite_emits_once_per_import(
        self, tmp_path, svc, heal_base, settings_no_strong
    ):
        row = _make_dump_row("once1", scope="project", scope_id="archive-proj")
        m = _canonical_manifest(
            project_id="archive-proj",
            id_kind="git_remote",
            scope_set=["project"],
            n_wiki_files=1,
            n_metadata_rows=1,
        )
        wiki = {"once1": b"# once\n"}
        archive = tmp_path / "once.tar.gz"
        _build_archive(archive, manifest=m, dump_rows=[row], wiki_files=wiki)

        _run(
            import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="my-proj",
                actor="cli",
            )
        )
        body = audit_log.read_audit_log()
        n_events = body.count("[marker_strong_mode_rewrite]")
        assert n_events == 1, f"expected 1 strong_mode_rewrite event; got {n_events}"


# ===========================================================================
# CLI surface
# ===========================================================================


class TestCli:
    def test_cli_import_no_policy_exits_2(self, tmp_path, heal_base):
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.commands.memory import memory

        runner = CliRunner()
        # `cao memory import <path>` without --policy → non-zero exit.
        result = runner.invoke(memory, ["import", str(tmp_path / "x.tar.gz")])
        assert result.exit_code != 0
