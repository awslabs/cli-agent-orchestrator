"""Lifecycle tests for the Kimi Code managed runtime home.

**Review C regression.** A worker's ``KIMI_CODE_HOME`` is a snapshot of the
operator's ``credentials/``, MCP configuration and Kimi state. It used to be
built under a random ``/tmp/cao_kimi_<random>/kimi-home`` whose path existed only
in the live provider instance (``self._temp_dir``). After a cao-server restart the
provider is reconstructed from terminal metadata, where ``provider_variant`` is
the only persisted Kimi state, so ``_temp_dir`` was ``None``, ``cleanup()`` was a
no-op, and ``ProviderManager.cleanup_provider()`` returned ``True`` while the
copied credentials stayed on disk forever.

The home is now named deterministically from the terminal id
(``CAO_HOME_DIR/providers/kimi_code/<sha256(terminal_id)>/kimi-home``), mirroring
``minimax_code`` and ``grok_cli``, so cleanup recovers it with neither a database
column nor a persisted free-form path.

**§18 adversarial residue.** Because ``cleanup()`` must work on a provider that
carries no path state at all, it is the one worth attacking: every managed
ancestor is validated as a real directory, the terminal directory must be the
exact deterministic child of the managed root, and no symlink is ever followed.

Every test writes only under ``tmp_path``; the operator's real ``CAO_HOME_DIR``,
``KIMI_CODE_HOME`` and ``~/.kimi-code`` are never read or written.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers import kimi_cli as kimi_cli_module
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, ProviderError
from cli_agent_orchestrator.providers.manager import ProviderManager


def _code_provider(terminal_id: str, source_home: Path) -> KimiCliProvider:
    """A provider pre-armed as if the launch-shell probe returned CODE."""

    provider = KimiCliProvider(terminal_id, "session-1", "window-1")
    provider._kimi_binary = "/usr/bin/kimi"
    provider._dialect = kimi_cli_module.KimiDialect.CODE
    provider._kimi_source_home = source_home
    return provider


def _source_home(root: Path, token: str = "source-secret") -> Path:
    """A synthetic source ``KIMI_CODE_HOME`` holding one synthetic credential."""

    source = root / "kimi-code-source"
    credentials = source / "credentials"
    credentials.mkdir(parents=True, exist_ok=True)
    (credentials / "auth.json").write_text(json.dumps({"token": token}), encoding="utf-8")
    return source


def _expected_home(cao_home: Path, terminal_id: str) -> Path:
    """The managed home path, derived independently of the provider."""

    digest = hashlib.sha256(terminal_id.encode("utf-8")).hexdigest()
    return cao_home / "providers" / "kimi_code" / digest / "kimi-home"


def _metadata(terminal_id: str, variant: str | None) -> dict:
    """A terminal row as the database serves it after a restart."""

    metadata = {
        "provider": ProviderType.KIMI_CLI.value,
        "tmux_session": "s1",
        "tmux_window": "w1",
        "agent_profile": None,
    }
    if variant is not None:
        metadata["provider_variant"] = variant
    return metadata


class TestReviewCRestartCleanup:
    """Review C — terminal deletion after a restart must not leak credentials."""

    def test_restart_cleanup_removes_the_managed_home(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path)

        provider = _code_provider("term-restart", source)
        provider._build_kimi_code_command()

        home = _expected_home(cao_home, "term-restart")
        assert home.is_dir(), "the launch must build the deterministic managed home"
        copied = home / "credentials" / "auth.json"
        copied.write_text('{"token": "copied-secret"}', encoding="utf-8")

        # Simulated restart: no provider instance survives, and the surviving DB
        # row records only the launch variant.
        manager = ProviderManager()
        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=_metadata("term-restart", kimi_cli_module.KimiDialect.CODE.value),
        ):
            assert manager.cleanup_provider("term-restart") is True

        assert not copied.exists()
        assert not home.exists()
        # The source home is the operator's real state: never removed.
        assert (source / "credentials" / "auth.json").is_file()

    def test_restart_cleanup_removes_a_home_it_never_built(self, tmp_path, monkeypatch):
        """The recovery path needs nothing but the terminal id and the DB row.

        This is the exact Review C leak: the home exists, the provider that built
        it is gone, and the surviving metadata names the variant only. ``True``
        here would mean the copied credentials were left on disk.
        """

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        home = _expected_home(cao_home, "term-prebuilt")
        (home / "credentials").mkdir(parents=True)
        copied = home / "credentials" / "auth.json"
        copied.write_text('{"token": "copied-secret"}', encoding="utf-8")

        manager = ProviderManager()
        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=_metadata("term-prebuilt", kimi_cli_module.KimiDialect.CODE.value),
        ):
            assert manager.cleanup_provider("term-prebuilt") is True

        assert not copied.exists()
        assert not home.exists()

    @pytest.mark.parametrize(
        "variant",
        [None, "unknown", kimi_cli_module.KimiDialect.LEGACY.value],
    )
    def test_restart_cleanup_removes_the_home_even_without_a_code_variant(
        self, tmp_path, monkeypatch, variant
    ):
        """The removal is not gated on the variant; the dialect is never guessed.

        Two concrete lifecycle reasons leave a managed home behind on a row whose
        persisted variant is not ``code``: ``initialize()`` materialises the home
        before the service persists ``provider_variant`` (a crash in that window
        reproduces a credential-bearing home on a NULL row), and a terminal
        re-launched under the other dialect keeps the home its earlier CODE launch
        created. Removing this terminal's own deterministic directory is therefore
        unconditional — and it is not a dialect guess, because nothing about the
        terminal's dialect is inferred from it and the reconstruction contract
        (`get_provider` refuses a NULL variant) is untouched.
        """

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        home = _expected_home(cao_home, "term-ambiguous")
        (home / "credentials").mkdir(parents=True)
        copied = home / "credentials" / "auth.json"
        copied.write_text('{"token": "copied-secret"}', encoding="utf-8")

        manager = ProviderManager()
        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=_metadata("term-ambiguous", variant),
        ):
            assert manager.cleanup_provider("term-ambiguous") is True

        assert not copied.exists()
        assert not home.exists()

    def test_a_crash_between_the_home_and_the_variant_persistence_does_not_leak(
        self, tmp_path, monkeypatch
    ):
        """The review reproduction: home on disk, variant never persisted."""

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)

        # A CODE launch materialised the credential home ...
        provider = KimiCliProvider("term-crash", "session-1", "window-1")
        home = provider._managed_runtime_home()
        (home / "credentials").mkdir(parents=True)
        copied = home / "credentials" / "auth.json"
        copied.write_text('{"token": "copied-secret"}', encoding="utf-8")

        # ... and cao-server died before the service wrote `provider_variant`.
        manager = ProviderManager()
        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=_metadata("term-crash", None),
        ):
            assert manager.cleanup_provider("term-crash") is True

        assert not copied.exists(), "the copied credential must not survive the crash window"
        assert not home.exists()

    def test_no_managed_home_is_a_successful_no_op(self, tmp_path, monkeypatch):
        """The ordinary legacy case: nothing was ever built, so cleanup succeeds."""

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)

        manager = ProviderManager()
        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=_metadata("term-none", kimi_cli_module.KimiDialect.LEGACY.value),
        ):
            assert manager.cleanup_provider("term-none") is True
        assert not _expected_home(cao_home, "term-none").exists()


class TestCleanupFailureIsRetryable:
    """A cleanup that cannot prove removal must report ``False``, not success."""

    def test_deferred_removal_keeps_the_home_and_a_retry_finishes_it(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path)

        provider = _code_provider("term-retry", source)
        provider._build_kimi_code_command()
        home = _expected_home(cao_home, "term-retry")
        assert home.is_dir()

        manager = ProviderManager()
        manager._providers["term-retry"] = provider

        real_rmtree = shutil.rmtree
        state: dict = {"fail": True, "removals": []}

        def controlled_rmtree(path, *args, **kwargs):
            target = Path(path)
            if target == home:
                if state["fail"]:
                    raise OSError("simulated removal failure")
                state["removals"].append(target)
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(kimi_cli_module.shutil, "rmtree", controlled_rmtree)

        assert manager.cleanup_provider("term-retry") is False
        assert home.exists(), "a deferred cleanup must leave the home for the retry"
        assert manager._providers["term-retry"] is provider

        state["fail"] = False
        assert manager.cleanup_provider("term-retry") is True
        assert not home.exists()
        assert state["removals"] == [home], "the home is removed exactly once"
        assert "term-retry" not in manager._providers

    def test_restart_cleanup_reports_false_when_removal_fails(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path)

        provider = _code_provider("term-restart-fail", source)
        provider._build_kimi_code_command()
        home = _expected_home(cao_home, "term-restart-fail")
        assert home.is_dir()

        real_rmtree = shutil.rmtree
        state = {"fail": True}

        def controlled_rmtree(path, *args, **kwargs):
            if state["fail"] and Path(path) == home:
                raise OSError("simulated removal failure")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(kimi_cli_module.shutil, "rmtree", controlled_rmtree)

        manager = ProviderManager()
        metadata = _metadata("term-restart-fail", kimi_cli_module.KimiDialect.CODE.value)
        with patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=metadata,
        ):
            assert manager.cleanup_provider("term-restart-fail") is False
            assert home.exists()

            # The DB row is the retry handle: the same recovery path finishes it.
            state["fail"] = False
            assert manager.cleanup_provider("term-restart-fail") is True
            assert not home.exists()


class TestManagedHomeBuildPath:
    """The launch builds into the managed directory and resets stale state."""

    def test_build_resets_a_stale_home_from_an_interrupted_lifecycle(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path, token="fresh")

        home = _expected_home(cao_home, "term-stale")
        (home / "credentials").mkdir(parents=True)
        (home / "credentials" / "auth.json").write_text('{"token": "stale-secret"}')
        (home / "mcp.json").write_text('{"mcpServers": {"stale": {"command": "x"}}}')

        _code_provider("term-stale", source)._build_kimi_code_command()

        assert (home / "credentials" / "auth.json").read_text() == '{"token": "fresh"}'
        assert "stale" not in (home / "mcp.json").read_text(encoding="utf-8")

    def test_build_refuses_a_symlinked_managed_root(self, tmp_path, monkeypatch):
        """A redirected managed root must fail the launch, never write through it."""

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        attacker_root = tmp_path / "attacker"
        (attacker_root / "providers").mkdir(parents=True)
        (cao_home / "providers").mkdir(parents=True)
        (cao_home / "providers" / "kimi_code").symlink_to(
            attacker_root / "providers" / "kimi_code", target_is_directory=True
        )

        provider = _code_provider("term-build-symlink", _source_home(tmp_path))
        with pytest.raises(ProviderError, match="Refusing to build"):
            provider._build_kimi_code_command()


class TestManagedPathSafety:
    """``cleanup()`` must never become a general recursive-delete primitive."""

    def test_terminal_a_cleanup_leaves_terminal_b_intact(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path)

        provider_a = _code_provider("term-a", source)
        provider_a._build_kimi_code_command()
        provider_b = _code_provider("term-b", source)
        provider_b._build_kimi_code_command()

        home_a = _expected_home(cao_home, "term-a")
        home_b = _expected_home(cao_home, "term-b")
        assert home_a.is_dir() and home_b.is_dir()

        assert provider_a.cleanup() is True

        assert not home_a.exists()
        assert home_b.is_dir()
        assert (home_b / "credentials" / "auth.json").is_file()

    def test_source_home_is_never_removed(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path)

        provider = _code_provider("term-source", source)
        provider._build_kimi_code_command()
        assert provider.cleanup() is True

        assert source.is_dir()
        assert (source / "credentials" / "auth.json").is_file()

    def test_legacy_dialect_removes_its_own_managed_home(self, tmp_path, monkeypatch):
        """A legacy row can own a leftover CODE home; cleanup still removes it.

        `cleanup()` is not gated on the dialect, so a terminal whose earlier CODE
        launch created a managed home does not leak it when it is later running as
        legacy. The removal is this terminal's own validated path, not a guess.
        """

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        home = _expected_home(cao_home, "term-legacy")
        (home / "credentials").mkdir(parents=True)
        copied = home / "credentials" / "auth.json"
        copied.write_text('{"token": "copied-secret"}')

        provider = KimiCliProvider("term-legacy", "session-1", "window-1")
        provider.restore_runtime_variant(kimi_cli_module.KimiDialect.LEGACY.value)
        assert provider.cleanup() is True

        assert not copied.exists()
        assert not home.exists()

    def test_symlinked_managed_root_component_is_refused(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)

        attacker_root = tmp_path / "attacker"
        linked_home = _expected_home(attacker_root, "term-symlink-root")
        (linked_home / "credentials").mkdir(parents=True)
        (linked_home / "credentials" / "auth.json").write_text('{"token": "elsewhere"}')

        (cao_home / "providers").mkdir(parents=True)
        (cao_home / "providers" / "kimi_code").symlink_to(
            attacker_root / "providers" / "kimi_code", target_is_directory=True
        )

        provider = _code_provider("term-symlink-root", tmp_path / "src")
        assert provider._is_managed_runtime_home(provider._managed_runtime_home()) is False
        assert provider.cleanup() is False
        assert (linked_home / "credentials" / "auth.json").is_file()

    def test_symlinked_terminal_directory_is_refused(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)

        target = tmp_path / "attacker-terminal"
        (target / "kimi-home" / "credentials").mkdir(parents=True)
        (target / "kimi-home" / "credentials" / "auth.json").write_text('{"token": "elsewhere"}')

        root = cao_home / "providers" / "kimi_code"
        root.mkdir(parents=True)
        digest = hashlib.sha256(b"term-symlink-dir").hexdigest()
        (root / digest).symlink_to(target, target_is_directory=True)

        provider = _code_provider("term-symlink-dir", tmp_path / "src")
        assert provider._is_managed_terminal_dir(root / digest) is False
        assert provider._is_managed_runtime_home(provider._managed_runtime_home()) is False
        assert provider.cleanup() is False
        assert (target / "kimi-home" / "credentials" / "auth.json").is_file()

    def test_symlinked_runtime_home_is_unlinked_without_following_its_target(
        self, tmp_path, monkeypatch
    ):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)

        terminal_dir = (
            cao_home / "providers" / "kimi_code" / hashlib.sha256(b"term-link-home").hexdigest()
        )
        terminal_dir.mkdir(parents=True)
        target = tmp_path / "shared-home"
        (target / "credentials").mkdir(parents=True)
        (target / "credentials" / "auth.json").write_text('{"token": "shared"}')
        (terminal_dir / "kimi-home").symlink_to(target, target_is_directory=True)

        provider = _code_provider("term-link-home", tmp_path / "src")
        assert provider._is_managed_runtime_home(provider._managed_runtime_home()) is True

        assert provider.cleanup() is True

        assert not (terminal_dir / "kimi-home").is_symlink()
        assert not terminal_dir.exists(), "the emptied terminal directory is dropped too"
        assert (target / "credentials" / "auth.json").is_file()

    def test_tmpdir_change_across_a_restart_does_not_affect_recovery(self, tmp_path, monkeypatch):
        """The managed path never derives from ``TMPDIR``, so a new one cannot hide it."""

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        source = _source_home(tmp_path)

        _code_provider("term-tmpdir", source)._build_kimi_code_command()
        home = _expected_home(cao_home, "term-tmpdir")
        assert home.is_dir()

        other_tmp = tmp_path / "other-tmp"
        other_tmp.mkdir()
        monkeypatch.setenv("TMPDIR", str(other_tmp))
        monkeypatch.setattr(tempfile, "tempdir", str(other_tmp))

        reconstructed = _code_provider("term-tmpdir", source)
        assert reconstructed._managed_runtime_home() == home
        assert reconstructed.cleanup() is True
        assert not home.exists()

    def test_path_is_derived_from_the_terminal_id(self, tmp_path, monkeypatch):
        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)

        first = _code_provider("term-stable", tmp_path / "src")._managed_runtime_home()
        second = _code_provider("term-stable", tmp_path / "src")._managed_runtime_home()
        other = _code_provider("term-distinct", tmp_path / "src")._managed_runtime_home()

        assert first == second == _expected_home(cao_home, "term-stable")
        assert first.is_relative_to(cao_home / "providers" / "kimi_code")
        assert other != first

    def test_nested_or_foreign_paths_are_refused(self, tmp_path, monkeypatch):
        """Lexical equality, not "somewhere below the managed root"."""

        cao_home = tmp_path / "cao"
        monkeypatch.setattr(kimi_cli_module, "CAO_HOME_DIR", cao_home)
        provider = _code_provider("term-exact", tmp_path / "src")
        terminal_dir = provider._managed_terminal_dir()

        assert provider._is_managed_terminal_dir(terminal_dir) is True
        assert provider._is_managed_terminal_dir(terminal_dir / "nested") is False
        assert provider._is_managed_terminal_dir(terminal_dir.parent) is False
        assert provider._is_managed_terminal_dir(cao_home / "providers" / "kimi_code") is False
        assert provider._is_managed_terminal_dir(tmp_path / "elsewhere") is False
        assert provider._is_managed_runtime_home(terminal_dir / "other-home") is False
