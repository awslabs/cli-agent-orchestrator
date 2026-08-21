"""Provenance-based opencode collision guard tests (PR #493).

Unlike ``test_install_opencode.py`` — which monkeypatches ``get_agent_dirs`` to
``{}`` and so never scans the CAO installed dir — these tests keep the DEFAULT
``cao_installed`` mapping pointed at the real context dir. That is the only way
to reproduce the regression: on reinstall, ``list_agent_profiles()`` discovers
the profile's OWN prior context copy (written by ``_write_context_file`` under
the RESOLVED name) as a ``source == "installed"`` candidate, and a naive guard
flags a profile against itself.

Two problems must hold simultaneously:

* **A (regression fixed):** reinstalling a profile whose install stem differs
  from its frontmatter ``name:`` must succeed (its own installed copy must not
  be mistaken for a colliding profile).
* **B (must not reopen):** two GENUINELY different profiles that resolve to the
  same opencode agent id must still raise — including the trap case where the
  first profile's local-store copy has been removed but its installed artifact
  survives.
"""

import os
import stat
from pathlib import Path
from typing import Any, Dict

import frontmatter
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import install
from cli_agent_orchestrator.services.install_service import (
    _CONTEXT_SOURCE_STEM_KEY,
    _context_content_with_provenance,
    _context_source_stem,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Redirect install paths to tmp while keeping the default cao_installed mapping.

    Crucially ``get_agent_dirs`` returns ``{"cao_installed": <context_dir>}`` —
    the same directory ``_write_context_file`` writes to — so discovery scans
    the installed copies exactly as it does in production. ``AGENT_CONTEXT_DIR``
    is pointed at that same dir so the provenance read and the context write
    agree.
    """
    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    opencode_agents = tmp_path / "opencode_cli" / "agents"
    opencode_config = tmp_path / "opencode_cli" / "opencode.json"
    kiro_agents = tmp_path / "kiro" / "agents"

    local_store.mkdir(parents=True)
    context_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR", opencode_agents
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_agents
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE", opencode_config
    )
    # DEFAULT mapping preserved (NOT {}): cao_installed points at the context dir.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
        lambda: {"cao_installed": str(context_dir)},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.ensure_skills_symlink", lambda: None
    )

    return {
        "local_store": local_store,
        "context_dir": context_dir,
        "agents_dir": opencode_agents,
        "config_file": opencode_config,
        "kiro_agents_dir": kiro_agents,
    }


def _write_profile(path: Path, *, name: str, body: str = "You are a helpful agent.") -> None:
    path.write_text(f"---\nname: {name}\ndescription: Test agent\n---\n{body}\n", encoding="utf-8")


def _install(runner: CliRunner, stem: str):
    return runner.invoke(install, [stem, "--provider", "opencode_cli"])


def _assert_installed_copy_remedy(output: str, context_copy: Path) -> None:
    assert str(context_copy) in output
    assert (
        f"If '{context_copy}' is your own profile's context copy from an earlier "
        "CAO version, delete it and reinstall."
    ) in output


def _assert_non_regular_context_error(output: str, context_copy: Path) -> None:
    assert str(context_copy) in output
    assert "non-regular filesystem entry" in output
    assert "Remove that path or replace it with a regular file, then reinstall." in output
    assert "Errno" not in output


def _line_body_and_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _remove_marker_line(text: str) -> str:
    lines = text.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        body, _ = _line_body_and_ending(line)
        if body.lstrip(" \t").startswith(f"{_CONTEXT_SOURCE_STEM_KEY}:"):
            return "".join(lines[:idx] + lines[idx + 1 :])
    raise AssertionError("marker line not found")


def _quoted_marker_line(source_name: str, newline: str = "\n") -> str:
    return f"{_CONTEXT_SOURCE_STEM_KEY}: '{source_name.replace(chr(39), chr(39) * 2)}'{newline}"


def _assert_inserted_marker_only(raw: str, stamped: str, source_name: str) -> None:
    assert _context_source_stem(stamped) == source_name
    assert stamped.count(f"{_CONTEXT_SOURCE_STEM_KEY}:") == 1
    assert _remove_marker_line(stamped).encode("utf-8") == raw.encode("utf-8")


# ---------------------------------------------------------------------------
# Problem A: reinstall/upgrade of a stem != name profile must succeed.
# (Fails on b67ba96 — the guard flags the profile's own installed copy.)
# ---------------------------------------------------------------------------


class TestProblemAReinstallStemNotEqualName:
    def test_reinstall_stem_ne_name_succeeds(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        # Install stem 'my-agent' whose frontmatter name is 'my-resolved'. The
        # context copy lands at context/my-resolved.md (named by RESOLVED name).
        _write_profile(workspace["local_store"] / "my-agent.md", name="my-resolved")

        r1 = _install(runner, "my-agent")
        assert r1.exit_code == 0 and "Error:" not in r1.output, r1.output

        # The installed copy exists under the resolved name and carries the marker.
        context_copy = workspace["context_dir"] / "my-resolved.md"
        assert context_copy.exists()
        assert frontmatter.loads(context_copy.read_text()).metadata[_CONTEXT_SOURCE_STEM_KEY] == (
            "my-agent"
        )

        # Reinstall the SAME profile. On b67ba96 this raises the collision error
        # (own copy mistaken for a sibling); on the fix it must succeed.
        r2 = _install(runner, "my-agent")
        assert r2.exit_code == 0, r2.output
        assert "Error:" not in r2.output, r2.output
        assert (workspace["agents_dir"] / "my-resolved.md").exists()

    def test_idempotent_across_multiple_cycles(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        _write_profile(workspace["local_store"] / "my-agent.md", name="my-resolved")

        first = None
        for _ in range(4):
            r = _install(runner, "my-agent")
            assert r.exit_code == 0 and "Error:" not in r.output, r.output
            contents = (workspace["agents_dir"] / "my-resolved.md").read_bytes()
            if first is None:
                first = contents
            else:
                assert contents == first, "reinstall must be byte-identical"

    def test_kiro_then_opencode_same_profile_succeeds(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        _write_profile(workspace["local_store"] / "shared-source.md", name="shared-resolved")

        kiro = runner.invoke(install, ["shared-source", "--provider", "kiro_cli"])
        opencode = runner.invoke(install, ["shared-source", "--provider", "opencode_cli"])

        assert kiro.exit_code == 0 and "Error:" not in kiro.output, kiro.output
        assert opencode.exit_code == 0 and "Error:" not in opencode.output, opencode.output
        context_copy = workspace["context_dir"] / "shared-resolved.md"
        assert _context_source_stem(context_copy.read_text(encoding="utf-8")) == "shared-source"
        assert (workspace["kiro_agents_dir"] / "shared-resolved.json").exists()
        assert (workspace["agents_dir"] / "shared-resolved.md").exists()


class TestContextWriteTargetIsRegularFile:
    def test_dangling_symlink_target_blocks_without_creating_link_target(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]
        _write_profile(store / "source.md", name="shared", body="New body.")
        context_copy = context_dir / "shared.md"
        link_target = context_dir / "dangling-target.md"
        context_copy.symlink_to(link_target)

        result = _install(runner, "source")

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        _assert_non_regular_context_error(result.output, context_copy)
        assert context_copy.is_symlink()
        assert not link_target.exists()
        assert not (workspace["agents_dir"] / "shared.md").exists()

    def test_live_symlink_inside_context_dir_blocks_without_modifying_target(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]
        _write_profile(store / "source.md", name="shared", body="New body.")
        context_copy = context_dir / "shared.md"
        link_target = context_dir / "real-target.md"
        original_target = "---\nname: shared\ndescription: Existing target\n---\nDo not modify.\n"
        link_target.write_text(original_target, encoding="utf-8")
        context_copy.symlink_to(link_target)

        result = runner.invoke(install, ["source", "--provider", "kiro_cli"])

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        _assert_non_regular_context_error(result.output, context_copy)
        assert context_copy.is_symlink()
        assert link_target.read_text(encoding="utf-8") == original_target
        assert not (workspace["kiro_agents_dir"] / "shared.json").exists()

    def test_directory_target_blocks_with_actionable_message(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]
        _write_profile(store / "source.md", name="shared", body="New body.")
        context_copy = context_dir / "shared.md"
        context_copy.mkdir()

        result = _install(runner, "source")

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        _assert_non_regular_context_error(result.output, context_copy)
        assert context_copy.is_dir()
        assert not (workspace["agents_dir"] / "shared.md").exists()


# ---------------------------------------------------------------------------
# Problem B: genuinely different profiles that collide must still raise.
# ---------------------------------------------------------------------------


class TestProblemBLocalStoreCollisionStillRaises:
    def test_two_local_store_profiles_same_id_raise(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """Two distinct local-store files with the same resolved name collide.

        This mirrors the existing guard test but with the DEFAULT cao_installed
        mapping active, proving the provenance change did not weaken the
        local-store collision path.
        """
        store = workspace["local_store"]
        _write_profile(store / "one.md", name="dup-name", body="First body.")

        r1 = _install(runner, "one")
        assert r1.exit_code == 0 and "Error:" not in r1.output, r1.output

        # A second, distinct file with the same resolved name appears later.
        _write_profile(store / "two.md", name="dup-name", body="Second body.")
        r2 = _install(runner, "two")
        assert r2.exit_code == 0  # failure result, not a crash
        assert "Error:" in r2.output
        assert "one" in r2.output and "two" in r2.output and "dup-name" in r2.output
        # First install untouched.
        assert "First body." in (workspace["agents_dir"] / "dup-name.md").read_text()


# ---------------------------------------------------------------------------
# THE TRAP: install A -> remove A's local-store copy -> install distinct B
# with a colliding id. Guard must RAISE and A's installed artifacts survive.
# A blanket `source == "installed"` exclusion would silently overwrite here.
# ---------------------------------------------------------------------------


class TestTrapCaseLeftoverInstalledArtifact:
    def test_leftover_installed_artifact_blocks_distinct_profile(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        agents_dir = workspace["agents_dir"]
        context_dir = workspace["context_dir"]

        # 1. Install profile A: stem 'alpha', resolved name 'shared'.
        _write_profile(store / "alpha.md", name="shared", body="Alpha body.")
        rA = _install(runner, "alpha")
        assert rA.exit_code == 0 and "Error:" not in rA.output, rA.output

        a_agent = agents_dir / "shared.md"
        a_context = context_dir / "shared.md"
        assert a_agent.exists() and a_context.exists()
        a_agent_bytes = a_agent.read_bytes()
        a_context_bytes = a_context.read_bytes()

        # 2. Emulate `cao profile remove alpha`: it deletes ONLY the local-store
        #    copy (cli/commands/profile.py), leaving the installed artifacts.
        (store / "alpha.md").unlink()

        # 3. Install a DIFFERENT profile B: stem 'beta', resolved name 'shared'
        #    (same id as A). The only surviving trace of A is its installed
        #    context copy, whose provenance marker records stem 'alpha' != 'beta'.
        _write_profile(store / "beta.md", name="shared", body="Beta body.")
        rB = _install(runner, "beta")

        # Must RAISE — not silently overwrite.
        assert rB.exit_code == 0  # failure result, not a crash
        assert "Error:" in rB.output, rB.output
        assert "shared" in rB.output

        # A's installed artifacts must be byte-for-byte intact.
        assert a_agent.read_bytes() == a_agent_bytes
        assert a_context.read_bytes() == a_context_bytes
        assert "Beta body." not in a_agent.read_text()
        assert "Beta body." not in a_context.read_text()


# ---------------------------------------------------------------------------
# Criterion 5: pre-existing installs (no provenance marker) cannot prove
# ownership, so they must block instead of being silently overwritten.
# ---------------------------------------------------------------------------


class TestMissingMarkerPreExistingInstalls:
    def test_markerless_installed_copy_blocks_distinct_profile_with_same_id(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]

        legacy_copy = context_dir / "shared.md"
        _write_profile(legacy_copy, name="shared", body="Original installed body.")
        first_context = legacy_copy.read_text()
        assert _CONTEXT_SOURCE_STEM_KEY not in first_context

        _write_profile(store / "new-source.md", name="shared", body="New profile body.")
        result = _install(runner, "new-source")

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        _assert_installed_copy_remedy(result.output, legacy_copy)
        assert legacy_copy.read_text() == first_context
        assert "New profile body." not in legacy_copy.read_text()
        assert not (workspace["agents_dir"] / "shared.md").exists()

    def test_markerless_installed_copy_blocks_same_profile_reinstall(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]

        _write_profile(store / "my-agent.md", name="my-resolved")
        legacy_copy = context_dir / "my-resolved.md"
        _write_profile(legacy_copy, name="my-resolved", body="Legacy context body.")
        first_context = legacy_copy.read_text()
        assert _CONTEXT_SOURCE_STEM_KEY not in first_context

        r = _install(runner, "my-agent")

        assert r.exit_code == 0
        assert "Error:" in r.output, r.output
        _assert_installed_copy_remedy(r.output, legacy_copy)
        assert legacy_copy.read_text() == first_context
        assert not (workspace["agents_dir"] / "my-resolved.md").exists()

    def test_truncated_installed_copy_without_marker_blocks_install(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]
        legacy_copy = context_dir / "shared.md"
        legacy_copy.write_text("---\nname: shared\ndescription: Partial legacy\n", encoding="utf-8")
        first_context = legacy_copy.read_text()

        _write_profile(store / "new-source.md", name="shared", body="New profile body.")
        result = _install(runner, "new-source")

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        _assert_installed_copy_remedy(result.output, legacy_copy)
        assert legacy_copy.read_text() == first_context
        assert not (workspace["agents_dir"] / "shared.md").exists()

    def test_corrupt_installed_copy_blocks_install(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]
        legacy_copy = context_dir / "shared.md"
        legacy_copy.write_text(
            "---\nname: [unterminated\ndescription: Corrupt legacy\n---\nBody\n",
            encoding="utf-8",
        )
        first_context = legacy_copy.read_text()

        _write_profile(store / "new-source.md", name="shared", body="New profile body.")
        result = _install(runner, "new-source")

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        _assert_installed_copy_remedy(result.output, legacy_copy)
        assert legacy_copy.read_text() == first_context
        assert not (workspace["agents_dir"] / "shared.md").exists()

    def test_legacy_slash_trap_still_raises_without_marker(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """The '/'->'__' collision still raises even with a marker-less sibling.

        The colliding sibling is a local-store file (source 'local', never
        'installed'), so the provenance fallback does not apply to it and the
        normal collision path fires.
        """
        store = workspace["local_store"]
        # Literal 'a__b' sibling installs first, alone.
        _write_profile(store / "a__b.md", name="a__b")
        r1 = _install(runner, "a__b")
        assert r1.exit_code == 0 and "Error:" not in r1.output, r1.output

        # A slash-named profile ('a/b' -> id 'a__b') appears later and collides.
        _write_profile(store / "slash-named.md", name="a/b")
        # This pre-creates the slash-name context parent because a separate
        # _write_context_file defect still affects successful slash-name writes.
        (workspace["context_dir"] / "a").mkdir(parents=True, exist_ok=True)
        r2 = _install(runner, "slash-named")
        assert r2.exit_code == 0  # failure result, not a crash
        assert "Error:" in r2.output, r2.output
        assert "a__b" in r2.output


class TestNonOpenCodeContextWrites:
    def test_kiro_context_copy_differs_from_source_only_by_marker_line(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        source_text = (
            "---\n"
            "# leading comment that must survive\n"
            'provider: "kiro_cli"\n'
            "tags:\n"
            "  - beta\n"
            "  - alpha\n"
            'description: "Quoted description: keep style"\n'
            'name: "kiro-byte-copy"\n'
            "# trailing frontmatter comment must survive\n"
            "---\n"
            "Body keeps ${UNSET_VAR}, unicode café, and markdown # headings untouched.\n"
        )
        source = workspace["local_store"] / "kiro-byte-copy.md"
        source.write_text(source_text, encoding="utf-8")

        result = runner.invoke(install, ["kiro-byte-copy", "--provider", "kiro_cli"])

        assert result.exit_code == 0
        assert "Error:" not in result.output, result.output
        context_text = (workspace["context_dir"] / "kiro-byte-copy.md").read_text(encoding="utf-8")
        _assert_inserted_marker_only(source_text, context_text, "kiro-byte-copy")
        assert (workspace["kiro_agents_dir"] / "kiro-byte-copy.json").exists()


class TestTextualProvenanceInsert:
    def test_frontmatter_shape_cases_preserve_all_non_marker_bytes(self) -> None:
        cases = [
            (
                "empty frontmatter block",
                "---\n---\nBody\n",
                "empty-source",
            ),
            (
                "CRLF line endings",
                "---\r\nname: crlf-agent\r\ndescription: CRLF\r\n---\r\nBody\r\n",
                "crlf-source",
            ),
            (
                "body delimiter is not frontmatter delimiter",
                "---\nname: body-delimiter\ndescription: Body delimiter\n---\nBody\n---\nStill body\n",
                "body-source",
            ),
            (
                "leading blank lines before opening delimiter",
                "\n\n---\nname: leading-blank\ndescription: Leading blank\n---\nBody\n",
                "blank-source",
            ),
            (
                "BOM before opening delimiter",
                "\ufeff---\nname: bom-agent\ndescription: BOM\n---\nBody\n",
                "bom-source",
            ),
            (
                "nested structures comments quoting sequences and unicode",
                "---\n"
                "# leading comment\n"
                'description: "Quoted: keep # literal"\n'
                "name: unicode-agent\n"
                "capabilities:\n"
                '  - "quoted item"\n'
                "  - café\n"
                "settings:\n"
                "  nested:\n"
                "    - keep: order\n"
                "# trailing comment\n"
                "---\n"
                "Unicode body café\n",
                "nested-source",
            ),
            (
                "YAML-ambiguous source stem is quoted",
                "---\nname: ambiguous\ndescription: Ambiguous source\n---\nBody\n",
                "yes",
            ),
            (
                "source stem with YAML punctuation is quoted",
                "---\nname: punctuation\ndescription: Punctuation source\n---\nBody\n",
                "odd: # value ' stem",
            ),
        ]

        for label, raw, source_name in cases:
            stamped = _context_content_with_provenance(raw, source_name)
            try:
                _assert_inserted_marker_only(raw, stamped, source_name)
            except AssertionError as exc:
                raise AssertionError(label) from exc

    def test_no_frontmatter_gets_minimal_marker_block(self) -> None:
        raw = "Body without frontmatter.\n---\nThis delimiter belongs to the body.\n"

        stamped = _context_content_with_provenance(raw, "plain-source")

        assert _context_source_stem(stamped) == "plain-source"
        assert stamped == f"---\n{_quoted_marker_line('plain-source')}---\n{raw}"

    def test_existing_marker_line_is_replaced_in_place(self) -> None:
        raw = (
            "---\n"
            "name: replace-agent\n"
            f"{_CONTEXT_SOURCE_STEM_KEY}: old-source\n"
            "description: Replacement keeps neighboring lines\n"
            "---\n"
            "Body\n"
        )

        stamped = _context_content_with_provenance(raw, "new-source")

        assert _context_source_stem(stamped) == "new-source"
        assert stamped == raw.replace(
            f"{_CONTEXT_SOURCE_STEM_KEY}: old-source\n",
            _quoted_marker_line("new-source"),
            1,
        )
        assert stamped.count(f"{_CONTEXT_SOURCE_STEM_KEY}:") == 1

    def test_duplicate_plain_marker_lines_are_all_removed_before_reinsertion(self) -> None:
        """Two competing UNQUOTED marker lines are a fixable case, not just a
        raise-worthy one: both are deleted and one clean line is inserted, so
        the reader's last-wins duplicate-key resolution can never disagree
        with what CAO intended to stamp."""
        raw = (
            "---\n"
            f"{_CONTEXT_SOURCE_STEM_KEY}: 'profile-b'\n"
            "name: shared\n"
            f"{_CONTEXT_SOURCE_STEM_KEY}: 'z'\n"
            "description: D\n"
            "---\n"
            "Body\n"
        )

        stamped = _context_content_with_provenance(raw, "profile-a")

        assert _context_source_stem(stamped) == "profile-a"
        assert stamped.count(f"{_CONTEXT_SOURCE_STEM_KEY}:") == 1
        post = frontmatter.loads(stamped)
        assert post.metadata["name"] == "shared"
        assert post.metadata["description"] == "D"


class TestProvenanceMarkerSpoofRefused:
    """R1: a marker spelling the textual regex cannot see must not silently
    win over the one CAO wrote. Round 3's frontmatter.loads/dumps round-trip
    gave this property for free (a dict assignment collapses every spelling
    of the key into one entry); the textual inserter must re-earn it by
    verifying its own output through the same YAML path the guard reads.
    Every shape here reads back a value CAO never wrote if the write is
    trusted blindly, so the install must be refused instead of silently
    stamping (and therefore trusting) the wrong stem.
    """

    @pytest.mark.parametrize(
        "label,extra_frontmatter_line",
        [
            ("double-quoted-key", "\"x-cao-source-stem\": 'bbb'\n"),
            ("single-quoted-key", "'x-cao-source-stem': 'bbb'\n"),
        ],
    )
    def test_quoted_key_spoof_refuses_install(
        self, label: str, extra_frontmatter_line: str
    ) -> None:
        raw = f"---\nname: shared\ndescription: A\n{extra_frontmatter_line}---\nA BODY\n"

        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(raw, "aaa")

    def test_flow_mapping_frontmatter_refuses_install(self) -> None:
        raw = "---\n{x-cao-source-stem: profile-b, name: shared}\n---\nBody\n"

        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(raw, "profile-a")

    def test_folded_scalar_marker_value_refuses_install(self) -> None:
        """R3: a multi-line marker value would otherwise leave an orphaned
        continuation line (invalid YAML) in the written copy. Refusing here
        means the corrupt copy is never written, so reinstall is never
        permanently blocked by CAO's own output."""
        raw = "---\nx-cao-source-stem: >\n  old\nname: foo\n---\nbody\n"

        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(raw, "mystem")

    def test_end_to_end_install_of_spoofed_profile_fails_cleanly(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """CLI-level: a profile carrying a quoted-key marker must fail to
        install at all, rather than install successfully with a provenance
        marker that lies about its own stem (the exact precondition the R1
        end-to-end silent-overwrite scenario in the review depends on)."""
        store = workspace["local_store"]
        context_dir = workspace["context_dir"]
        (store / "aaa.md").write_text(
            "---\nname: shared\ndescription: A\n\"x-cao-source-stem\": 'bbb'\n---\nA BODY\n",
            encoding="utf-8",
        )

        result = _install(runner, "aaa")

        assert result.exit_code == 0  # failure result, not a crash
        assert "Error:" in result.output, result.output
        assert not (context_dir / "shared.md").exists()
        assert not (workspace["agents_dir"] / "shared.md").exists()

    def test_trap_case_still_raises_when_a_never_installed_due_to_spoof(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """End-to-end regression proof for the review's exact repro: since A
        (carrying the spoofed marker) never installs at all, B's later
        install of the same resolved name has nothing to collide with and
        must succeed cleanly — the opposite of a silent overwrite, and not a
        crash either."""
        store = workspace["local_store"]
        agents_dir = workspace["agents_dir"]

        (store / "aaa.md").write_text(
            "---\nname: shared\ndescription: A\n\"x-cao-source-stem\": 'bbb'\n---\nA BODY\n",
            encoding="utf-8",
        )
        rA = _install(runner, "aaa")
        assert rA.exit_code == 0
        assert "Error:" in rA.output, rA.output
        assert not (agents_dir / "shared.md").exists()

        (store / "aaa.md").unlink()
        _write_profile(store / "bbb.md", name="shared", body="B BODY")
        rB = _install(runner, "bbb")

        assert rB.exit_code == 0 and "Error:" not in rB.output, rB.output
        assert "B BODY" in (agents_dir / "shared.md").read_text()


class TestFourDashFrontmatterDelimiter:
    """R2: python-frontmatter accepts 3+ dashes as a delimiter; the writer
    must recognise the same shapes or it demotes real frontmatter into the
    body."""

    def test_four_dash_delimiters_are_recognized_as_frontmatter(self) -> None:
        raw = "----\nname: dashy-name\ndescription: D\n----\nBODY\n"

        stamped = _context_content_with_provenance(raw, "dashy")

        assert _context_source_stem(stamped) == "dashy"
        post = frontmatter.loads(stamped)
        assert post.metadata["name"] == "dashy-name"
        assert post.metadata["description"] == "D"
        assert post.content.strip() == "BODY"

    def test_four_dash_delimiters_preserve_bytes_outside_marker(self) -> None:
        raw = "----\nname: dashy-name\ndescription: D\n----\nBODY\n"

        stamped = _context_content_with_provenance(raw, "dashy")

        _assert_inserted_marker_only(raw, stamped, "dashy")

    def test_end_to_end_install_recognizes_four_dash_frontmatter(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        (store / "dashy.md").write_text(
            "----\nname: dashy-name\ndescription: D\n----\nBODY\n", encoding="utf-8"
        )

        result = _install(runner, "dashy")

        assert result.exit_code == 0 and "Error:" not in result.output, result.output
        context_text = (workspace["context_dir"] / "dashy-name.md").read_text(encoding="utf-8")
        post = frontmatter.loads(context_text)
        assert post.metadata["name"] == "dashy-name"
        assert post.metadata["description"] == "D"


class TestFrontmatterlessBodyOpeningWithDashRule:
    """Regression: the R2 delimiter widening (``^-{3,}$``) made
    ``_find_frontmatter_block`` open a "frontmatter block" at ANY leading
    line of 3+ dashes, including a markdown thematic-break rule in a
    frontmatter-less document. The marker then got inserted into the middle
    of prose, the assembled content was invalid YAML, and the readback gate
    (correctly) refused the install — but the refusal took down a profile
    shape that fixup 4 (and pre-PR CAO) installed successfully. A leading
    dash line only counts as frontmatter if what follows it actually parses
    as a YAML mapping; a bare markdown rule does not, and must fall back to
    the ordinary "no leading block -> prepend a clean one" path.
    """

    def test_three_dash_rule_body_installs_and_copy_parses(self) -> None:
        raw = "---\n\n# My Agent\n\nDoes stuff.\n\n---\n\n## Details\n"

        stamped = _context_content_with_provenance(raw, "profile-a")

        assert _context_source_stem(stamped) == "profile-a"
        post = frontmatter.loads(stamped)  # must not raise
        assert post.metadata[_CONTEXT_SOURCE_STEM_KEY] == "profile-a"
        # Prepend form: original bytes intact after the inserted block.
        assert stamped == f"---\n{_quoted_marker_line('profile-a')}---\n{raw}"

    def test_four_dash_rule_body_installs_and_copy_parses(self) -> None:
        raw = "----\n\n# My Agent\n\nDoes stuff.\n\n----\n\n## Details\n"

        stamped = _context_content_with_provenance(raw, "profile-a")

        assert _context_source_stem(stamped) == "profile-a"
        frontmatter.loads(stamped)  # must not raise
        assert stamped == f"---\n{_quoted_marker_line('profile-a')}---\n{raw}"

    def test_end_to_end_install_of_dash_rule_body_succeeds(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        store_agent = store / "ruled.md"
        store_agent.write_text(
            "----\n\n# My Agent\n\nDoes stuff.\n\n----\n\n## Details\n", encoding="utf-8"
        )

        result = _install(runner, "ruled")

        assert result.exit_code == 0 and "Error:" not in result.output, result.output
        context_copy = workspace["context_dir"] / "ruled.md"
        assert context_copy.exists()
        # The written copy must itself round-trip through the parser CAO
        # uses everywhere else, not merely "the CLI exited 0".
        post = frontmatter.loads(context_copy.read_text(encoding="utf-8"))
        assert post.metadata[_CONTEXT_SOURCE_STEM_KEY] == "ruled"

    def test_indented_frontmatter_keys_install_and_copy_parses(self) -> None:
        """Real frontmatter whose keys are indented (still a valid YAML
        mapping) must have the marker inserted at the SAME indentation, not
        column 0 — a column-0 insertion breaks the block's indentation
        consistency and corrupts the YAML."""
        raw = "---\n name: indented-name\n description: D\n---\nBody\n"

        stamped = _context_content_with_provenance(raw, "profile-a")

        assert _context_source_stem(stamped) == "profile-a"
        post = frontmatter.loads(stamped)
        assert post.metadata["name"] == "indented-name"
        assert post.metadata["description"] == "D"
        assert post.content.strip() == "Body"
        _assert_inserted_marker_only(raw, stamped, "profile-a")

    def test_end_to_end_install_of_indented_frontmatter_succeeds(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        (store / "indented.md").write_text(
            "---\n name: indented-name\n description: D\n---\nBody\n", encoding="utf-8"
        )

        result = _install(runner, "indented")

        assert result.exit_code == 0 and "Error:" not in result.output, result.output
        context_copy = workspace["context_dir"] / "indented-name.md"
        post = frontmatter.loads(context_copy.read_text(encoding="utf-8"))
        assert post.metadata["name"] == "indented-name"
        assert post.metadata[_CONTEXT_SOURCE_STEM_KEY] == "indented"

    def test_refusal_message_names_real_cause_not_a_nonexistent_key(self) -> None:
        """A profile that genuinely has no ``x-cao-source-stem`` key must
        never be told to go remove one if it is (still, safely) refused."""
        raw = "---\nfoo: |\n  ----\nx-cao-source-stem: 'old'\n---\nBody\n"

        with pytest.raises(ValueError) as excinfo:
            _context_content_with_provenance(raw, "profile-a")

        message = str(excinfo.value)
        # This shape DOES carry a real conflicting key, so blaming it is
        # accurate here — the point of the assertion is that the message
        # names the readback's actual disagreement, not a boilerplate guess.
        assert "reads back" in message
        assert "'profile-a'" in message

    def test_spoof_matrix_stays_closed(self) -> None:
        """The mapping-validity check must not reopen any previously-closed
        spoofing shape: quoted key, duplicate plain marker, folded value,
        flow mapping, and the 4-dash delimiter must all behave exactly as
        before this fix."""
        control = _context_content_with_provenance("---\nname: a\n---\nBody\n", "profile-a")
        assert _context_source_stem(control) == "profile-a"

        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(
                '---\n"x-cao-source-stem": "evil"\nname: a\n---\nBody\n', "profile-a"
            )
        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(
                "---\n'x-cao-source-stem': 'evil'\nname: a\n---\nBody\n", "profile-a"
            )
        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(
                "---\nx-cao-source-stem: >\n  evil\nname: a\n---\nBody\n", "profile-a"
            )
        with pytest.raises(ValueError, match=_CONTEXT_SOURCE_STEM_KEY):
            _context_content_with_provenance(
                "---\n{x-cao-source-stem: evil, name: a}\n---\nBody\n", "profile-a"
            )

        duplicate = _context_content_with_provenance(
            "---\nx-cao-source-stem: evil\nname: a\nx-cao-source-stem: evil2\n---\nBody\n",
            "profile-a",
        )
        assert _context_source_stem(duplicate) == "profile-a"

        four_dash = _context_content_with_provenance("----\nname: a\n----\nBody\n", "profile-a")
        assert _context_source_stem(four_dash) == "profile-a"


class TestContextFileModePreservation:
    """R4: os.replace() carries the temp file's mode (always 0600 from
    tempfile.NamedTemporaryFile) onto the target, so without restoring the
    mode first, every reinstall silently tightens an existing copy's
    permissions and every brand-new copy is 0600 instead of umask-derived."""

    def test_reinstall_preserves_existing_target_mode(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        _write_profile(workspace["local_store"] / "mode-agent.md", name="mode-agent")
        r1 = _install(runner, "mode-agent")
        assert r1.exit_code == 0 and "Error:" not in r1.output, r1.output

        context_copy = workspace["context_dir"] / "mode-agent.md"
        os.chmod(context_copy, 0o644)

        r2 = _install(runner, "mode-agent")
        assert r2.exit_code == 0 and "Error:" not in r2.output, r2.output

        mode = stat.S_IMODE(context_copy.stat().st_mode)
        assert mode == 0o644, oct(mode)

    def test_brand_new_copy_uses_umask_default_not_hardcoded_0600(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        _write_profile(workspace["local_store"] / "new-agent.md", name="new-agent")

        old_umask = os.umask(0o022)
        try:
            result = _install(runner, "new-agent")
        finally:
            os.umask(old_umask)

        assert result.exit_code == 0 and "Error:" not in result.output, result.output
        context_copy = workspace["context_dir"] / "new-agent.md"
        mode = stat.S_IMODE(context_copy.stat().st_mode)
        assert mode == 0o644, oct(mode)


class TestReadOnlyContextDirErrorNamesRealTarget:
    """R5: a read-only context dir must report the target path the user
    actually cares about, not tempfile's internal randomly-named ``.tmp``
    file (which no longer exists by the time the error is shown)."""

    def test_permission_denied_names_target_not_temp_file(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        _write_profile(workspace["local_store"] / "ro-agent.md", name="ro-agent")
        context_dir = workspace["context_dir"]
        os.chmod(context_dir, 0o500)
        try:
            result = _install(runner, "ro-agent")
        finally:
            os.chmod(context_dir, 0o700)

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        assert str(context_dir / "ro-agent.md") in result.output
        assert ".tmp" not in result.output
