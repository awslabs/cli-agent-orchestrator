"""Per-launch Kiro homes for ephemeral helpers, on v3 (#1167 Phase 2, #1074 phase 5).

kiro-cli is never run here, and nothing under the real ``~/.kiro`` is touched:
every test runs with a fake ``HOME`` (``operator_home``) whose ``~/.kiro`` only
``stat`` may reach (G19b). The rule strings are provisional (#1167, #1074): the
tests check which rule a refusal names, not its wording.
"""

from __future__ import annotations

import errno
import functools
import hashlib
import itertools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from test.providers.kiro_helper_support import operator_home  # noqa: F401
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.providers import kiro_launch_home as k
from cli_agent_orchestrator.providers.kiro_v3_schema import (
    SUPPORTED_KIRO_CLI_VERSIONS,
    pinned_kiro_v3_schemas,
)
from cli_agent_orchestrator.services import ephemeral_service, install_service
from cli_agent_orchestrator.services.kiro_v3_validate import validate_kiro_v3_agent_bytes
from cli_agent_orchestrator.utils import private_home

TID = "0a1b2c3d"
AGENT = "Heart-log_triage-3f9a"
PROFILE = AgentProfile(
    name=AGENT, description="Log triage", system_prompt="You are Heart.\n[[BEGIN CREATOR BRIEF]]"
)
ALL_ATOM_SETS = [
    list(combo)
    for size in range(len(TOOL_ATOMS) + 1)
    for combo in itertools.combinations(TOOL_ATOMS, size)
]
SCHEMAS = {entry.schema.engine: entry.schema for entry in SUPPORTED_KIRO_CLI_VERSIONS.values()}
SCHEMA = SUPPORTED_KIRO_CLI_VERSIONS["2.35.1"].schema
# Every capability Phase 1 denies to an agent with no grant at all.
EVERY_CAPABILITY = {
    "context",
    "diagnostics",
    "fs_read",
    "fs_write",
    "mcp",
    "power",
    "shell",
    "skill",
    "subagent",
    "web_fetch",
    "web_search",
    "workflow",
}


def _unseal_tree(root: Path) -> None:
    """Give every 0000 placeholder under ``root`` its owner bits back, so tmp cleanup works."""
    for directory, dirnames, _ in os.walk(root):
        for name in dirnames:
            path = Path(directory) / name
            if not path.is_symlink() and stat.S_IMODE(os.lstat(path).st_mode) == 0:
                path.chmod(0o700)


def home_tree(root: Path) -> dict[str, tuple]:
    """Every entry under ``root`` by lstat: type, mode and inode, and a file's bytes.

    A directory this user cannot list (the 0000 placeholder) is recorded, not entered.
    """
    found: dict[str, tuple] = {}

    def visit(path: Path, name: str) -> None:
        info = os.lstat(path)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            found[name] = ("file", mode, info.st_ino, path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            found[name] = ("dir", mode, info.st_ino)
            if mode & stat.S_IRUSR and mode & stat.S_IXUSR:
                for child in sorted(os.listdir(path)):
                    visit(path / child, f"{name}/{child}")
        else:
            found[name] = ("other", mode, info.st_ino)

    visit(root, ".")
    return found


@pytest.fixture
def store(tmp_path, monkeypatch, operator_home):  # noqa: F811
    """A CAO ephemeral store under tmp_path; the Kiro root is its realpath."""
    ephemeral = tmp_path / "cao" / "ephemeral"
    ephemeral.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(ephemeral_service, "EPHEMERAL_DIR", ephemeral)
    yield ephemeral
    k.discard_launch_home(TID)
    _unseal_tree(ephemeral)


def snapshot():
    return k.snapshot_kiro_global_home(k.server_home(), placeholder_listable=False)


def grant_of(atoms=("fs_read", "fs_list")):
    return k.kiro_helper_grant(list(atoms))


def prepare(atoms=("fs_read", "fs_list"), terminal_id=TID):
    payload = k.render_helper_agent(AGENT, PROFILE, grant_of(atoms), terminal_id)
    return k.create_launch_home(terminal_id, AGENT, payload, snapshot())


def replace_with_same_bytes(path: Path) -> None:
    """Put a new file with the same bytes at ``path``, by rename, so its inode differs.

    Unlink and write again can get the freed inode back at once (ext4), and then
    nothing the check can see has changed.
    """
    spare = path.with_name(path.name + ".spare")
    spare.write_bytes(path.read_bytes())
    os.replace(spare, path)


def refusal(callable_, *args, **kwargs) -> str:
    with pytest.raises(k.KiroLaunchRefusal) as err:
        callable_(*args, **kwargs)
    return err.value.rule


class TestPaths:
    def test_the_home_is_the_terminal_id_under_the_realpath_root(self, store):
        home = k.kiro_home_path(TID)
        assert home == Path(os.path.realpath(store)) / "kiro" / TID
        assert k.kiro_probe_home() == Path(os.path.realpath(store)) / "kiro-probe"

    @pytest.mark.parametrize("bad", ["", "0A1B2C3D", "0a1b2c3", "0a1b2c3d4", "../x", None, 7])
    def test_only_an_eight_hex_terminal_id_names_a_home(self, store, bad):
        assert not k.valid_terminal_id(bad)
        with pytest.raises(ValueError):
            k.kiro_home_path(bad)


class TestResolveKiroCli:
    def test_missing_kiro_cli_is_unresolved(self):
        assert refusal(k.resolve_kiro_cli, lambda _: None) == "kiro_cli_unresolved"

    def test_a_relative_path_entry_is_refused(self, tmp_path, monkeypatch):
        # A real executable at the relative path: only the absolute-path rule refuses it.
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "kiro-cli").write_text("#!/bin/sh\n")
        (tmp_path / "bin" / "kiro-cli").chmod(0o755)
        monkeypatch.chdir(tmp_path)
        assert refusal(k.resolve_kiro_cli, lambda _: "bin/kiro-cli") == "kiro_cli_unresolved"

    def test_the_link_is_kept_and_its_target_recorded(self, tmp_path):
        target = tmp_path / "bundle" / "kiro-cli"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        link = tmp_path / "bin" / "kiro-cli"
        link.parent.mkdir()
        link.symlink_to(target)
        resolved = k.resolve_kiro_cli(lambda _: str(link))
        assert resolved == k.ResolvedKiroCli(path=str(link), target=os.path.realpath(target))

    def test_a_path_holding_equals_is_refused(self, tmp_path):
        """/usr/bin/env would read ``…/dir=x/kiro-cli`` as an assignment, not a program."""
        target = tmp_path / "dir=x" / "kiro-cli"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        with pytest.raises(k.KiroLaunchRefusal) as err:
            k.resolve_kiro_cli(lambda _: str(target))
        assert err.value.rule == "kiro_cli_unresolved"
        assert str(tmp_path) not in str(err.value)

    @pytest.mark.parametrize("mode", [0o775, 0o757, 0o644])
    def test_a_writable_or_non_executable_target_is_refused(self, tmp_path, mode):
        target = tmp_path / "kiro-cli"
        target.write_text("#!/bin/sh\n")
        target.chmod(mode)
        assert refusal(k.resolve_kiro_cli, lambda _: str(target)) == "kiro_cli_unresolved"


class TestGrant:
    @pytest.mark.parametrize("atoms", ALL_ATOM_SETS, ids=lambda a: "+".join(a) or "none")
    def test_every_atom_set_gains_cao_mcp_server_and_nothing_else(self, atoms):
        assert k.kiro_helper_grant(atoms) == sorted(set(atoms) | {"@cao-mcp-server"})
        assert k.kiro_helper_grant(atoms + ["@cao-mcp-server"]) == k.kiro_helper_grant(atoms)

    @pytest.mark.parametrize("bad", [["*"], ["fs_read", "fs_*"], ["@other"], ["read"], ["shell"]])
    def test_a_wildcard_or_non_atom_refuses(self, bad):
        assert refusal(k.kiro_helper_grant, bad) == "kiro_agent_invalid:tools"


class TestRender:
    """G16: Phase 1's universal file, from the core ``cao install`` uses."""

    @pytest.mark.parametrize("atoms", ALL_ATOM_SETS, ids=lambda a: "+".join(a) or "none")
    def test_the_helper_file_is_cao_installs_render_for_the_same_grant(self, store, atoms):
        grant = grant_of(atoms)
        raw = k.render_helper_agent(AGENT, PROFILE, grant, TID)
        data = json.loads(raw)
        server = data["mcpServers"]["cao-mcp-server"]
        installed = json.loads(
            install_service._render_kiro_universal_agent(
                AgentProfile(
                    name=AGENT,
                    description="Log triage",
                    mcpServers={"cao-mcp-server": dict(server)},
                ),
                grant,
                [],
                None,
                AGENT,
            )
        )
        for key in ("tools", "excludedTools", "permissions", "allowedTools", "mcpServers"):
            assert data[key] == installed[key], key
        assert data["allowedTools"] == grant
        for schema in (*pinned_kiro_v3_schemas(), *SCHEMAS.values()):
            assert (
                validate_kiro_v3_agent_bytes(
                    raw, schema, expected_name=AGENT, launch_allowlist=grant
                )
                == []
            )

    def test_the_helper_file_names_only_cao_mcp_server_and_carries_no_extras(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("CAO_API_PORT", "50123")
        data = json.loads(k.render_helper_agent(AGENT, PROFILE, grant_of(), TID))
        assert data["name"] == AGENT and data["prompt"] == PROFILE.system_prompt
        assert data["description"] == PROFILE.description
        assert data["includeMcpJson"] is False and "useLegacyMcpJson" not in data
        assert data["resources"] == []
        assert not {
            "model",
            "hooks",
            "toolsSettings",
            "toolAliases",
            "includePowers",
            "kiroUpgrade",
        } & set(data)
        (name, server), *rest = data["mcpServers"].items()
        assert name == "cao-mcp-server" and not rest
        assert os.path.isabs(server["command"])
        assert server["env"]["CAO_TERMINAL_ID"] == TID
        assert server["env"]["CAO_API_PORT"] == "50123"
        assert server["timeout"] >= 1_200_000

    def test_the_sentinel_has_no_tool_and_denies_every_capability(self):
        raw = k.render_sentinel_agent()
        data = json.loads(raw)
        assert (data["name"], data["description"], data["prompt"]) == (
            "cao-null",
            k.SENTINEL_DESCRIPTION,
            k.SENTINEL_PROMPT,
        )
        assert data["tools"] == [] and data["allowedTools"] == []
        assert data["excludedTools"] == ["knowledge"]
        assert data["resources"] == [] and data["includeMcpJson"] is False
        assert "mcpServers" not in data and "includePowers" not in data
        rules = data["permissions"]["rules"]
        assert all(rule == {"capability": rule["capability"], "effect": "deny"} for rule in rules)
        assert {rule["capability"] for rule in rules} == EVERY_CAPABILITY
        for schema in (*pinned_kiro_v3_schemas(), *SCHEMAS.values()):
            assert (
                validate_kiro_v3_agent_bytes(
                    raw,
                    schema,
                    expected_name="cao-null",
                    launch_allowlist=[],
                    fallback_sentinel=True,
                )
                == []
            )

    def test_the_sentinel_option_admits_only_cao_null_with_the_empty_grant(self, store):
        sentinel = k.render_sentinel_agent()
        # Without it, cao-null is a fallback name like any other.
        assert [
            f.code
            for f in validate_kiro_v3_agent_bytes(
                sentinel, SCHEMA, expected_name="cao-null", launch_allowlist=[]
            )
        ] == ["fallback_name"]
        helper = k.render_helper_agent(AGENT, PROFILE, grant_of(), TID)
        for raw, name, allowlist in (
            (helper, AGENT, grant_of()),
            (helper, "cao-null", []),
            (sentinel, "cao-null", ["fs_read"]),
            (sentinel, AGENT, []),
        ):
            findings = validate_kiro_v3_agent_bytes(
                raw, SCHEMA, expected_name=name, launch_allowlist=allowlist, fallback_sentinel=True
            )
            assert findings, (name, allowlist)

    def test_a_relative_mcp_command_refuses(self, store, monkeypatch):
        monkeypatch.setattr(
            k,
            "resolve_mcp_server_config",
            lambda *a, **kw: {"command": "cao-mcp-server", "args": []},
        )
        assert (
            refusal(k.render_helper_agent, AGENT, PROFILE, grant_of(), TID)
            == "kiro_agent_invalid:mcp"
        )


class TestCreate:
    def test_the_home_has_exactly_its_entries_and_modes(self, store):
        prepared = prepare()
        home = prepared.home
        assert stat.S_IMODE(os.lstat(home).st_mode) == 0o700
        assert sorted(os.listdir(home)) == ["agents", "powers", "secrets.json", "settings"]
        assert sorted(os.listdir(home / "agents")) == [f"{AGENT}.json", "cao-null.json"]
        assert os.listdir(home / "settings") == ["cli.json"]
        for entry in (*prepared.files, prepared.secrets):
            path = home / entry.name
            info = os.lstat(path)
            assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
            assert (info.st_dev, info.st_ino) == (entry.device, entry.inode)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry.sha256
        assert set(prepared.hashes()) == {
            "settings/cli.json",
            "agents/cao-null.json",
            f"agents/{AGENT}.json",
            "secrets.json",
        }
        assert json.loads((home / "secrets.json").read_text()) == {}
        assert k.prepared_launch_home(TID) == prepared

    def test_the_seed_is_k2s_four_keys_byte_for_byte(self, store):
        prepared = prepare()
        assert k.SETTINGS_SEED == {
            "chat.enableAutoAgentUpgrade": False,
            "chat.disableTrustAllConfirmation": True,
            "chat.greeting.enabled": False,
            "chat.defaultAgent": "cao-null",
        }
        raw = (prepared.home / "settings" / "cli.json").read_bytes()
        assert raw == (json.dumps(k.SETTINGS_SEED, indent=2) + "\n").encode()

    def test_the_powers_placeholder_is_empty_unlistable_and_recorded(self, store):
        prepared = prepare()
        info = os.lstat(prepared.home / "powers")
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0
        assert k.PlaceholderDir.of(info) == prepared.powers
        assert prepared.powers.uid == os.geteuid() and prepared.powers.mode == 0
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                os.listdir(prepared.home / "powers")

    @pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling"])
    def test_an_entry_at_the_path_refuses_and_is_left_alone(self, store, tmp_path, kind):
        home = k.kiro_home_path(TID)
        home.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if kind == "directory":
            home.mkdir()
            (home / "planted").write_text("x")
        elif kind == "file":
            home.write_text("planted")
        elif kind == "symlink":
            (tmp_path / "elsewhere").mkdir()
            home.symlink_to(tmp_path / "elsewhere")
        else:
            home.symlink_to(tmp_path / "missing")
        before = os.lstat(home)
        assert refusal(prepare) == "kiro_home_invalid:exists"
        after = os.lstat(home)
        assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
        assert k.prepared_launch_home(TID) is None

    @pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
    def test_a_failed_write_leaves_nothing_behind(self, store, monkeypatch, fail_at):
        real, calls = k.write_exclusive, []

        def failing(path, payload, owned):
            calls.append(path)
            if len(calls) == fail_at:
                raise OSError("disk full")
            real(path, payload, owned)

        monkeypatch.setattr(k, "write_exclusive", failing)
        assert refusal(prepare) == "kiro_home_invalid:write"
        assert not os.path.lexists(k.kiro_home_path(TID))

    def test_a_placeholder_that_cannot_be_sealed_leaves_nothing_behind(self, store, monkeypatch):
        def failing(path):
            raise OSError("no fchmod")

        monkeypatch.setattr(k, "_make_placeholder", failing)
        assert refusal(prepare) == "kiro_home_invalid:write"
        assert not os.path.lexists(k.kiro_home_path(TID))

    def test_a_symlinked_kiro_root_refuses_and_its_target_is_untouched(self, store, tmp_path):
        target = tmp_path / "real-root"
        target.mkdir()
        root = k.kiro_homes_root()
        root.symlink_to(target)
        assert refusal(prepare) == "kiro_home_invalid:root"
        assert os.listdir(target) == [] and root.is_symlink()
        assert k.prepared_launch_home(TID) is None

    @pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "a\0b", "a..b"])
    def test_an_agent_name_that_is_not_a_plain_file_name_refuses(self, store, name):
        payload = k.render_helper_agent(AGENT, PROFILE, grant_of(), TID)
        with pytest.raises(k.KiroLaunchRefusal) as err:
            k.create_launch_home(TID, name, payload, snapshot())
        assert err.value.rule == "kiro_agent_invalid:name"
        assert not os.path.lexists(k.kiro_homes_root())

    def test_a_home_the_root_cannot_take_refuses_as_create(self, store, monkeypatch):
        home, real = k.kiro_home_path(TID), os.mkdir

        def mkdir(path, *args, **kwargs):
            if Path(path) == home:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(k.os, "mkdir", mkdir)
        assert refusal(prepare) == "kiro_home_invalid:create"
        assert not os.path.lexists(home)
        assert k.prepared_launch_home(TID) is None


class TestKiroHomeRule:
    """G17: Kiro ignores a KIRO_HOME that is relative, the root, or contains HOME."""

    @pytest.mark.parametrize(
        "home, user_home, reason",
        [
            ("relative/kiro", "/Users/op", "relative"),
            ("/", "/Users/op", "root"),
            ("/Users/op", "/Users/op", "contains-home"),
            ("/Users", "/Users/op", "contains-home"),
            ("/Users/op/../op", "/Users/op", "contains-home"),
            ("/Users/op/.aws/cao/ephemeral/kiro/0a1b2c3d", "/Users/op", None),
            ("/Users/other/kiro", "/Users/op", None),
            ("/Users/op-kiro", "/Users/op", None),
        ],
    )
    def test_kiros_own_rule(self, home, user_home, reason):
        assert k.kiro_home_ignored_reason(home, user_home) == reason

    def test_a_link_to_an_ancestor_of_home_contains_it(self, tmp_path):
        user_home = tmp_path / "users" / "op"
        user_home.mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "users")
        assert k.kiro_home_ignored_reason(str(tmp_path / "link"), str(user_home)) == (
            "contains-home"
        )
        assert k.kiro_home_ignored_reason(str(tmp_path / "link" / "x"), str(user_home)) is None

    def test_a_cao_home_that_contains_home_refuses_before_anything_is_made(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(k.kiro_home_path(TID) / "operator"))
        assert refusal(prepare) == "kiro_home_invalid:ignored"
        assert not os.path.lexists(k.kiro_homes_root())
        monkeypatch.setenv("HOME", str(k.kiro_probe_home() / "operator"))
        assert refusal(k.ensure_probe_home) == "kiro_home_invalid:ignored"
        assert not os.path.lexists(k.kiro_probe_home())

    def test_a_link_planted_at_the_path_is_never_followed_by_the_rule(
        self, store, operator_home  # noqa: F811
    ):
        # The link names an ancestor of HOME: followed, Kiro's rule would call
        # it "contains-home". Judged on the path CAO creates, it is "exists".
        home = k.kiro_home_path(TID)
        home.parent.mkdir(parents=True, mode=0o700)
        home.symlink_to(operator_home.home.parent)
        assert refusal(prepare) == "kiro_home_invalid:exists"
        assert home.is_symlink() and os.readlink(home) == str(operator_home.home.parent)


class TestVerifyAndSeal:
    def test_a_fresh_home_verifies_and_a_sealed_one_verifies_sealed(self, store):
        prepared = prepare()
        k.verify_launch_home(prepared, sealed=False)
        k.seal_launch_home(prepared)
        assert stat.S_IMODE(os.lstat(prepared.home / "agents").st_mode) == 0o500
        for entry in prepared.files:
            assert stat.S_IMODE(os.lstat(prepared.home / entry.name).st_mode) == 0o400
        assert stat.S_IMODE(os.lstat(prepared.home / "secrets.json").st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(prepared.home / "powers").st_mode) == 0
        k.verify_launch_home(prepared, sealed=True)
        # Kiro's own files elsewhere in the home are its business.
        (prepared.home / "sessions").mkdir()
        (prepared.home / "secrets.json").write_text('{"rewritten": true}')
        k.verify_launch_home(prepared, sealed=True)

    @pytest.mark.parametrize(
        "change",
        [
            "agent",
            "settings",
            "sentinel",
            "extra",
            "example",
            "home_mode",
            "replaced",
            "unsealed",
            "powers_mode",
            "powers_replaced",
            "powers_filled",
            "powers_gone",
        ],
    )
    def test_any_change_since_preparation_refuses_as_modified(self, store, change):
        prepared = prepare()
        k.seal_launch_home(prepared)
        home = prepared.home
        agents = home / "agents"
        powers = home / "powers"
        agents.chmod(0o700)
        if change == "agent":
            prepared.agent_file.chmod(0o600)
            prepared.agent_file.write_text(prepared.agent_file.read_text().replace("glob", "shell"))
            prepared.agent_file.chmod(0o400)
        elif change == "settings":
            path = home / "settings" / "cli.json"
            path.chmod(0o600)
            path.write_text("{}")
            path.chmod(0o400)
        elif change == "sentinel":
            path = agents / "cao-null.json"
            path.chmod(0o600)
            path.write_text('{"name": "cao-null", "tools": ["*"]}')
            path.chmod(0o400)
        elif change == "extra":
            (agents / f"{AGENT}.json.bak").write_text("{}")
        elif change == "example":
            # No validate or list runs any more, so Kiro's example has no reason to appear.
            (agents / "agent_config.json.example").write_text("{}")
        elif change == "home_mode":
            home.chmod(0o755)
        elif change == "replaced":
            replace_with_same_bytes(prepared.agent_file)
            prepared.agent_file.chmod(0o400)
        elif change == "powers_mode":
            powers.chmod(0o700)
        elif change == "powers_replaced":
            powers.rmdir()
            powers.mkdir()
            powers.chmod(0)
        elif change == "powers_filled":
            powers.chmod(0o700)
            (powers / "installed.json").write_text("{}")
            powers.chmod(0)
        elif change == "powers_gone":
            powers.rmdir()
        if change != "unsealed":
            agents.chmod(0o500)
        assert refusal(k.verify_launch_home, prepared, sealed=True) == "kiro_home_invalid:modified"

    def test_a_file_replaced_before_the_seal_refuses_as_seal(self, store):
        prepared = prepare()
        replace_with_same_bytes(prepared.agent_file)
        assert refusal(k.seal_launch_home, prepared) == "kiro_home_invalid:seal"
        # The replacement is not sealed on CAO's behalf.
        assert stat.S_IMODE(os.lstat(prepared.agent_file).st_mode) != 0o400

    @pytest.mark.parametrize(
        "check, rule",
        [
            ("verify", "kiro_home_invalid:modified"),
            ("legacy_copy", "kiro_v3_global_home:changed"),
        ],
    )
    def test_a_placeholder_filled_in_the_tick_it_was_made_still_refuses(
        self, store, monkeypatch, check, rule
    ):
        """A coarse clock: the entry lands in the same tick as the placeholder's creation."""
        made = {}
        real_mkdir = os.mkdir

        def mkdir(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            if os.path.basename(os.fspath(path)) == "powers":
                made["mtime_ns"] = os.lstat(path).st_mtime_ns

        with monkeypatch.context() as m:
            m.setattr(os, "mkdir", mkdir)
            prepared = prepare()
        powers = prepared.home / "powers"
        powers.chmod(0o700)
        (powers / "installed.json").write_text("{}")
        powers.chmod(0)
        # Every stamp since the creation fell in the creation's tick.
        os.utime(powers, ns=(made["mtime_ns"], made["mtime_ns"]))
        if check == "verify":
            assert refusal(k.verify_launch_home, prepared, sealed=False) == rule
        else:
            assert refusal(k.check_global_home_unchanged, prepared) == rule

    def test_a_home_swapped_for_a_symlink_refuses(self, store, tmp_path):
        prepared = prepare()
        moved = tmp_path / "moved"
        prepared.home.rename(moved)
        prepared.home.symlink_to(moved)
        assert refusal(k.verify_launch_home, prepared, sealed=False) == "kiro_home_invalid:modified"
        prepared.home.unlink()
        moved.rename(prepared.home)


class TestHelperFiles:
    """Stage B: both agent files pass Phase 1's validator from disk, plus the helper shape."""

    def test_the_written_files_pass_on_every_schema(self, store):
        prepared = prepare(("fs_read", "fs_list", "fs_write"))
        for schema in SCHEMAS.values():
            k.check_helper_files(prepared, grant_of(("fs_read", "fs_list", "fs_write")), schema)

    def _edit(self, path, edit):
        data = json.loads(path.read_text())
        edit(data)
        path.write_text(json.dumps(data))

    @pytest.mark.parametrize(
        "edit, rule",
        [
            (lambda d: d["tools"].remove("glob"), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d["tools"].append("execute_bash"), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d["excludedTools"].remove("code"), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d["permissions"]["rules"].pop(), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d["allowedTools"].append("fs_write"), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d.update(includeMcpJson=True), "kiro_v3_invalid:include_mcp_json"),
            (lambda d: d.update(includePowers=True), "kiro_v3_invalid:include_powers"),
            (lambda d: d.update(hooks=[]), "kiro_v3_invalid:forbidden_key"),
            (lambda d: d.update(kiroUpgrade={}), "kiro_v3_invalid:forbidden_key"),
            (lambda d: d.update(name="other"), "kiro_v3_invalid:name_mismatch"),
            (lambda d: d.update(resources=["file://AGENTS.md"]), "kiro_agent_invalid:shape"),
            (lambda d: d.update(model="claude-x"), "kiro_agent_invalid:shape"),
            (lambda d: d.update(mcpServers={}), "kiro_agent_invalid:mcp"),
            (
                lambda d: d["mcpServers"]["cao-mcp-server"].update(command="cao-mcp-server"),
                "kiro_agent_invalid:mcp",
            ),
        ],
    )
    def test_any_drift_in_the_helper_file_refuses(self, store, edit, rule):
        prepared = prepare()
        self._edit(prepared.agent_file, edit)
        assert refusal(k.check_helper_files, prepared, grant_of(), SCHEMA) == rule

    def test_the_installed_grant_or_a_wider_launch_never_passes(self, store):
        prepared = prepare(("fs_read", "fs_list", "fs_write"))
        # A file rendered for a wider grant than this launch's refuses.
        assert refusal(k.check_helper_files, prepared, grant_of(), SCHEMA).startswith(
            "kiro_v3_invalid:"
        )

    @pytest.mark.parametrize(
        "edit, rule",
        [
            (lambda d: d["tools"].append("read_file"), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d.update(allowedTools=["fs_read"]), "kiro_v3_invalid:sentinel"),
            (lambda d: d["permissions"]["rules"].pop(), "kiro_v3_invalid:render_mismatch"),
            (lambda d: d.update(mcpServers={"x": {"command": "/bin/x"}}), None),
            (lambda d: d.update(resources=["file://x"]), "kiro_agent_invalid:shape"),
        ],
    )
    def test_any_drift_in_the_sentinel_refuses(self, store, edit, rule):
        prepared = prepare()
        self._edit(prepared.sentinel_file, edit)
        found = refusal(k.check_helper_files, prepared, grant_of(), SCHEMA)
        assert found == rule if rule else found.startswith(("kiro_v3_invalid:", "kiro_agent"))

    def test_a_link_in_place_of_the_agent_file_refuses(self, store, tmp_path):
        prepared = prepare()
        target = tmp_path / "copy.json"
        target.write_bytes(prepared.agent_file.read_bytes())
        prepared.agent_file.unlink()
        prepared.agent_file.symlink_to(target)
        assert refusal(k.check_helper_files, prepared, grant_of(), SCHEMA) == (
            "kiro_agent_invalid:unreadable"
        )

    def test_the_grant_attested_is_the_per_launch_files_and_must_be_unchanged(self, store):
        prepared = prepare()
        assert k.launch_agent_grant(prepared) == (grant_of(), ["cao-mcp-server"])
        self._edit(prepared.agent_file, lambda d: d["allowedTools"].append("execute_bash"))
        assert refusal(k.launch_agent_grant, prepared) == "kiro_home_invalid:modified"


class TestProbeHome:
    def test_the_probe_home_holds_the_seed_and_both_placeholders(self, store):
        probe = k.ensure_probe_home()
        assert probe == k.kiro_probe_home()
        assert stat.S_IMODE(os.lstat(probe).st_mode) == 0o700
        assert sorted(os.listdir(probe)) == ["powers", "secrets.json", "settings"]
        assert json.loads((probe / "settings" / "cli.json").read_text()) == k.SETTINGS_SEED
        assert (probe / "secrets.json").read_bytes() == k.SECRETS_PLACEHOLDER
        assert stat.S_IMODE(os.lstat(probe / "powers").st_mode) == 0
        assert k.ensure_probe_home() == probe  # idempotent

    def test_an_empty_placeholder_left_at_0700_is_sealed_again(self, store):
        probe = k.kiro_probe_home()
        (probe / "powers").mkdir(parents=True)
        k.ensure_probe_home()
        assert stat.S_IMODE(os.lstat(probe / "powers").st_mode) == 0

    @pytest.mark.parametrize(
        "entry, kind",
        [
            ("settings/cli.json", "directory"),
            ("settings/cli.json", "symlink"),
            ("secrets.json", "symlink"),
            ("powers", "file"),
            ("powers", "symlink"),
            ("powers", "populated"),
        ],
    )
    def test_an_entry_that_is_not_cao_s_refuses(self, store, tmp_path, entry, kind):
        probe = k.kiro_probe_home()
        (probe / "settings").mkdir(parents=True, mode=0o700)
        path = probe / entry
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "keep").write_text("{}")
        if kind == "directory":
            path.mkdir()
        elif kind == "symlink":
            path.symlink_to(target)
        elif kind == "file":
            path.write_text("x")
        else:
            path.mkdir()
            (path / "installed.json").write_text("{}")
        assert refusal(k.ensure_probe_home) == "kiro_home_invalid:probe"
        assert (target / "keep").read_text() == "{}"


# --- the legacy-copy guard (H5) ----------------------------------------------------

ENTRY_KINDS = ("absent", "present", "dangling")


def _arrange(operator_home, name, kind, *, directory=False):  # noqa: F811
    operator_home.set_entry(name, {"present": "dir" if directory else "file"}.get(kind, kind))


def _expected(powers, powers_marker, secrets, secrets_marker, listable):
    source = {"absent": False, "present": True, "dangling": True}
    marker = {"absent": False, "present": True, "dangling": False}
    if listable and source[powers] and not marker[powers_marker]:
        return "kiro_v3_global_home:powers"
    if source[secrets] and not marker[secrets_marker]:
        return "kiro_v3_global_home:secrets"
    return None


class BytesPath:
    """An ``os.PathLike`` whose path is bytes."""

    def __init__(self, path: Path) -> None:
        self._path = os.fsencode(path)

    def __fspath__(self) -> bytes:
        return self._path


class TestGlobalKiroSpy:
    """G19b's spy is not vacuous: it sees every forbidden kind of touch, and allows stat."""

    def test_the_spy_records_opens_lists_writes_and_changes(self, operator_home):  # noqa: F811
        kiro = operator_home.kiro
        os.stat(kiro / "powers")
        os.lstat(kiro / ".powers-migrated")
        os.path.exists(kiro / ".secrets-migrated")
        os.path.lexists(kiro / "secrets.json")
        assert operator_home.spy.touches == []
        os.listdir(kiro)
        (kiro / "agents" / "mine.json").read_text()
        os.close(os.open(kiro / "settings" / "cli.json", os.O_RDONLY))
        (kiro / "new.json").write_text("{}")
        os.chmod(kiro / "new.json", 0o600)
        os.rename(kiro / "new.json", kiro / "renamed.json")
        os.unlink(kiro / "renamed.json")
        (kiro / "made").mkdir()
        shutil.rmtree(kiro / "made")
        kinds = {touch.split("(")[0] for touch in operator_home.spy.touches}
        assert kinds >= {
            "os.listdir",
            "open",
            "os.open",
            "os.chmod",
            "os.rename",
            "os.unlink",
            "os.mkdir",
            "shutil.rmtree",
        }
        operator_home.spy.touches.clear()

    @pytest.mark.parametrize("form", ["str", "bytes", "pathlike", "bytes_pathlike"])
    def test_a_path_counts_in_every_form_it_can_take(self, operator_home, form):  # noqa: F811
        """A bytes path (psutil lists /proc that way on Linux) is compared, not a crash."""
        as_form = {"str": str, "bytes": os.fsencode, "pathlike": Path, "bytes_pathlike": BytesPath}
        to = as_form[form]
        spy = operator_home.spy
        kiro = operator_home.kiro
        assert spy._under(to(kiro)) is True
        assert spy._under(to(kiro / "agents" / "mine.json")) is True
        assert spy._under(to(operator_home.home)) is False
        assert spy._under(to(operator_home.home / ".kiro-other")) is False
        os.listdir(to(operator_home.home))
        assert spy.touches == []
        os.listdir(to(kiro / "agents"))
        assert spy.touches == [f"os.listdir({kiro / 'agents'})"]
        spy.touches.clear()

    def test_a_file_descriptor_is_not_a_path(self, operator_home):  # noqa: F811
        spy = operator_home.spy
        with operator_home.arranging():
            fd = os.open(operator_home.kiro, os.O_RDONLY)
        try:
            assert spy._under(fd) is False
            assert spy._under(None) is False
            os.listdir(fd)  # by descriptor: no path to name, and no error
        finally:
            os.close(fd)
        assert spy.touches == []


class TestGlobalHomeGuard:
    """G19: every combination of Kiro's two sources and two markers, and the self-test."""

    @pytest.mark.parametrize("listable", [False, True], ids=["unlistable", "listable"])
    @pytest.mark.parametrize("secrets_marker", ENTRY_KINDS)
    @pytest.mark.parametrize("secrets", ENTRY_KINDS)
    @pytest.mark.parametrize("powers_marker", ENTRY_KINDS)
    @pytest.mark.parametrize("powers", ENTRY_KINDS)
    def test_the_verdict_matches_h5(
        self,
        store,
        operator_home,  # noqa: F811
        monkeypatch,
        powers,
        powers_marker,
        secrets,
        secrets_marker,
        listable,
    ):
        _arrange(operator_home, "powers", powers, directory=True)
        _arrange(operator_home, ".powers-migrated", powers_marker)
        _arrange(operator_home, "secrets.json", secrets)
        _arrange(operator_home, ".secrets-migrated", secrets_marker)
        monkeypatch.setattr(k, "placeholder_is_listable", lambda path: listable)
        expected = _expected(powers, powers_marker, secrets, secrets_marker, listable)
        probe = k.ensure_probe_home()
        if expected is None:
            snap = k.check_kiro_global_home(probe)
            assert snap.placeholder_listable is listable
            assert snap.powers_source is (powers != "absent")
            assert snap.secrets_marker is (secrets_marker == "present")
        else:
            assert refusal(k.check_kiro_global_home, probe) == expected

    def test_the_self_test_lists_the_probe_homes_placeholder(self, store, monkeypatch):
        probe = k.ensure_probe_home()
        listed = []
        real = k.placeholder_is_listable
        monkeypatch.setattr(
            k, "placeholder_is_listable", lambda path: listed.append(path) or real(path)
        )
        k.check_kiro_global_home(probe)
        assert listed == [probe / "powers"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can list a mode-0000 directory")
    def test_only_a_permission_error_proves_a_placeholder_unlistable(self, tmp_path, monkeypatch):
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        sealed.chmod(0)
        try:
            assert k.placeholder_is_listable(sealed) is False
        finally:
            sealed.chmod(0o700)
        assert k.placeholder_is_listable(sealed) is True
        assert k.placeholder_is_listable(tmp_path / "missing") is True

    @pytest.mark.parametrize("code", [errno.EACCES, errno.EIO, errno.ELOOP, errno.ENAMETOOLONG])
    def test_a_source_that_cannot_be_stat_ed_counts_as_present(
        self, store, operator_home, monkeypatch, code  # noqa: F811
    ):
        """An lstat error other than absence never reads as "no secret store"."""
        probe = k.ensure_probe_home()
        source = os.path.join(str(operator_home.home), ".kiro", "secrets.json")
        real = os.lstat
        # OSError maps EACCES to PermissionError; the others stay plain OSErrors.
        assert (type(OSError(code, "")) is PermissionError) == (code == errno.EACCES)

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == source:
                raise OSError(code, os.strerror(code), source)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
        assert refusal(k.check_kiro_global_home, probe) == "kiro_v3_global_home:secrets"

    @pytest.mark.parametrize("value", [None, "", "relative/home"])
    def test_a_server_without_an_absolute_home_refuses(self, store, monkeypatch, value):
        probe = k.ensure_probe_home()
        if value is None:
            monkeypatch.delenv("HOME")
        else:
            monkeypatch.setenv("HOME", value)
        assert refusal(k.check_kiro_global_home, probe) == "kiro_v3_global_home:unknown"


class TestGlobalHomeUnchanged:
    """G20, as a function: a changed placeholder, a staging entry or a marker refuses."""

    @pytest.mark.parametrize(
        "change",
        [
            "chmod",
            "replaced",
            "filled",
            "gone",
            "staging",
            "powers_marker_appeared",
            "secrets_marker_appeared",
            "powers_marker_vanished",
        ],
    )
    def test_any_sign_of_a_legacy_copy_refuses(self, store, operator_home, change):  # noqa: F811
        prepared = prepare()
        powers = prepared.home / "powers"
        if change == "chmod":
            powers.chmod(0o700)
        elif change == "replaced":
            powers.rmdir()
            powers.mkdir()
            powers.chmod(0)
        elif change == "filled":
            powers.chmod(0o700)
            (powers / "installed.json").write_text("{}")
            powers.chmod(0)
        elif change == "gone":
            powers.rmdir()
        elif change == "staging":
            (prepared.home / ".powers-migrate-x1y2").mkdir()
        elif change == "powers_marker_appeared":
            operator_home.set_entry(".powers-migrated", "absent")
            prepared = prepare(terminal_id="0a1b2c3e")
            operator_home.set_entry(".powers-migrated", "file")
        elif change == "secrets_marker_appeared":
            operator_home.set_entry(".secrets-migrated", "file")
        elif change == "powers_marker_vanished":
            operator_home.set_entry(".powers-migrated", "absent")
        assert refusal(k.check_global_home_unchanged, prepared) == "kiro_v3_global_home:changed"
        k.discard_launch_home("0a1b2c3e")

    def test_a_source_rewritten_meanwhile_does_not_refuse(self, store, operator_home):  # noqa: F811
        prepared = prepare()
        operator_home.set_entry("secrets.json", "file")
        operator_home.set_entry("powers", "absent")
        k.check_global_home_unchanged(prepared)
        # Kiro's own session files beside the placeholder are not a sign either.
        (prepared.home / "sessions").mkdir()
        (prepared.home / "secrets.json").write_text('{"kiro": 1}')
        k.check_global_home_unchanged(prepared)


# --- release -----------------------------------------------------------------------


class _Proc:
    """A psutil.Process stand-in: no real process is inspected or signalled."""

    def __init__(self, pid, name, env, uid=None, denied=None):
        self.pid, self._name, self._env = pid, name, env
        self._uid = os.geteuid() if uid is None else uid
        self._denied = denied
        self.signals = []
        self.alive = True

    def _check(self, what):
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)
        if self._denied == what:
            raise psutil.AccessDenied(self.pid)

    def create_time(self):
        self._check("create_time")
        return 1.0

    def uids(self):
        self._check("uids")
        return SimpleNamespace(effective=self._uid)

    def name(self):
        self._check("name")
        return self._name

    def environ(self):
        self._check("environ")
        return dict(self._env)

    def send_signal(self, sig):
        self._check("send_signal")
        self.signals.append(sig)
        if sig == signal.SIGTERM and self._name != "stubborn":
            self.alive = False
        if sig == signal.SIGKILL:
            self.alive = False


@pytest.fixture
def processes(monkeypatch):
    table: dict[int, _Proc] = {}

    def process(pid):
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        return table[pid]

    monkeypatch.setattr(private_home.psutil, "pids", lambda: list(table))
    monkeypatch.setattr(private_home.psutil, "Process", process)
    monkeypatch.setattr(k, "_HOME_PROCESS_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(k, "_HOME_PROCESS_KILL_GRACE_SECONDS", 0.2)
    return table


class TestOwnerStop:
    def test_only_processes_with_this_exact_home_are_signalled(self, processes, tmp_path):
        home = tmp_path / "h"
        processes[11] = _Proc(11, "kiro-cli-chat", {"KIRO_HOME": str(home)})
        processes[12] = _Proc(12, "zsh", {"KIRO_HOME": str(home)})
        processes[13] = _Proc(13, "kiro-cli-chat", {"KIRO_HOME": str(home) + "-other"})
        processes[14] = _Proc(14, "kiro-cli", {})  # the operator's own Kiro
        processes[15] = _Proc(15, "bun", {"KIRO_HOME": str(home)}, uid=os.geteuid() + 1)
        processes[16] = _Proc(16, "Safari", {"KIRO_HOME": str(home)})
        # v3 runs its engine under node (round 1's process tree).
        processes[17] = _Proc(17, "node", {"KIRO_HOME": str(home)})
        assert k.stop_kiro_home_processes(home) is True
        for pid in (11, 12, 17):
            assert processes[pid].signals == [signal.SIGTERM], pid
        assert all(not processes[pid].signals for pid in (13, 14, 15, 16))

    def test_a_process_that_ignores_sigterm_gets_sigkill(self, processes, tmp_path, monkeypatch):
        home = tmp_path / "h"
        processes[21] = _Proc(21, "stubborn", {"KIRO_HOME": str(home)})
        monkeypatch.setattr(k, "_KIRO_OWNER_NAMES", k._KIRO_OWNER_NAMES | {"stubborn"})
        assert k.stop_kiro_home_processes(home) is True
        assert processes[21].signals == [signal.SIGTERM, signal.SIGKILL]

    @pytest.mark.parametrize("denied", ["uids", "environ"])
    def test_an_uninspectable_candidate_retains_the_home(self, processes, tmp_path, denied):
        home = tmp_path / "h"
        processes[31] = _Proc(31, "python3.12", {"KIRO_HOME": str(home)}, denied=denied)
        assert k.stop_kiro_home_processes(home) is False

    def test_a_process_of_another_terminal_is_never_an_owner(self, processes, tmp_path):
        """A sibling window that inherited this home keeps its processes (#1074)."""
        home = tmp_path / TID
        own = {"KIRO_HOME": str(home), "CAO_TERMINAL_ID": TID}
        sibling = {"KIRO_HOME": str(home), "CAO_TERMINAL_ID": "bbbbbbbb"}
        processes[41] = _Proc(41, "kiro-cli-chat", own)
        processes[42] = _Proc(42, "cao-mcp-server", {"KIRO_HOME": str(home)})  # no terminal id
        processes[43] = _Proc(43, "python3.12", sibling)
        processes[44] = _Proc(44, "kiro-cli-chat", sibling)
        processes[45] = _Proc(
            45, "python3.12", {**sibling, "CAO_TERMINAL_ID": ""}
        )  # present, empty
        assert k.stop_kiro_home_processes(home) is True
        assert processes[41].signals == [signal.SIGTERM]
        assert processes[42].signals == [signal.SIGTERM]
        assert all(not processes[pid].signals for pid in (43, 44, 45))


def _sleeper(tmp_path, label, env):
    """A real python process with exactly these extra env vars, once it has started."""
    marker = tmp_path / f"{label}.started"
    code = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(marker)],
        env={**os.environ, **env},
    )
    deadline = time.monotonic() + 20
    while not marker.exists():
        assert proc.poll() is None and time.monotonic() < deadline, f"{label} did not start"
        time.sleep(0.05)
    return proc


def test_a_real_sibling_carrying_the_home_is_not_its_owner(tmp_path):
    """Dry: real processes are inspected, never signalled (#1074)."""
    home = tmp_path / TID
    sleepers = {}
    try:
        for label, terminal_id in (("own", TID), ("sibling", "bbbbbbbb")):
            env = {"KIRO_HOME": str(home), "CAO_TERMINAL_ID": terminal_id}
            sleepers[label] = _sleeper(tmp_path, label, env)
        sibling, own = sleepers["sibling"].pid, sleepers["own"].pid
        # Without the terminal id the sibling would count: the home alone is not enough.
        assert isinstance(
            private_home.inspect_home_process(sibling, home, k.KIRO_HOME_OWNERS), psutil.Process
        )
        assert k._pid_uses_kiro_home(sibling, home) is False
        assert k._pid_uses_kiro_home(own, home) is True
    finally:
        for proc in sleepers.values():
            proc.kill()
            proc.wait(timeout=10)


class TestRemove:
    def test_owners_are_stopped_before_a_sealed_home_is_removed(self, store):
        prepared = prepare()
        k.seal_launch_home(prepared)
        order = []

        def stop(home):
            order.append(("stop", os.path.lexists(home)))
            return True

        removals = []
        assert (
            k.remove_kiro_home(
                TID,
                prepared.hashes(),
                global_home=prepared.global_home,
                stop_owners=stop,
                on_removed=removals.append,
            )
            is True
        )
        assert order == [("stop", True)]
        assert not os.path.lexists(prepared.home)
        assert removals == [
            k.HomeRemoval(
                agent_modified=False,
                settings_modified=False,
                secrets_store_changed=False,
                global_home_changed=False,
            )
        ]
        assert k.prepared_launch_home(TID) is None

    def test_the_unlistable_placeholder_is_unsealed_and_removed(self, store, monkeypatch):
        prepared = prepare()
        k.seal_launch_home(prepared)
        unsealed = []
        real = k._unseal
        monkeypatch.setattr(
            k, "_unseal", lambda home: unsealed.append(os.path.lexists(home)) or real(home)
        )
        assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=lambda _: True) is True
        assert unsealed == [True] and not os.path.lexists(prepared.home)

    @pytest.mark.parametrize("kind", ["file", "hard_link"])
    def test_a_placeholder_that_is_not_a_directory_is_never_chmod_ed(
        self, store, operator_home, tmp_path, monkeypatch, kind  # noqa: F811
    ):
        """Only a directory is unsealed: a file, or a hard link to one outside, keeps its mode."""
        prepared = prepare()
        k.seal_launch_home(prepared)
        powers = prepared.home / "powers"
        powers.rmdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("keep")
        outside.chmod(0o600)
        if kind == "file":
            powers.write_text("x")
            powers.chmod(0o600)
            watched = powers
        else:
            os.link(outside, powers)
            watched = outside
        before = stat.S_IMODE(os.lstat(watched).st_mode)
        assert before == 0o600
        modes = []
        real = os.chmod

        def chmod(path, mode, *args, **kwargs):
            if os.fspath(path) == "powers":
                modes.append(mode)
            return real(path, mode, *args, **kwargs)

        # Scoped, so the fixtures' fake HOME, ~/.kiro spy and store root stay in force.
        with monkeypatch.context() as m:
            m.setattr(os, "chmod", chmod)
            assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=lambda _: True) is True
        assert os.chmod is real
        assert os.environ["HOME"] == str(operator_home.home)
        assert ephemeral_service.EPHEMERAL_DIR == store
        assert modes == []  # never chmod-ed: not a directory
        assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o600
        assert outside.read_text() == "keep"
        # The home itself is gone: rmtree unlinks a file or link like any other entry.
        assert not os.path.lexists(prepared.home)

    def test_a_home_swapped_for_a_link_is_never_unsealed_through_it(self, store, tmp_path):
        """The swap lands after the path checks: the unseal opens the home without following it."""
        prepared = prepare()
        k.seal_launch_home(prepared)
        victim = tmp_path / "victim"
        (victim / "agents").mkdir(parents=True)
        (victim / "agents").chmod(0o500)
        moved = tmp_path / "moved-home"

        def swap(home):
            home.rename(moved)
            home.symlink_to(victim)
            return True

        assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=swap) is False
        assert stat.S_IMODE(os.lstat(victim / "agents").st_mode) == 0o500
        assert prepared.home.is_symlink()
        prepared.home.unlink()
        (victim / "agents").chmod(0o700)
        _unseal_tree(moved)
        (moved / "agents").chmod(0o700)

    def test_a_placeholder_swapped_for_a_link_is_never_followed(self, store, tmp_path):
        prepared = prepare()
        victim = tmp_path / "victim"
        victim.mkdir(mode=0o500)
        powers = prepared.home / "powers"
        powers.rmdir()
        powers.symlink_to(victim)
        assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=lambda _: True) is True
        assert stat.S_IMODE(os.lstat(victim).st_mode) == 0o500 and victim.is_dir()
        assert not os.path.lexists(prepared.home)

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("chmod: follow_symlinks unavailable on this platform"),
            NotImplementedError("chmod: follow_symlinks unavailable on this platform"),
            OSError(errno.EOPNOTSUPP, "Operation not supported"),
        ],
        ids=["ValueError", "NotImplementedError", "EOPNOTSUPP"],
    )
    def test_a_home_that_cannot_be_unsealed_is_kept_whole(self, store, monkeypatch, error):
        """No rmtree: it would delete the other files first, and the next try call that tampering."""
        prepared = prepare()
        k.seal_launch_home(prepared)
        (prepared.home / "sessions" / "cli").mkdir(parents=True)
        (prepared.home / "sessions" / "cli" / "0f0e.jsonl").write_text("{}")
        before = home_tree(prepared.home)
        removals = []
        rmtree_calls = []
        real_rmtree = shutil.rmtree

        def chmod(*args, **kwargs):
            raise error

        @functools.wraps(real_rmtree)
        def rmtree(path, *args, **kwargs):
            rmtree_calls.append(os.fspath(path))
            return real_rmtree(path, *args, **kwargs)

        def remove():
            return k.remove_kiro_home(
                TID,
                prepared.hashes(),
                global_home=prepared.global_home,
                stop_owners=lambda _: True,
                on_removed=removals.append,
            )

        with monkeypatch.context() as m:
            m.setattr(os, "chmod", chmod)
            m.setattr(shutil, "rmtree", rmtree)
            assert remove() is False
        assert home_tree(prepared.home) == before
        assert rmtree_calls == []
        assert removals == []
        assert k.prepared_launch_home(TID) is prepared
        # With chmod working again, the next removal finds every CAO file as it was made.
        assert remove() is True
        assert not os.path.lexists(prepared.home)
        assert removals == [
            k.HomeRemoval(
                agent_modified=False,
                settings_modified=False,
                secrets_store_changed=False,
                global_home_changed=False,
            )
        ]

    def test_a_home_whose_owners_may_remain_is_kept(self, store):
        prepared = prepare()
        assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=lambda _: False) is False
        assert prepared.home.is_dir()

    def test_without_fd_based_rmtree_the_home_is_kept(self, store, monkeypatch):
        prepared = prepare()
        monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
        assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=lambda _: True) is False
        assert prepared.home.is_dir()

    def test_tamper_evidence_is_recorded_before_the_files_go(
        self, store, operator_home
    ):  # noqa: F811
        prepared = prepare()
        prepared.agent_file.write_text("{}")
        (prepared.home / "secrets.json").write_text('{"copied": true}')
        operator_home.set_entry(".secrets-migrated", "file")
        removals = []
        assert k.remove_kiro_home(
            TID,
            prepared.hashes(),
            global_home=prepared.global_home,
            stop_owners=lambda _: True,
            on_removed=removals.append,
        )
        assert removals == [
            k.HomeRemoval(
                agent_modified=True,
                settings_modified=False,
                secrets_store_changed=True,
                global_home_changed=True,
            )
        ]

    def test_without_a_record_the_evidence_is_unknown(self, store):
        prepare()
        removals = []
        assert k.remove_kiro_home(TID, {}, stop_owners=lambda _: True, on_removed=removals.append)
        assert removals == [k.HomeRemoval(None, None, None, None)]

    def test_kiro_session_files_go_with_the_home(self, store):
        prepared = prepare()
        transcripts = prepared.home / "sessions" / "cli"
        transcripts.mkdir(parents=True)
        (transcripts / "0f0e.jsonl").write_text("{}")
        assert k.remove_kiro_home(TID, {}, stop_owners=lambda _: True)
        assert not os.path.lexists(prepared.home)

    @pytest.mark.parametrize("kind", ["directory", "symlink"])
    def test_an_entry_cao_has_no_record_of_is_left_alone(self, store, tmp_path, kind):
        home = k.kiro_home_path(TID)
        home.parent.mkdir(parents=True, exist_ok=True)
        if kind == "directory":
            home.mkdir()
        else:
            home.symlink_to(tmp_path)
        stop = Mock(return_value=True)
        assert k.remove_kiro_home(TID, None, stop_owners=stop) is True
        assert os.path.lexists(home)
        stop.assert_not_called()

    def test_a_recorded_path_that_is_now_a_symlink_is_never_followed(self, store, tmp_path):
        prepared = prepare()
        target = tmp_path / "victim"
        target.mkdir()
        (target / "keep").write_text("x")
        (prepared.home / "powers").chmod(0o700)
        shutil.rmtree(prepared.home)
        prepared.home.symlink_to(target)
        assert k.remove_kiro_home(TID, prepared.hashes(), stop_owners=lambda _: True) is True
        assert (target / "keep").exists()

    def test_a_missing_home_is_already_removed(self, store):
        assert k.remove_kiro_home(TID, None) is True
        assert k.remove_kiro_home("not-a-tid", None) is True


class TestUnseal:
    """``_unseal`` says whether rmtree can now empty the home, and changes nothing it refuses."""

    def test_a_sealed_home_is_unsealed(self, store):
        prepared = prepare()
        k.seal_launch_home(prepared)
        assert k._unseal(prepared.home) is True
        for name in ("agents", "powers"):
            assert stat.S_IMODE(os.lstat(prepared.home / name).st_mode) == 0o700

    def test_a_missing_home_or_placeholder_is_nothing_to_unseal(self, store, tmp_path):
        prepared = prepare()
        (prepared.home / "powers").rmdir()
        assert k._unseal(prepared.home) is True
        assert k._unseal(tmp_path / "gone") is True

    def test_another_users_directory_is_left_sealed(self, store, monkeypatch):
        prepared = prepare()
        k.seal_launch_home(prepared)
        before = home_tree(prepared.home)
        euid = os.geteuid()
        with monkeypatch.context() as m:
            m.setattr(os, "geteuid", lambda: euid + 1)
            assert k._unseal(prepared.home) is False
        assert home_tree(prepared.home) == before

    def test_a_home_that_cannot_be_opened_is_left_sealed(self, store, tmp_path):
        victim = tmp_path / "victim"
        (victim / "agents").mkdir(parents=True)
        (victim / "agents").chmod(0o500)
        link = tmp_path / "link"
        link.symlink_to(victim)
        assert k._unseal(link) is False
        assert stat.S_IMODE(os.lstat(victim / "agents").st_mode) == 0o500
        (victim / "agents").chmod(0o700)
