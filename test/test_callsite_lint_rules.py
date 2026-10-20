"""Three call-site lint rules built on ``test/_support/callsite_lint.py`` (#1005).

Each rule closes one half of a defect class the repo has already paid for:

* **Rule 1 (enum subsets, #896)** -- ``dispatch_input``'s dialog guard asked
  "is this machine-originated?" by testing membership in a hand-written
  ``{ASSIGN, HANDOFF}`` subset of ``OrchestrationType``. ``SEND_MESSAGE``
  walked past it and got typed into an open AskUserQuestion dialog. Nothing in
  the tree linked the literal to the enum, and ``mypy`` sees no exhaustiveness
  obligation on ``in {...}``.
* **Rule 2 (timeout literals, #710 / #787)** -- a bare number next to
  ``timeout=`` records the author's guess, not the bound it has to respect.
  #787 is the shape: ``mcp_server`` helpers kept a hard-coded ``30`` after the
  setting that was meant to govern them landed, so raising the setting changed
  nothing and left no trace in the logs.
* **Rule 3 (staleness gate, #735)** -- a poll that accepts whatever frame is on
  screen can report "done" for a turn that had not started. The fix is a
  generation/sequence floor at every call site.

All three rules are **ratchets**. A new unexplained call site fails; so does a
site beyond its row's reviewed occurrence count (a key collision does not
grant unlimited headroom); so does an allowlist row that no longer matches
anything, so the tables can only shrink. The failure message names
``file:line`` and what to do.

Cost: one ``ast.parse`` of every module under ``src/`` (269 files), cached
across the rules by ``callsite_lint.parse_sources``. Measured at well under a
second on this tree; see the PR description for the number.
"""

from __future__ import annotations

from test._support import callsite_lint as cl

import pytest

# ===========================================================================
# Rule 1: an Enum-subset membership literal must be deliberate
# ===========================================================================
#
# Keys are ``(module, enum name, sorted member names)`` -- deliberately free of
# line numbers so a reviewed row survives reformatting, and so one row covers
# the repetitions of a single idiom inside one module. A site may instead carry
# an inline ``# subset-ok: <reason>`` comment, which is the better choice for a
# one-off; the table is for idioms that repeat.
#
# The two sites #1005 named as seeds (``services/agui_stream.py`` ~161 and
# ``services/event_primitives.py`` ~69-70) are deliberately absent: neither is
# an Enum subset. Both compare *strings* read out of an untyped event detail
# dict, so no Enum-keyed rule can see them. That stringly-typed sibling class
# needs its own rule and is called out in the PR description instead of being
# papered over with a row here.

ENUM_SUBSET_ALLOWLIST = {
    # ---- terminal lifecycle: "the agent is not busy" ----
    # TerminalStatus has six members; {COMPLETED, IDLE} is the standard
    # "settled, safe to act" pair. ERROR/UNKNOWN/PROCESSING/WAITING_USER_ANSWER
    # are all deliberately excluded: acting on them is the bug.
    (
        "cli_agent_orchestrator/cli/commands/session.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "settled-status pair; a session is reported done only when not busy",
    (
        "cli_agent_orchestrator/providers/codex.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "launch readiness gate: the CLI has reached a prompt",
    (
        "cli_agent_orchestrator/providers/copilot_cli.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "launch readiness gate: the CLI has reached a prompt",
    (
        "cli_agent_orchestrator/providers/grok_cli.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "launch readiness gate: the CLI has reached a prompt",
    (
        "cli_agent_orchestrator/providers/kimi_cli.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "launch readiness gate: the CLI has reached a prompt",
    (
        "cli_agent_orchestrator/providers/minimax_code.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "launch readiness gate: the CLI has reached a prompt",
    (
        "cli_agent_orchestrator/services/inbox_service.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): (2, "delivery is allowed only into a settled terminal"),
    (
        "cli_agent_orchestrator/services/memory_service.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): "settled-status pair; memory capture waits for a quiet terminal",
    (
        "cli_agent_orchestrator/services/status_monitor.py",
        "TerminalStatus",
        "COMPLETED, IDLE",
    ): (4, "settled-status pair; the monitor's idle/completed transitions"),
    # ---- terminal lifecycle: "the agent is busy" ----
    # The complement pair. #896 lives here: WAITING_USER_ANSWER must never be
    # treated as merely slow, so it is named alongside PROCESSING on purpose.
    (
        "cli_agent_orchestrator/services/inbox_service.py",
        "TerminalStatus",
        "PROCESSING, WAITING_USER_ANSWER",
    ): "busy pair (#896): a dialog is not a delivery window",
    (
        "cli_agent_orchestrator/services/status_monitor.py",
        "TerminalStatus",
        "PROCESSING, WAITING_USER_ANSWER",
    ): (2, "busy pair (#896): a dialog is not a delivery window"),
    (
        "cli_agent_orchestrator/utils/terminal.py",
        "TerminalStatus",
        "PROCESSING, WAITING_USER_ANSWER",
    ): "busy pair (#896): keep polling rather than declaring done",
    (
        "cli_agent_orchestrator/services/status_monitor.py",
        "TerminalStatus",
        "PROCESSING, UNKNOWN",
    ): "not-yet-settled pair: UNKNOWN is treated as still-starting, not as done",
    # ---- workflow / run state ----
    # RunState has four members (RUNNING, COMPLETED, FAILED, CANCELLED). Each
    # subset below is the complement of RUNNING in some combination, which is
    # exactly the "terminal state" question these sites ask.
    (
        "cli_agent_orchestrator/api/main.py",
        "RunState",
        "CANCELLED, COMPLETED, FAILED",
    ): (2, "every terminal RunState (the complement of RUNNING)"),
    (
        "cli_agent_orchestrator/api/main.py",
        "RunState",
        "CANCELLED, FAILED",
    ): "unsuccessful terminal states; COMPLETED is handled separately",
    (
        "cli_agent_orchestrator/services/script_runner.py",
        "RunState",
        "CANCELLED, COMPLETED, FAILED",
    ): "every terminal RunState (the complement of RUNNING)",
    (
        "cli_agent_orchestrator/services/workflow_service.py",
        "RunState",
        "CANCELLED, COMPLETED, FAILED",
    ): "every terminal RunState (the complement of RUNNING)",
    (
        "cli_agent_orchestrator/services/workflow_service.py",
        "RunState",
        "CANCELLED, COMPLETED",
    ): "states that stop further step dispatch without being a failure",
    (
        "cli_agent_orchestrator/services/workflow_service.py",
        "RunState",
        "CANCELLED, FAILED",
    ): (2, "unsuccessful terminal states; COMPLETED is handled separately"),
    (
        "cli_agent_orchestrator/services/workflow_service.py",
        "StepState",
        "COMPLETED, COMPLETED_UNVALIDATED",
    ): (3, "both 'the step ran' states; validation status is a separate axis"),
    # ---- memory scope ----
    # MemoryScope has five members. Both subsets are real partitions of it.
    (
        "cli_agent_orchestrator/api/main.py",
        "MemoryScope",
        "AGENT, SESSION",
    ): (2, "the two scopes that require an owning id in the request"),
    (
        "cli_agent_orchestrator/services/memory_service.py",
        "MemoryScope",
        "AGENT, SESSION",
    ): (7, "the two scopes that require an owning id"),
    (
        "cli_agent_orchestrator/services/memory_service.py",
        "MemoryScope",
        "GLOBAL, PROJECT, SESSION",
    ): "the complement of {AGENT, FEDERATED}: scopes with no agent owner",
    # ---- provider parsing ----
    (
        "cli_agent_orchestrator/providers/kimi_cli.py",
        "KimiDialect",
        "CODE, LEGACY",
    ): "the two recognised dialects; UNKNOWN is the fallthrough by design",
    (
        "cli_agent_orchestrator/providers/kimi_transcript.py",
        "KimiLineKind",
        "FINAL_BULLET, USER_INPUT",
    ): "the two line kinds that bound an answer region",
    (
        "cli_agent_orchestrator/providers/kimi_transcript.py",
        "KimiLineKind",
        "LIVE_SWARM_PROGRESS, SWARM_PROGRESS",
    ): (4, "both swarm-progress spellings; neither is transcript content"),
    # ---- misc ----
    (
        "cli_agent_orchestrator/backends/base.py",
        "TerminalCleanupOutcome",
        "ABSENT, DELETED",
    ): "the two outcomes that mean the terminal is gone",
    # #896's own site. Left as a row rather than fixed here: deriving it from a
    # property is #896's job, and the #896 branch owns that change. When it
    # lands, this row goes stale and the rule says so.
    (
        "cli_agent_orchestrator/services/terminal_service.py",
        "OrchestrationType",
        "ASSIGN, HANDOFF",
    ): "#896: hand-enumerated and known wrong; SEND_MESSAGE belongs here too. "
    "Being fixed on the #896 branch by deriving from a property.",
}

ENUM_SUBSET_REMEDY = (
    "A membership test over part of an Enum stops covering the surface the day "
    "a member is added (#896). Either derive the test from the property you "
    "actually mean (a predicate on the member, an exhaustive match, or the "
    "complement spelled as `not in {...}` over the rest), or -- if the subset "
    "is genuinely deliberate -- put an inline `# subset-ok: <reason>` comment "
    "on the line, or add a row to ENUM_SUBSET_ALLOWLIST in this file with a "
    "reason that says why the excluded members are excluded."
)


def test_enum_subset_literals_are_deliberate() -> None:
    """Every partial-Enum membership literal under ``src/`` is explained (#896)."""
    sources = cl.parse_sources()
    sites = cl.enum_subset_sites(sources)
    marked = {}
    unmarked = []
    for site in sites:
        module = next(m for m in sources if m.relpath == site.relpath)
        reason = cl.inline_marker(module, site.line, "subset-ok")
        if reason is not None:
            marked[site.key] = reason
        else:
            unmarked.append(site)

    cl.check_against_allowlist(
        rule="enum-subset",
        sites=sites,
        allowlist=ENUM_SUBSET_ALLOWLIST,
        marked=marked,
        remedy=ENUM_SUBSET_REMEDY,
    )
    # Belt and braces: check_against_allowlist already covers this, but asserting
    # it directly keeps the intent readable when the helper changes.
    assert not [s for s in unmarked if s.key not in ENUM_SUBSET_ALLOWLIST]


# ===========================================================================
# Rule 2: a timeout literal must derive from the bound it respects
# ===========================================================================
#
# The rule flags ``timeout=<bare number>``. It does *not* flag a name, an
# attribute, a call or an arithmetic expression -- deriving the number is the
# behaviour being asked for, so it has to pass silently.
#
# Every row below carries a one-line justification. Five are flagged as
# genuinely questionable rather than rubber-stamped; their reasons start with
# "SUSPECT" and the PR description repeats them.

BARE_TIMEOUT_ALLOWLIST = {
    # ---- subprocess calls: a wall-clock cap on a local binary ----
    (
        "cli_agent_orchestrator/clients/tmux.py",
        "run(timeout=5)",
    ): "`ps` on the local host; 5s is a liveness cap, not a derived bound",
    (
        "cli_agent_orchestrator/providers/cursor_cli.py",
        "run(timeout=3)",
    ): "`cursor --version` probe; the comment above it states the intent",
    (
        "cli_agent_orchestrator/services/memory_service.py",
        "run(timeout=2)",
    ): "`git config --get remote.origin.url`; docstring cites laggy NFS",
    (
        "cli_agent_orchestrator/backends/herdr_backend.py",
        "run(timeout=30)",
    ): "SUSPECT: one 30s cap for every `herdr` subcommand, from snapshot to "
    "session create. No stated derivation and no per-command differentiation.",
    (
        "cli_agent_orchestrator/services/herdr_inbox_service.py",
        "run(timeout=10)",
    ): (
        3,
        "SUSPECT: `herdr api` reads inside the socket loop. 10s is three "
        "independent literals in one module with no shared constant.",
    ),
    (
        "cli_agent_orchestrator/services/herdr_inbox_service.py",
        "run(timeout=5)",
    ): "SUSPECT: a fourth herdr literal in the same module, a different number "
    "again. These four want one module-level constant.",
    (
        "cli_agent_orchestrator/services/flow_service.py",
        "to_thread(timeout=30)",
    ): "flow script cap; the comment above derives the off-loop hop from it",
    # ---- HTTP calls to the CAO API from the CLI ----
    (
        "cli_agent_orchestrator/utils/terminal.py",
        "get(timeout=2)",
    ): "/health probe; short on purpose so backend selection cannot hang",
    (
        "cli_agent_orchestrator/utils/terminal.py",
        "get(timeout=5)",
    ): (
        3,
        "per-request cap inside a polling loop that has its own outer budget; "
        "the comment at the first site states the relationship",
    ),
    (
        "cli_agent_orchestrator/cli/commands/launch.py",
        "get(timeout=5)",
    ): "single status read on the launch path",
    (
        "cli_agent_orchestrator/services/agui/base.py",
        "post(timeout=10)",
    ): "SUSPECT: AG-UI emit to the local API. This is the #787 shape -- a "
    "hard-coded HTTP timeout with a settings-driven sibling elsewhere in the "
    "tree. Worth checking against server.mcp_request_timeout's precedent.",
    (
        "cli_agent_orchestrator/services/terminal_service.py",
        "post(timeout=5)",
    ): "elastic-broker release notify; best-effort and already in try/except",
    # ---- provider launch readiness ----
    (
        "cli_agent_orchestrator/providers/antigravity_cli.py",
        "wait_for_shell(timeout=10)",
    ): "shell-prompt wait before the launch command is typed",
    (
        "cli_agent_orchestrator/providers/mock_cli.py",
        "wait_for_shell(timeout=10)",
    ): "test-double provider; the number has no production consequence",
    (
        "cli_agent_orchestrator/providers/mock_cli.py",
        "wait_until_status(timeout=15)",
    ): "test-double provider; the number has no production consequence",
    (
        "cli_agent_orchestrator/providers/copilot_cli.py",
        "_accept_trust_prompts(timeout=10)",
    ): "first trust-dialog pass, longer than the retry below it",
    (
        "cli_agent_orchestrator/providers/copilot_cli.py",
        "_accept_trust_prompts(timeout=5)",
    ): "second trust-dialog pass; shorter because the dialog is already up",
    (
        "cli_agent_orchestrator/providers/opencode_cli.py",
        "wait_until_status(timeout=120)",
    ): "the comment above derives 120s from first-run npm install plus "
    "concurrent multi-agent launches -- this is the shape the rule wants, "
    "except the number is still a literal rather than a named constant",
    (
        "cli_agent_orchestrator/providers/hermes.py",
        "wait_until_status(timeout=120)",
    ): "SUSPECT: the same 120s as opencode_cli but with no comment and no "
    "shared constant. If both derive from provider_init_timeout (#710 raised "
    "that allowance), neither should be reading a literal.",
    # ---- in-process waits ----
    (
        "cli_agent_orchestrator/api/main.py",
        "wait_for(timeout=1)",
    ): "asyncio.wait_for on a local queue; bounds a single request handler",
    (
        "cli_agent_orchestrator/api/main.py",
        "wait_for(timeout=3)",
    ): "asyncio.wait_for on a local queue; bounds a single request handler",
    (
        "cli_agent_orchestrator/cli/commands/agent.py",
        "join(timeout=1)",
    ): "thread join on shutdown; a join timeout is a give-up, not a budget",
    (
        "cli_agent_orchestrator/services/fifo_reader.py",
        "join(timeout=2)",
    ): (2, "thread join on shutdown; a join timeout is a give-up, not a budget"),
    (
        "cli_agent_orchestrator/decisions/shadow.py",
        "wait(timeout=0.5)",
    ): "grace period for cancelled shadow tasks; shadow work is discardable",
    (
        "cli_agent_orchestrator/services/vault/writer.py",
        "_file_lock(timeout=10)",
    ): "advisory file-lock acquisition for one vault write",
}

BARE_TIMEOUT_REMEDY = (
    "A bare number next to `timeout=` records a guess. When the bound it has "
    "to respect changes -- a setting, an outer deadline, a server-side cap -- "
    "nothing connects the two and the literal silently stops being right "
    "(#710, #787). Read the bound instead: a setting, an outer budget, or a "
    "module-level named constant whose comment cites what it derives from. If "
    "the number really is a local give-up with nothing to derive from, add a "
    "row to BARE_TIMEOUT_ALLOWLIST in this file saying so."
)


def test_timeout_literals_are_derived_or_justified() -> None:
    """Every bare ``timeout=<number>`` under ``src/`` is derived or justified (#710)."""
    cl.check_against_allowlist(
        rule="bare-timeout",
        sites=cl.bare_timeout_sites(),
        allowlist=BARE_TIMEOUT_ALLOWLIST,
        remedy=BARE_TIMEOUT_REMEDY,
    )


def test_suspect_timeout_rows_are_named_not_buried() -> None:
    """The allowlist must keep flagging the rows this review did not endorse.

    Rubber-stamping is the failure mode of an allowlist. Five rows above were
    accepted only to get the rule green and are marked ``SUSPECT``. This test
    pins that count so removing the marker without fixing the site is a visible
    edit, and so fixing one of them (which deletes its row) shows up here.
    """
    suspects = sorted(
        k
        for k, v in BARE_TIMEOUT_ALLOWLIST.items()
        if cl.row_count_and_reason(v)[1].startswith("SUSPECT")
    )
    assert len(suspects) == 5, (
        "The SUSPECT set changed. If you fixed a site, drop its row and this "
        f"count. Current: {[k[0] + ' ' + k[1] for k in suspects]}"
    )


# ===========================================================================
# Rule 3: the staleness gate on poll_until_done (#735)
# ===========================================================================

STALENESS_FUNC = "poll_until_done"
STALENESS_KWARG = "min_generation"

#: Call sites that may legitimately poll without a generation floor once the
#: parameter exists. Empty on purpose: no site has been reviewed for an opt-out
#: yet, because the parameter does not exist on main (see the test below).
STALENESS_OPT_OUT: dict = {}

#: The complete census of ``poll_until_done`` call sites as of this commit.
#: This half of the rule bites *today*: a new caller fails the test and the
#: author is told, at the moment they add it, that this poll has a staleness
#: obligation. Without it rule 3 would be dormant until #812 merges.
STALENESS_CALL_SITES = {
    ("cli_agent_orchestrator/cli/commands/launch.py", STALENESS_FUNC): (
        "initial-delivery wait after launch"
    ),
    ("cli_agent_orchestrator/cli/commands/session.py", STALENESS_FUNC): (
        "`cao session send` wait for the agent to settle"
    ),
    ("cli_agent_orchestrator/cli/commands/worker.py", STALENESS_FUNC): (
        2,
        "worker-loop waits (two sites, `send` and the interactive loop) for "
        "the assigned turn to finish -- reviewed as exactly two; a third "
        "caller here needs its own review",
    ),
}

STALENESS_REMEDY = (
    "`poll_until_done` decides 'the agent is done' from whatever the terminal "
    "shows now. Without a floor on which turn that frame belongs to, a frame "
    "from before your input was sent reads as a finished turn (#735). Pass "
    f"`{STALENESS_KWARG}=` the generation you are waiting past, or add this "
    "site to STALENESS_OPT_OUT with a reason why a stale frame is acceptable "
    "here."
)


def test_poll_until_done_call_site_census_is_complete() -> None:
    """A new ``poll_until_done`` caller must be reviewed for staleness (#735).

    Line-free keys, so one row covers both ``worker.py`` sites and neither row
    is invalidated by reformatting -- but the row says ``2``, so a third
    ``worker.py`` caller is still a new, unreviewed obligation rather than a
    free pass through an existing key.
    """
    cl.check_against_allowlist(
        rule="staleness-census",
        sites=cl.call_sites(STALENESS_FUNC),
        allowlist=STALENESS_CALL_SITES,
        remedy=(
            "You added a `poll_until_done` call site. Decide whether it needs a "
            "staleness floor (#735) and then add a row to STALENESS_CALL_SITES "
            "in this file describing what it waits for. " + STALENESS_REMEDY
        ),
    )


def test_every_poll_until_done_passes_a_generation_floor() -> None:
    """Once ``min_generation`` exists, every call site must pass it (#735).

    Deliberately conditional. ``min_generation`` does not exist anywhere on
    main: ``poll_until_done`` at ``utils/terminal.py`` declares
    ``(terminal_id, timeout, polling_interval, idle_stable_polls, read_status)``
    and ``grep -r min_generation src test`` is empty. The generation concept
    arrives with PR #812 (``joeguo/735-turn-sequence``), which is still open.

    Asserting the kwarg today would fail all four call sites for a parameter
    that cannot be passed, so the test skips while the contract is absent and
    starts enforcing it the moment #812 adds the parameter -- which is exactly
    when a call site could forget it. The census test above keeps the rule from
    being dormant in the meantime.
    """
    declared = cl.declared_parameters(STALENESS_FUNC)
    if not declared:
        pytest.fail(f"{STALENESS_FUNC} is no longer defined under src/; update this rule.")
    if STALENESS_KWARG not in declared:
        pytest.skip(
            f"{STALENESS_FUNC} has no {STALENESS_KWARG} parameter on this tree "
            f"(declares {sorted(declared)}). The #735 staleness contract arrives "
            "with PR #812; this rule activates automatically when it does."
        )

    cl.check_against_allowlist(
        rule="staleness-gate",
        sites=cl.missing_kwarg_sites(STALENESS_FUNC, STALENESS_KWARG),
        allowlist=STALENESS_OPT_OUT,
        remedy=STALENESS_REMEDY,
    )
