//! The static run-policy table: what the TUI offers, and how (issue #321).
//!
//! One row per leaf command of the CAO Click tree — **70 of them** — each classified `InApp`,
//! `Handoff`, or `Hidden`. Three infallible lookups read that table and nothing else.
//!
//! # No I/O, and that is the security property (SR-1)
//!
//! No HTTP, no subprocess, no file reads, at build time or run time. There is no injection
//! surface because there is no input channel: the table is a compile-time constant. Stated as a
//! requirement rather than an observation, so a later change that reads the classification from
//! disk or the network is visibly a *security* change and not a refactor.
//!
//! # Why the table exists at all (FR-1.3)
//!
//! The superseded TUI built its catalog by **scraping `cao ... --help`**. That is design defect
//! #1 of the three motivating this rewrite: Click renders `--agents` and `--provider` as bare
//! `TEXT`, not `Choice`, so scraped help yields `choices=None` and a picker becomes structurally
//! impossible to build. The rows below were produced by walking `cli_agent_orchestrator.cli.main:cli`
//! programmatically — the enumeration is of the command *tree*, never of its help output.
//!
//! # Why [`CommandId`] is an enum and not a map key (FR-4.2)
//!
//! This is the load-bearing decision in the module, and it is about a failure mode rather than
//! about ergonomics.
//!
//! `project.md` affirms that an unclassified CAO command **defaults to HIDE**, so an unvetted
//! command cannot appear half-working in the front door. There are two ways to implement that
//! default:
//!
//! - A `HashMap<String, Policy>` plus `unwrap_or(Policy::Hidden)`. A new command then hides
//!   itself **silently**, at run time, and nobody is told. The affirmed rule survives as a
//!   convention that happens to hold.
//! - An enum with one variant per command, and an exhaustive `match`. A new command **fails to
//!   compile** until a human classifies it.
//!
//! The second is what is implemented. [`entry`] carries no `_` arm, deliberately: a fallback arm
//! would restore exactly the silence the enum exists to remove. `HIDE-by-default` is therefore a
//! property the compiler enforces, not a branch that chooses it.
//!
//! # What that mechanism does NOT catch
//!
//! An exhaustive match catches a **missing** classification and never a **wrong** one, and that
//! is not hypothetical here: `memory compact` and `memory heal` were classified HANDOFF during
//! design, compiled perfectly, and were wrong — only human review caught them. Both are HIDE
//! below.
//!
//! Two things follow, and both are in the code rather than in this comment:
//!
//! - Every `Handoff` row carries a **required reason** ([`Command::handoff_reason`], BR-4/VR-1),
//!   so the justification sits where a reviewer reads it.
//! - Every `Hidden` row carries its reason as a trailing comment, for the same purpose. There is
//!   no field for it because the design gives `Command` a *handoff* reason specifically; a
//!   general-purpose reason field would blur what BR-4 makes mandatory.
//!
//! # Infallible, and defining no error type (INV-3)
//!
//! All three public functions are infallible. `team.md` affirms `thiserror` for crate-internal
//! error types and `anyhow` at integration boundaries; **neither applies here** — this module is
//! not a boundary and has no fallible operation. Adding a variant to [`crate::error::TuiError`]
//! for it would be dead code, and a fallible signature would force six consumers to handle a
//! case that cannot occur. `unwrap`/`expect` do not appear either: there is nothing to unwrap.

use std::vec::Vec;

/// The number of leaf commands in the CAO Click tree.
///
/// **70 as of this branch.** Two separate merges from `main` each brought four new leaf commands
/// that this table did not know about, and both were caught by
/// `test/test_command_catalog_matches_click.py` rather than by review — the second one in CI,
/// because CI tests the PR MERGED against `main` while a local run only sees the branch. That is
/// the guard doing exactly what it exists for, twice.
///
/// The four `cao workflow *` leaves — `runs`, `wait`, `result`, `events` — arrived with PR #525
/// (issue #505, commit `e2e6318`). The four `cao memory relationships *` leaves were added by
/// PR #524 (issue #511, commit `8e8695a`, 2026-08-03) and landed on `main` before this branch
/// merged it — and this table was never updated, so the TUI simply did not know they existed. That
/// is the exact silence `CommandId`'s docs claim the closed enum eliminated: the enum makes an
/// *unclassified variant* a compile error, but nothing made a *missing* variant anything at all.
/// `test/test_command_catalog_matches_click.py` now walks the live Click tree and fails on the
/// difference, which is the check that was absent. (Reported by review on PR #547.)
///
/// The count below the four additions was **61, not the 60 the design records** — and the discrepancy is a prediction coming true
/// rather than a defect. `business-logic-model.md` wrote that `cao tui` was "absent from the
/// table … `skeleton-wheel-bundle` adds the subcommand"; Bolt 1 then added it. The affirmed
/// distribution of 33/5/22 no longer summed, so `cao tui` was classified — HIDE, because the TUI
/// must not offer itself — giving **33 IN-APP / 5 HANDOFF / 23 HIDE = 61**. Recorded here
/// because a reader comparing the design's 60 against this 61 would otherwise suspect drift.
/// (#321)
const COMMAND_COUNT: usize = 70;

/// What the TUI does with a command.
///
/// A **closed** three-variant enum. A fourth state — "offer with a warning", say — would have to
/// be handled at every match site in `renderer`, `guided-flow`, and `results-pane`, and the
/// run-policy decision was made per command by the operator rather than deferred to run time.
/// (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Policy {
    /// Run captured; render the output in the results pane.
    InApp,
    /// Drive the terminal backend so the command runs on real stdio in a **new** window.
    ///
    /// The new window is not a detail: `project.md` mandates that a hand-off must leave the TUI
    /// running, and both Python backends violate that by construction today
    /// (`tmux_backend.attach_session` blocks, `herdr_backend.attach_session` calls `os.execvp`).
    /// (#321)
    Handoff,
    /// Not offered in the TUI at all.
    ///
    /// FR-4.3 requires hidden commands be **absent from navigation**, not greyed out — which is
    /// why [`commands`] filters them rather than marking them.
    Hidden,
}

/// The shape of a parameter's value.
///
/// Two variants because the Click tree yields exactly these two shapes. **A `Choice` variant is
/// deliberately absent.** Enumerated choices for `--agents` and `--provider` come from
/// `GET /agents/profiles` and `GET /agents/providers` at run time (ADR-02), never from this
/// static table. A `Choice` here would invite precisely the baked-in-choices pattern that
/// produced `choices=None` in the predecessor. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParamKind {
    /// Takes a value.
    Text,
    /// Boolean presence.
    Flag,
}

/// One parameter, mirroring the CLI's own declaration.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Param {
    /// **The CLI's exact spelling** — `--agents`, not "Agents" and not "agents" (BR-8).
    ///
    /// A display label may be prettified; the value that reaches `SessionParams` may not. A
    /// renamed parameter is a request the CLI rejects.
    ///
    /// A name with **no `--` prefix is a positional argument**, which is how `cao launch`'s
    /// trailing `message` appears (BR-9). Callers building an argv must place those by position
    /// and must not invent a flag for them.
    pub name: &'static str,
    /// Whether the CLI requires it. For `cao launch` this is true for `--agents` and nothing
    /// else — marking a second parameter required would block runs the CLI accepts (FR-2.2).
    pub required: bool,
    /// Value shape.
    pub kind: ParamKind,
}

/// A catalog row as callers see it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Command {
    /// The generated variant naming this row.
    pub id: CommandId,
    /// The Click group, e.g. `session`. `None` for a top-level leaf such as `cao launch`.
    pub parent: Option<&'static str>,
    /// The leaf token, e.g. `list`.
    pub leaf_name: &'static str,
    /// The command's own one-line help, for display.
    pub summary: &'static str,
    /// The classification.
    pub policy: Policy,
    /// The parameter set, in the order the CLI declares it. Empty for many commands.
    pub params: &'static [Param],
    /// **Mandatory when `policy == Policy::Handoff`** (BR-4, VR-1); `None` otherwise.
    ///
    /// `Option` in the type rather than `Handoff { reason: &'static str }`, which *would* have
    /// been compiler-enforced. The trade-off is deliberate and recorded rather than accidental:
    /// a payload-carrying variant stops [`Policy`] being a plain comparable enum, and the many
    /// call sites that only test *which* variant it is would all have to destructure. The cost
    /// is that BR-4 is a review rule (VR-1, guarded by a test) instead of a compile rule. (#321)
    pub handoff_reason: Option<&'static str>,
}

/// Every leaf command, in display order.
///
/// **Order is by parent group, then leaf name** — the eight top-level leaves first, then each
/// Click group contiguously. Not alphabetical across the flattened set, which would scatter
/// `session list` away from `session status`; an operator scanning for session commands expects
/// them adjacent.
///
/// The length lives in the **type**, so a list that has drifted from [`COMMAND_COUNT`] is a
/// compile error at this item rather than a short navigation list at run time. (#321)
///
/// `pub(crate)` since Bolt 3: `server-client`'s route-table tests walk it to assert that every
/// IN-APP command has a route and that no HANDOFF or HIDE command does. Deriving that set any
/// other way would mean re-listing 69 commands in a second place, which is a worse trade than
/// widening the visibility of a compile-time constant. Still crate-private — no consumer outside
/// this crate exists, and the table is not a public API. (#321)
pub(crate) const DISPLAY_ORDER: [CommandId; COMMAND_COUNT] = [
    CommandId::Info,
    CommandId::Init,
    CommandId::Install,
    CommandId::Launch,
    CommandId::McpServer,
    CommandId::Shutdown,
    CommandId::Tui,
    CommandId::Update,
    CommandId::ConfigGet,
    CommandId::ConfigList,
    CommandId::ConfigPath,
    CommandId::ConfigSet,
    CommandId::EnvGet,
    CommandId::EnvList,
    CommandId::EnvSet,
    CommandId::EnvUnset,
    CommandId::FlowAdd,
    CommandId::FlowDisable,
    CommandId::FlowEnable,
    CommandId::FlowList,
    CommandId::FlowRemove,
    CommandId::FlowRun,
    CommandId::MemoryClear,
    CommandId::MemoryCompact,
    CommandId::MemoryDelete,
    CommandId::MemoryExport,
    CommandId::MemoryHeal,
    CommandId::MemoryImport,
    CommandId::MemoryLint,
    CommandId::MemoryList,
    CommandId::MemoryPromote,
    CommandId::MemoryRelationshipsInspect,
    CommandId::MemoryRelationshipsList,
    CommandId::MemoryRelationshipsPromote,
    CommandId::MemoryRelationshipsReject,
    CommandId::MemoryRepair,
    CommandId::MemoryShow,
    CommandId::ProfileCreate,
    CommandId::ProfileFind,
    CommandId::ProfileList,
    CommandId::ProfileRemove,
    CommandId::ProfileShow,
    CommandId::ProfileTemplates,
    CommandId::ProfileValidate,
    CommandId::ScheduleAdd,
    CommandId::ScheduleDisable,
    CommandId::ScheduleEnable,
    CommandId::ScheduleList,
    CommandId::ScheduleRemove,
    CommandId::ScheduleRun,
    CommandId::SessionList,
    CommandId::SessionSend,
    CommandId::SessionStatus,
    CommandId::SkillsAdd,
    CommandId::SkillsList,
    CommandId::SkillsRemove,
    CommandId::TerminalRestore,
    CommandId::WorkflowApprove,
    CommandId::WorkflowCancel,
    CommandId::WorkflowDelete,
    CommandId::WorkflowEvents,
    CommandId::WorkflowGet,
    CommandId::WorkflowList,
    CommandId::WorkflowResult,
    CommandId::WorkflowResume,
    CommandId::WorkflowRun,
    CommandId::WorkflowRuns,
    CommandId::WorkflowStatus,
    CommandId::WorkflowWait,
    CommandId::WorkflowValidate,
];

/// One variant per leaf command — **all 69**.
///
/// Why an enum rather than a `String` key is the subject of this module's own docs: it is what
/// makes an unclassified command a **compile error** instead of a runtime `None` (FR-4.2).
///
/// Variants are named by concatenating the command path, so `cao mcp-server` is `McpServer` and
/// `cao workflow run` is `WorkflowRun`. Lifecycle: **compile time only** — the set is fixed when
/// the crate is built. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum CommandId {
    // Top-level leaves.
    /// `cao info`
    Info,
    /// `cao init`
    Init,
    /// `cao install`
    Install,
    /// `cao launch`
    Launch,
    /// `cao mcp-server`
    McpServer,
    /// `cao shutdown`
    Shutdown,
    /// `cao tui`
    Tui,
    /// `cao update`
    Update,

    // `cao config *`
    /// `cao config get`
    ConfigGet,
    /// `cao config list`
    ConfigList,
    /// `cao config path`
    ConfigPath,
    /// `cao config set`
    ConfigSet,

    // `cao env *`
    /// `cao env get`
    EnvGet,
    /// `cao env list`
    EnvList,
    /// `cao env set`
    EnvSet,
    /// `cao env unset`
    EnvUnset,

    // `cao flow *`
    /// `cao flow add`
    FlowAdd,
    /// `cao flow disable`
    FlowDisable,
    /// `cao flow enable`
    FlowEnable,
    /// `cao flow list`
    FlowList,
    /// `cao flow remove`
    FlowRemove,
    /// `cao flow run`
    FlowRun,

    // `cao memory *`
    /// `cao memory clear`
    MemoryClear,
    /// `cao memory compact`
    MemoryCompact,
    /// `cao memory delete`
    MemoryDelete,
    /// `cao memory export`
    MemoryExport,
    /// `cao memory heal`
    MemoryHeal,
    /// `cao memory import`
    MemoryImport,
    /// `cao memory lint`
    MemoryLint,
    /// `cao memory list`
    MemoryList,
    /// `cao memory promote`
    MemoryPromote,
    /// `cao memory relationships inspect`
    MemoryRelationshipsInspect,
    /// `cao memory relationships list`
    MemoryRelationshipsList,
    /// `cao memory relationships promote`
    MemoryRelationshipsPromote,
    /// `cao memory relationships reject`
    MemoryRelationshipsReject,
    /// `cao memory repair`
    MemoryRepair,
    /// `cao memory show`
    MemoryShow,

    // `cao profile *`
    /// `cao profile create`
    ProfileCreate,
    /// `cao profile find`
    ProfileFind,
    /// `cao profile list`
    ProfileList,
    /// `cao profile remove`
    ProfileRemove,
    /// `cao profile show`
    ProfileShow,
    /// `cao profile templates`
    ProfileTemplates,
    /// `cao profile validate`
    ProfileValidate,

    // `cao schedule *`
    /// `cao schedule add`
    ScheduleAdd,
    /// `cao schedule disable`
    ScheduleDisable,
    /// `cao schedule enable`
    ScheduleEnable,
    /// `cao schedule list`
    ScheduleList,
    /// `cao schedule remove`
    ScheduleRemove,
    /// `cao schedule run`
    ScheduleRun,

    // `cao session *`
    /// `cao session list`
    SessionList,
    /// `cao session send`
    SessionSend,
    /// `cao session status`
    SessionStatus,

    // `cao skills *`
    /// `cao skills add`
    SkillsAdd,
    /// `cao skills list`
    SkillsList,
    /// `cao skills remove`
    SkillsRemove,

    // `cao terminal *`
    /// `cao terminal restore`
    TerminalRestore,

    // `cao workflow *`
    /// `cao workflow approve`
    WorkflowApprove,
    /// `cao workflow cancel`
    WorkflowCancel,
    /// `cao workflow delete`
    WorkflowDelete,
    /// `cao workflow events`
    WorkflowEvents,
    /// `cao workflow get`
    WorkflowGet,
    /// `cao workflow list`
    WorkflowList,
    /// `cao workflow result`
    WorkflowResult,
    /// `cao workflow resume`
    WorkflowResume,
    /// `cao workflow run`
    WorkflowRun,
    /// `cao workflow runs`
    WorkflowRuns,
    /// `cao workflow status`
    WorkflowStatus,
    /// `cao workflow wait`
    WorkflowWait,
    /// `cao workflow validate`
    WorkflowValidate,
}

/// The one place a command's row is written.
///
/// **Exhaustive, with no `_` arm — that is the whole mechanism** (FR-4.2, BR-5, SR-2). Adding a
/// variant to [`CommandId`] without adding an arm here does not compile, so a new CAO command
/// cannot reach the front door until a human has classified it. A `_ => Policy::Hidden` fallback
/// would compile, hide the command silently, and tell nobody; deleting the fallback is what
/// turns `project.md`'s affirmed HIDE-by-default rule from a convention into a mechanism.
///
/// Returns by value: [`Command`] is `Copy` and every field is `&'static` or a scalar, so nothing
/// is owned, cloned, or allocated here. The `Hidden` rows carry their reason as a trailing
/// comment — see the module docs for why that is a comment and not a field. (#321)
fn entry(id: CommandId) -> Command {
    match id {

        CommandId::Info => Command {
            id: CommandId::Info,
            parent: None,
            leaf_name: "info",
            summary: "Display information about the current session.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: no HTTP route exists; ADR-02 forbids subprocess execution
        },
        CommandId::Init => Command {
            id: CommandId::Init,
            parent: None,
            leaf_name: "init",
            summary: "Initialize CLI Agent Orchestrator database.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: one-time bootstrap; already done by the time the TUI runs
        },
        CommandId::Install => Command {
            id: CommandId::Install,
            parent: None,
            leaf_name: "install",
            summary: "Install an agent from local store, built-in store, URL, or file path.",
            policy: Policy::Handoff,
            params: &[Param { name: "agent_source", required: true, kind: ParamKind::Text }, Param { name: "--provider", required: false, kind: ParamKind::Text }, Param { name: "--env", required: false, kind: ParamKind::Text }],
            handoff_reason: Some("may prompt and fetch from network/URL"),
        },
        CommandId::Launch => Command {
            id: CommandId::Launch,
            parent: None,
            leaf_name: "launch",
            summary: "Launch cao session with specified agent profile.",
            policy: Policy::Handoff,
            params: &[Param { name: "message", required: false, kind: ParamKind::Text }, Param { name: "--agents", required: true, kind: ParamKind::Text }, Param { name: "--session-name", required: false, kind: ParamKind::Text }, Param { name: "--headless", required: false, kind: ParamKind::Flag }, Param { name: "--provider", required: false, kind: ParamKind::Text }, Param { name: "--allowed-tools", required: false, kind: ParamKind::Text }, Param { name: "--async", required: false, kind: ParamKind::Flag }, Param { name: "--auto-approve", required: false, kind: ParamKind::Flag }, Param { name: "--yolo", required: false, kind: ParamKind::Flag }, Param { name: "--working-directory", required: false, kind: ParamKind::Text }, Param { name: "--memory", required: false, kind: ParamKind::Flag }, Param { name: "--env", required: false, kind: ParamKind::Text }],
            handoff_reason: Some("interactive agent session — the stated hand-off case; MUST open a NEW tab/window and leave the TUI alive"),
        },
        CommandId::McpServer => Command {
            id: CommandId::McpServer,
            parent: None,
            leaf_name: "mcp-server",
            summary: "Start the CAO MCP server.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: long-running foreground server; nothing to render, never exits
        },
        CommandId::Shutdown => Command {
            id: CommandId::Shutdown,
            parent: None,
            leaf_name: "shutdown",
            summary: "Shutdown tmux sessions and cleanup terminal records.",
            policy: Policy::Hidden,
            params: &[Param { name: "--all", required: false, kind: ParamKind::Flag }, Param { name: "--session", required: false, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: kills tmux sessions — can kill the session hosting the TUI
        },
        CommandId::Tui => Command {
            id: CommandId::Tui,
            parent: None,
            leaf_name: "tui",
            summary: "Launch the terminal UI (bundled Rust binary).",
            policy: Policy::Hidden,
            params: &[Param { name: "tui_args", required: false, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: the TUI must not offer itself; nesting is a no-op or a mess
        },
        CommandId::Update => Command {
            id: CommandId::Update,
            parent: None,
            leaf_name: "update",
            summary: "Update CAO to the latest version.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: self-update may replace the binary under a running TUI
        },

        CommandId::ConfigGet => Command {
            id: CommandId::ConfigGet,
            parent: Some("config"),
            leaf_name: "get",
            summary: "Get the resolved value for a dotted config KEY, e.g.",
            policy: Policy::Hidden,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: human ruled: wrong CLI surface to drive from a TUI
        },
        CommandId::ConfigList => Command {
            id: CommandId::ConfigList,
            parent: Some("config"),
            leaf_name: "list",
            summary: "List every known config key with its resolved value.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: human ruled: wrong CLI surface to drive from a TUI
        },
        CommandId::ConfigPath => Command {
            id: CommandId::ConfigPath,
            parent: Some("config"),
            leaf_name: "path",
            summary: "Print the absolute path to the unified settings.json file.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: human ruled: wrong CLI surface to drive from a TUI
        },
        CommandId::ConfigSet => Command {
            id: CommandId::ConfigSet,
            parent: Some("config"),
            leaf_name: "set",
            summary: "Set config KEY to VALUE, persisting it to settings.json.",
            policy: Policy::Hidden,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }, Param { name: "value", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: human ruled: wrong CLI surface to drive from a TUI
        },

        CommandId::EnvGet => Command {
            id: CommandId::EnvGet,
            parent: Some("env"),
            leaf_name: "get",
            summary: "Get a managed environment variable.",
            policy: Policy::Hidden,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: no HTTP route exists; ADR-02 forbids subprocess execution
        },
        CommandId::EnvList => Command {
            id: CommandId::EnvList,
            parent: Some("env"),
            leaf_name: "list",
            summary: "List managed environment variables.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: no HTTP route exists; ADR-02 forbids subprocess execution
        },
        CommandId::EnvSet => Command {
            id: CommandId::EnvSet,
            parent: Some("env"),
            leaf_name: "set",
            summary: "Set a managed environment variable.",
            policy: Policy::Hidden,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }, Param { name: "value", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: no HTTP route exists; ADR-02 forbids subprocess execution
        },
        CommandId::EnvUnset => Command {
            id: CommandId::EnvUnset,
            parent: Some("env"),
            leaf_name: "unset",
            summary: "Unset a managed environment variable.",
            policy: Policy::Hidden,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: no HTTP route exists; ADR-02 forbids subprocess execution
        },

        CommandId::FlowAdd => Command {
            id: CommandId::FlowAdd,
            parent: Some("flow"),
            leaf_name: "add",
            summary: "Add a flow from file.",
            policy: Policy::Hidden,
            params: &[Param { name: "file_path", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: Click marks the group `hidden=True` at cli/commands/schedule.py:133; deprecated alias
            // for `schedule`, registered at cli/main.py:44 (issue #378). FR-4.4 — the TUI must not
            // resurrect a command the CLI itself conceals
        },
        CommandId::FlowDisable => Command {
            id: CommandId::FlowDisable,
            parent: Some("flow"),
            leaf_name: "disable",
            summary: "Disable a flow.",
            policy: Policy::Hidden,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: Click marks the group `hidden=True` at cli/commands/schedule.py:133; deprecated alias
            // for `schedule`, registered at cli/main.py:44 (issue #378). FR-4.4 — the TUI must not
            // resurrect a command the CLI itself conceals
        },
        CommandId::FlowEnable => Command {
            id: CommandId::FlowEnable,
            parent: Some("flow"),
            leaf_name: "enable",
            summary: "Enable a flow.",
            policy: Policy::Hidden,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: Click marks the group `hidden=True` at cli/commands/schedule.py:133; deprecated alias
            // for `schedule`, registered at cli/main.py:44 (issue #378). FR-4.4 — the TUI must not
            // resurrect a command the CLI itself conceals
        },
        CommandId::FlowList => Command {
            id: CommandId::FlowList,
            parent: Some("flow"),
            leaf_name: "list",
            summary: "List all flows.",
            policy: Policy::Hidden,
            params: &[],
            handoff_reason: None,
            // HIDE: Click marks the group `hidden=True` at cli/commands/schedule.py:133; deprecated alias
            // for `schedule`, registered at cli/main.py:44 (issue #378). FR-4.4 — the TUI must not
            // resurrect a command the CLI itself conceals
        },
        CommandId::FlowRemove => Command {
            id: CommandId::FlowRemove,
            parent: Some("flow"),
            leaf_name: "remove",
            summary: "Remove a flow.",
            policy: Policy::Hidden,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: Click marks the group `hidden=True` at cli/commands/schedule.py:133; deprecated alias
            // for `schedule`, registered at cli/main.py:44 (issue #378). FR-4.4 — the TUI must not
            // resurrect a command the CLI itself conceals
        },
        CommandId::FlowRun => Command {
            id: CommandId::FlowRun,
            parent: Some("flow"),
            leaf_name: "run",
            summary: "Manually run a flow.",
            policy: Policy::Hidden,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: Click marks the group `hidden=True` at cli/commands/schedule.py:133; deprecated alias
            // for `schedule`, registered at cli/main.py:44 (issue #378). FR-4.4 — the TUI must not
            // resurrect a command the CLI itself conceals
        },

        CommandId::MemoryClear => Command {
            id: CommandId::MemoryClear,
            parent: Some("memory"),
            leaf_name: "clear",
            summary: "Clear all memories for a given scope.",
            policy: Policy::InApp,
            params: &[Param { name: "--scope", required: true, kind: ParamKind::Text }, Param { name: "--yes", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::MemoryCompact => Command {
            id: CommandId::MemoryCompact,
            parent: Some("memory"),
            leaf_name: "compact",
            summary: "Compact wiki topics with the LLM compiler (repair sweep).",
            policy: Policy::Hidden,
            params: &[Param { name: "--scope", required: false, kind: ParamKind::Text }, Param { name: "--key", required: false, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: maintenance operation; no HTTP route; not an interactive session
        },
        CommandId::MemoryDelete => Command {
            id: CommandId::MemoryDelete,
            parent: Some("memory"),
            leaf_name: "delete",
            summary: "Delete a memory by key.",
            policy: Policy::InApp,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }, Param { name: "--scope", required: false, kind: ParamKind::Text }, Param { name: "--yes", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::MemoryExport => Command {
            id: CommandId::MemoryExport,
            parent: Some("memory"),
            leaf_name: "export",
            summary: "Export a memory scope as an archive bundle (OKF directory by default).",
            policy: Policy::InApp,
            params: &[Param { name: "--format", required: false, kind: ParamKind::Text }, Param { name: "--scope", required: true, kind: ParamKind::Text }, Param { name: "--output", required: true, kind: ParamKind::Text }, Param { name: "--include-private", required: false, kind: ParamKind::Flag }, Param { name: "--include-history", required: false, kind: ParamKind::Flag }, Param { name: "--redact", required: false, kind: ParamKind::Flag }, Param { name: "--prune", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::MemoryHeal => Command {
            id: CommandId::MemoryHeal,
            parent: Some("memory"),
            leaf_name: "heal",
            summary: "Repair wiki lint findings (orphan pages, contradictions, stale claims).",
            policy: Policy::Hidden,
            params: &[Param { name: "--scope", required: false, kind: ParamKind::Text }, Param { name: "--apply", required: false, kind: ParamKind::Flag }, Param { name: "--aggressive", required: false, kind: ParamKind::Flag }, Param { name: "--issue-type", required: false, kind: ParamKind::Text }, Param { name: "--format", required: false, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: maintenance operation; no HTTP route; not an interactive session
        },
        CommandId::MemoryImport => Command {
            id: CommandId::MemoryImport,
            parent: Some("memory"),
            leaf_name: "import",
            summary: "Import an archive bundle directory into a memory scope.",
            policy: Policy::Handoff,
            params: &[Param { name: "path", required: true, kind: ParamKind::Text }, Param { name: "--format", required: false, kind: ParamKind::Text }, Param { name: "--scope", required: true, kind: ParamKind::Text }, Param { name: "--conflict", required: false, kind: ParamKind::Text }, Param { name: "--dry-run", required: false, kind: ParamKind::Flag }],
            handoff_reason: Some(
                "no HTTP route: svc.import_memories is in-process (memory.py:703); OQ-6",
            ),
        },
        CommandId::MemoryLint => Command {
            id: CommandId::MemoryLint,
            parent: Some("memory"),
            leaf_name: "lint",
            summary: "Run wiki lint detectors and print findings.",
            policy: Policy::Handoff,
            params: &[Param { name: "--scope", required: false, kind: ParamKind::Text }, Param { name: "--format", required: false, kind: ParamKind::Text }],
            handoff_reason: Some(
                "no HTTP route: wiki_lint.run_lint is in-process (memory.py:286); OQ-6",
            ),
        },
        CommandId::MemoryList => Command {
            id: CommandId::MemoryList,
            parent: Some("memory"),
            leaf_name: "list",
            summary: "List stored memories.",
            policy: Policy::InApp,
            params: &[Param { name: "--scope", required: false, kind: ParamKind::Text }, Param { name: "--type", required: false, kind: ParamKind::Text }, Param { name: "--all", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::MemoryPromote => Command {
            id: CommandId::MemoryPromote,
            parent: Some("memory"),
            leaf_name: "promote",
            summary: "Promote reinforced agent-scope lessons into AGENT_NAME's profile file.",
            policy: Policy::Handoff,
            params: &[Param { name: "agent_name", required: true, kind: ParamKind::Text }, Param { name: "--apply", required: false, kind: ParamKind::Flag }, Param { name: "--min-recalls", required: false, kind: ParamKind::Text }, Param { name: "--profile-path", required: false, kind: ParamKind::Text }],
            handoff_reason: Some(
                "no HTTP route: PromotionService is in-process (memory.py:815); OQ-6",
            ),
        },
        // ── `cao memory relationships *` — all four HIDE ────────────────────────────────
        //
        // Added to the CLI by PR #524 (issue #511) and MISSING from this table until review on
        // PR #547 caught it. HIDE is not a shrug: `project.md`'s mandated rule is that a new or
        // unclassified CAO command **defaults to HIDE in the TUI** until it is deliberately
        // classified, so that an unvetted command cannot surface half-working. Classifying them
        // IN-APP is a separate, reviewable decision.
        //
        // Routes DO exist (`GET /memory/relationships` at `api/main.py:3770`, `POST` at `:3801`,
        // `PATCH /{relationship_id}` at `:3829`, `POST /{relationship_id}/promote` at `:3854`),
        // and `promote`/`reject` are curation actions that MUTATE stored memory — which is
        // exactly the kind of command that must not become reachable by accident. `reject` maps
        // to the PATCH route, and `Method` here has no `Patch` variant, so wiring it would widen
        // the transport enum too.
        CommandId::MemoryRelationshipsInspect => Command {
            id: CommandId::MemoryRelationshipsInspect,
            parent: Some("memory relationships"),
            leaf_name: "inspect",
            summary: "Show one relationship's endpoints, provenance, status, and timestamps.",
            policy: Policy::Hidden,
            params: &[
                Param { name: "relationship_id", required: true, kind: ParamKind::Text },
                Param { name: "--format", required: false, kind: ParamKind::Text },
            ],
            handoff_reason: None,
        },
        CommandId::MemoryRelationshipsList => Command {
            id: CommandId::MemoryRelationshipsList,
            parent: Some("memory relationships"),
            leaf_name: "list",
            summary: "List relationships (default: active).",
            policy: Policy::Hidden,
            params: &[
                Param { name: "--scope", required: false, kind: ParamKind::Text },
                Param { name: "--scope-id", required: false, kind: ParamKind::Text },
                Param { name: "--source-key", required: false, kind: ParamKind::Text },
                Param { name: "--status", required: false, kind: ParamKind::Text },
                Param { name: "--stale", required: false, kind: ParamKind::Flag },
                Param { name: "--format", required: false, kind: ParamKind::Text },
            ],
            handoff_reason: None,
        },
        CommandId::MemoryRelationshipsPromote => Command {
            id: CommandId::MemoryRelationshipsPromote,
            parent: Some("memory relationships"),
            leaf_name: "promote",
            summary: "Promote a proposal to active.",
            policy: Policy::Hidden,
            params: &[Param { name: "relationship_id", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::MemoryRelationshipsReject => Command {
            id: CommandId::MemoryRelationshipsReject,
            parent: Some("memory relationships"),
            leaf_name: "reject",
            summary: "Reject a proposal.",
            policy: Policy::Hidden,
            params: &[Param { name: "relationship_id", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::MemoryRepair => Command {
            id: CommandId::MemoryRepair,
            parent: Some("memory"),
            leaf_name: "repair",
            summary: "Reconcile surviving canonical topics into SQLite and index.md.",
            policy: Policy::Handoff,
            params: &[Param { name: "--apply", required: false, kind: ParamKind::Flag }],
            handoff_reason: Some(
                "no HTTP route: reconcile runs only at server startup (main.py:514); OQ-6",
            ),
        },
        CommandId::MemoryShow => Command {
            id: CommandId::MemoryShow,
            parent: Some("memory"),
            leaf_name: "show",
            summary: "Display full content of a memory.",
            policy: Policy::InApp,
            params: &[Param { name: "key", required: true, kind: ParamKind::Text }, Param { name: "--scope", required: false, kind: ParamKind::Text }],
            handoff_reason: None,
        },

        CommandId::ProfileCreate => Command {
            id: CommandId::ProfileCreate,
            parent: Some("profile"),
            leaf_name: "create",
            summary: "Generate an agent profile from a template.",
            policy: Policy::Handoff,
            params: &[Param { name: "--template", required: true, kind: ParamKind::Text }, Param { name: "--config", required: true, kind: ParamKind::Text }, Param { name: "--output-dir", required: false, kind: ParamKind::Text }],
            handoff_reason: Some(
                "no HTTP route: agent_scaffold.render_template is in-process (profile.py:318); OQ-6",
            ),
        },
        CommandId::ProfileFind => Command {
            id: CommandId::ProfileFind,
            parent: Some("profile"),
            leaf_name: "find",
            summary: "Find agent profiles by keyword (searches name, description, tags, capabilities).",
            policy: Policy::InApp,
            params: &[Param { name: "query", required: true, kind: ParamKind::Text }, Param { name: "--limit", required: false, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::ProfileList => Command {
            id: CommandId::ProfileList,
            parent: Some("profile"),
            leaf_name: "list",
            summary: "List all available agent profiles.",
            policy: Policy::InApp,
            params: &[],
            handoff_reason: None,
        },
        CommandId::ProfileRemove => Command {
            id: CommandId::ProfileRemove,
            parent: Some("profile"),
            leaf_name: "remove",
            summary: "Remove an agent profile from the local store.",
            policy: Policy::Handoff,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }, Param { name: "--yes", required: false, kind: ParamKind::Flag }],
            handoff_reason: Some(
                "no HTTP route: unlink() locally; no DELETE on /agents/* (profile.py:250); OQ-6",
            ),
        },
        CommandId::ProfileShow => Command {
            id: CommandId::ProfileShow,
            parent: Some("profile"),
            leaf_name: "show",
            summary: "Show details of an agent profile.",
            policy: Policy::InApp,
            params: &[Param { name: "name_or_path", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::ProfileTemplates => Command {
            id: CommandId::ProfileTemplates,
            parent: Some("profile"),
            leaf_name: "templates",
            summary: "List available agent templates for scaffolding.",
            policy: Policy::Handoff,
            params: &[],
            handoff_reason: Some(
                "no HTTP route: agent_scaffold.list_templates (profile.py:277); OQ-6",
            ),
        },
        CommandId::ProfileValidate => Command {
            id: CommandId::ProfileValidate,
            parent: Some("profile"),
            leaf_name: "validate",
            summary: "Validate an agent profile against the CAO schema.",
            policy: Policy::Handoff,
            params: &[Param { name: "name_or_path", required: true, kind: ParamKind::Text }],
            handoff_reason: Some(
                "no HTTP route: schema validation is local (profile.py:207); OQ-6",
            ),
        },

        CommandId::ScheduleAdd => Command {
            id: CommandId::ScheduleAdd,
            parent: Some("schedule"),
            leaf_name: "add",
            summary: "Add a flow from file.",
            policy: Policy::InApp,
            params: &[Param { name: "file_path", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::ScheduleDisable => Command {
            id: CommandId::ScheduleDisable,
            parent: Some("schedule"),
            leaf_name: "disable",
            summary: "Disable a flow.",
            policy: Policy::InApp,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::ScheduleEnable => Command {
            id: CommandId::ScheduleEnable,
            parent: Some("schedule"),
            leaf_name: "enable",
            summary: "Enable a flow.",
            policy: Policy::InApp,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::ScheduleList => Command {
            id: CommandId::ScheduleList,
            parent: Some("schedule"),
            leaf_name: "list",
            summary: "List all flows.",
            policy: Policy::InApp,
            params: &[],
            handoff_reason: None,
        },
        CommandId::ScheduleRemove => Command {
            id: CommandId::ScheduleRemove,
            parent: Some("schedule"),
            leaf_name: "remove",
            summary: "Remove a flow.",
            policy: Policy::InApp,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::ScheduleRun => Command {
            id: CommandId::ScheduleRun,
            parent: Some("schedule"),
            leaf_name: "run",
            summary: "Manually run a flow.",
            policy: Policy::Handoff,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: Some("runs a flow; duration unbounded"),
        },

        CommandId::SessionList => Command {
            id: CommandId::SessionList,
            parent: Some("session"),
            leaf_name: "list",
            summary: "List all active CAO sessions.",
            policy: Policy::InApp,
            params: &[Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::SessionSend => Command {
            id: CommandId::SessionSend,
            parent: Some("session"),
            leaf_name: "send",
            summary: "Send a message to a session's conductor (or specific terminal).",
            policy: Policy::InApp,
            params: &[Param { name: "session_name", required: true, kind: ParamKind::Text }, Param { name: "message", required: true, kind: ParamKind::Text }, Param { name: "--terminal", required: false, kind: ParamKind::Text }, Param { name: "--async", required: false, kind: ParamKind::Flag }, Param { name: "--timeout", required: false, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::SessionStatus => Command {
            id: CommandId::SessionStatus,
            parent: Some("session"),
            leaf_name: "status",
            summary: "Show status of a session's conductor (or specific terminal).",
            policy: Policy::InApp,
            params: &[Param { name: "session_name", required: true, kind: ParamKind::Text }, Param { name: "--terminal", required: false, kind: ParamKind::Text }, Param { name: "--workers", required: false, kind: ParamKind::Flag }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },

        CommandId::SkillsAdd => Command {
            id: CommandId::SkillsAdd,
            parent: Some("skills"),
            leaf_name: "add",
            summary: "Install a skill from a local folder path.",
            policy: Policy::Handoff,
            params: &[Param { name: "folder_path", required: true, kind: ParamKind::Text }, Param { name: "--force", required: false, kind: ParamKind::Flag }],
            handoff_reason: Some(
                "no HTTP route: shutil.copytree into the global store (skills.py:32); OQ-6",
            ),
        },
        CommandId::SkillsList => Command {
            id: CommandId::SkillsList,
            parent: Some("skills"),
            leaf_name: "list",
            summary: "List installed skills.",
            policy: Policy::Handoff,
            params: &[],
            handoff_reason: Some(
                "no HTTP route: list_skills never imported server-side (skills.py:86); OQ-6",
            ),
        },
        CommandId::SkillsRemove => Command {
            id: CommandId::SkillsRemove,
            parent: Some("skills"),
            leaf_name: "remove",
            summary: "Remove an installed skill.",
            policy: Policy::Handoff,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }],
            handoff_reason: Some(
                "no HTTP route: shutil.rmtree (skills.py:78); OQ-6",
            ),
        },

        CommandId::TerminalRestore => Command {
            id: CommandId::TerminalRestore,
            parent: Some("terminal"),
            leaf_name: "restore",
            summary: "Restore a deleted terminal from its snapshot.",
            policy: Policy::Hidden,
            params: &[Param { name: "terminal_id", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
            // HIDE: human ruled out; recovery-by-terminal-ID tooling, not a launcher action
        },

        CommandId::WorkflowApprove => Command {
            id: CommandId::WorkflowApprove,
            parent: Some("workflow"),
            leaf_name: "approve",
            summary: "Approve a plan identifier so runs of that plan may start.",
            // Hidden per the mandated default: an unclassified command defaults to HIDE so an
            // unvetted command cannot surface half-working. Not reviewed for in-app use, and there
            // is a reason not to rush that review — approving a plan is a deliberate human
            // authorisation act, and surfacing it as a one-click pane entry is exactly the shape
            // that would make it casual. (#583 Bolt 2, approval-operation)
            policy: Policy::Hidden,
            params: &[Param { name: "plan_id", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::WorkflowCancel => Command {
            id: CommandId::WorkflowCancel,
            parent: Some("workflow"),
            leaf_name: "cancel",
            summary: "Cooperatively cancel a running workflow.",
            policy: Policy::InApp,
            params: &[Param { name: "run_id", required: true, kind: ParamKind::Text }],
            handoff_reason: None,
        },
        CommandId::WorkflowDelete => Command {
            id: CommandId::WorkflowDelete,
            parent: Some("workflow"),
            leaf_name: "delete",
            summary: "Delete a workflow's spec file and its index row.",
            policy: Policy::InApp,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }, Param { name: "--yes", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::WorkflowGet => Command {
            id: CommandId::WorkflowGet,
            parent: Some("workflow"),
            leaf_name: "get",
            summary: "Show the parsed/validated spec for a workflow name or file path.",
            policy: Policy::InApp,
            params: &[Param { name: "name", required: true, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::WorkflowList => Command {
            id: CommandId::WorkflowList,
            parent: Some("workflow"),
            leaf_name: "list",
            summary: "List indexed workflows (rebuilt from the spec files on disk).",
            policy: Policy::InApp,
            params: &[Param { name: "--dir", required: false, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        // ── The four `cao workflow *` leaves added by PR #525 (issue #505) ──────────────
        //
        // Missing from this table until CI caught it on PR #547 — CI tests the PR merged against
        // `main`, so it saw four commands a local run could not.
        //
        // `runs` and `result` are ordinary journal reads and are classified IN-APP: their routes
        // (`GET /workflows/runs` at `api/main.py:2630`, `GET /workflows/runs/{run_id}/result` at
        // `:3426`) return buffered JSON and terminate. `wait` and `events` are HANDOFF for the
        // reason `workflow run`/`resume` already are — unbounded duration, which the single-
        // threaded event loop cannot host.
        CommandId::WorkflowEvents => Command {
            id: CommandId::WorkflowEvents,
            parent: Some("workflow"),
            leaf_name: "events",
            summary: "Follow a run's live event stream, rendering per-run ordered progress.",
            policy: Policy::Handoff,
            params: &[
                Param { name: "run_id", required: true, kind: ParamKind::Text },
                Param { name: "--follow", required: false, kind: ParamKind::Flag },
                Param { name: "--after-seq", required: false, kind: ParamKind::Text },
                Param { name: "--json", required: false, kind: ParamKind::Flag },
            ],
            handoff_reason: Some(
                "an SSE stream consumed with `Accept: text/event-stream` and reconnect-on-drop                  (`workflow.py:929-955`): unbounded duration, and `ServerClient::run` is a                  request/response call with a 30s timeout — it cannot follow a live stream",
            ),
        },
        CommandId::WorkflowResult => Command {
            id: CommandId::WorkflowResult,
            parent: Some("workflow"),
            leaf_name: "result",
            summary: "Show the complete retained result for a (finished or in-flight) run.",
            policy: Policy::InApp,
            params: &[
                Param { name: "run_id", required: true, kind: ParamKind::Text },
                Param { name: "--json", required: false, kind: ParamKind::Flag },
            ],
            handoff_reason: None,
        },
        CommandId::WorkflowRuns => Command {
            id: CommandId::WorkflowRuns,
            parent: Some("workflow"),
            leaf_name: "runs",
            summary: "List workflow RUNS newest-first (distinct from `list`, which lists specs).",
            policy: Policy::InApp,
            params: &[
                Param { name: "--state", required: false, kind: ParamKind::Text },
                Param { name: "--limit", required: false, kind: ParamKind::Text },
                Param { name: "--json", required: false, kind: ParamKind::Flag },
            ],
            handoff_reason: None,
        },
        CommandId::WorkflowWait => Command {
            id: CommandId::WorkflowWait,
            parent: Some("workflow"),
            leaf_name: "wait",
            summary: "Follow an existing run by polling its status until it reaches a terminal state.",
            policy: Policy::Handoff,
            params: &[
                Param { name: "run_id", required: true, kind: ParamKind::Text },
                Param { name: "--json", required: false, kind: ParamKind::Flag },
            ],
            handoff_reason: Some(
                "polls until the run reaches a terminal state (`workflow.py:624`), so its duration                  is the run's duration — unbounded, and it would block the single-threaded event                  loop for as long as the workflow takes",
            ),
        },
        CommandId::WorkflowResume => Command {
            id: CommandId::WorkflowResume,
            parent: Some("workflow"),
            leaf_name: "resume",
            summary: "Resume a crashed/failed run from its durable journal (blocks until done).",
            policy: Policy::Handoff,
            params: &[Param { name: "run_id", required: true, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: Some("blocks until done, same as run"),
        },
        CommandId::WorkflowRun => Command {
            id: CommandId::WorkflowRun,
            parent: Some("workflow"),
            leaf_name: "run",
            summary: "Run a workflow to completion (blocks until the run finishes).",
            policy: Policy::Handoff,
            params: &[Param { name: "name_or_path", required: true, kind: ParamKind::Text }, Param { name: "--input", required: false, kind: ParamKind::Text }, Param { name: "--run-id", required: false, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: Some("blocks until the run finishes; unbounded duration"),
        },
        CommandId::WorkflowStatus => Command {
            id: CommandId::WorkflowStatus,
            parent: Some("workflow"),
            leaf_name: "status",
            summary: "Show a point-in-time status snapshot for a run.",
            policy: Policy::InApp,
            params: &[Param { name: "run_id", required: true, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
        CommandId::WorkflowValidate => Command {
            id: CommandId::WorkflowValidate,
            parent: Some("workflow"),
            leaf_name: "validate",
            summary: "Validate a workflow spec file WITHOUT running it.",
            policy: Policy::InApp,
            params: &[Param { name: "file", required: true, kind: ParamKind::Text }, Param { name: "--json", required: false, kind: ParamKind::Flag }],
            handoff_reason: None,
        },
    }
}

/// Every command the TUI offers, in display order — **`Hidden` rows excluded** (INV-1).
///
/// The exclusion is a correctness property, not a display choice: FR-4.3 requires hidden
/// commands be *absent from navigation*, so a `Hidden` row reaching a caller is a defect. It
/// also means `Hidden` cannot occur downstream of a selection — the operator was never able to
/// pick one.
///
/// Infallible: it filters a compile-time constant. (#321)
#[allow(dead_code)] // consumed by `renderer` (Bolt 5), which does not exist yet. (#321)
pub fn commands() -> Vec<Command> {
    DISPLAY_ORDER
        .iter()
        .copied()
        .map(entry)
        .filter(|command| command.policy != Policy::Hidden)
        .collect()
}

/// How to run `id`. Infallible (INV-3) — see [`entry`] for why there is no `None` case.
///
/// Returns `Hidden` honestly when asked. Reachability through [`commands`] does not imply
/// non-hidden for a *programmatic* caller, so a caller holding a [`CommandId`] from somewhere
/// other than `commands()` must still check. (#321)
#[allow(dead_code)] // consumed by `renderer` and `results-pane` (Bolt 5). (#321)
pub fn policy(id: CommandId) -> Policy {
    entry(id).policy
}

/// `id`'s parameters, in the CLI's own spelling (BR-8). Infallible (INV-3).
///
/// An empty `Vec` for a command that takes none is the honest answer, not an error — many do.
/// (#321)
#[allow(dead_code)] // consumed by `guided-flow` (Bolt 4). (#321)
pub fn params(id: CommandId) -> Vec<Param> {
    entry(id).params.to_vec()
}

#[cfg(test)]
mod tests {
    use super::{
        commands, entry, params, policy, CommandId, ParamKind, Policy, COMMAND_COUNT, DISPLAY_ORDER,
    };
    use std::collections::BTreeSet;

    /// A command's full path, e.g. `workflow run` — the *identifying* name.
    ///
    /// Not [`super::Command::leaf_name`] on its own, which is ambiguous in exactly the place it
    /// matters: `flow run`, `schedule run`, and `workflow run` all have `leaf_name == "run"`, and
    /// two of the three are HANDOFF. A failure message reading "`cao run` is HANDOFF with no
    /// reason" would send the reader to the wrong row. Found by reading a mutation's own output
    /// rather than by review. (#321)
    fn full_name(command: &super::Command) -> String {
        match command.parent {
            Some(parent) => format!("{parent} {}", command.leaf_name),
            None => command.leaf_name.to_string(),
        }
    }

    /// Counts each policy across the whole table, by asking production code.
    ///
    /// Returns `(in_app, handoff, hidden)`. The counts are *derived*; every number they are
    /// compared against is a hard-coded literal in the test body. That direction matters — see
    /// [`the_policy_distribution_is_twentytwo_sixteen_twentythree`].
    fn distribution() -> (usize, usize, usize) {
        let mut counts = (0, 0, 0);
        for id in DISPLAY_ORDER {
            match policy(id) {
                Policy::InApp => counts.0 += 1,
                Policy::Handoff => counts.1 += 1,
                Policy::Hidden => counts.2 += 1,
            }
        }
        counts
    }

    /// Test 1 — **the policy distribution is 24 IN-APP / 18 HANDOFF / 28 HIDE, totalling 70.**
    ///
    /// Every number here is a **hard-coded literal**, and that is the entire design of the test.
    /// Deriving any of them from the table — `assert_eq!(in_app, TABLE.iter().filter(..).count())`
    /// — compares production against itself and can never fail. That vacuous-guard shape is the
    /// dominant failure mode in this project's history, so it is worth naming what this test
    /// would look like if it had it.
    ///
    /// **Four assertions rather than one summed check**, also deliberately: a single
    /// `in_app + handoff + hidden == 61` stays green when a command moves from IN-APP to HIDE,
    /// because the total is conserved. Reclassification is exactly the change most likely to
    /// happen by accident, so each policy is pinned separately and the failure names *which* one
    /// moved.
    ///
    /// The distribution's own history is why the literals are worth this much care: the design
    /// recorded 33/5/22 = 60, Bolt 1 added `cao tui`, and an earlier revision of the
    /// implementation plan "corrected" the total to 61 while leaving the decomposition at
    /// 33/5/24 — which sums to 62. Fixing an instance without re-deriving the count is the same
    /// failure mode twice over. (#321)
    ///
    /// Then **OQ-6 moved 11 more**, from IN-APP to HANDOFF: `api/main.py` has no route that does
    /// their work, and ADR-02 forbids the subprocess execution that would be the only alternative,
    /// so they cannot run captured in-pane at all. `decisions.md:121` had claimed "33 of 38 IN-APP
    /// commands have a route" — the real figure is 21 served plus `profile find` client-side.
    /// The count reached 22/16/23 only after being wrong at 38/7/15, 33/5/22, and 33/5/24. (#321)
    ///
    /// **Then four commands turned up that had been missing entirely.** `cao memory relationships`
    /// {list, inspect, promote, reject} were added to the CLI by PR #524 (issue #511) and never
    /// reached this table, so the figures were 22/16/23 = 61 while the Click tree had 65 leaves.
    /// All four are HIDE, per `project.md`'s mandated default for an unclassified command. A second
    /// merge then brought `cao workflow` {`runs`, `result`, `wait`, `events`} from PR #525 — caught
    /// in CI, which tests the PR merged against `main` and so saw four commands a local run could
    /// not. `runs`/`result` are ordinary journal reads (IN-APP); `wait`/`events` are unbounded
    /// (HANDOFF). That gives **24/18/27 = 69**. Note what the shape of this failure was: every count here was internally
    /// consistent and every test green, because nothing compared the table against the CLI. That
    /// is what `test/test_command_catalog_matches_click.py` now does. (Review on PR #547.)
    #[test]
    fn the_policy_distribution_is_twentyfour_eighteen_twentyeight() {
        let (in_app, handoff, hidden) = distribution();

        assert_eq!(in_app, 24, "expected 24 IN-APP commands, found {in_app}");
        assert_eq!(handoff, 18, "expected 18 HANDOFF commands, found {handoff}");
        assert_eq!(hidden, 28, "expected 28 HIDE commands, found {hidden}");
        assert_eq!(
            in_app + handoff + hidden,
            70,
            "the three policy counts must account for all 70 leaf commands of the Click tree"
        );

        // The three counts summing to 69 does not prove 69 *distinct* commands were counted: a
        // duplicated entry in DISPLAY_ORDER would inflate one policy while a real command went
        // uncounted, and the arithmetic above would still close. DISPLAY_ORDER is generated, so
        // this is a live hazard rather than a theoretical one.
        let distinct: BTreeSet<CommandId> = DISPLAY_ORDER.iter().copied().collect();
        assert_eq!(
            distinct.len(),
            70,
            "DISPLAY_ORDER must list 70 DISTINCT commands; a duplicate would let one command go \
             uncounted while the totals still summed correctly"
        );
    }

    /// Test 1b — **every [`CommandId`] variant is listed in [`DISPLAY_ORDER`]** (FR-4.3).
    ///
    /// # The hole this closes, found by mutation rather than by review
    ///
    /// The exhaustive `match` in [`entry`] proves a variant is **classified**. Nothing proved it
    /// was **reachable**. Measured: adding a `SessionKill` variant, satisfying both `E0004`
    /// errors (a row here and a route arm in `server.rs`), and simply *forgetting*
    /// [`DISPLAY_ORDER`] **compiled cleanly and passed all 151 tests** — leaving a command that
    /// is classified, routed, and absent from navigation forever.
    ///
    /// That is defect #3 of the three motivating this rewrite wearing a new costume: the
    /// superseded TUI's results pane was likewise built, tested, and never reached from
    /// production. "The compiler has my back" is exactly where a contributor stops checking, so
    /// the uncovered case needs a test rather than a caveat in a doc comment.
    ///
    /// Neither existing guard catches it. [`the_policy_distribution_is_twentytwo_sixteen_twentythree`]
    /// counts what `DISPLAY_ORDER` *contains*, so a variant missing from it is simply never
    /// counted; and its `distinct.len() == 69` assertion detects a **duplicate**, which is the
    /// opposite direction. [`COMMAND_COUNT`] pins the array's *length*, never its membership.
    ///
    /// # Why an exhaustive match and NOT a discriminant trick
    ///
    /// The first version of this test used `CommandId::WorkflowValidate as usize + 1` to count
    /// variants. **It did not work, and the mutation above is what proved it**: `LAST` names a
    /// specific variant, so appending a new one *after* it leaves the constant stale and the
    /// computed total too low — the very edit the test exists to catch is the edit that
    /// invalidates its own yardstick. It passed the replayed mutation cleanly. A guard whose
    /// reference value is maintained by hand fails exactly when the hand forgets, which is
    /// always the same moment the defect arrives.
    ///
    /// So the count comes from the **compiler** instead. Stable Rust cannot iterate an enum's
    /// variants — that needs `strum` or a macro generating the enum and the array from one list
    /// (the real fix, and a larger change than this guard warrants today). What stable Rust
    /// *does* offer is exhaustiveness: the `match` below has no `_` arm, so **adding a variant
    /// makes this test file stop compiling**, and the contributor is sent here to add it to
    /// `EVERY_ID` — at which point the length assertions do the rest.
    ///
    /// That inverts the maintenance burden. `LAST` had to be *remembered*; `EVERY_ID` cannot be
    /// forgotten, because forgetting it is a build failure in this file.
    ///
    /// The two sources stay independent, which is what keeps this from being the vacuous
    /// `assert_eq!(x.len(), x.len())` shape this project keeps rediscovering: `EVERY_ID` is
    /// written against the **enum**, `DISPLAY_ORDER` against the **display sequence**. (#321)
    #[test]
    fn display_order_lists_every_command_id() {
        /// Maps each variant to itself, purely so the `match` is exhaustive over `CommandId`.
        ///
        /// The body is deliberately trivial; the *type check* is the mechanism. No `_` arm — that
        /// is what turns a new variant into a compile error right here.
        fn every_id() -> Vec<CommandId> {
            // A new `CommandId` variant makes this match non-exhaustive and this file stops
            // compiling. Add the variant to this list AND to DISPLAY_ORDER, then bump
            // COMMAND_COUNT.
            //
            // `needless_match` is allowed because the match is NEEDED for its exhaustiveness
            // check, not for its return value — clippy is judging the body while the type check
            // is the whole point. Replacing it with the identity function clippy suggests would
            // delete the mechanism and leave a test that cannot notice a new variant, which is
            // precisely the defect this test was rewritten to fix. (#321)
            #[allow(clippy::needless_match)]
            fn identity(id: CommandId) -> CommandId {
                match id {
                    CommandId::Info => CommandId::Info,
                    CommandId::Init => CommandId::Init,
                    CommandId::Install => CommandId::Install,
                    CommandId::Launch => CommandId::Launch,
                    CommandId::McpServer => CommandId::McpServer,
                    CommandId::Shutdown => CommandId::Shutdown,
                    CommandId::Tui => CommandId::Tui,
                    CommandId::Update => CommandId::Update,
                    CommandId::ConfigGet => CommandId::ConfigGet,
                    CommandId::ConfigList => CommandId::ConfigList,
                    CommandId::ConfigPath => CommandId::ConfigPath,
                    CommandId::ConfigSet => CommandId::ConfigSet,
                    CommandId::EnvGet => CommandId::EnvGet,
                    CommandId::EnvList => CommandId::EnvList,
                    CommandId::EnvSet => CommandId::EnvSet,
                    CommandId::EnvUnset => CommandId::EnvUnset,
                    CommandId::FlowAdd => CommandId::FlowAdd,
                    CommandId::FlowDisable => CommandId::FlowDisable,
                    CommandId::FlowEnable => CommandId::FlowEnable,
                    CommandId::FlowList => CommandId::FlowList,
                    CommandId::FlowRemove => CommandId::FlowRemove,
                    CommandId::FlowRun => CommandId::FlowRun,
                    CommandId::MemoryClear => CommandId::MemoryClear,
                    CommandId::MemoryCompact => CommandId::MemoryCompact,
                    CommandId::MemoryDelete => CommandId::MemoryDelete,
                    CommandId::MemoryExport => CommandId::MemoryExport,
                    CommandId::MemoryHeal => CommandId::MemoryHeal,
                    CommandId::MemoryImport => CommandId::MemoryImport,
                    CommandId::MemoryLint => CommandId::MemoryLint,
                    CommandId::MemoryList => CommandId::MemoryList,
                    CommandId::MemoryPromote => CommandId::MemoryPromote,
                    CommandId::MemoryRelationshipsInspect => CommandId::MemoryRelationshipsInspect,
                    CommandId::MemoryRelationshipsList => CommandId::MemoryRelationshipsList,
                    CommandId::MemoryRelationshipsPromote => CommandId::MemoryRelationshipsPromote,
                    CommandId::MemoryRelationshipsReject => CommandId::MemoryRelationshipsReject,
                    CommandId::MemoryRepair => CommandId::MemoryRepair,
                    CommandId::MemoryShow => CommandId::MemoryShow,
                    CommandId::ProfileCreate => CommandId::ProfileCreate,
                    CommandId::ProfileFind => CommandId::ProfileFind,
                    CommandId::ProfileList => CommandId::ProfileList,
                    CommandId::ProfileRemove => CommandId::ProfileRemove,
                    CommandId::ProfileShow => CommandId::ProfileShow,
                    CommandId::ProfileTemplates => CommandId::ProfileTemplates,
                    CommandId::ProfileValidate => CommandId::ProfileValidate,
                    CommandId::ScheduleAdd => CommandId::ScheduleAdd,
                    CommandId::ScheduleDisable => CommandId::ScheduleDisable,
                    CommandId::ScheduleEnable => CommandId::ScheduleEnable,
                    CommandId::ScheduleList => CommandId::ScheduleList,
                    CommandId::ScheduleRemove => CommandId::ScheduleRemove,
                    CommandId::ScheduleRun => CommandId::ScheduleRun,
                    CommandId::SessionList => CommandId::SessionList,
                    CommandId::SessionSend => CommandId::SessionSend,
                    CommandId::SessionStatus => CommandId::SessionStatus,
                    CommandId::SkillsAdd => CommandId::SkillsAdd,
                    CommandId::SkillsList => CommandId::SkillsList,
                    CommandId::SkillsRemove => CommandId::SkillsRemove,
                    CommandId::TerminalRestore => CommandId::TerminalRestore,
                    CommandId::WorkflowApprove => CommandId::WorkflowApprove,
                    CommandId::WorkflowCancel => CommandId::WorkflowCancel,
                    CommandId::WorkflowDelete => CommandId::WorkflowDelete,
                    CommandId::WorkflowEvents => CommandId::WorkflowEvents,
                    CommandId::WorkflowGet => CommandId::WorkflowGet,
                    CommandId::WorkflowList => CommandId::WorkflowList,
                    CommandId::WorkflowResult => CommandId::WorkflowResult,
                    CommandId::WorkflowResume => CommandId::WorkflowResume,
                    CommandId::WorkflowRun => CommandId::WorkflowRun,
                    CommandId::WorkflowRuns => CommandId::WorkflowRuns,
                    CommandId::WorkflowStatus => CommandId::WorkflowStatus,
                    CommandId::WorkflowWait => CommandId::WorkflowWait,
                    CommandId::WorkflowValidate => CommandId::WorkflowValidate,
                }
            }

            // Every variant, each passed through the exhaustive map above.
            [
                CommandId::Info,
                CommandId::Init,
                CommandId::Install,
                CommandId::Launch,
                CommandId::McpServer,
                CommandId::Shutdown,
                CommandId::Tui,
                CommandId::Update,
                CommandId::ConfigGet,
                CommandId::ConfigList,
                CommandId::ConfigPath,
                CommandId::ConfigSet,
                CommandId::EnvGet,
                CommandId::EnvList,
                CommandId::EnvSet,
                CommandId::EnvUnset,
                CommandId::FlowAdd,
                CommandId::FlowDisable,
                CommandId::FlowEnable,
                CommandId::FlowList,
                CommandId::FlowRemove,
                CommandId::FlowRun,
                CommandId::MemoryClear,
                CommandId::MemoryCompact,
                CommandId::MemoryDelete,
                CommandId::MemoryExport,
                CommandId::MemoryHeal,
                CommandId::MemoryImport,
                CommandId::MemoryLint,
                CommandId::MemoryList,
                CommandId::MemoryPromote,
                CommandId::MemoryRelationshipsInspect,
                CommandId::MemoryRelationshipsList,
                CommandId::MemoryRelationshipsPromote,
                CommandId::MemoryRelationshipsReject,
                CommandId::MemoryRepair,
                CommandId::MemoryShow,
                CommandId::ProfileCreate,
                CommandId::ProfileFind,
                CommandId::ProfileList,
                CommandId::ProfileRemove,
                CommandId::ProfileShow,
                CommandId::ProfileTemplates,
                CommandId::ProfileValidate,
                CommandId::ScheduleAdd,
                CommandId::ScheduleDisable,
                CommandId::ScheduleEnable,
                CommandId::ScheduleList,
                CommandId::ScheduleRemove,
                CommandId::ScheduleRun,
                CommandId::SessionList,
                CommandId::SessionSend,
                CommandId::SessionStatus,
                CommandId::SkillsAdd,
                CommandId::SkillsList,
                CommandId::SkillsRemove,
                CommandId::TerminalRestore,
                CommandId::WorkflowApprove,
                CommandId::WorkflowCancel,
                CommandId::WorkflowDelete,
                CommandId::WorkflowEvents,
                CommandId::WorkflowGet,
                CommandId::WorkflowList,
                CommandId::WorkflowResult,
                CommandId::WorkflowResume,
                CommandId::WorkflowRun,
                CommandId::WorkflowRuns,
                CommandId::WorkflowStatus,
                CommandId::WorkflowWait,
                CommandId::WorkflowValidate,
            ]
            .into_iter()
            .map(identity)
            .collect()
        }

        let in_enum: BTreeSet<CommandId> = every_id().into_iter().collect();
        let in_display: BTreeSet<CommandId> = DISPLAY_ORDER.iter().copied().collect();

        // The load-bearing assertion. Set difference rather than a length compare, so the failure
        // NAMES the unreachable command instead of reporting an off-by-one.
        let missing: Vec<CommandId> = in_enum.difference(&in_display).copied().collect();
        assert!(
            missing.is_empty(),
            "these CommandId variants are absent from DISPLAY_ORDER and can therefore NEVER \
             appear in navigation: {missing:?}. Such a command still COMPILES and still passes \
             every other test — `entry()`'s exhaustive match forces it to be classified, and the \
             distribution test only counts what DISPLAY_ORDER already contains. It is the same \
             'built but never reached from production' defect this rewrite exists to fix \
             (FR-4.3). Add each to DISPLAY_ORDER in its group's position and bump COMMAND_COUNT."
        );

        // The converse: a variant listed for display that the enum no longer has cannot occur
        // (the array is typed `[CommandId; _]`), but a DUPLICATE can — and would make the set
        // smaller than the array while every length check above still passed.
        assert_eq!(
            in_display.len(),
            DISPLAY_ORDER.len(),
            "DISPLAY_ORDER contains a duplicate: {} distinct ids across {} slots",
            in_display.len(),
            DISPLAY_ORDER.len()
        );

        // And pin both against the module's own literal, so a wrong-but-internally-consistent
        // pair cannot drift past all three checks.
        assert_eq!(
            in_enum.len(),
            COMMAND_COUNT,
            "`CommandId` declares {} variants but COMMAND_COUNT is {COMMAND_COUNT}",
            in_enum.len()
        );
    }

    /// Test 2 — **`commands()` never returns a `Hidden` entry** (INV-1).
    ///
    /// FR-4.3 requires hidden commands be *absent from navigation*, not greyed out, so a
    /// `Hidden` row reaching a caller is a correctness defect rather than a display nit.
    ///
    /// The length assertion is what stops this being vacuous in the other direction: a
    /// `commands()` that returned an empty `Vec` would satisfy "contains no `Hidden` entry"
    /// perfectly. 38 is `33 + 5` written as a literal for the same reason as test 1. (#321)
    #[test]
    fn commands_excludes_every_hidden_entry() {
        let offered = commands();

        assert_eq!(
            offered.len(),
            42,
            "commands() must offer the 24 IN-APP plus 18 HANDOFF commands and nothing else; an \
             empty or short list would satisfy the Hidden check below while offering nothing"
        );

        for command in &offered {
            assert_ne!(
                command.policy,
                Policy::Hidden,
                "commands() returned `cao {}` with policy Hidden; FR-4.3 requires hidden \
                 commands be ABSENT from navigation, not present-and-marked",
                full_name(command)
            );
        }
    }

    /// Test 3 — **every HANDOFF entry carries a non-empty reason** (BR-4, VR-1).
    ///
    /// This is the control against a **wrong** classification, which the exhaustive match cannot
    /// catch at all: `memory compact` and `memory heal` were classified HANDOFF during design,
    /// compiled cleanly, and were wrong — only human review found them. The reason field exists
    /// so the justification sits where a reviewer reads it.
    ///
    /// The count assertion is load-bearing, not decoration. A loop over "every HANDOFF entry"
    /// passes trivially when there are none, so reclassifying all five away would turn this test
    /// green while destroying what it checks. The literal 5 is what makes the loop's body
    /// guaranteed to execute. (#321)
    #[test]
    fn every_handoff_entry_states_a_reason() {
        let mut handoffs = Vec::new();

        for id in DISPLAY_ORDER {
            let command = entry(id);
            let name = full_name(&command);
            match command.policy {
                Policy::Handoff => {
                    let reason = command.handoff_reason.unwrap_or_else(|| {
                        panic!(
                            "`cao {name}` is HANDOFF with handoff_reason: None; BR-4 makes the \
                             reason mandatory because an exhaustive match catches a MISSING \
                             classification but never a WRONG one"
                        )
                    });
                    assert!(
                        !reason.trim().is_empty(),
                        "`cao {name}` is HANDOFF with an empty reason; an empty reason compiles \
                         and is still a defect (VR-1)"
                    );
                    handoffs.push(name);
                }
                // The converse: a reason on a non-HANDOFF row means a classification was
                // changed and its justification left behind, which misleads the next reviewer.
                _ => assert!(
                    command.handoff_reason.is_none(),
                    "`cao {name}` is not HANDOFF but carries a handoff_reason; a stale reason \
                     left behind by a reclassification misinforms review"
                ),
            }
        }

        // The exact sixteen, not merely sixteen of them. A count alone cannot distinguish "the
        // right sixteen" from "one reclassified in and another out" — and VR-3 exists because a
        // count-only check passed while two commands were misclassified.
        //
        // Eleven of these were IN-APP until OQ-6 (#321): they have NO server route, so under
        // ADR-02's no-subprocess rule the TUI cannot run them captured in-pane at all. Their
        // reasons name the in-process call site, because "no route exists" is a different reason
        // for HANDOFF than the original five's "interactive or unbounded".
        assert_eq!(
            handoffs,
            vec![
                "install",
                "launch",
                "memory import",
                "memory lint",
                "memory promote",
                "memory repair",
                "profile create",
                "profile remove",
                "profile templates",
                "profile validate",
                "schedule run",
                "skills add",
                "skills list",
                "skills remove",
                "workflow events",
                "workflow resume",
                "workflow run",
                "workflow wait"
            ],
            "expected exactly these 18 HANDOFF commands; without this assertion the loop above \
             passes vacuously when zero entries are HANDOFF"
        );
    }

    /// Test 4 — **all six `cao flow *` commands are HIDE** (FR-4.4).
    ///
    /// `flow` is a deprecated alias for `schedule` whose group Click itself marks
    /// `hidden=True`, at `cli/commands/schedule.py:133` (issue **#378**). FR-4.4 forbids the TUI
    /// resurrecting a command the CLI conceals — and the alias is not inert: invoking it emits a
    /// deprecation warning to stderr, so surfacing all six would give the operator six phantom
    /// commands that complain when run.
    ///
    /// The expected set is written out rather than filtered from the table, so the test reddens
    /// in **both** directions: a seventh `flow` command appearing, and one of the six vanishing.
    /// A `filter(parent == "flow")` loop would silently shrink with the table. (#321)
    #[test]
    fn every_flow_alias_command_is_hidden() {
        const FLOW_COMMANDS: [CommandId; 6] = [
            CommandId::FlowAdd,
            CommandId::FlowDisable,
            CommandId::FlowEnable,
            CommandId::FlowList,
            CommandId::FlowRemove,
            CommandId::FlowRun,
        ];

        for id in FLOW_COMMANDS {
            assert_eq!(
                policy(id),
                Policy::Hidden,
                "{id:?} must be Hidden: Click marks the `flow` group hidden=True at \
                 cli/commands/schedule.py:133, and FR-4.4 forbids the TUI resurrecting a \
                 command the CLI itself conceals (issue #378)"
            );
        }

        let in_table = DISPLAY_ORDER
            .iter()
            .filter(|id| entry(**id).parent == Some("flow"))
            .count();
        assert_eq!(
            in_table, 6,
            "expected exactly 6 `cao flow *` commands in the table; the hard-coded list above \
             cannot notice a seventh being added"
        );

        assert!(
            !commands()
                .iter()
                .any(|command| command.parent == Some("flow")),
            "no `cao flow *` command may appear in the navigable list"
        );
    }

    /// Test 5 — **`cao tui` is HIDE.**
    ///
    /// The 61st entry, and the one the design predicted would arrive: `business-logic-model.md`
    /// recorded `cao tui` as "absent from the table … `skeleton-wheel-bundle` adds the
    /// subcommand", and Bolt 1 duly added it. It is HIDE because the TUI must not offer itself —
    /// launching a second TUI from inside the first is either a no-op or a nested-terminal mess.
    ///
    /// This test is what guards the arithmetic correction described in test 1: if `cao tui` were
    /// ever reclassified, or dropped from the table, the 33/5/23 distribution would stop
    /// describing reality and the reason would be this specific command. (#321)
    #[test]
    fn the_tui_command_does_not_offer_itself() {
        assert_eq!(
            policy(CommandId::Tui),
            Policy::Hidden,
            "`cao tui` must be Hidden: the TUI offering itself as a runnable command nests a \
             second TUI inside the first"
        );

        assert!(
            !commands()
                .iter()
                .any(|command| command.id == CommandId::Tui),
            "`cao tui` must be absent from the navigable list, not merely marked (FR-4.3)"
        );
    }

    /// Test 6 — **`cao launch` has 12 parameters: 1 required, 7 text, 5 flags** (BR-9).
    ///
    /// Every number is hard-coded. The surface was enumerated from the Click tree, and getting
    /// it wrong is not cosmetic in either direction: marking a second parameter required blocks
    /// runs the CLI would accept (FR-2.2), while missing one hides a parameter the operator
    /// needs. An earlier artifact in this record claimed "all 12 parameters are reachable" while
    /// showing only 10 — `--auto-approve` and the positional `message` were the two omitted.
    ///
    /// `message` being **positional** is asserted separately because it changes how a caller
    /// builds argv. `params()` returns the CLI's own spelling (BR-8), so a positional argument
    /// is recognisable by having no `--` prefix — there is no separate flag on [`super::Param`]
    /// for it, and inventing `--message` would make the CLI reject the request. Note also that
    /// `--memory` is a **flag** despite reading like a value-taking option. (#321)
    #[test]
    fn launch_exposes_twelve_parameters_with_agents_the_only_required_one() {
        let launch = params(CommandId::Launch);

        assert_eq!(launch.len(), 12, "`cao launch` declares 12 parameters");

        let required: Vec<&str> = launch
            .iter()
            .filter(|p| p.required)
            .map(|p| p.name)
            .collect();
        assert_eq!(
            required,
            vec!["--agents"],
            "`--agents` is the ONLY required parameter of `cao launch`; marking a second one \
             required blocks runs the CLI would accept (FR-2.2)"
        );

        let text = launch.iter().filter(|p| p.kind == ParamKind::Text).count();
        let flags = launch.iter().filter(|p| p.kind == ParamKind::Flag).count();
        assert_eq!(text, 7, "7 of the 12 take a value");
        assert_eq!(flags, 5, "5 of the 12 are boolean flags");

        let message = launch
            .iter()
            .find(|p| p.name == "message")
            .expect("`cao launch` takes a trailing positional `message` argument");
        assert!(
            !message.name.starts_with("--"),
            "`message` is a POSITIONAL argument, so it must carry no `--` prefix; a caller \
             building argv places it by position and `--message` is a flag the CLI rejects"
        );
        assert!(
            !message.required,
            "the positional `message` is optional; only `--agents` is required"
        );
    }
}
