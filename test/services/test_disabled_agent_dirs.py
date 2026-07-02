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
    """The disable check normalizes paths (GH #280/#281), so a directory is still
    skipped when the disabled entry is spelled differently from the scanned path."""

    def test_differing_spelling_still_skips(self, isolated_settings, monkeypatch):
        d = isolated_settings / "extra"
        d.mkdir()
        _profile(d, "zzspelling", "s")
        svc.set_extra_agent_dirs([str(d)])  # configured WITHOUT a trailing slash

        # Sanity: visible while enabled.
        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zzspelling" in names

        # Disable it with a DIFFERENT spelling (trailing slash). set_disabled_agent_dirs
        # validates raw strings, so we bypass it here to isolate the scan-side
        # normalization we actually want to prove — this is the part the UI relies on.
        monkeypatch.setattr(svc, "get_disabled_agent_dirs", lambda: [str(d) + "/"])

        names = {p["name"] for p in agent_profiles.list_agent_profiles()}
        assert "zzspelling" not in names  # normalization matched despite the slash
        with pytest.raises(FileNotFoundError):
            agent_profiles._read_agent_profile_source("zzspelling")
