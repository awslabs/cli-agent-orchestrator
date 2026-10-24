"""create_terminal bounds every child by the caller's STORED delegation ceiling.

Real SQLite rows (``isolated_memory_db``) so the laundering test reads the
intermediate terminal's recorded ceiling the way the server does; tmux, the
provider and the FIFO pipeline are mocked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.decisions.types import LaunchPlan
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.delegation import DelegationCeilingError

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

_TS = "cli_agent_orchestrator.services.terminal_service"
REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]
SUPERVISOR = ROLE_TOOL_DEFAULTS["supervisor"]
DEVELOPER = ROLE_TOOL_DEFAULTS["developer"]

PROFILES = {
    "reviewer": AgentProfile(name="reviewer", description="r", role="reviewer"),
    "developer": AgentProfile(name="developer", description="d", role="developer"),
    # A user profile that CLAIMS the supervisor role (ceiling * by default).
    "sup": AgentProfile(name="sup", description="s", role="supervisor"),
    "widened_reviewer": AgentProfile(
        name="widened_reviewer",
        description="r",
        role="reviewer",
        delegateTools=["execute_bash", "fs_*"],
    ),
    "unrestricted": AgentProfile(name="unrestricted", description="u", allowedTools=["*"]),
    "shell_worker": AgentProfile(
        name="shell_worker", description="w", allowedTools=["fs_read", "execute_bash"]
    ),
}


@pytest.fixture
def launch(monkeypatch):
    """Patch out tmux, the provider, FIFOs and ids; keep the database real."""
    ids = iter(f"{n:08x}" for n in range(0xA0000000, 0xA00000FF))
    monkeypatch.setattr(f"{_TS}.generate_terminal_id", lambda: next(ids))
    monkeypatch.setattr(f"{_TS}.generate_window_name", lambda p: f"{p}-win")
    monkeypatch.setattr(f"{_TS}.generate_session_name", lambda: "cao-sess")
    monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda name: PROFILES[name])
    monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
    monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
    provider = AsyncMock()
    provider.initialize.return_value = True
    pm = MagicMock()
    pm.create_provider.return_value = provider
    monkeypatch.setattr(f"{_TS}.provider_manager", pm)
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "win"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)

    async def _create(profile, **kw):
        return await create_terminal(
            "claude_code", profile, session_name="cao-sess", new_session=False, **kw
        )

    return _create


def _seed(terminal_id, profile, allowed, ceiling):
    db_create_terminal(
        terminal_id,
        "cao-sess",
        f"{profile}-win",
        "claude_code",
        profile,
        allowed,
        delegate_tools=ceiling,
    )


@pytest.mark.asyncio
async def test_three_generation_laundering_is_bounded_by_the_stored_ceiling(launch):
    """reviewer -> supervisor-role profile -> developer must still be refused.

    The intermediate profile says ``role: supervisor`` (declared ceiling ``*``),
    but its ROW records the reviewer's ceiling, and the grandchild is bounded by
    that row, never by what the intermediate's profile claims.
    """
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)  # gen 1, operator-launched

    middle = await launch("sup", caller_id="0000beef")  # gen 2: fits (its tools ⊆ reviewer)
    assert middle.allowed_tools == SUPERVISOR
    assert middle.delegate_tools == REVIEWER, "declared * must be capped by the parent's row"
    assert get_terminal_metadata(middle.id)["delegate_tools"] == REVIEWER

    with pytest.raises(DelegationCeilingError) as excinfo:  # gen 3
        await launch("developer", caller_id=middle.id)
    message = str(excinfo.value)
    assert middle.id in message and "profile 'sup'" in message and "'developer'" in message
    assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]
    assert "delegateTools" in message


@pytest.mark.asyncio
async def test_reviewer_cannot_launch_developer_but_supervisor_can(launch):
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    _seed("0000cafe", "supervisor", SUPERVISOR, ["*"])

    with pytest.raises(DelegationCeilingError):
        await launch("developer", caller_id="0000beef")
    child = await launch("developer", caller_id="0000cafe")
    assert child.allowed_tools == DEVELOPER
    assert child.delegate_tools == DEVELOPER  # developer's default ceiling is its own list


@pytest.mark.asyncio
async def test_client_allowed_tools_string_is_bounded(launch):
    """The in-session API path stores the client's list; the server must not
    trust it beyond the caller's ceiling (``allowed_tools=*`` from a reviewer)."""
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)

    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("reviewer", caller_id="0000beef", allowed_tools=["*"])
    assert excinfo.value.excess == ["*"]

    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("reviewer", caller_id="0000beef", allowed_tools=["fs_read", "execute_bash"])
    assert excinfo.value.excess == ["execute_bash"]

    ok = await launch("reviewer", caller_id="0000beef", allowed_tools=["fs_read"])
    assert ok.allowed_tools == ["fs_read"]
    assert ok.delegate_tools == ["fs_read"]


@pytest.mark.asyncio
async def test_missing_caller_row_fails_closed(launch):
    with pytest.raises(DelegationCeilingError, match="has no registered row"):
        await launch("reviewer", caller_id="deadbeef")


@pytest.mark.asyncio
async def test_legacy_caller_row_is_bounded_by_its_own_allowlist(launch):
    """A supervisor row from before the column (delegate_tools NULL) cannot widen
    until relaunched: strict reading, documented in the migration note."""
    _seed("0000cafe", "supervisor", SUPERVISOR, None)
    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("developer", caller_id="0000cafe")
    assert "relaunch" in str(excinfo.value).lower()
    assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]


@pytest.mark.asyncio
async def test_declared_delegate_tools_widen_a_restricted_parent(launch):
    """The remedy works: a reviewer that declares delegateTools may launch a
    child holding tools the reviewer itself lacks, and the child's own ceiling
    is capped by the parent's."""
    widened = [*REVIEWER, "execute_bash", "fs_*"]
    _seed("0000beef", "widened_reviewer", REVIEWER, widened)
    child = await launch("shell_worker", caller_id="0000beef")
    assert child.allowed_tools == ["fs_read", "execute_bash"]
    assert child.delegate_tools == ["fs_read", "execute_bash"]


@pytest.mark.asyncio
async def test_declared_delegate_tools_still_refuse_what_they_omit(launch):
    widened = [*REVIEWER, "execute_bash", "fs_*"]
    _seed("0000beef", "widened_reviewer", REVIEWER, widened)
    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("developer", caller_id="0000beef")
    assert excinfo.value.excess == ["web_fetch"]


@pytest.mark.asyncio
async def test_workflow_step_inherits_the_callers_ceiling(launch):
    """delegating_terminal_id bounds a step whose callback parent is not the caller."""
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    with pytest.raises(DelegationCeilingError):
        await launch("developer", delegating_terminal_id="0000beef")
    step = await launch("reviewer", delegating_terminal_id="0000beef")
    assert step.caller_id is None
    assert step.delegate_tools == REVIEWER


@pytest.mark.asyncio
async def test_asserted_caller_ceiling_bounds_a_remote_child(launch):
    with pytest.raises(DelegationCeilingError):
        await launch("developer", caller_ceiling=REVIEWER)
    child = await launch("reviewer", caller_ceiling=REVIEWER)
    assert child.delegate_tools == REVIEWER
    wide = await launch("developer", caller_ceiling=["*"])
    assert wide.delegate_tools == DEVELOPER


@pytest.mark.asyncio
async def test_asserted_ceiling_is_rejected_with_a_local_caller(launch):
    _seed("0000cafe", "supervisor", SUPERVISOR, ["*"])
    with pytest.raises(ValueError, match="caller_ceiling is only accepted"):
        await launch("developer", caller_id="0000cafe", caller_ceiling=["*"])


@pytest.mark.asyncio
async def test_operator_launch_records_the_declared_ceiling(launch):
    sup = await launch("sup")
    assert sup.delegate_tools == ["*"]
    rev = await launch("reviewer")
    assert rev.delegate_tools == REVIEWER
    yolo = await launch("reviewer", allowed_tools=["*"])
    assert yolo.delegate_tools == ["*"]
    unrestricted = await launch("unrestricted")
    assert unrestricted.delegate_tools == ["*"]


# ---------------------------------------------------------------------------
# Review B, F3(b): a remote-shaped create (callback terminal in env) must carry
# the supervisor's ceiling, and a local callback row bounds the assertion.
# ---------------------------------------------------------------------------
from cli_agent_orchestrator.constants import (  # noqa: E402
    CALLBACK_TERMINAL_ID_ENV,
    CALLBACK_URL_ENV,
)

_REMOTE_ENV = {CALLBACK_URL_ENV: "http://sup:9889", CALLBACK_TERMINAL_ID_ENV: "0000beef"}


@pytest.mark.asyncio
async def test_remote_shaped_create_without_ceiling_is_refused(launch):
    with pytest.raises(DelegationCeilingError, match="must carry caller_ceiling"):
        await launch("developer", env_vars=_REMOTE_ENV)


@pytest.mark.asyncio
async def test_remote_shaped_create_with_ceiling_is_bounded(launch):
    with pytest.raises(DelegationCeilingError):
        await launch("developer", env_vars=_REMOTE_ENV, caller_ceiling=REVIEWER)
    child = await launch("reviewer", env_vars=_REMOTE_ENV, caller_ceiling=REVIEWER)
    assert child.delegate_tools == REVIEWER


@pytest.mark.asyncio
async def test_local_callback_row_bounds_an_edited_ceiling(launch):
    """Loopback: the callback terminal has a row here. An asserted ``*`` that
    exceeds that row's recorded ceiling is refused; the row is authoritative."""
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    with pytest.raises(DelegationCeilingError, match="recorded ceiling is authoritative"):
        await launch("developer", env_vars=_REMOTE_ENV, caller_ceiling=["*"])
    child = await launch("reviewer", env_vars=_REMOTE_ENV, caller_ceiling=REVIEWER)
    assert child.delegate_tools == REVIEWER


# ---------------------------------------------------------------------------
# Review B, F6: a double-NULL legacy caller row refuses (no profile re-read).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_double_null_legacy_caller_refuses_without_reading_the_profile(launch, monkeypatch):
    _seed("0000dead", "reviewer", None, None)
    PROFILES["reviewer"] = AgentProfile(name="reviewer", description="edited", allowedTools=["*"])
    try:
        with pytest.raises(DelegationCeilingError, match="could not be resolved"):
            await launch("developer", caller_id="0000dead")
    finally:
        PROFILES["reviewer"] = AgentProfile(name="reviewer", description="r", role="reviewer")


# ---------------------------------------------------------------------------
# Review B, F4: Kiro runs the INSTALLED policy; a delegated create whose installed
# agent grants more than the bounded row is refused before allocation.
# ---------------------------------------------------------------------------
@pytest.fixture
def kiro_install(tmp_path, monkeypatch):
    import json

    from cli_agent_orchestrator.services import install_service
    from cli_agent_orchestrator.utils.tool_mapping import kiro_agent_tools

    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(f"{_TS}.probe_kiro_capabilities", lambda engine, requested: MagicMock())

    def _write(profile, allowed):
        (tmp_path / f"{profile}.json").write_text(json.dumps({"tools": kiro_agent_tools(allowed)}))

    return _write


@pytest.mark.asyncio
async def test_kiro_installed_policy_wider_than_the_row_is_refused(launch, kiro_install):
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    kiro_install("developer", ["execute_bash", "fs_*", "@cao-mcp-server"])

    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    assert "shell" in excinfo.value.excess
    assert "cao install developer" in str(excinfo.value)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_kiro_installed_policy_within_the_row_launches(launch, kiro_install):
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    kiro_install("developer", ["fs_read"])
    child = await create_terminal(
        "kiro_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        caller_id="0000beef",
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


@pytest.mark.asyncio
async def test_kiro_operator_launch_keeps_the_warning_path(launch, kiro_install):
    """No caller: the operator is the authority; the installed-vs-requested mismatch
    stays the launch-time warning, not a refusal."""
    kiro_install("developer", ["execute_bash", "fs_*", "@cao-mcp-server"])
    child = await create_terminal(
        "kiro_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


# ---------------------------------------------------------------------------
# Review B round 3, R1: the reviewer's three-generation glob case to a real launch.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_glob_shaped_child_cannot_widen_to_a_grandchild(launch):
    parent = ["fs_read", "@plugin-?"]
    _seed("0000beef", "narrow-parent", parent, parent)
    PROFILES["wide-child"] = AgentProfile(
        name="wide-child", description="w", allowedTools=["fs_read", "@plugin-*"]
    )
    PROFILES["shell-user"] = AgentProfile(
        name="shell-user", description="s", allowedTools=["fs_read", "@plugin-shell"]
    )
    PROFILES["plugin-a-user"] = AgentProfile(
        name="plugin-a-user", description="a", allowedTools=["fs_read", "@plugin-a"]
    )
    try:
        with pytest.raises(DelegationCeilingError) as excinfo:
            await launch("wide-child", caller_id="0000beef")
        assert excinfo.value.excess == ["@plugin-*"]
        with pytest.raises(DelegationCeilingError):
            await launch("shell-user", caller_id="0000beef")
        ok = await launch("plugin-a-user", caller_id="0000beef")  # concrete name within @plugin-?
        assert ok.delegate_tools == ["fs_read", "@plugin-a"]
    finally:
        for name in ("wide-child", "shell-user", "plugin-a-user"):
            PROFILES.pop(name, None)


# ---------------------------------------------------------------------------
# Review B round 3, R3: OpenCode runs the installed permission block.
# ---------------------------------------------------------------------------
@pytest.fixture
def opencode_install(tmp_path, monkeypatch):
    import frontmatter

    from cli_agent_orchestrator.providers import opencode_cli
    from cli_agent_orchestrator.utils.opencode_permissions import cao_tools_to_opencode_permission

    config = tmp_path / "opencode"
    (config / "agents").mkdir(parents=True)
    monkeypatch.setattr(opencode_cli, "OPENCODE_CONFIG_DIR", config)

    def _write(profile, allowed):
        (config / "agents" / f"{profile}.md").write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    "x",
                    description="d",
                    mode="all",
                    permission=cao_tools_to_opencode_permission(allowed),
                )
            )
        )

    return _write


@pytest.mark.asyncio
async def test_opencode_installed_policy_wider_than_the_row_is_refused(launch, opencode_install):
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    opencode_install("developer", ["fs_*", "execute_bash"])
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "opencode_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    assert "bash" in excinfo.value.excess
    assert "cao install developer --provider opencode_cli" in str(excinfo.value)


@pytest.mark.asyncio
async def test_opencode_installed_policy_within_the_row_launches(launch, opencode_install):
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    opencode_install("developer", ["fs_read"])
    child = await create_terminal(
        "opencode_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        caller_id="0000beef",
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


@pytest.mark.asyncio
async def test_opencode_operator_launch_keeps_the_warning_path(launch, opencode_install):
    opencode_install("developer", ["fs_*", "execute_bash"])
    child = await create_terminal(
        "opencode_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


def test_installed_opencode_permission_reader(tmp_path):
    import frontmatter

    from cli_agent_orchestrator.services.install_service import installed_opencode_permission

    agents = tmp_path / "agents"
    agents.mkdir()
    assert installed_opencode_permission("developer", agents) is None
    (agents / "developer.md").write_text(
        frontmatter.dumps(frontmatter.Post("x", permission={"bash": "allow", "read": "allow"}))
    )
    assert installed_opencode_permission("developer", agents) == {"bash": "allow", "read": "allow"}
    (agents / "odd.md").write_text(frontmatter.dumps(frontmatter.Post("x", permission="allow")))
    assert installed_opencode_permission("odd", agents) is None


# ---------------------------------------------------------------------------
# Round 5: OpenCode Step 0d fails closed when CAO cannot read the installed
# policy, mirroring the Kiro fail-closed tests below. @haofeif re-review at
# fc4af221, P2.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opencode_bounded_launch_without_an_installed_agent_file_is_refused(
    launch, opencode_install
):
    """``installed_opencode_permission`` answers None for a missing agent file. On
    OpenCode the installed file IS the policy, so a bounded create that cannot read
    it is refused with the install remedy rather than skipping the comparison."""
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    # No opencode_install(...): the agents directory holds no developer.md.
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "opencode_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    message = str(excinfo.value)
    assert "no installed OpenCode agent file" in message
    assert "cao install developer --provider opencode_cli" in message
    assert excinfo.value.status_code == 403
    assert excinfo.value.parent_id == "0000beef"


@pytest.mark.asyncio
async def test_opencode_bounded_launch_without_a_permission_block_is_refused(
    launch, opencode_install
):
    """An agent file that exists but carries no dict ``permission:`` block is the
    same fail-open gap as a missing file: the installed policy still cannot be
    read, so the create must refuse rather than skip Step 0d."""
    import frontmatter

    from cli_agent_orchestrator.providers import opencode_cli
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    (opencode_cli.OPENCODE_CONFIG_DIR / "agents" / "developer.md").write_text(
        frontmatter.dumps(frontmatter.Post("x", description="d", mode="all"))
    )
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "opencode_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    assert "no installed OpenCode agent file" in str(excinfo.value)


@pytest.mark.asyncio
async def test_opencode_operator_launch_without_an_installed_file_is_not_refused(
    launch, opencode_install
):
    """The operator path is untouched: no caller, no ceiling comparison to fail closed on."""
    child = await create_terminal(
        "opencode_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


@pytest.mark.asyncio
async def test_opencode_unrestricted_delegator_without_an_installed_file_is_not_refused(
    launch, opencode_install
):
    """A ``*`` ceiling has nothing the installed file could exceed; the warning path stays."""
    _seed("0000beef", "sup", SUPERVISOR, ["*"])
    child = await create_terminal(
        "opencode_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        caller_id="0000beef",
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


# ---------------------------------------------------------------------------
# P2b: an installed `permission:` block that is readable but incomplete (an
# empty dict, or a partial mapping) must still be read as allowing whatever
# it does not explicitly deny -- OpenCode treats an omitted tool as allowed.
# Found verifying the round-5 fix (fc4af221); not raised by the reviewer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opencode_empty_permission_block_defaults_every_tool_to_allow(
    launch, tmp_path, monkeypatch
):
    """``permission: {}`` grants nothing explicitly, but OpenCode's own defaults
    allow everything not denied, so a bounded create narrower than that must
    still be refused rather than treated as "nothing wider"."""
    import frontmatter

    from cli_agent_orchestrator.providers import opencode_cli
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    config = tmp_path / "opencode"
    agents = config / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(opencode_cli, "OPENCODE_CONFIG_DIR", config)
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    (agents / "developer.md").write_text(
        frontmatter.dumps(frontmatter.Post("x", description="d", mode="all", permission={}))
    )
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "opencode_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    assert "bash" in excinfo.value.excess


@pytest.mark.asyncio
async def test_opencode_partial_permission_block_defaults_omitted_tools_to_allow(
    launch, tmp_path, monkeypatch
):
    """``permission: {read: allow}`` says nothing about ``bash``; an omitted key
    is not a deny, so the installed policy still grants it beyond a bounded
    allowlist of only ``fs_read``."""
    import frontmatter

    from cli_agent_orchestrator.providers import opencode_cli
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    config = tmp_path / "opencode"
    agents = config / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(opencode_cli, "OPENCODE_CONFIG_DIR", config)
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    (agents / "developer.md").write_text(
        frontmatter.dumps(
            frontmatter.Post("x", description="d", mode="all", permission={"read": "allow"})
        )
    )
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "opencode_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    assert "bash" in excinfo.value.excess


# ---------------------------------------------------------------------------
# Round 6: an OpenCode agent file whose front matter does not parse must take
# the same refusal as a missing file, not escape Step 0d as a 500. @haofeif
# re-review at 95e580fd, P3.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opencode_bounded_launch_with_unparseable_front_matter_is_refused(
    launch, opencode_install
):
    """``frontmatter.load`` raises ``yaml.YAMLError`` for bad YAML, which is not a
    ``ValueError``. Before the fix that escaped ``installed_opencode_permission``
    and Step 0d, so the API answered 500 instead of the refusal with the install
    remedy. The reader must answer None so the same fail-closed path applies."""
    from cli_agent_orchestrator.providers import opencode_cli
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    (opencode_cli.OPENCODE_CONFIG_DIR / "agents" / "developer.md").write_text(
        "---\npermission: [unclosed\nmode: all\n---\nbody\n"
    )
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "opencode_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    message = str(excinfo.value)
    assert "no installed OpenCode agent file" in message
    assert "cao install developer --provider opencode_cli" in message
    assert excinfo.value.status_code == 403


# The class behind that P3: "CAO cannot read the installed policy" is not one
# exception type per reader but every way a file on disk can fail to yield a
# policy. Both readers must answer None for all of them, because an exception
# escaping either one turns the fail-closed refusal into a 500 with no remedy.
# Enumerated here so the next parser quirk is caught by a table row, not a
# reviewer.
_CORRUPT_OPENCODE_FILES = {
    "yaml_parser_error": b"---\npermission: [unclosed\nmode: all\n---\nbody\n",
    "yaml_scanner_error_tab_indent": b"---\npermission:\n\tbash: allow\n---\nbody\n",
    "yaml_composer_error_undefined_alias": b"---\na: &x [1]\npermission: *y\n---\n",
    "scalar_front_matter": b"---\njust a string\n---\nbody\n",
    "list_front_matter": b"---\n- a\n- b\n---\nbody\n",
    "non_utf8_bytes": b"---\npermission: \xff\xfe\n---\n",
    "empty_file": b"",
    "no_front_matter": b"just a body with no block\n",
    "permission_is_a_string": b"---\npermission: allow\n---\n",
    "permission_values_not_strings": b"---\npermission: {bash: {x: y}}\n---\n",
}

_CORRUPT_KIRO_FILES = {
    "json_decode_error": b"{not json",
    "non_utf8_bytes": b"\xff\xfe",
    "empty_file": b"",
    "top_level_list": b"[1, 2]",
    "tools_not_a_list": b'{"tools": "fs_read"}',
    "tools_not_all_strings": b'{"tools": ["fs_read", 1]}',
}


@pytest.mark.parametrize("shape", sorted(_CORRUPT_OPENCODE_FILES))
def test_installed_opencode_permission_answers_none_for_every_corrupt_file(tmp_path, shape):
    from cli_agent_orchestrator.services.install_service import installed_opencode_permission

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "developer.md").write_bytes(_CORRUPT_OPENCODE_FILES[shape])
    assert installed_opencode_permission("developer", agents) is None


def test_installed_opencode_permission_answers_none_for_a_directory(tmp_path):
    from cli_agent_orchestrator.services.install_service import installed_opencode_permission

    agents = tmp_path / "agents"
    (agents / "developer.md").mkdir(parents=True)
    assert installed_opencode_permission("developer", agents) is None


@pytest.mark.parametrize("shape", sorted(_CORRUPT_KIRO_FILES))
def test_installed_kiro_tools_answers_none_for_every_corrupt_file(tmp_path, monkeypatch, shape):
    from cli_agent_orchestrator.services import install_service

    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", str(tmp_path))
    (tmp_path / "developer.json").write_bytes(_CORRUPT_KIRO_FILES[shape])
    assert install_service.installed_kiro_tools("developer") is None


def test_installed_kiro_tools_answers_none_for_a_directory(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import install_service

    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", str(tmp_path))
    (tmp_path / "developer.json").mkdir()
    assert install_service.installed_kiro_tools("developer") is None


# ---------------------------------------------------------------------------
# Round 4: Kiro Step 0d fails closed on a missing agent file; the idempotency
# fingerprint hashes the delegating terminal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kiro_bounded_launch_without_an_installed_agent_file_is_refused(launch, kiro_install):
    """``installed_kiro_tools`` answers None for a missing or unreadable file. On Kiro
    the installed file IS the policy, so a bounded create that cannot read it is
    refused with the install remedy rather than skipping the comparison."""
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    # No kiro_install(...): the agent directory holds no developer.json.
    with pytest.raises(InstalledPolicyExceedsAllowlistError) as excinfo:
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )
    message = str(excinfo.value)
    assert "no installed Kiro agent file" in message
    assert "cao install developer --provider kiro_cli" in message
    assert excinfo.value.status_code == 403
    assert excinfo.value.parent_id == "0000beef"


@pytest.mark.asyncio
async def test_kiro_bounded_launch_with_an_unreadable_agent_file_is_refused(
    launch, kiro_install, tmp_path
):
    from cli_agent_orchestrator.utils.delegation import InstalledPolicyExceedsAllowlistError

    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    (tmp_path / "developer.json").write_text("{not json")
    with pytest.raises(InstalledPolicyExceedsAllowlistError):
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-sess",
            new_session=False,
            caller_id="0000beef",
            allowed_tools=["fs_read"],
        )


@pytest.mark.asyncio
async def test_kiro_operator_launch_without_an_installed_file_is_not_refused(launch, kiro_install):
    """The operator path is untouched: no caller, no ceiling comparison to fail closed on."""
    child = await create_terminal(
        "kiro_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


@pytest.mark.asyncio
async def test_kiro_unrestricted_delegator_without_an_installed_file_is_not_refused(
    launch, kiro_install
):
    """A ``*`` ceiling has nothing the installed file could exceed; the warning path stays."""
    _seed("0000beef", "sup", SUPERVISOR, ["*"])
    child = await create_terminal(
        "kiro_cli",
        "developer",
        session_name="cao-sess",
        new_session=False,
        caller_id="0000beef",
        allowed_tools=["fs_read"],
    )
    assert child.allowed_tools == ["fs_read"]


def test_fingerprint_hashes_the_delegating_terminal():
    """Two keyed creates differing only in the bounding terminal must not resolve to
    one terminal: the bound decides the recorded ceiling, a privilege, like
    ``allowed_tools``. Requests without the field keep their pre-existing digest."""
    from cli_agent_orchestrator.services.terminal_service import _request_fingerprint

    base = dict(
        provider="claude_code",
        agent_profile="memory_manager",
        session_name="cao-sess",
        working_directory=None,
        caller_id=None,
        model=None,
        use_worktree=False,
        engine=None,
        allowed_tools=None,
        env_vars=None,
        resume_session_id=None,
        initial_message=None,
        initial_message_orchestration_type=None,
    )
    bound_a = _request_fingerprint(**base, delegating_terminal_id="0000beef")
    bound_b = _request_fingerprint(**base, delegating_terminal_id="0000cafe")
    unbound = _request_fingerprint(**base)
    assert bound_a != bound_b
    assert bound_a != unbound
    assert unbound == _request_fingerprint(**base, delegating_terminal_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["model-small", "model-large", None])
async def test_decision_routed_launch_keeps_the_stored_ceiling(launch, model):
    """A #954 model decision cannot widen #948's recorded tool authority."""
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("developer", caller_id="0000beef", launch_plan=LaunchPlan(model=model))
    assert "execute_bash" in excinfo.value.excess
    assert get_terminal_metadata("a0000000") is None
