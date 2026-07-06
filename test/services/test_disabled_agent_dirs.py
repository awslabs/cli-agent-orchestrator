"""Enable/disable an agent-profile directory without removing it (GH #280, #281).

Covers the settings persistence, the scan-time skip (list_agent_profiles), the
load-time skip (_read_agent_profile_source), the same-name conflict flag
(duplicated_in), and the #281 regression: a disabled default stays *listed* but
is skipped during scanning.
"""

import pytest

from cli_agent_orchestrator.services import settings_service as svc
from cli_agent_orchestrator.utils import agent_profiles


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point settings at a throwaway file so tests never touch real config."""
    monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json")
    return tmp_path


def _profile(dir_path, name, description):
    (dir_path / f"{name}.md").write_text(
        f"---\ndescription: {description}\n---\nbody-{description}"
    )


class TestDisabledDirsPersistence:
    def test_roundtrip_and_default_empty(self, isolated_settings):
        assert svc.get_disabled_agent_dirs() == []
        extra = isolated_settings / "extra"
        extra.mkdir()
        svc.set_extra_agent_dirs([str(extra)])
        assert svc.set_disabled_agent_dirs([str(extra)]) == [str(extra)]
        assert svc.get_disabled_agent_dirs() == [str(extra)]

    def test_rejects_unknown_paths_and_dedupes(self, isolated_settings):
        extra = isolated_settings / "extra"
        extra.mkdir()
        svc.set_extra_agent_dirs([str(extra)])
        # unknown path is dropped; known path kept once despite duplicate
        result = svc.set_disabled_agent_dirs(["/not/configured", str(extra), str(extra)])
        assert result == [str(extra)]

    def test_default_path_can_be_disabled(self, isolated_settings):
        # a provider default is a valid disable target (this is the #281 fix)
        any_default = next(iter(svc.get_agent_dirs().values()))
        assert svc.set_disabled_agent_dirs([any_default]) == [any_default]


class TestScanSkip:
    def test_disable_swaps_which_same_named_profile_wins(self, isolated_settings):
        a = isolated_settings / "teamA"
        b = isolated_settings / "teamB"
        a.mkdir()
        b.mkdir()
        _profile(a, "zztoggle", "from-A")
        _profile(b, "zztoggle", "from-B")
        svc.set_extra_agent_dirs([str(a), str(b)])

        # A scanned first -> A wins, and the name is flagged as duplicated.
        profs = {p["name"]: p for p in agent_profiles.list_agent_profiles()}
        assert profs["zztoggle"]["description"] == "from-A"
        assert profs["zztoggle"]["duplicated_in"]  # non-empty -> shadowed elsewhere

        # Disable A -> B now wins and the duplicate flag clears.
        svc.set_disabled_agent_dirs([str(a)])
        profs = {p["name"]: p for p in agent_profiles.list_agent_profiles()}
        assert profs["zztoggle"]["description"] == "from-B"
        assert profs["zztoggle"]["duplicated_in"] == []

        # The load path honours the toggle too (not just the listing).
        assert "body-from-B" in agent_profiles._read_agent_profile_source("zztoggle")

        # Disable both -> the profile disappears entirely.
        svc.set_disabled_agent_dirs([str(a), str(b)])
        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zztoggle" not in names

    def test_unique_profile_has_empty_duplicated_in(self, isolated_settings):
        a = isolated_settings / "solo"
        a.mkdir()
        _profile(a, "zzsolo", "only")
        svc.set_extra_agent_dirs([str(a)])
        profs = {p["name"]: p for p in agent_profiles.list_agent_profiles()}
        assert profs["zzsolo"]["duplicated_in"] == []


class TestDefaultRemovalRegression:
    """#281: removing (disabling) a default persists and skips it during scan,
    while the default stays LISTED so the UI can offer to re-enable it."""

    def test_disabled_default_listed_but_skipped(self, isolated_settings):
        d = isolated_settings / "kiro"
        d.mkdir()
        _profile(d, "zzkirodefault", "k")
        # Override the kiro_cli default to our temp dir, then confirm scanning.
        svc.set_agent_dirs({"kiro_cli": str(d)})
        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zzkirodefault" in names

        svc.set_disabled_agent_dirs([str(d)])
        # Still returned by get_agent_dirs (so the UI keeps showing it)...
        assert str(d) in svc.get_agent_dirs().values()
        # ...but its profiles are gone from the active set.
        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zzkirodefault" not in names


class TestNormalizedPathMatching:
    """SET and SCAN/LOAD share the same path normalization (GH #280/#281): a
    valid directory sent in a different spelling is accepted by the setter
    (persisted as the configured spelling) and skipped by scan + load."""

    def test_differing_spelling_accepted_and_skips_end_to_end(self, isolated_settings):
        d = isolated_settings / "extra"
        d.mkdir()
        _profile(d, "zzspelling", "s")
        svc.set_extra_agent_dirs([str(d)])  # configured WITHOUT a trailing slash

        # Sanity: visible while enabled.
        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zzspelling" in names

        # Disable with a DIFFERENT spelling (trailing slash) through the REAL
        # setter — validation normalizes both sides, and the configured
        # spelling is what gets persisted (so UI exact-string matching works).
        stored = svc.set_disabled_agent_dirs([str(d) + "/"])
        assert stored == [str(d)]

        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zzspelling" not in names  # normalization matched despite the slash
        with pytest.raises(FileNotFoundError):
            agent_profiles._read_agent_profile_source("zzspelling")


class TestSharedPathDedup:
    """One physical directory configured under several names (claude_code and
    codex share the agent-store by default) is scanned once — no duplicated_in
    false positive, and disabling it via either spelling works."""

    def test_two_providers_same_dir_scanned_once(self, isolated_settings):
        d = isolated_settings / "store"
        d.mkdir()
        _profile(d, "zzshared", "one-copy")
        svc.set_agent_dirs({"claude_code": str(d), "codex": str(d)})

        profs = {p["name"]: p for p in agent_profiles.list_agent_profiles()}
        assert profs["zzshared"]["duplicated_in"] == []  # scanned once, not twice

    def test_md_file_and_subdir_same_name_not_flagged(self, isolated_settings):
        """A dir holding both <name>.md and <name>/ counts once for
        duplicated_in — cosmetic edge from review round 2."""
        d = isolated_settings / "mixed"
        d.mkdir()
        _profile(d, "zzmixed", "flat")
        (d / "zzmixed").mkdir()
        (d / "zzmixed" / "agent.md").write_text("---\ndescription: nested\n---\nn")
        svc.set_extra_agent_dirs([str(d)])

        profs = {p["name"]: p for p in agent_profiles.list_agent_profiles()}
        assert profs["zzmixed"]["duplicated_in"] == []


class TestLoadPathBranches:
    """The disable toggle is honoured on EVERY load branch, not just extras."""

    def test_disabled_provider_default_skipped_at_load(self, isolated_settings):
        d = isolated_settings / "kiro"
        d.mkdir()
        _profile(d, "zzloadprov", "k")
        svc.set_agent_dirs({"kiro_cli": str(d)})
        assert "body-k" in agent_profiles._read_agent_profile_source("zzloadprov")

        svc.set_disabled_agent_dirs([str(d)])
        with pytest.raises(FileNotFoundError):
            agent_profiles._read_agent_profile_source("zzloadprov")

    def test_disabled_local_store_skipped_at_load(self, isolated_settings, monkeypatch):
        local = isolated_settings / "local-store"
        local.mkdir()
        _profile(local, "zzloadlocal", "l")
        monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", local)
        assert "body-l" in agent_profiles._read_agent_profile_source("zzloadlocal")

        # The local store is disableable via the matching provider-default path;
        # configure it as one so the setter accepts it, then disable.
        svc.set_agent_dirs({"claude_code": str(local)})
        svc.set_disabled_agent_dirs([str(local)])
        with pytest.raises(FileNotFoundError):
            agent_profiles._read_agent_profile_source("zzloadlocal")


class TestStaleDisabledPruning:
    """Removing an extra dir prunes its disabled entry, so re-adding the path
    later does not come back silently pre-disabled (review round 2)."""

    def test_removed_extra_dir_prunes_disabled_entry(self, isolated_settings):
        d = isolated_settings / "extra"
        d.mkdir()
        svc.set_extra_agent_dirs([str(d)])
        svc.set_disabled_agent_dirs([str(d)])
        assert svc.get_disabled_agent_dirs() == [str(d)]

        # Remove the extra dir entirely -> its disabled entry is pruned.
        svc.set_extra_agent_dirs([])
        assert svc.get_disabled_agent_dirs() == []

        # Re-adding the path later starts ENABLED.
        svc.set_extra_agent_dirs([str(d)])
        assert svc.get_disabled_agent_dirs() == []
