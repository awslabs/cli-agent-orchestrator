"""Launch command for CLI Agent Orchestrator CLI."""

import os

import click

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    DEFAULT_PROVIDER,
    PROVIDERS,
    SERVER_HOST,
    SERVER_PORT,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.install_service import (
    kiro_install_predates_native_enforcement,
)
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils import api_http
from cli_agent_orchestrator.utils.enforcement import (
    NATIVE,
    describe_enforcement,
    enforcement_for,
    is_install_time,
    is_restricted,
    native_providers,
)
from cli_agent_orchestrator.utils.forwarded_env import (
    ForwardedEnvError,
    validate_forwarded_env,
)
from cli_agent_orchestrator.utils.terminal import (
    poll_until_done,
    sync_backend_from_server,
    wait_for_initial_delivery,
    wait_until_terminal_status,
)

# Providers that require workspace folder access
PROVIDERS_REQUIRING_WORKSPACE_ACCESS = {
    "antigravity_cli",
    "claude_code",
    "codex",
    "copilot_cli",
    "cursor_cli",
    "grok_cli",
    "hermes",
    "kimi_cli",
    "kiro_cli",
    "mcode",
    "opencode_cli",
    "omp",
}

# Validation constraints for ``--env`` forwarded vars live in
# ``utils.forwarded_env`` (shared with the ops-MCP ``launch_session`` tool so
# the two client paths cannot drift) and are mirrored server-side in
# ``TmuxClient._merge_extra_env``. See issue #248.

# How long the non-headless path waits for the provider to settle before
# attaching (advisory; see the attach block below).
_READINESS_WAIT_TIMEOUT = 120

# How long the agent gets to finish MESSAGE on the headless non-async path.
# This budget starts only once the server has CONFIRMED delivery of MESSAGE
# (``wait_for_initial_delivery``), never while the provider is still
# initializing -- otherwise a legitimately slow init consumed the task's time
# and a client deadline could fire while the server went on to run the task,
# so a retry duplicated the work (PR #566 review, haofeif).
_HEADLESS_TASK_TIMEOUT = 300

# Worst case of the server's confirm-and-resubmit loop after the send
# (``_DEFERRED_SUBMIT_CONFIRM_TIMEOUT`` x (1 + ``_DEFERRED_SUBMIT_MAX_RESUBMITS``)
# = 32s) with headroom for the pre-dispatch work (memory injection) and polling.
_DELIVERY_CONFIRM_ALLOWANCE = 60

# Provider init is not one wait of ``provider_init_timeout`` (T). A successful
# path legitimately spans several T-bounded phases: Claude's shell wait +
# startup-prompt handler + readiness wait is 3T (+ settle), Kimi's is up to
# T + 2*max(120, T), Kiro's legacy fallback 4T. The client allowance has to
# cover the longest of those or a valid init blows the deadline while the
# server later runs the task anyway.
_INIT_PHASES = 4


def _init_allowance(agent_profile: str, settings: dict) -> int:
    """Client-side allowance for server-side provider init plus delivery confirmation.

    Derived from the SAME value the server will use: the profile's
    ``provider_init_timeout`` when it declares one (``BaseProvider.get_init_timeout``
    prefers it), else the server setting. A profile that cannot be loaded here
    falls back to the server default rather than failing the launch -- the
    server resolves the real profile itself; this only sizes the wait.
    """
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

    base = int(settings["provider_init_timeout"])
    try:
        profile = load_agent_profile(agent_profile)
    except (FileNotFoundError, RuntimeError):
        profile = None
    if profile is not None and profile.provider_init_timeout is not None:
        base = int(profile.provider_init_timeout)
    return _INIT_PHASES * base + _DELIVERY_CONFIRM_ALLOWANCE


def _is_waiting_on_user(terminal_id: str) -> bool:
    """Return True when the terminal's live status is WAITING_USER_ANSWER.

    Read separately because ``wait_until_terminal_status`` reports only whether
    one of its target statuses was reached, not which one, and the pre-attach
    poll accepts three.

    Best-effort by design: this only decides which advisory line to print before
    attaching, so a transport blip must not turn a successful launch into a
    ``ClickException``. Hence the local except rather than letting it reach the
    caller's ``RequestException`` handler, which reports "Failed to connect to
    cao-server" — untrue here, since the poll above just talked to it.

    An *unparseable* body is covered by the except:
    ``requests.exceptions.JSONDecodeError`` subclasses ``RequestException`` (and
    ``ValueError``) and has since requests 2.27, below this project's
    ``requests>=2.32.0`` floor. A body that parses but isn't an object is not —
    ``[].get`` raises ``AttributeError``, which is no kind of
    ``RequestException`` — so the shape is checked rather than assumed. Without
    that check a 200 carrying a JSON array, string or ``null`` escapes to the
    caller's generic handler and aborts the launch with ``exit 1`` *after* the
    session exists, leaving it orphaned in tmux: the precise failure this
    function's local except is here to prevent.
    """
    try:
        resp = api_http.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=5.0)
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict):
                # bool(): ``payload.get`` is Any, so the comparison is too, and
                # this function is annotated ``-> bool``.
                return bool(payload.get("status") == TerminalStatus.WAITING_USER_ANSWER.value)
    except api_http.exceptions.RequestException:
        pass
    return False


def _parse_env_pairs(pairs):
    """Parse repeated ``KEY=VALUE`` entries into a validated dict.

    Splitting each ``KEY=VALUE`` string (and the last-wins duplicate handling)
    is CLI-specific, but every validation rule is delegated to the shared
    ``validate_forwarded_env`` so ``--env`` and the ops-MCP ``launch_session``
    tool can never drift. Each shared message begins with ``env ``; prefixing
    with ``--`` reproduces the historical ``--env ...`` CLI messages exactly.
    """
    parsed: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise click.ClickException(
                f"--env expects KEY=VALUE (got {raw!r}); did you forget the '='?"
            )
        key, value = raw.split("=", 1)
        parsed[key] = value  # last-wins on a duplicate key
    try:
        return validate_forwarded_env(parsed)
    except ForwardedEnvError as exc:
        raise click.ClickException(f"--{exc}") from exc


@click.command()
@click.argument("message", required=False, default=None)
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider",
    default=None,
    help=f"Provider to use (default: profile provider or {DEFAULT_PROVIDER})",
)
@click.option(
    "--engine",
    "engine",
    type=click.Choice(["v2", "kas"], case_sensitive=True),
    default=None,
    help="Explicit Kiro engine (default: profile engine or v2).",
)
@click.option(
    "--allowed-tools",
    multiple=True,
    help="Override allowedTools (CAO format: execute_bash, fs_read, @cao-mcp-server). Repeatable.",
)
@click.option(
    "--async",
    "is_async",
    is_flag=True,
    help="Send message and return immediately without waiting for completion",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    help=(
        "Skip the confirmation prompt. Does not change the tool policy; whether that "
        "policy is enforced depends on the provider (see the Enforcement line)."
    ),
)
@click.option(
    "--yolo",
    is_flag=True,
    help="[DANGEROUS] Unrestricted tool access AND skip confirmation prompts. "
    "Agent can execute ANY command including aws, rm, curl.",
)
@click.option(
    "--working-directory",
    default=None,
    help="Working directory for the session (default: current directory)",
)
@click.option(
    "--memory",
    "memory",
    is_flag=True,
    help="Also launch a context-manager (memory_manager) terminal for curated memory injection.",
)
@click.option(
    "--env",
    "env_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Forward an env var to the supervisor AND every worker spawned later "
    "in the same session. Repeatable. Values travel in the request body, not "
    "the URL. Rejected: provider prefixes (CLAUDE/CODEX_/__MISE_), the loader, "
    "shell, interpreter and AWS-config startup keys, and >=2048-byte values.",
)
@click.option(
    "--resume-session-id",
    "resume_session_id",
    default=None,
    metavar="SESSION_ID",
    help="Resume a prior Claude Code conversation in the launched supervisor "
    "(claude --resume <id>). claude_code provider only.",
)
def launch(
    message,
    agents,
    session_name,
    headless,
    is_async,
    provider,
    engine,
    allowed_tools,
    auto_approve,
    yolo,
    working_directory,
    memory,
    env_pairs,
    resume_session_id,
):
    """Launch cao session with specified agent profile.

    Without --headless, attaches your terminal to the new session. With
    --headless and MESSAGE, waits for the provider to start and confirm
    delivery, then up to 300s for the reply; --async returns once delivery
    is confirmed.

    Requires a running cao-server.

    \b
    Examples:
      cao launch --agents developer
      cao launch --agents developer --headless --auto-approve "Run the tests"
    """
    try:
        display_dir = working_directory or os.path.realpath(os.getcwd())
        explicit_provider = provider is not None  # True only when --provider was passed
        forwarded_env = _parse_env_pairs(env_pairs) if env_pairs else {}

        # Resolve allowedTools: --yolo > --allowed-tools CLI > profile/role defaults
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
        from cli_agent_orchestrator.utils.tool_mapping import (
            format_tool_summary,
            get_disallowed_tools,
            resolve_allowed_tools,
        )

        resolved_allowed_tools = None
        no_role_set = False
        if yolo:
            resolved_allowed_tools = ["*"]
        elif allowed_tools:
            resolved_allowed_tools = list(allowed_tools)
        else:
            # Load profile to get role-based defaults
            try:
                profile = load_agent_profile(agents)
                mcp_server_names = grantable_server_names(profile)
                no_role_set = not profile.role and not profile.allowedTools
                resolved_allowed_tools = resolve_allowed_tools(
                    profile.allowedTools, profile.role, mcp_server_names
                )
            except (FileNotFoundError, RuntimeError):
                # Profile not found — use developer defaults (backward compatible)
                no_role_set = True
                resolved_allowed_tools = resolve_allowed_tools(None, None, None)

        # Honour profile.provider whenever the user did not pass --provider
        # explicitly. This runs regardless of which permission-resolution
        # branch above fired — provider selection ("which CLI runs this
        # agent?") is orthogonal to tool restrictions ("what is the agent
        # allowed to do?"). Previously this lookup lived inside the ``else``
        # branch and ``--yolo`` / ``--allowed-tools`` silently bypassed the
        # profile's ``provider:`` field, breaking heterogeneous-panel
        # workflows. See issue #239. ``resolve_provider`` falls back to
        # ``DEFAULT_PROVIDER`` when the profile is missing or has no
        # ``provider`` key, so the trailing fallback is no longer needed.
        if provider is None:
            from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

            provider = resolve_provider(agents, DEFAULT_PROVIDER)

        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}"
            )
        # Confirmation / warning prompts
        if provider in PROVIDERS_REQUIRING_WORKSPACE_ACCESS:
            if yolo:
                # --yolo: warn but don't block
                click.echo(click.style("\n[WARNING] --yolo mode enabled", fg="yellow", bold=True))
                click.echo(
                    f"  Agent '{agents}' launching UNRESTRICTED on {provider}.\n"
                    f"  Agent can execute ANY command (aws, rm, curl, read credentials).\n"
                    f"  Directory: {display_dir}\n"
                )
                if provider == "kiro_cli":
                    # The kiro-cli TUI blocks on an interactive "Yes, I accept"
                    # consent dialog when --trust-all-tools is set. CAO answers
                    # it automatically after launch (the provider verifies the
                    # dialog first), so no --legacy-ui suppression is needed —
                    # and --legacy-ui must not be used, because it selects the
                    # v1 engine, which serves the agent no MCP tools.
                    click.echo(
                        "  Note: kiro_cli's --trust-all-tools consent dialog will be "
                        "auto-answered at startup.\n"
                    )
                    # --trust-all-tools only suppresses prompts. What the agent
                    # can use is the installed agent JSON's `tools`, written by
                    # `cao install` from the profile, so --yolo cannot widen it.
                    click.echo(
                        click.style(
                            "  Note: --yolo does not widen kiro_cli's tool set.\n"
                            "  Availability is the installed agent's `tools` list, set at\n"
                            "  cao install time. To get unrestricted access, set\n"
                            "  'allowedTools: [\"*\"]' in the profile and re-run 'cao install'.\n",
                            fg="yellow",
                        )
                    )
                elif provider == "opencode_cli":
                    # opencode's TUI has no runtime skip-permissions flag
                    # (tracked upstream in sst/opencode#8463). Permissions are
                    # install-time only, so --yolo cannot loosen them here.
                    click.echo(
                        click.style(
                            "  Note: --yolo has no runtime effect on opencode_cli.\n"
                            "  Permissions are set at cao install time. To get unrestricted\n"
                            "  access, set 'allowedTools: [\"*\"]' in the profile and re-run\n"
                            "  'cao install'. See docs/opencode-cli.md for details.\n",
                            fg="yellow",
                        )
                    )
            else:
                # Normal launch: show tool summary and confirm
                tool_summary = format_tool_summary(resolved_allowed_tools)
                blocked = get_disallowed_tools(provider, resolved_allowed_tools)
                blocked_summary = ", ".join(blocked) if blocked else "(none)"
                level = enforcement_for(provider)
                if is_install_time(provider):
                    # opencode enforces the permission block, and kiro the
                    # `tools` list, that `cao install` wrote from the profile;
                    # both ignore the list resolved here. Say where the policy
                    # lives rather than printing a deny list next to a native
                    # promise that the installed file may not keep.
                    blocked_summary = (
                        "(set at install time from the installed agent's policy; "
                        "not shown here, and --allowed-tools does not change it)"
                    )
                    if provider == "kiro_cli" and kiro_install_predates_native_enforcement(
                        agents, resolved_allowed_tools
                    ):
                        click.echo(
                            click.style(
                                f"\n  WARNING: the installed Kiro agent '{agents}' has "
                                'tools: ["*"]: it was installed before CAO wrote the\n'
                                "  tool policy into `tools`, so this restriction is NOT "
                                "applied. Re-run:\n"
                                f"    cao install {agents} --provider kiro_cli\n",
                                fg="yellow",
                            )
                        )
                elif level != NATIVE and is_restricted(resolved_allowed_tools) and not blocked:
                    # Providers with no TOOL_MAPPING entry return an empty
                    # deny list; "(none)" would read as "nothing is blocked
                    # because nothing needs to be", which is the opposite of
                    # what is true here.
                    blocked_summary = "(not translated for this provider)"

                click.echo(
                    f"\nAgent '{agents}' launching on {provider}:\n"
                    f"  Allowed:  {tool_summary}\n"
                    f"  Blocked:  {blocked_summary}\n"
                    f"  Enforcement: {describe_enforcement(provider, resolved_allowed_tools)}\n"
                    f"  Directory: {display_dir}\n"
                )
                if level != NATIVE and is_restricted(resolved_allowed_tools):
                    click.echo(
                        click.style(
                            "  WARNING: this provider does not enforce the Blocked list. "
                            "The agent can use any tool.\n"
                            f"  For enforced restrictions use one of: "
                            f"{', '.join(native_providers())}.\n",
                            fg="yellow",
                        )
                    )
                if no_role_set:
                    click.echo(
                        "  Note: No role or allowedTools set — defaulting to 'developer'.\n"
                        "  Add 'role' or 'allowedTools' to your agent profile to control tool access.\n"
                        "  Docs: https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/tool-restrictions.md\n"
                    )
                click.echo(
                    "  To skip this prompt next time, relaunch with --auto-approve\n"
                    "  To remove all restrictions, relaunch with --yolo\n"
                )
                if not auto_approve and not click.confirm("Proceed?", default=True):
                    raise click.ClickException("Launch cancelled by user")

        # Call API to create session — pass working_directory only if explicitly
        # provided. When omitted, the server defaults to its own CWD.
        url = f"http://{SERVER_HOST}:{SERVER_PORT}/sessions"
        params = {
            "agent_profile": agents,
            "working_directory": working_directory or os.getcwd(),
        }
        if explicit_provider:
            params["provider"] = provider
        if engine is not None:
            params["engine"] = engine
        if session_name:
            params["session_name"] = session_name
        if resolved_allowed_tools:
            # Pass as comma-separated string for query param
            params["allowed_tools"] = ",".join(resolved_allowed_tools)
        if memory:
            params["memory_manager"] = "true"
        if resume_session_id:
            params["resume_session_id"] = resume_session_id

        # Hand MESSAGE to the server rather than sending it ourselves.
        # ``initial_message`` on ``POST /sessions`` puts the initial terminal on
        # the existing deferred-init path (``session_service.create_session`` ->
        # ``create_terminal(defer_init=True)``): the server responds as soon as
        # the terminal record exists, then finishes provider init, delivers the
        # message, and confirms/re-submits if the TUI swallowed it.
        #
        # The CLI used to create the session and then issue a SEPARATE
        # ``POST /terminals/{id}/input``. Because ``POST /sessions`` ran the
        # provider's full ``initialize()`` inline, a slow cold start outlived
        # the client's read timeout: ``requests`` raised ``ReadTimeout``,
        # ``launch`` reported "Failed to connect to cao-server", and that second
        # request never happened — MESSAGE was silently dropped even though the
        # session, the terminal and a healthy idle TUI all existed server-side,
        # and nothing retried because from the server's point of view the launch
        # had succeeded. Server-side delivery closes the window for every
        # provider at once; ``cao launch`` was the last client still doing its
        # own create-then-send (mcp_server and ops_mcp_server already pass
        # ``initial_message``).
        #
        # Headless only: that is the path that used to send MESSAGE. A
        # non-headless launch attaches instead and has never delivered MESSAGE,
        # and deferring init there would make the pre-attach readiness poll
        # below race the agent's first turn.
        server_delivers_message = bool(message) and headless

        # Forwarded env vars travel in the JSON body so values (which may
        # contain secrets) don't end up in cao-server's HTTP access log.
        # MESSAGE rides in the body for the same reason, plus URL-length. See
        # issue #248 and ``CreateSessionBody``.
        settings = get_server_settings()
        post_kwargs: dict = {"params": params, "timeout": settings["mcp_request_timeout"]}
        body: dict = {}
        if forwarded_env:
            body["env_vars"] = forwarded_env
        if server_delivers_message:
            body["initial_message"] = message
        if body:
            post_kwargs["json"] = body

        response = api_http.post(url, **post_kwargs)
        response.raise_for_status()

        terminal = response.json()

        click.echo(f"Session created: {terminal['session_name']}")
        click.echo(f"Terminal created: {terminal['name']}")

        # Attach to tmux session unless headless. Wait for the provider to
        # finish initializing first — otherwise tmux attach races with the
        # TUI's input handler wiring, resizes the pty mid-init, and the TUI
        # silently drops keystrokes. See issue #220. The wait is advisory:
        # if it times out we still attach so the user can inspect the
        # half-initialized session rather than orphan it in tmux.
        #
        # WAITING_USER_ANSWER counts as settled here, not as a stall. A provider
        # can finish initializing on a screen that legitimately needs the
        # operator — Codex's first-run login menu is the case that forced this —
        # and such a screen never becomes IDLE on its own, so waiting for IDLE
        # burned the full ``_READINESS_WAIT_TIMEOUT`` and then blamed init for a
        # pane that was simply waiting for a human. Safe because non-headless
        # ``POST /sessions`` initializes synchronously (no ``initial_message``,
        # see ``server_delivers_message`` above), so by the time this poll runs
        # every provider's startup handler has already returned: a
        # WAITING_USER_ANSWER here is a settled prompt, not a dialog caught
        # mid-dismissal. If non-headless init is ever deferred, this poll would
        # race the startup handler and attaching early would resize the pty
        # mid-init — issue #220 again — so that change must gate attach on init
        # completion rather than reuse this set.
        if not headless:
            # Align the CLI's backend singleton with the running server.
            # Without this, ``cao-server --terminal herdr`` + no config.json
            # entry causes the CLI to default to tmux. See issue #308.
            sync_backend_from_server()
            ready = wait_until_terminal_status(
                terminal["id"],
                {
                    TerminalStatus.IDLE,
                    TerminalStatus.COMPLETED,
                    TerminalStatus.WAITING_USER_ANSWER,
                },
                timeout=_READINESS_WAIT_TIMEOUT,
            )
            if not ready:
                click.echo(
                    click.style(
                        f"  Warning: {terminal['id']} did not reach idle within "
                        f"{_READINESS_WAIT_TIMEOUT}s — attaching anyway; input may be "
                        "unreliable until init completes.",
                        fg="yellow",
                    )
                )
            elif _is_waiting_on_user(terminal["id"]):
                click.echo(
                    click.style(
                        f"  {terminal['id']} is waiting for an answer in the pane "
                        "(a first-run sign-in, for example) — complete it after "
                        "attaching.",
                        fg="yellow",
                    )
                )
            get_backend().attach_session(terminal["session_name"])
        elif message:
            # Nothing to send: the server took MESSAGE in the create body above
            # and owns init, delivery and re-submission. What the CLI waits for
            # is the server's DURABLE verdict on that delivery, not a status
            # sample: ``initial_delivery`` on the terminal row goes pending ->
            # delivered once the worker has been observed working on MESSAGE
            # (post-dispatch evidence), or -> failed with the reason (init error,
            # task never started, worker parked on a prompt, server restarted
            # before confirmation). Waiting on it here restores the contract the
            # client-side send had -- ``--async`` exits 0 only once the message
            # is known to have reached the agent, and non-zero with the reason
            # otherwise -- without reintroducing the request that raced init.
            # The allowance is sized from the provider's real init path, not a
            # flat 120s (see ``_init_allowance``).
            wait_for_initial_delivery(terminal["id"], timeout=_init_allowance(agents, settings))
            if is_async:
                click.echo(f"Message delivered to {terminal['name']}. Running in background.")
                return
            # The task budget starts HERE, at confirmed delivery, so a slow but
            # successful init cannot eat into it. The terminal is no longer
            # masked, so ``poll_until_done``'s own working-then-idle gate judges
            # completion on the task's activity only.
            poll_until_done(terminal["id"], timeout=_HEADLESS_TASK_TIMEOUT)
            output_resp = api_http.get(
                f"{API_BASE_URL}/terminals/{terminal['id']}/output",
                params={"mode": "last"},
                timeout=settings["mcp_request_timeout"],
            )
            output_resp.raise_for_status()
            output = output_resp.json().get("output", "")
            if output:
                click.echo(output)

    except api_http.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {str(e)}")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))
