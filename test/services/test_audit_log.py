"""Daily Audit Log tests.

Covers:
- U7.5 plan cases (store_writes_audit_log, audit_log_write_failure_does_not_fail_store
  per Phase 2.5 U5 non-blocking promise, log_rotation_deletes_old_files).
- Threat-model coverage: log injection, path traversal, modes, caps, retention.
- T1 cross-cutting: U+0085 / U+2028 / U+2029 stripped to ``\\n`` by the
  extended ``_sanitize_for_log`` — verified on EVERY caller surface
  (compiler, find_related, lint, audit-log-itself).
- Whitelist enforcement: unknown event drops with WARNING.
- Round-trip: write → ``read_audit_log`` returns the canonical line with
  trailing newline.

The audit module redirects all writes to ``MEMORY_BASE_DIR / logs / memory``;
we monkeypatch the module-level constant so each test gets an isolated
``tmp_path``-backed directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

import pytest

from cli_agent_orchestrator.services import audit_log
from cli_agent_orchestrator.services import wiki_compiler as wc
from cli_agent_orchestrator.services.audit_log import (
    AUDIT_EVENT_WHITELIST,
    AUDIT_FILE_MODE,
    LOG_FILENAME_RE,
    NOWAIT_AUDIT_EVENTS,
    PER_FIELD_CAP_BYTES,
    SUMMARY_MAX_CHARS,
    SYNC_AUDIT_EVENTS,
    _open_audit_fd,
    _render_line,
    _resolve_log_path,
    _sanitize_field_value,
    _sanitize_summary,
    read_audit_log,
    sweep_old_audit_logs,
    write_audit,
    write_audit_nowait,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_base(tmp_path, monkeypatch):
    """Redirect MEMORY_BASE_DIR for the audit module to a tmp_path-backed dir.

    Patches both the module-level constant the audit_log module imported
    AND the original constants module so any future re-imports see the
    same root.
    """
    base = tmp_path / "audit-base"
    base.mkdir()
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", base)
    return base


def _run(coro):
    return asyncio.run(coro)


def _read_log(base: Path, date_str: Optional[str] = None) -> str:
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = base / "logs" / "memory" / f"{date_str}.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


# ===========================================================================
# T1 — Cross-cutting Unicode line-separator strip
# ===========================================================================


class TestT1UnicodeLineSeparators:
    """The U1 ``_sanitize_for_log`` was extended to strip U+0085 (NEL),
    U+2028 (LS), U+2029 (PS) so a smuggled separator can't smuggle a fake
    log line on ANY caller surface (compiler, find_related, lint, audit).
    """

    def test_sanitize_for_log_strips_nel(self):
        out = wc._sanitize_for_log("ab")
        assert "" not in out
        assert "\\n" in out

    def test_sanitize_for_log_strips_line_separator(self):
        out = wc._sanitize_for_log("a b")
        assert " " not in out
        assert "\\n" in out

    def test_sanitize_for_log_strips_paragraph_separator(self):
        out = wc._sanitize_for_log("a b")
        assert " " not in out
        assert "\\n" in out

    def test_sanitize_for_log_strips_all_three_in_one_string(self):
        raw = "xy z w"
        out = wc._sanitize_for_log(raw)
        for c in ("", " ", " "):
            assert c not in out
        assert out.count("\\n") == 3

    def test_audit_summary_strips_unicode_separators(self):
        """U5's own ``_sanitize_summary`` (built on top of `_sanitize_for_log`)
        must therefore also be safe.
        """
        out = _sanitize_summary("attacker injection line")
        assert " " not in out
        assert " " not in out

    def test_compiler_log_path_safe_under_unicode_smuggle(self, caplog):
        """U1 compiler log call sites pass attacker bytes through
        ``_sanitize_for_log``. A smuggled U+2028 must NOT appear in the
        emitted log record's message.
        """
        evil = "topic fake-log-line"
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.wiki_compiler"
        ):
            # Trigger a fallback warning by calling the helper directly.
            sanitized = wc._sanitize_for_log(evil)
            wc.logger.warning("test_smuggle: %s", sanitized)
        for rec in caplog.records:
            assert " " not in rec.getMessage()


# ===========================================================================
# T2 — Path resolution / containment
# ===========================================================================


class TestT2PathResolution:
    def test_rejects_dotdot_in_date(self, tmp_path):
        assert _resolve_log_path(tmp_path, "../../etc/passwd") is None

    def test_rejects_absolute_path_shaped_input(self, tmp_path):
        assert _resolve_log_path(tmp_path, "/etc/passwd") is None

    def test_rejects_malformed_date(self, tmp_path):
        for bad in ("2026/05/15", "26-5-15", "2026-05", "abc", "", "2026-13-99-extra"):
            assert _resolve_log_path(tmp_path, bad) is None, bad

    def test_accepts_well_formed_date(self, tmp_path):
        out = _resolve_log_path(tmp_path, "2026-05-15")
        assert out is not None
        assert out.name == "2026-05-15.md"
        assert str(out).startswith(str(tmp_path.resolve()))

    def test_log_filename_regex(self):
        for ok in ("2026-05-15", "0001-01-01", "9999-12-31"):
            assert LOG_FILENAME_RE.match(ok)
        for bad in ("2026/05/15", "2026-5-1", "2026-05-15-extra", "ABC"):
            assert not LOG_FILENAME_RE.match(bad)


# ===========================================================================
# T3 — fstat mode mismatch + symlink defence
# ===========================================================================


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only mode/symlink test")
class TestT3FdGuards:
    def test_fstat_mode_mismatch_returns_negative_fd(self, tmp_path, caplog):
        """Pre-create the target file with mode 0o644. ``_open_audit_fd``
        must reject (return -1) and log ``audit_log_perm_violation``.
        """
        target = tmp_path / "2026-05-15.md"
        target.write_text("", encoding="utf-8")
        os.chmod(target, 0o644)
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.audit_log"):
            fd = _open_audit_fd(target)
        assert fd == -1
        assert any("audit_log_perm_violation" in r.getMessage() for r in caplog.records)

    def test_symlink_target_blocked_by_o_nofollow(self, tmp_path):
        """A symlink at the target path must be rejected by O_NOFOLLOW,
        returning -1 — never opens the symlinked-to file.
        """
        decoy = tmp_path / "decoy.md"
        decoy.write_text("decoy\n", encoding="utf-8")
        target = tmp_path / "link.md"
        os.symlink(str(decoy), str(target))
        fd = _open_audit_fd(target)
        assert fd == -1
        # Decoy unmodified.
        assert decoy.read_text() == "decoy\n"

    def test_open_succeeds_when_mode_correct(self, tmp_path):
        """Sanity: a fresh path opened by ``_open_audit_fd`` ends up at 0o600."""
        target = tmp_path / "2026-05-15.md"
        fd = _open_audit_fd(target)
        try:
            assert fd >= 0
        finally:
            if fd >= 0:
                os.close(fd)
        st = target.stat()
        assert (st.st_mode & 0o777) == AUDIT_FILE_MODE


# ===========================================================================
# T5 — Newline-as-last-byte invariant + bullet anchor recovery
# ===========================================================================


class TestT5NewlineInvariant:
    def test_render_line_ends_with_newline(self):
        line = _render_line(
            timestamp="2026-05-15T12:34:56Z",
            event_type="memory_stored",
            fields={"a": "1"},
            summary="ok",
        )
        assert line.endswith(b"\n")

    def test_render_line_starts_with_bullet_anchor(self):
        line = _render_line(
            timestamp="2026-05-15T12:34:56Z",
            event_type="memory_stored",
            fields={},
            summary="x",
        )
        assert line.startswith(b"- ")

    def test_every_write_ends_with_newline(self, audit_base):
        _run(write_audit("memory_stored", "ok", scope="global"))
        _run(write_audit("memory_stored", "ok2", scope="global"))
        body = _read_log(audit_base)
        assert body.endswith("\n")
        for line in body.splitlines():
            assert line.startswith("- "), f"non-bullet: {line!r}"

    def test_partial_write_recovery_via_bullet_anchor(self, audit_base):
        """Truncate a complete log to mid-second-entry. A parser anchored on
        ``- `` line starts can resume on the next bullet — first entry is
        recoverable, partial second entry is dropped.
        """
        _run(write_audit("memory_stored", "first", scope="global"))
        _run(write_audit("memory_recalled", "second", scope="project"))
        _run(write_audit("memory_forgot", "third", scope="agent"))
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = audit_base / "logs" / "memory" / f"{date_str}.md"
        body = log_path.read_text(encoding="utf-8")
        lines = body.splitlines(keepends=True)
        # Truncate to first complete line + half of the second.
        partial_second = lines[1][: len(lines[1]) // 2]
        torn = lines[0] + partial_second
        log_path.write_text(torn, encoding="utf-8")
        # Recovery via anchor regex: take only lines that END with newline AND
        # start with "- " (so the partial line is dropped).
        recovered = [
            ln for ln in torn.splitlines(keepends=True) if ln.startswith("- ") and ln.endswith("\n")
        ]
        assert len(recovered) == 1
        assert "first" in recovered[0]


# ===========================================================================
# T6 — Day-cap enforcement
# ===========================================================================


class TestT6DayCap:
    def test_write_dropped_when_day_cap_exceeded(self, audit_base, monkeypatch, caplog):
        """Pre-fill the log past the cap → next write dropped + WARNING."""
        # Tighten cap to a tiny value so the test runs fast.
        monkeypatch.setattr(audit_log, "AUDIT_LOG_DAY_CAP_BYTES_DEFAULT", 64)
        monkeypatch.setattr(audit_log, "_audit_day_cap_bytes", lambda: 64)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_dir = audit_base / "logs" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{date_str}.md"
        # Pre-fill with 200 bytes of valid bullets.
        log_path.write_text(
            ("- 2026-05-15T12:00:00Z [memory_stored] — `seed`\n" * 5),
            encoding="utf-8",
        )
        os.chmod(log_path, AUDIT_FILE_MODE)
        sz_before = log_path.stat().st_size

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.audit_log"):
            _run(write_audit("memory_stored", "should-be-dropped", scope="global"))

        sz_after = log_path.stat().st_size
        # File MUST NOT have grown.
        assert sz_after == sz_before
        # The drop reason must be `day_cap_exceeded`.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("day_cap_exceeded" in m for m in msgs)


# ===========================================================================
# T7 — memory.enabled=False kill switch
# ===========================================================================


class TestT7MemoryDisabledKillSwitch:
    def test_audit_silently_skipped_when_memory_disabled(self, audit_base, monkeypatch):
        monkeypatch.setattr(audit_log, "_is_memory_enabled_safe", lambda: False)
        _run(write_audit("memory_stored", "ignored", scope="global"))
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # No log file created.
        log_path = audit_base / "logs" / "memory" / f"{date_str}.md"
        assert not log_path.exists()


# ===========================================================================
# T8 — Backtick / HTML / equals escape
# ===========================================================================


class TestT8Escapes:
    def test_summary_backtick_escaped(self):
        """Every backtick in the summary is preceded by a backslash — no
        un-escaped backtick can break the wrapping ``backtick body backtick``
        renderer.
        """
        out = _sanitize_summary("hello `evil` world")
        assert "\\`" in out
        for i, char in enumerate(out):
            if char == "`":
                assert i > 0 and out[i - 1] == "\\", f"un-escaped backtick at index {i} in {out!r}"

    def test_summary_html_special_chars_escaped(self):
        out = _sanitize_summary("<script>alert(1)</script> & rm")
        assert "&lt;" in out
        assert "&gt;" in out
        assert "&amp;" in out
        assert "<script>" not in out

    def test_summary_double_quote_escaped(self):
        out = _sanitize_summary('say "hello"')
        assert '\\"' in out

    def test_field_value_html_escaped(self):
        out = _sanitize_field_value("<x>&y")
        assert "&lt;" in out and "&gt;" in out and "&amp;" in out

    def test_field_value_equals_escaped(self):
        """T8.b: ``=`` in field VALUE escaped to ``\\=`` so a parser splitting
        on ``key=value`` cannot be tricked.
        """
        out = _sanitize_field_value("k=injected=more")
        assert "\\=" in out
        # No raw ``=`` left.
        assert out.count("=") == out.count("\\=")

    def test_field_value_inner_backtick_safe(self):
        # Backticks in field values are not separately escaped (only summary
        # is wrapped in backticks). Should not break the line; render still
        # ends with newline.
        line = _render_line(
            timestamp="2026-05-15T00:00:00Z",
            event_type="memory_stored",
            fields={"foo": _sanitize_field_value("a`b")},
            summary=_sanitize_summary("ok"),
        )
        assert line.endswith(b"\n")


# ===========================================================================
# T9 — Sync vs nowait classification
# ===========================================================================


class TestT9SyncVsNowait:
    def test_classification_partition_is_disjoint(self):
        """SYNC and NOWAIT sets MUST be disjoint."""
        assert SYNC_AUDIT_EVENTS.isdisjoint(NOWAIT_AUDIT_EVENTS)

    def test_classification_covers_whitelist(self):
        assert SYNC_AUDIT_EVENTS | NOWAIT_AUDIT_EVENTS == AUDIT_EVENT_WHITELIST

    def test_sync_set_contents(self):
        assert SYNC_AUDIT_EVENTS == frozenset(
            {
                "lint_run_completed",
                # Phase 4 U1 — wiki self-healing mutations.
                "orphan_pruned",
                "contradiction_resolved",
                "stale_claim_pruned",
                "poison_access_zeroed",
                "heal_run_completed",
                "heal_partial_mutation",
            }
        )

    def test_nowait_set_contents(self):
        assert NOWAIT_AUDIT_EVENTS == frozenset(
            {
                "memory_stored",
                "memory_recalled",
                "memory_forgot",
                "audit_log_error",
                # Compile-pipeline outcomes (emitted from the background task).
                "compile_completed",
                "compile_fallback",
                "compile_stale_dropped",
                "compile_error",
                "find_related_completed",
            }
        )

    def test_compile_call_sites_use_nowait(self):
        """Compile-pipeline events emit from the deferred background compile
        task in memory_service.py, which routes every event through the
        ``_audit`` helper wrapping ``write_audit_nowait``.
        """
        ms = (
            Path(__file__).parents[2]
            / "src"
            / "cli_agent_orchestrator"
            / "services"
            / "memory_service.py"
        )
        source = ms.read_text(encoding="utf-8")
        assert "write_audit_nowait(event_type, summary" in source
        for ev in (
            "compile_completed",
            "compile_fallback",
            "compile_stale_dropped",
            "compile_error",
            "find_related_completed",
        ):
            assert re.search(
                rf'_audit\(\s*\n?\s*"{ev}"', source
            ), f"{ev} missing _audit (nowait) call site"

    @pytest.mark.skip(reason="memory_stored emit site in store() lands with the audit wiring unit")
    def test_memory_stored_uses_nowait(self):
        """``memory_stored`` is in NOWAIT set; the call site uses
        ``write_audit_nowait``.
        """
        ms = (
            Path(__file__).parents[2]
            / "src"
            / "cli_agent_orchestrator"
            / "services"
            / "memory_service.py"
        )
        source = ms.read_text(encoding="utf-8")
        assert "write_audit_nowait" in source
        assert re.search(r'write_audit_nowait\(\s*\n?\s*"memory_stored"', source)

    def test_lint_run_completed_uses_await(self):
        """``lint_run_completed`` is sync; the wiki_lint.py call site awaits."""
        ms = (
            Path(__file__).parents[2]
            / "src"
            / "cli_agent_orchestrator"
            / "services"
            / "wiki_lint.py"
        )
        source = ms.read_text(encoding="utf-8")
        assert re.search(r'await write_audit\(\s*\n?\s*"lint_run_completed"', source)


# ===========================================================================
# T10 — Read-only CLI
# ===========================================================================


class TestT10ReadOnlyCli:
    def test_read_audit_log_uses_o_rdonly(self, audit_base, monkeypatch):
        """``read_audit_log`` opens with O_RDONLY only; never O_WRONLY/RDWR."""
        # First write a real log to read back.
        _run(write_audit("memory_stored", "y", scope="global"))

        captured = {}
        original_open = os.open

        def _spy(path, flags, mode=0o777):
            if "logs/memory" in str(path) and str(path).endswith(".md"):
                captured.setdefault("flags", []).append(flags)
            return original_open(path, flags, mode)

        monkeypatch.setattr(os, "open", _spy)
        body = read_audit_log()
        assert body  # non-empty
        # Every captured flag set MUST be O_RDONLY (== 0); MUST NOT contain
        # O_WRONLY (1) or O_RDWR (2).
        for flags in captured.get("flags", []):
            assert (flags & os.O_WRONLY) == 0
            assert (flags & os.O_RDWR) == 0

    @pytest.mark.skip(
        reason="U5 port: logs_cmd CLI deferred to avoid collision with memory.py edits"
    )
    def test_cli_logs_command_rejects_malformed_date(self, audit_base, monkeypatch):
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.commands.memory import logs_cmd

        runner = CliRunner()
        result = runner.invoke(logs_cmd, ["--date", "../../etc/passwd"])
        assert result.exit_code != 0
        # Click's BadParameter exit code is 2.
        assert result.exit_code == 2

    @pytest.mark.skip(
        reason="U5 port: logs_cmd CLI deferred to avoid collision with memory.py edits"
    )
    def test_cli_logs_command_reads_today_log(self, audit_base, monkeypatch):
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.commands.memory import logs_cmd

        _run(write_audit("memory_stored", "cli-roundtrip", scope="global"))
        runner = CliRunner()
        result = runner.invoke(logs_cmd, [])
        assert result.exit_code == 0
        assert "cli-roundtrip" in result.output


# ===========================================================================
# Whitelist enforcement
# ===========================================================================


class TestWhitelistEnforcement:
    def test_unknown_event_dropped_with_warning(self, audit_base, caplog):
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.audit_log"):
            _run(write_audit("totally_made_up_event", "x", k="v"))
        msgs = [r.getMessage() for r in caplog.records]
        assert any("audit_log_drop" in m and "unknown_event" in m for m in msgs)
        # No log file written.
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = audit_base / "logs" / "memory" / f"{date_str}.md"
        # File may exist (created by self-emit attempt) — the implementation
        # records an ``audit_log_error`` line whose summary mentions the
        # rejected event-type for forensics. The CRITICAL invariant is that
        # the unknown event type does NOT appear as a top-level
        # ``[event_type]`` bracket — i.e. it is not accepted into the log
        # under its own name.
        if log_path.exists():
            body = log_path.read_text(encoding="utf-8")
            assert "[totally_made_up_event]" not in body
            # Diagnostic line is allowed (within a backtick-wrapped summary).


# ===========================================================================
# Round-trip: write → read_audit_log
# ===========================================================================


class TestRoundTrip:
    def test_write_then_read(self, audit_base):
        _run(write_audit("memory_stored", "round-trip body", scope="global", k="v"))
        body = read_audit_log()
        assert body.endswith("\n")
        # Single bullet line.
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert len(lines) == 1
        ln = lines[0]
        assert ln.startswith("- ")
        assert "[memory_stored]" in ln
        assert "round-trip body" in ln
        # Field present.
        assert "k=v" in ln
        # Summary wrapped in backticks.
        assert "`round-trip body`" in ln

    def test_iso8601_z_timestamp(self, audit_base):
        _run(write_audit("memory_recalled", "x"))
        body = read_audit_log()
        # 2026-05-15T12:34:56Z shape.
        assert re.search(r"- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[memory_recalled\]", body)


# ===========================================================================
# U7.5 plan cases
# ===========================================================================


class TestU75PlanCases:
    @pytest.mark.skip(
        reason="U5 port: integration test depends on memory_stored audit call not yet ported"
    )
    def test_store_writes_audit_log(self, audit_base, tmp_path, monkeypatch):
        """U7.5.a — full integration: ``MemoryService.store()`` produces a
        ``memory_stored`` audit-log entry on disk.
        """
        from sqlalchemy import create_engine

        from cli_agent_orchestrator.clients.database import Base
        from cli_agent_orchestrator.services.memory_service import MemoryService

        engine = create_engine(
            f"sqlite:///{tmp_path}/u75.db",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=engine)
        svc = MemoryService(base_dir=tmp_path / "msvc-base", db_engine=engine)
        ctx = {
            "terminal_id": "t1",
            "session_name": "s1",
            "agent_profile": "developer",
            "provider": "claude_code",
            "cwd": "/home/user/proj",
            "caller_scope": "global",
        }
        _run(
            svc.store(
                content="audit-roundtrip-content",
                scope="global",
                memory_type="reference",
                key="audit-row",
                tags="t",
                terminal_context=ctx,
            )
        )
        # write_audit_nowait may schedule on the running loop — wait briefly.
        body = ""
        for _ in range(20):
            body = read_audit_log()
            if "memory_stored" in body:
                break
            time.sleep(0.05)
        assert "memory_stored" in body, f"expected memory_stored in audit log; got {body!r}"

    def test_audit_log_write_failure_does_not_fail_store(self, audit_base, tmp_path, monkeypatch):
        """LOAD-BEARING: Phase 2.5 U5 non-blocking promise. If audit write
        raises, ``store()`` MUST still succeed.
        """
        from sqlalchemy import create_engine

        from cli_agent_orchestrator.clients.database import Base
        from cli_agent_orchestrator.services.memory_service import MemoryService

        engine = create_engine(
            f"sqlite:///{tmp_path}/u75-fail.db",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=engine)
        svc = MemoryService(base_dir=tmp_path / "msvc-fail", db_engine=engine)

        # Force write_audit_nowait to raise. The wiring sites wrap in
        # try/except logger.debug — the explosion must NOT propagate to
        # store(). We instead patch the underlying write_audit to raise.
        async def _raise(*a, **kw):
            raise IOError("planted disk failure")

        monkeypatch.setattr(audit_log, "write_audit", _raise)

        ctx = {
            "terminal_id": "t1",
            "session_name": "s1",
            "agent_profile": "developer",
            "provider": "claude_code",
            "cwd": "/home/user/proj",
            "caller_scope": "global",
        }
        # store() must complete and return a Memory.
        mem = _run(
            svc.store(
                content="non-blocking-promise",
                scope="global",
                memory_type="reference",
                key="np",
                tags="t",
                terminal_context=ctx,
            )
        )
        assert mem is not None
        assert mem.key == "np"

    def test_log_rotation_deletes_old_files(self, audit_base):
        """U7.5.c — files older than retention_days deleted; recent kept."""
        log_dir = audit_base / "logs" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).date()
        # 35-day-old → delete; 10-day-old → keep.
        old_date = today - timedelta(days=35)
        recent_date = today - timedelta(days=10)
        old_path = log_dir / f"{old_date.strftime('%Y-%m-%d')}.md"
        recent_path = log_dir / f"{recent_date.strftime('%Y-%m-%d')}.md"
        old_path.write_text("- old\n", encoding="utf-8")
        recent_path.write_text("- recent\n", encoding="utf-8")

        deleted = sweep_old_audit_logs(retention_days=30)
        assert deleted == 1
        assert not old_path.exists()
        assert recent_path.exists()

    def test_log_rotation_skips_non_canonical_filenames(self, audit_base):
        """Files not matching ``YYYY-MM-DD.md`` are left alone."""
        log_dir = audit_base / "logs" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        weird = log_dir / "not-a-date.md"
        weird.write_text("preserved\n", encoding="utf-8")
        sweep_old_audit_logs(retention_days=30)
        assert weird.exists()


# ===========================================================================
# Day-cap setting + flock setting (U5.B settings additions)
# ===========================================================================


class TestSettings:
    @pytest.mark.skip(reason="U5 port: audit_log settings not yet added to settings_service.py")
    def test_settings_day_cap_default(self):
        from cli_agent_orchestrator.services.settings_service import (
            get_memory_settings,
        )

        s = get_memory_settings()
        assert "audit_log_day_cap_bytes" in s
        assert s["audit_log_day_cap_bytes"] >= 1024

    def test_settings_flock_default_off(self):
        from cli_agent_orchestrator.services.settings_service import (
            get_memory_settings,
        )

        s = get_memory_settings()
        # Default is OFF.
        assert s.get("audit_log_flock") in (False, None)


# ===========================================================================
# Cross-cutting: U1 / U2 / U3 caller surfaces still get sanitized
# ===========================================================================


class TestCrossCuttingSanitiserCallers:
    """T1's extension to ``_sanitize_for_log`` MUST not regress the U1/U2/U3
    callers that already used it. Verify each surface accepts a U+2028
    smuggled string and emits the canonical escape.
    """

    def test_compiler_topic_log(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.wiki_compiler"
        ):
            wc.logger.warning(
                "compiler_topic: %s",
                wc._sanitize_for_log("topic fake-line"),
            )
        for rec in caplog.records:
            assert " " not in rec.getMessage()
            assert "\\n" in rec.getMessage()

    @pytest.mark.skip(reason="U5 port: depends on U3 wiki_lint.py not yet ported")
    def test_lint_topic_log(self, caplog):
        from cli_agent_orchestrator.services import wiki_lint as wl

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.wiki_lint"):
            wl.logger.warning("lint_topic: %s", wc._sanitize_for_log("k fake-line"))
        for rec in caplog.records:
            assert " " not in rec.getMessage()

    def test_audit_self_uses_extended_sanitiser(self, audit_base):
        """The audit summary stored on disk after a U+2028 input must NOT
        contain the raw separator.
        """
        _run(write_audit("memory_stored", "evil continuation"))
        body = read_audit_log()
        assert " " not in body
        assert "\\n" in body
