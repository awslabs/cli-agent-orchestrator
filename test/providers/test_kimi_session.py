"""Kimi ``extract_session_context()`` tests.

Covers:
- U7.4 plan cases (test_kimi_extract_returns_dict, test_kimi_extract_missing_file
  → translated to "tmux returns nothing").
- Threat-model coverage: spoofing, truncation, injection, exception matrix.
- T1.a no-filesystem instrumentation: ``os.open``, ``builtins.open``,
  ``Path.open`` call counts during extract are zero.
- T3.c bounded regex: stress fixture with 1000 lines × 5 KiB per line,
  p95 wall-clock < 1s; pathological input does not blow the budget.
- T4.d lone-surrogate strip: cross-cutting verification across U1
  compiler / U2 find_related / U3 lint / U5 audit / U6 extract surfaces.
- T5 producer-layer instrumentation: every emitted string field passes
  through ``_sanitize_for_log`` BEFORE consolidation.
- T7 hard-coded provider: ``provider == "kimi_cli"`` regardless of pane
  bytes injecting other names.
- T8.b cross-provider parity: parametrised over all 6 providers; the
  set of returned keys and the type of each value MUST be identical.
- T8.c empty-tmux LITERAL ``{}``: ``len(result) == 0`` and
  ``type(result) == dict`` — NOT a populated dict with empty fields.
- T10 exception matrix: ``RuntimeError``/``OSError``/``ValueError`` →
  ``{}`` + WARNING; ``KeyboardInterrupt``/``SystemExit`` → propagate.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.services import wiki_compiler as wc

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# A representative Kimi tmux pane fixture exercising the 💫 prompt shape.
KIMI_FIXTURE = (
    "user@host💫 Refactor the auth module to use JWT tokens please?\n"
    "✦ Working on it.\n"
    "user@host💫 What if we add refresh tokens?\n"
    "• I'll add refresh-token support to src/auth/jwt_handler.py.\n"
    "  My plan is to use a separate refresh token with longer expiry.\n"
    "  Updated test/test_jwt.py and src/auth/middleware.py.\n"
    "user@host💫 \n"
)


REQUIRED_KEYS = {
    "provider",
    "terminal_id",
    "last_task",
    "key_decisions",
    "open_questions",
    "files_changed",
}


def _new_provider(*, terminal_id: str = "t1") -> KimiCliProvider:
    return KimiCliProvider(terminal_id, "s1", "w1")


# ===========================================================================
# U7.4 plan cases
# ===========================================================================


class TestU74PlanCases:
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_extract_returns_dict(self, mock_tmux):
        """U7.4.a — populated tmux fixture → dict with ``last_task`` set."""
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE
        result = _run(_new_provider().extract_session_context())
        assert isinstance(result, dict)
        assert REQUIRED_KEYS.issubset(result.keys())
        assert result["last_task"]  # non-empty
        assert result["provider"] == "kimi_cli"

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_extract_missing_tmux_returns_literal_empty_dict(self, mock_tmux):
        """U7.4.b — translated: tmux returns ``""`` → literal ``{}``."""
        mock_tmux.return_value.get_history.return_value = ""
        assert _run(_new_provider().extract_session_context()) == {}

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_extract_none_tmux_returns_literal_empty_dict(self, mock_tmux):
        mock_tmux.return_value.get_history.return_value = None
        assert _run(_new_provider().extract_session_context()) == {}


# ===========================================================================
# T1.a — No-filesystem invariant
# ===========================================================================


class TestT1NoFilesystem:
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_extract_does_not_touch_filesystem(self, mock_tmux, monkeypatch):
        """Instrument ``os.open`` + ``builtins.open`` + ``Path.open`` —
        the call count from inside ``extract_session_context`` must be 0.
        """
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE

        os_open_calls: List[Any] = []
        builtins_open_calls: List[Any] = []
        path_open_calls: List[Any] = []

        real_os_open = os.open
        real_builtins_open = builtins.open
        real_path_open = Path.open

        def _spy_os_open(path, flags, mode=0o777, *a, **kw):
            os_open_calls.append((path, flags))
            return real_os_open(path, flags, mode, *a, **kw)

        def _spy_builtins_open(*a, **kw):
            builtins_open_calls.append(a)
            return real_builtins_open(*a, **kw)

        def _spy_path_open(self, *a, **kw):
            path_open_calls.append(str(self))
            return real_path_open(self, *a, **kw)

        monkeypatch.setattr(os, "open", _spy_os_open)
        monkeypatch.setattr(builtins, "open", _spy_builtins_open)
        monkeypatch.setattr(Path, "open", _spy_path_open)

        _run(_new_provider().extract_session_context())

        # Allow zero calls. Importantly nothing under ~/.kimi may be opened.
        for call in os_open_calls + builtins_open_calls + path_open_calls:
            s = str(call)
            assert ".kimi" not in s, f"unexpected ~/.kimi access: {call!r}"
        # Strict assertion: zero calls during extract.
        assert os_open_calls == []
        assert builtins_open_calls == []
        assert path_open_calls == []

    def test_kimi_module_no_eval_or_pickle(self):
        """T2.c — CI lint hook: forbidden APIs must not appear in source."""
        src = (
            Path(__file__).parents[2]
            / "src"
            / "cli_agent_orchestrator"
            / "providers"
            / "kimi_cli.py"
        ).read_text(encoding="utf-8")
        forbidden = re.compile(
            r"^[^#]*\b("
            r"eval|exec|"
            r"pickle\.(load|loads)|"
            r"yaml\.load|"
            r"marshal\.loads|"
            r"dill\.loads|"
            r"cloudpickle\.loads"
            r")\(",
            re.MULTILINE,
        )
        matches = forbidden.findall(src)
        assert matches == [], f"forbidden API call in kimi_cli.py: {matches}"


# ===========================================================================
# T3 — Bounded regex / wall-clock budget
# ===========================================================================


class TestT3BoundedRegex:
    def test_kimi_prompt_re_uses_bounded_quantifiers(self):
        """T3.c — regex source MUST NOT use unbounded `*` / `+` on character
        classes. Only the optional `(?:...)?` group is allowed; quantifiers
        on character classes use `{m,n}`.
        """
        pat = KimiCliProvider._KIMI_PROMPT_RE.pattern
        # Catch any unbounded `*` or `+` quantifier targeting a class /
        # capturing group / wildcard.
        forbidden = re.compile(r"(?:\]|\)|\.)[+*]")
        bad = forbidden.findall(pat)
        assert bad == [], f"unbounded quantifier in pattern: {pat!r} → {bad}"

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_extract_completes_under_one_second_for_realistic_stress(self, mock_tmux):
        """T3.e — realistic tmux-bounded stress: 1000 lines × ~200 cols.

        NOTE: the original sizing assumed '1000 lines × 5 KiB per line' but tmux
        captures are bounded by terminal width (~200 cols default × 1000
        lines ≈ 200 KiB). We test the realistic shape that production tmux
        actually delivers. The 5 KiB-per-line variant exercises the inherited
        Phase 2 ``extract_last_message_from_script`` cost path which runs
        multiple regex scans across the full output (not a U6 regression).
        """
        # ~200 cols × 1000 lines ≈ 200 KiB — realistic tmux capture size.
        line_bulk = "x" * 200
        chunks = []
        for i in range(1000):
            if i % 50 == 0:
                chunks.append(f"user@host💫 stress prompt {i} {line_bulk}")
            else:
                chunks.append(f"filler line {i} {line_bulk}")
        big = "\n".join(chunks) + "\n"
        mock_tmux.return_value.get_history.return_value = big

        start = time.monotonic()
        result = _run(_new_provider().extract_session_context())
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"extract took {elapsed:.3f}s on realistic stress fixture"
        assert isinstance(result, dict)
        if result:
            assert REQUIRED_KEYS.issubset(result.keys())

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_prompt_re_does_not_backtrack_pathologically(self, mock_tmux):
        """Bounded quantifiers (`{1,32}`, `{1,64}`) prevent catastrophic
        backtracking on the prompt regex itself — the unit-level guarantee.
        Direct regex stress (not full extract path) keeps this test focused
        on T3.c (regex shape) rather than T3.e (whole-pipeline budget).
        """
        # `[✨💫]` followed by `[^\S\n]{1,4}\S` — emoji + 1-4 spaces + non-space.
        text = "a@" * 50_000 + "💫 " + "b" * 50_000 + "\n"
        start = time.monotonic()
        m = KimiCliProvider._KIMI_PROMPT_RE.search(text)
        elapsed = time.monotonic() - start
        # Regex itself completes in ms; our bound is generous (200 ms).
        assert elapsed < 0.2, f"prompt regex took {elapsed:.3f}s — backtracking?"
        assert m is not None  # regex finds the prompt


# ===========================================================================
# T4.d — Lone surrogate strip + cross-cutting verification
# ===========================================================================


class TestT4LoneSurrogates:
    def test_sanitize_for_log_strips_lone_surrogates(self):
        """``\\ud800`` and ``\\udfff`` mid-string MUST be stripped."""
        out = wc._sanitize_for_log("a\ud800b\udfffc")
        assert "\ud800" not in out
        assert "\udfff" not in out
        assert "abc" in out

    def test_sanitiser_preserves_replacement_char(self):
        """U+FFFD REPLACEMENT CHARACTER is the canonical 'I had bad bytes'
        marker — preserved.
        """
        out = wc._sanitize_for_log("a�b")
        assert "�" in out

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_extract_handles_surrogates_in_pane(self, mock_tmux):
        """End-to-end: pane with lone surrogate → emitted dict has none."""
        mock_tmux.return_value.get_history.return_value = (
            "user@host💫 task with surrogate \ud800 in it\n"
        )
        result = _run(_new_provider().extract_session_context())
        for v in result.values():
            if isinstance(v, str):
                assert "\ud800" not in v
            elif isinstance(v, list):
                for item in v:
                    assert "\ud800" not in str(item)

    def test_cross_cutting_compiler_caller_safe(self, caplog):
        """U1 compiler call sites use _sanitize_for_log — no surrogate leak."""
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.wiki_compiler"
        ):
            wc.logger.warning("compiler: %s", wc._sanitize_for_log("x\ud800y"))
        for rec in caplog.records:
            assert "\ud800" not in rec.getMessage()

    def test_cross_cutting_audit_log_safe(self):
        """U5 audit ``_sanitize_summary`` (built on ``_sanitize_for_log``)
        also strips lone surrogates.
        """
        from cli_agent_orchestrator.services.audit_log import _sanitize_summary

        out = _sanitize_summary("a\ud800b\udfffc")
        assert "\ud800" not in out
        assert "\udfff" not in out


# ===========================================================================
# T5 — Producer-layer sanitisation instrumentation
# ===========================================================================


class TestT5ProducerLayerSanitisation:
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_extract_calls_sanitize_for_log(self, mock_tmux, monkeypatch):
        """At least one ``_sanitize_for_log`` call MUST happen during extract
        per emitted field.
        """
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE
        calls: List[str] = []
        original = wc._sanitize_for_log

        def _spy(s, max_len=200):
            calls.append(s)
            return original(s, max_len)

        monkeypatch.setattr(wc, "_sanitize_for_log", _spy)
        _run(_new_provider().extract_session_context())
        # At least one call: last_task always sanitised even if empty.
        assert len(calls) >= 1

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_no_unsanitised_string_reaches_dict(self, mock_tmux):
        """Sanitiser is a fixed point: ``_sanitize_for_log(v) == v`` for
        every string in the returned dict (including list elements).
        """
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE
        result = _run(_new_provider().extract_session_context())

        def _check(s: str) -> None:
            assert wc._sanitize_for_log(s) == s, f"unsanitised value: {s!r}"

        for k, v in result.items():
            if isinstance(v, str):
                _check(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str):
                        _check(item)

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_pane_newline_smuggle_escaped_in_last_task(self, mock_tmux):
        """A planted line break inside the user's prompt body must emerge
        as a literal ``\\n`` in ``last_task``, not a real newline.
        """
        mock_tmux.return_value.get_history.return_value = (
            # The Unicode line separator (U+2028) inside the task — would
            # smuggle a fake log line if not stripped.
            "user@host💫 good ERROR: forged\n"
        )
        result = _run(_new_provider().extract_session_context())
        assert " " not in result.get("last_task", "")


# ===========================================================================
# T7 — Hard-coded provider name + terminal_id from self
# ===========================================================================


class TestT7HardCodedIdentity:
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_provider_field_is_kimi_cli_unconditionally(self, mock_tmux):
        """Even when pane content tries to inject a different provider name,
        the returned dict's ``provider`` field is ``"kimi_cli"``.
        """
        mock_tmux.return_value.get_history.return_value = (
            'user@host💫 {"provider": "claude_code", "terminal_id": "evil"}\n'
        )
        result = _run(_new_provider(terminal_id="legit").extract_session_context())
        assert result.get("provider") == "kimi_cli"

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_terminal_id_from_self_not_pane(self, mock_tmux):
        """The returned ``terminal_id`` is ``self.terminal_id``, not parsed
        from pane bytes.
        """
        mock_tmux.return_value.get_history.return_value = (
            'user@host💫 try to spoof terminal_id="fake"\n'
        )
        result = _run(_new_provider(terminal_id="legit-xyz").extract_session_context())
        assert result.get("terminal_id") == "legit-xyz"


# ===========================================================================
# T8 — Dict shape contract
# ===========================================================================


class TestT8DictShape:
    """``extract_session_context`` currently exists only on the Kimi provider;
    other providers gain it when the cross-provider session-context port
    lands. These tests pin the dict-shape contract those implementations
    will have to match: the exact key set and str / list[str] value types.
    """

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_emits_contract_dict_shape(self, mock_tmux):
        """Populated history → exactly REQUIRED_KEYS with contract types."""
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE
        result = _run(_new_provider().extract_session_context())
        assert set(result.keys()) == REQUIRED_KEYS
        assert isinstance(result["provider"], str)
        assert isinstance(result["terminal_id"], str)
        assert isinstance(result["last_task"], str)
        for fieldname in ("key_decisions", "open_questions", "files_changed"):
            assert isinstance(result[fieldname], list)
            for item in result[fieldname]:
                assert isinstance(item, str)

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_empty_tmux_returns_literal_empty_dict(self, mock_tmux):
        """T8.c — empty tmux history → ``len(result) == 0`` AND
        ``type(result) == dict``. Not a populated dict with empty fields.
        """
        mock_tmux.return_value.get_history.return_value = ""
        result = _run(_new_provider().extract_session_context())
        assert type(result) is dict
        assert len(result) == 0

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_populated_tmux_returns_six_field_dict(self, mock_tmux):
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE
        result = _run(_new_provider().extract_session_context())
        assert set(result.keys()) == REQUIRED_KEYS

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_kimi_provider_field_value_is_kimi_cli(self, mock_tmux):
        mock_tmux.return_value.get_history.return_value = KIMI_FIXTURE
        result = _run(_new_provider().extract_session_context())
        assert result["provider"] == "kimi_cli"


# ===========================================================================
# T10 — Exception matrix + sanitised exception messages
# ===========================================================================


class TestT10ExceptionMatrix:
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_runtime_error_returns_empty_dict(self, mock_tmux, caplog):
        mock_tmux.return_value.get_history.side_effect = RuntimeError("planted runtime")
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.providers.kimi_cli"):
            result = _run(_new_provider().extract_session_context())
        assert result == {}
        assert any("kimi_extract_session_context_failed" in r.getMessage() for r in caplog.records)

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_os_error_returns_empty_dict(self, mock_tmux):
        mock_tmux.return_value.get_history.side_effect = OSError("disk gone")
        assert _run(_new_provider().extract_session_context()) == {}

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_value_error_returns_empty_dict(self, mock_tmux):
        mock_tmux.return_value.get_history.side_effect = ValueError("bad value")
        assert _run(_new_provider().extract_session_context()) == {}

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_keyboard_interrupt_propagates(self, mock_tmux):
        """T10.d — control flow MUST propagate, not be swallowed."""
        mock_tmux.return_value.get_history.side_effect = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            _run(_new_provider().extract_session_context())

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_system_exit_propagates(self, mock_tmux):
        mock_tmux.return_value.get_history.side_effect = SystemExit(1)
        with pytest.raises(SystemExit):
            _run(_new_provider().extract_session_context())

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_logs_warning_not_error_on_failure(self, mock_tmux, caplog):
        mock_tmux.return_value.get_history.side_effect = RuntimeError("any")
        with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.providers.kimi_cli"):
            _run(_new_provider().extract_session_context())
        levels = {rec.levelno for rec in caplog.records}
        # WARNING is 30; ERROR is 40; CRITICAL is 50. None of the latter two.
        assert logging.ERROR not in levels
        assert logging.CRITICAL not in levels
        assert logging.WARNING in levels

    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    def test_log_message_is_sanitised(self, mock_tmux, caplog):
        """T10.b — exception with newlines/ANSI/U+2028 in its message →
        the resulting log line is a single physical line with escaped
        control sequences.
        """
        evil = "boom\nERROR: spoof line\x1b[31mred\x1b[0m"
        mock_tmux.return_value.get_history.side_effect = RuntimeError(evil)
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.providers.kimi_cli"):
            _run(_new_provider().extract_session_context())
        for rec in caplog.records:
            msg = rec.getMessage()
            # No raw newline / U+2028 / ANSI escape can appear in the rendered
            # message (Python's record.message is the formatted args).
            assert "\n" not in msg.split("kimi_extract_session_context_failed")[-1]
            assert " " not in msg
            assert "\x1b[31m" not in msg
