"""install.sh against a stub ``kirocrew`` on PATH (workstream C9). Nothing touches a real Kiro Crew."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HASHER = ROOT / "ui" / "scripts" / "inputs_hash.py"

STUB = r"""#!/usr/bin/env bash
# stub kirocrew: state lives in $STUB_STATE
set -u
S="$STUB_STATE"; mkdir -p "$S/config"
echo "$*" >>"$S/calls.log"
cfg() { cat "$S/config/$1" 2>/dev/null || echo "$2"; }
case "${1:-}" in
  --version) echo "kirocrew ${STUB_VERSION:-0.12.2}" ;;
  config)
    case "$2" in
      get) case "$3" in
             agent.apps_allow_third_party) cfg "$3" False ;;
             *) cfg "$3" '[]' ;;
           esac ;;
      set) key="$3"; val="${4:-}"
           case "$val" in --value-base64=*) val="$(printf '%s' "${val#--value-base64=}" | base64 -d)" ;; esac
           printf '%s' "$val" >"$S/config/$key" ;;
    esac ;;
  app)
    case "$2" in
      info) [ -f "$S/installed" ] ;;
      update) if [ "${STUB_NO_GATEWAY:-}" = 1 ]; then echo "No running gateway answered, so nothing was updated." >&2; exit 1; fi
              cp "$5/app.json" "$S/installed-app.json"; cp "$5/agents/cao-remote-control.json" "$S/installed-agent.json"; touch "$S/installed" ;;
      install) cp "$3/app.json" "$S/installed-app.json"; cp "$3/agents/cao-remote-control.json" "$S/installed-agent.json"; printf '%s' "$3" >"$S/install-src"; ls -A "$3" >"$S/installed-ls"; touch "$S/installed" ;;
      enable)
        if [ "${STUB_CONSENT:-}" = pending ]; then
          echo "❌ cao-shelf asks to control your chats. Nothing was enabled." >&2
          echo "   To allow it, open the app in the dashboard: http://127.0.0.1:5476/apps/detail/cao-shelf" >&2
          exit 1
        fi
        allow="$(cfg agent.apps_allow_third_party False | tr '[:upper:]' '[:lower:]')"
        grants="$(cfg agent.apps_trusted '[]')"
        if [ "$allow" = true ] || printf '%s' "$grants" | grep -q '"cao-shelf"'; then echo "enabled"; touch "$S/enabled"
        else echo "app_execution_denied: third-party apps are not trusted" >&2; exit 1; fi ;;
      disable) rm -f "$S/enabled" ;;
      uninstall) rm -f "$S/installed" "$S/enabled" ;;
    esac ;;
  *) echo "stub: unexpected $*" >&2; exit 9 ;;
esac
"""

NODE_OLD = '#!/usr/bin/env bash\n[ "${1:-}" = --version ] && echo v20.1.0\nexit 0\n'
NODE_NEW = '#!/usr/bin/env bash\n[ "${1:-}" = --version ] && echo v22.14.0\nexit 0\n'
NPM = '#!/usr/bin/env bash\necho "npm $*" >>"$STUB_STATE/npm.log"\nmkdir -p ui/bundle && echo built >ui/bundle/index.mjs\nexit 0\n'


def write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.extra_env: dict[str, str] = {}
        self.example = tmp / "example"
        self.bin = tmp / "bin"
        self.state = tmp / "state"
        self.bin.mkdir()
        (self.example / "ui" / "src").mkdir(parents=True)
        (self.example / "ui" / "bundle").mkdir(parents=True)
        shutil.copy(ROOT / "install.sh", self.example / "install.sh")
        shutil.copy(ROOT / "app.json", self.example / "app.json")
        shutil.copytree(ROOT / "agents", self.example / "agents")
        # install.sh imports the backend's own address check (cao.validate_cao_url) and default
        shutil.copytree(
            ROOT / "backend" / "cao_shelf",
            self.example / "backend" / "cao_shelf",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (self.example / "ui" / "scripts").mkdir()
        shutil.copy(HASHER, self.example / "ui" / "scripts" / "inputs_hash.py")
        (self.example / "ui" / "bundle" / "index.mjs").write_text("bundle", encoding="utf-8")
        (self.example / "ui" / "src" / "App.tsx").write_text("src", encoding="utf-8")
        self.stamp()
        # the bundle older than every source, as in a fresh clone: times must not matter
        past = 1_700_000_000
        os.utime(self.example / "ui" / "bundle" / "index.mjs", (past, past))
        write_exec(self.bin / "kirocrew", STUB)
        write_exec(self.bin / "cao-ops-mcp-server", "#!/usr/bin/env bash\nexit 0\n")
        # basic tools install.sh needs; python3 is the interpreter running this test
        for tool in (
            "bash",
            "sed",
            "head",
            "tr",
            "grep",
            "find",
            "dirname",
            "base64",
            "cat",
            "mktemp",
            "rm",
            "env",
            "touch",
            "mkdir",
            "sleep",
            "cut",
            "cp",
            "pwd",
            "basename",
            "ls",
        ):
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        (self.bin / "python3").symlink_to(sys.executable)

    def run(
        self,
        *args: str,
        with_kirocrew: bool = True,
        node: str | None = None,
        version: str = "0.12.2",
    ):
        for name in ("node", "npm"):
            (self.bin / name).unlink(missing_ok=True)
        if node:
            write_exec(self.bin / "node", NODE_OLD if node == "old" else NODE_NEW)
            write_exec(self.bin / "npm", NPM)
        kc = self.bin / "kirocrew"
        hidden = self.bin / "kirocrew.off"
        if not with_kirocrew and kc.exists():
            kc.rename(hidden)
        elif with_kirocrew and hidden.exists():
            hidden.rename(kc)
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.tmp / "home"),
            "STUB_STATE": str(self.state),
            "STUB_VERSION": version,
            **self.extra_env,
        }
        return subprocess.run(
            [str(self.bin / "bash"), str(self.example / "install.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(self.tmp),
            timeout=60,
        )

    def stamp(self) -> None:
        """Record the input hash of the current ui/ as the build does."""
        subprocess.run(
            [sys.executable, str(HASHER), "--write", str(self.example / "ui")], check=True
        )

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def config(self, key: str) -> str | None:
        f = self.state / "config" / key
        return f.read_text(encoding="utf-8") if f.exists() else None


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def mutating(calls: list[str]) -> list[str]:
    return [
        c
        for c in calls
        if c.startswith(
            (
                "app install",
                "app update",
                "app enable",
                "app disable",
                "app uninstall",
                "config set",
            )
        )
    ]


def test_missing_kirocrew_exits_2_with_a_plain_message(rig: Rig) -> None:
    res = rig.run(with_kirocrew=False)
    assert res.returncode == 2
    assert "kirocrew was not found on PATH" in res.stderr


def test_old_kirocrew_is_refused(rig: Rig) -> None:
    res = rig.run(version="0.11.9")
    assert res.returncode == 2 and "too old" in res.stderr
    assert mutating(rig.calls()) == []


def test_stale_bundle_and_old_node_exits_3(rig: Rig) -> None:
    (rig.example / "ui" / "src" / "App.tsx").write_text("edited", encoding="utf-8")
    res = rig.run(node="old")
    assert res.returncode == 3 and "Node 22.12" in res.stderr
    assert mutating(rig.calls()) == []


def test_stale_bundle_rebuilds_with_new_node(rig: Rig) -> None:
    (rig.example / "ui" / "src" / "App.tsx").write_text("edited", encoding="utf-8")
    res = rig.run("--trust-app", node="new")
    assert res.returncode == 0, res.stderr
    assert (rig.state / "npm.log").read_text().splitlines() == ["npm ci", "npm run build"]


def test_a_fresh_clone_installs_without_node(rig: Rig) -> None:
    # the real committed bundle, its sources and build config, with every source newer than the
    # bundle on disk (git writes ui/bundle before ui/src); no node or npm on PATH
    ui = rig.example / "ui"
    shutil.rmtree(ui / "src")
    shutil.copytree(ROOT / "ui" / "src", ui / "src")
    shutil.copytree(ROOT / "ui" / "bundle", ui / "bundle", dirs_exist_ok=True)
    for name in ("package.json", "package-lock.json", "vite.config.ts", "tsconfig.json"):
        shutil.copy(ROOT / "ui" / name, ui / name)
    past = 1_700_000_000
    for p in (ui / "bundle").iterdir():
        os.utime(p, (past, past))
    for p in (ui / "src").rglob("*"):
        os.utime(p, None)
    res = rig.run("--trust-app", "--dry-run")
    assert res.returncode == 0, (res.stdout, res.stderr)
    out = res.stdout + res.stderr
    assert "UI bundle is" not in out and "npm" not in out
    assert not (rig.state / "npm.log").exists()


def test_the_committed_bundle_records_the_hash_of_its_inputs() -> None:
    # make build-ui writes it; bundle-drift (git diff on ui/bundle) catches a stale one
    res = subprocess.run([sys.executable, str(HASHER), "--check", str(ROOT / "ui")])
    assert res.returncode == 0, "ui/bundle/.inputs-sha256 is stale: run make build-ui"


def test_an_edited_config_file_makes_the_bundle_stale(rig: Rig) -> None:
    (rig.example / "ui" / "package.json").write_text("{}", encoding="utf-8")
    res = rig.run(node="old")
    assert res.returncode == 3 and "input hash" in res.stderr


@pytest.mark.parametrize(
    "stray",
    (
        ".DS_Store",
        "components/.DS_Store",
        "App.tsx~",
        ".App.tsx.swp",
        "App.tsx.orig",
        "._App.tsx",
        "components/.hidden/Extra.tsx",
    ),
)
def test_stray_files_under_src_leave_the_hash_alone(rig: Rig, stray: str) -> None:
    # Finder files, editor backups and swap files are not build inputs
    src = rig.example / "ui" / "src"
    (src / stray).parent.mkdir(parents=True, exist_ok=True)
    (src / stray).write_text("noise", encoding="utf-8")
    check = [sys.executable, str(HASHER), "--check", str(rig.example / "ui")]
    assert subprocess.run(check).returncode == 0
    (src / "App.tsx").write_text("edited", encoding="utf-8")
    assert subprocess.run(check).returncode == 1


def test_a_bundle_without_a_recorded_hash_is_stale(rig: Rig) -> None:
    (rig.example / "ui" / "bundle" / ".inputs-sha256").unlink()
    assert rig.run(node="old").returncode == 3


def test_missing_bundle_without_node_exits_3_and_no_build_flag_too(rig: Rig) -> None:
    (rig.example / "ui" / "bundle" / "index.mjs").unlink()
    assert rig.run().returncode == 3
    res = rig.run("--no-build", node="new")
    assert res.returncode == 3 and "--no-build" in res.stderr


def test_without_trust_it_installs_reports_and_exits_4_changing_no_trust_setting(rig: Rig) -> None:
    res = rig.run()
    assert res.returncode == 4
    assert "Third-party trust: none" in res.stdout
    assert "--trust-app" in res.stdout and "--trust-all" in res.stdout
    assert "app_execution_denied" in res.stderr
    assert not any(c.startswith("config set") for c in rig.calls())
    assert (rig.state / "installed").exists() and not (rig.state / "enabled").exists()


def test_trust_app_grants_only_this_app_and_enables(rig: Rig) -> None:
    res = rig.run("--trust-app")
    assert res.returncode == 0, res.stderr
    assert json.loads(rig.config("agent.apps_trusted")) == ["cao-shelf"]
    assert rig.config("agent.apps_allow_third_party") is None
    assert (rig.state / "enabled").exists()


def test_trust_app_keeps_existing_grants(rig: Rig) -> None:
    (rig.state / "config").mkdir(parents=True)
    (rig.state / "config" / "agent.apps_trusted").write_text('["other-app"]')
    assert rig.run("--trust-app").returncode == 0
    assert json.loads(rig.config("agent.apps_trusted")) == ["other-app", "cao-shelf"]


def test_trust_all_sets_the_blanket_flag_and_says_so(rig: Rig) -> None:
    res = rig.run("--trust-all")
    assert res.returncode == 0, res.stderr
    assert rig.config("agent.apps_allow_third_party") == "true"
    assert "EVERY third-party App" in res.stdout


def test_second_run_updates_instead_of_installing(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    assert rig.run("--trust-app").returncode == 0
    calls = rig.calls()
    assert sum(c.startswith("app install") for c in calls) == 1
    assert sum(c.startswith("app update cao-shelf --source") for c in calls) == 1
    assert json.loads(rig.config("agent.apps_trusted")) == ["cao-shelf"]


def test_dry_run_changes_nothing(rig: Rig) -> None:
    res = rig.run("--dry-run", "--trust-app")
    assert res.returncode == 0, res.stderr
    assert mutating(rig.calls()) == []
    assert "+ " in res.stdout and "app install" in res.stdout
    assert not (rig.state / "installed").exists()


def test_uninstall_removes_app_and_its_grant_but_not_the_blanket_flag(rig: Rig) -> None:
    assert rig.run("--trust-all").returncode == 0
    (rig.state / "config" / "agent.apps_trusted").write_text('["x", "cao-shelf"]')
    res = rig.run("--uninstall")
    assert res.returncode == 0, res.stderr
    assert not (rig.state / "installed").exists()
    assert json.loads(rig.config("agent.apps_trusted")) == ["x"]
    assert rig.config("agent.apps_allow_third_party") == "true"
    assert "still true" in res.stdout and "App data" in res.stdout
    assert not any("--purge-data" in c for c in rig.calls())


def test_uninstall_is_idempotent_and_purge_is_explicit(rig: Rig) -> None:
    assert rig.run("--uninstall").returncode == 0
    assert rig.run("--trust-app").returncode == 0
    res = rig.run("--uninstall", "--purge-data")
    assert res.returncode == 0
    assert any(c == "app uninstall --purge-data cao-shelf" for c in rig.calls())
    assert rig.run("--purge-data").returncode == 2


def test_unknown_option_is_a_usage_error(rig: Rig) -> None:
    assert rig.run("--frobnicate").returncode == 2


def test_update_without_a_running_gateway_reinstalls_and_keeps_data(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    rig.extra_env["STUB_NO_GATEWAY"] = "1"
    res = rig.run("--trust-app")
    assert res.returncode == 0, res.stderr
    calls = rig.calls()
    assert "app uninstall cao-shelf" in calls
    assert not any("--purge-data" in c for c in calls)
    assert sum(c.startswith("app install") for c in calls) == 2
    assert (rig.state / "enabled").exists()


def test_installed_agent_names_the_absolute_ops_server_and_the_manifest_names_none(
    rig: Rig,
) -> None:
    """Kiro Crew writes every manifest mcpServers entry into the host's default agent config, so the
    installed app.json declares none; the relay agent's own entry carries the resolved server."""
    res = rig.run("--trust-app")
    assert res.returncode == 0, res.stderr
    installed = json.loads((rig.state / "installed-app.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in installed
    assert installed["agents"] == ["agents/cao-remote-control.json"]
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    ops = agent["mcpServers"]["cao-shelf:ops"]
    assert ops["command"] == str(rig.bin / "cao-ops-mcp-server")
    assert ops["env"] == {"CAO_API_HOST": "127.0.0.1", "CAO_API_PORT": "9889"}
    assert "cao-ops-mcp-server: " + str(rig.bin / "cao-ops-mcp-server") in res.stdout


def test_a_manifest_that_declares_mcp_servers_is_refused(rig: Rig) -> None:
    path = rig.example / "app.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["mcpServers"] = {"ops": {"command": "cao-ops-mcp-server"}}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    res = rig.run("--trust-app")
    assert res.returncode == 5 and not (rig.state / "installed").exists()


def test_installed_agent_carries_the_same_ops_server_with_the_other_tools_disabled(
    rig: Rig,
) -> None:
    """Kiro Crew 0.12.2 mounts the WHOLE server for an `@server/tool` grant on the Claude backend;
    only the agent's own `mcpServers.<server>.disabledTools` narrows it (as permissions.deny). So the
    installed agent must name the same resolved server as the manifest, with every other tool off.
    """
    res = rig.run("--trust-app", "--cao-url", "http://127.0.0.1:9990")
    assert res.returncode == 0, res.stderr
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    entry = agent["mcpServers"]["cao-shelf:ops"]
    assert entry["command"] == str(rig.bin / "cao-ops-mcp-server") and entry["args"] == []
    assert entry["env"] == {"CAO_API_HOST": "127.0.0.1", "CAO_API_PORT": "9990"}
    assert {"launch_session", "shutdown_session", "install_profile"} <= set(entry["disabledTools"])
    # the folder's own agent stays portable
    source = json.loads((rig.example / "agents" / "cao-remote-control.json").read_text("utf-8"))
    assert source["mcpServers"]["cao-shelf:ops"]["command"] == "cao-ops-mcp-server"


def test_update_also_installs_the_resolved_manifest(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    (rig.state / "installed-agent.json").unlink()
    assert rig.run("--trust-app").returncode == 0
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    assert agent["mcpServers"]["cao-shelf:ops"]["command"] == str(rig.bin / "cao-ops-mcp-server")


def test_missing_ops_server_is_refused_before_anything_changes(rig: Rig) -> None:
    (rig.bin / "cao-ops-mcp-server").unlink()
    res = rig.run("--trust-app")
    assert res.returncode == 6
    assert "cao-ops-mcp-server was not found" in res.stderr
    assert mutating(rig.calls()) == []
    assert not (rig.state / "installed").exists()


def test_uninstall_does_not_need_the_ops_server(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    (rig.bin / "cao-ops-mcp-server").unlink()
    assert rig.run("--uninstall").returncode == 0
    assert not (rig.state / "installed").exists()


@pytest.mark.parametrize(
    "how",
    [("--cao-url", "http://127.0.0.1:9990"), ("env", "http://127.0.0.1:9990")],
    ids=["flag", "env"],
)
def test_the_cao_address_can_be_overridden(rig: Rig, how: tuple) -> None:
    if how[0] == "env":
        rig.extra_env["CAO_SHELF_CAO_URL"] = how[1]
        res = rig.run("--trust-app")
    else:
        res = rig.run("--trust-app", "--cao-url", how[1])
    assert res.returncode == 0, res.stderr
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    assert agent["mcpServers"]["cao-shelf:ops"]["env"] == {
        "CAO_API_HOST": "127.0.0.1",
        "CAO_API_PORT": "9990",
    }


def test_the_flag_wins_over_the_environment(rig: Rig) -> None:
    rig.extra_env["CAO_SHELF_CAO_URL"] = "http://127.0.0.1:9990"
    assert rig.run("--trust-app", "--cao-url", "http://localhost:9991").returncode == 0
    env = json.loads((rig.state / "installed-agent.json").read_text())["mcpServers"][
        "cao-shelf:ops"
    ]["env"]
    assert env == {"CAO_API_HOST": "localhost", "CAO_API_PORT": "9991"}


def test_ipv6_loopback_is_written_in_brackets(rig: Rig) -> None:
    assert rig.run("--trust-app", "--cao-url", "http://[::1]:9990").returncode == 0
    env = json.loads((rig.state / "installed-agent.json").read_text())["mcpServers"][
        "cao-shelf:ops"
    ]["env"]
    assert env == {"CAO_API_HOST": "[::1]", "CAO_API_PORT": "9990"}


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1:9889",
        "https://127.0.0.1:9889",
        "http://example.com:9889",
        "http://127.0.0.1:9889/path",
        "http://u:p@127.0.0.1:9889",
        "http://127.0.0.1:notaport",
        "",
    ],
)
def test_a_non_loopback_cao_address_is_refused_before_anything_changes(rig: Rig, url: str) -> None:
    res = rig.run("--trust-app", "--cao-url", url)
    assert res.returncode == 2, res.stdout
    assert "--cao-url" in res.stderr or "CAO_SHELF_CAO_URL" in res.stderr
    assert mutating(rig.calls()) == []


def test_the_relay_agent_keeps_auto_without_a_relay_model(rig: Rig) -> None:
    rig.extra_env["CAO_SHELF_RELAY_MODEL"] = ""  # an empty variable counts as unset
    res = rig.run("--trust-app")
    assert res.returncode == 0, res.stderr
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    assert agent["model"] == "auto"
    assert "relay agent model" not in res.stdout


@pytest.mark.parametrize(
    "model",
    ["claude-haiku-4.5", "claude-sonnet-4-6[1m]", "auto", "bedrock/anthropic/claude-x:v1"],
)
@pytest.mark.parametrize("how", ["flag", "env"])
def test_an_accepted_relay_model_is_written_into_the_installed_agent(
    rig: Rig, model: str, how: str
) -> None:
    if how == "env":
        rig.extra_env["CAO_SHELF_RELAY_MODEL"] = model
        res = rig.run("--trust-app")
    else:
        res = rig.run("--trust-app", "--relay-model", model)
    assert res.returncode == 0, res.stderr
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    assert agent["model"] == model
    # the rest of the agent is as before: the ops server is still resolved, the tools unchanged
    assert agent["mcpServers"]["cao-shelf:ops"]["command"] == str(rig.bin / "cao-ops-mcp-server")
    source = json.loads((rig.example / "agents" / "cao-remote-control.json").read_text("utf-8"))
    assert agent["tools"] == source["tools"] and agent["allowedTools"] == source["allowedTools"]
    # the folder's own agent stays on auto
    assert source["model"] == "auto"
    assert f"relay agent model: {model}" in res.stdout


def test_the_relay_model_flag_wins_over_the_environment(rig: Rig) -> None:
    rig.extra_env["CAO_SHELF_RELAY_MODEL"] = "claude-sonnet-4.6"
    assert rig.run("--trust-app", "--relay-model", "claude-haiku-4.5").returncode == 0
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    assert agent["model"] == "claude-haiku-4.5"


@pytest.mark.parametrize(
    "model",
    [
        "",
        "-m",
        "--help",
        "/abs/path",
        "../x",
        ".hidden",
        "claude haiku",
        "claude-haiku-4.5\n",
        "a;rm -rf x",
        "$(id)",
        "model[a][b]",
        "model[]",
        "model[x]y",
        "a//b",
        "a/b:c:d",
        "a:b",
        "x" * 501,
        "a/" + "b" * 255,
    ],
)
@pytest.mark.parametrize("how", ["flag", "env"])
def test_a_bad_relay_model_is_refused_before_anything_changes(
    rig: Rig, model: str, how: str
) -> None:
    if how == "env":
        if model == "":
            pytest.skip("an empty variable counts as unset")
        rig.extra_env["CAO_SHELF_RELAY_MODEL"] = model
        res = rig.run("--trust-app")
    else:
        res = rig.run("--trust-app", "--relay-model", model)
    assert res.returncode == 2, res.stdout
    assert "relay model" in res.stderr
    assert ("--relay-model" if how == "flag" else "CAO_SHELF_RELAY_MODEL") in res.stderr
    assert mutating(rig.calls()) == []
    assert not (rig.state / "installed").exists()


def test_relay_model_needs_a_value(rig: Rig) -> None:
    res = rig.run("--relay-model")
    assert res.returncode == 2
    assert "--relay-model needs a model id" in res.stderr


def test_an_update_with_a_relay_model_says_kiro_crew_keeps_the_installed_model(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    res = rig.run("--trust-app", "--relay-model", "claude-haiku-4.5")
    assert res.returncode == 0, res.stderr
    assert "keeps the model already set on an installed agent" in res.stdout
    assert "--uninstall" in res.stdout


def test_a_reinstall_without_a_gateway_writes_the_relay_model_without_the_note(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    rig.extra_env["STUB_NO_GATEWAY"] = "1"
    res = rig.run("--trust-app", "--relay-model", "claude-haiku-4.5")
    assert res.returncode == 0, res.stderr
    agent = json.loads((rig.state / "installed-agent.json").read_text(encoding="utf-8"))
    assert agent["model"] == "claude-haiku-4.5"
    assert "keeps the model already set" not in res.stdout


def test_the_address_check_is_the_backends_own_not_a_copy() -> None:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "from cao_shelf.cao import validate_cao_url" in text
    assert "from cao_shelf.config import DEFAULTS" in text
    assert 're.search(r\'"cao_url"' not in text


def test_the_folders_own_data_is_never_installed(rig: Rig) -> None:
    (rig.example / "data").mkdir()
    (rig.example / "data" / "relays.json").write_text('{"relays": []}', encoding="utf-8")
    res = rig.run("--trust-app", "--cao-url", "http://127.0.0.1:9990")
    assert res.returncode == 0, res.stderr
    listed = (rig.state / "installed-ls").read_text(encoding="utf-8").split()
    assert "data" not in listed and {"app.json", "agents", "backend"} <= set(listed)
    assert not list((rig.example / "backend").rglob("__pycache__")), "no bytecode left behind"


def test_session_approval_consent_refusal_exits_7_with_the_dashboard_step(rig: Rig) -> None:
    rig.extra_env["STUB_CONSENT"] = "pending"
    res = rig.run("--trust-app")
    assert res.returncode == 7, (res.stdout, res.stderr)
    assert "dashboard" in res.stderr and "consent" in res.stderr.lower()
    assert "apps/detail/cao-shelf" in res.stderr  # Kiro Crew's own link is passed through
    assert "third-party" not in res.stderr.split("install.sh:")[-1]
    assert (rig.state / "installed").exists() and not (rig.state / "enabled").exists()


def test_the_stage_lives_under_home_without_tmpdir_and_is_removed(rig: Rig) -> None:
    assert rig.run("--trust-app").returncode == 0
    src = (rig.state / "install-src").read_text(encoding="utf-8")
    assert src.startswith(str(rig.tmp / "home" / ".cache" / "cao-shelf-install")), src
    assert not Path(src).exists()


def test_the_stage_honours_tmpdir(rig: Rig) -> None:
    scratch = rig.tmp / "scratch-tmp"
    scratch.mkdir()
    rig.extra_env["TMPDIR"] = str(scratch)
    assert rig.run("--trust-app").returncode == 0
    src = (rig.state / "install-src").read_text(encoding="utf-8")
    assert src.startswith(str(scratch)), src
    assert list(scratch.iterdir()) == []
