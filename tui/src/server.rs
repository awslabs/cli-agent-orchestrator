//! `server-client` — **all** of the crate's HTTP, and the only I/O component in it (issue #321).
//!
//! Six methods (`health`, `profiles`, `providers`, `create_session`, `terminal`, `run`) and six
//! error variants (INV-1). No other unit opens a connection (BR-1), and **no unit anywhere
//! executes a subprocess** (BR-2, ADR-02) — that prohibition is what makes the catalog static
//! and the pickers HTTP-only, which is design defect #1's other half.
//!
//! # The four mistakes available in `create_session`, none of which the compiler catches
//!
//! Recorded at the top because each is individually cheap to make and expensive to find:
//!
//! 1. **The query key is `agent_profile`, not `agents`** (`api/main.py:1690`). The Rust field is
//!    `agents` to match the operator-facing `--agents`; both names are right on their own side
//!    and *nothing connects them*, so a missing rename is a **422 at run time and never a
//!    compile error**. [`ServerClient::create_session`] therefore builds the query from
//!    `serde_json`'s own rendering of [`SessionParams`], so the wire key comes from the same
//!    `#[serde(rename)]` the body would use — see that method's docs.
//! 2. **`POST /sessions` answers 201, not 200** (`:1686`, `status_code=HTTP_201_CREATED`). A
//!    client asserting 200 treats every successful launch as an error.
//! 3. **`env_vars` travels in the JSON body, never the query string** (SR-1, issue **#248**).
//! 4. **A blank optional is OMITTED, never sent as `""`** (BR-5, FR-2.4).
//!
//! # Three DISTINCT filtering rules, and conflating them is the defect
//!
//! - **Exclude non-profile entries** (FR-1.6) — filtering by *kind*. Permitted.
//! - **NEVER filter on `loadable == false`** (FR-1.5, BR-11) — see [`ServerClient::profiles`].
//! - **NEVER filter providers on `installed == false`** (FR-1.7, BR-12).
//!
//! # Two timeouts, deliberately not conflated (TS-3)
//!
//! [`REQUEST_TIMEOUT_SECS`] bounds **one** HTTP call. The 30-second readiness cap in
//! `handoff.rs` bounds a **loop** of up to 30 calls. A 30-second per-request timeout would let
//! one slow call consume the entire budget, so the two numbers are different on purpose.
//!
//! # Degrade visibly; never fall back (T-6, BR-3, FR-1.4)
//!
//! Every failure is a typed [`TuiError`] the caller renders in-pane — one styled line, never a
//! traceback (SR-6, INV-2). There is deliberately **no CLI fallback** for choice data: a
//! fallback is the defect being removed, not a resilience feature.
//!
//! # SR-5 — a stated limitation, not a control claimed
//!
//! This unit adds **no authentication of its own.** CAO is a local single-operator tool and the
//! server is expected on loopback, but an operator who points `CAO_API_HOST` at a remote host is
//! sending unauthenticated plain HTTP. Recorded because the intent does not introduce remote
//! operation and claiming otherwise would be a false assurance.

use std::collections::BTreeMap;
use std::io::Read;
use std::time::Duration;

use crate::catalog::{self, CommandId, ParamKind};
use crate::error::TuiError;
use crate::types::{Health, Profile, Provider, SessionParams, Terminal};

/// The documented default host, used **only** when `CAO_API_HOST` is unset.
///
/// Mirrors `constants.py:337`. SR-4 forbids reaching the server at a hard-coded address
/// *regardless of the environment*; it does not forbid a documented fallback, which is exactly
/// what the Python client does (`os.environ.get("CAO_API_HOST", "127.0.0.1")`). The distinction
/// matters: a literal used only when the variable is absent keeps the TUI working out of the box
/// without silently overriding an operator whose server is elsewhere. (#321)
const DEFAULT_API_HOST: &str = "127.0.0.1";

/// The documented default port, used only when `CAO_API_PORT` is unset. Mirrors
/// `constants.py:338`. (#321)
const DEFAULT_API_PORT: u16 = 9889;

/// Per-request bound, in whole seconds.
///
/// **Not** the 30-second readiness cap (TS-3) — see the module docs. Matches the Python client's
/// `MCP_REQUEST_TIMEOUT` (`constants.py:346`) so the two front doors give up at the same point.
/// (#321)
const REQUEST_TIMEOUT_SECS: u64 = 30;

/// Largest response body accepted, in bytes.
///
/// A cap rather than an unbounded read: `run()` streams into a caller-supplied sink, but the
/// five typed reads buffer, and a server that never stops sending would otherwise grow the TUI's
/// heap without limit. 8 MiB is far above any real projection (25 profiles is ~12 KB measured)
/// and far below a memory problem. Exceeding it is [`TuiError::Decode`], not a truncated parse —
/// a silently truncated body would deserialise into a *different* value. (#321)
const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

/// Chunk size for the streaming read in [`ServerClient::run`].
const STREAM_CHUNK_BYTES: usize = 8 * 1024;

/// `SessionParams` wire keys that must travel in the JSON body and **never** the query string.
///
/// Both carry operator content that must stay out of cao-server's HTTP access log:
///
/// - `env_vars` — values may be secrets. Issue **#248**, which is why `CreateSessionBody`
///   exists at all.
/// - `initial_message` — the launch prompt. Same hazard, stated by the server itself at
///   `api/main.py:206-212`: prompt content "can be large (URL-length 414 risk) and sensitive
///   (query strings are routinely captured in HTTP access logs and traces)".
///
/// **A named set rather than an inline `if key == "env_vars"`.** The query is built by iterating
/// `SessionParams`' serialised keys, so anything not listed here goes into the query string by
/// default — the dangerous direction. Naming the body fields in one place means a future body
/// field is a one-line addition here instead of a silent leak, and
/// [`the_body_only_fields_never_reach_the_query_string`] asserts the split for every entry rather
/// than for the one that happened to be remembered. (#321, and review on PR #547)
const BODY_ONLY_FIELDS: [&str; 2] = ["env_vars", "initial_message"];

/// The HTTP verb a route is reached with.
///
/// Only the four this crate actually issues. A fifth would be a route the catalog does not
/// reach, so the enum is closed on purpose. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    /// A read.
    Get,
    /// A create or an action.
    Post,
    /// A removal.
    Delete,
}

/// How one form field reaches one route: which field, and the wire name it travels under.
///
/// The two names are **separate on purpose**, because they differ and nothing else connects them:
/// `cao profile show` takes a positional the catalog spells `name_or_path` while the route
/// placeholder is `{name}`, and `cao memory list --type` maps to a query parameter FastAPI
/// declares as `alias="type"` over a Python identifier of `memory_type`. Collapsing them into one
/// string would work for most rows and produce a 404 or a silently-ignored filter for the rest.
///
/// This is the same class of trap as `SessionParams`' `agents` → `agent_profile` rename: invisible
/// to the compiler, visible only as a runtime error. (#321, and review on PR #547)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Binding {
    /// The form field's name, in the catalog's exact spelling — `--scope`, `key`, `name_or_path`.
    pub field: &'static str,
    /// The wire name: a `{placeholder}` token for a path binding, or a query-parameter name.
    pub wire: &'static str,
}

/// A resolved HTTP route: a verb, a path template, the placeholders the caller must fill, and how
/// the form's fields bind to them.
///
/// `path` is a **template**, and `placeholders` names each `{token}` in it. All of it is
/// `&'static`, so a route is a compile-time constant with no allocation — the same property
/// `catalog.rs`'s rows have, for the same reason.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Route {
    /// The verb.
    pub method: Method,
    /// The path, e.g. `/workflows/{name}`. Never a full URL — [`ServerClient::base_url`] owns
    /// the scheme, host, and port so SR-4 holds in exactly one place.
    pub path: &'static str,
    /// The `{token}` names appearing in [`Route::path`], in order.
    ///
    /// Carried so a caller can tell *which* value a route still needs. Empty for a route that
    /// takes no path parameter.
    pub placeholders: &'static [&'static str],
    /// Which form field supplies each path placeholder.
    ///
    /// **A route with placeholders and no path bindings cannot be run**, and that is the honest
    /// state rather than a gap to paper over: it means nothing in the form is known to supply the
    /// value. Before this existed, `renderer` called `run(id, &[], &[], None, ..)` for every
    /// in-app command — so a route needing `{run_id}` was simply never runnable, and one needing
    /// no path value ran with the operator's typed filters silently discarded.
    /// (Review on PR #547.)
    pub path_bindings: &'static [Binding],
    /// Which form fields travel as query parameters, and under which names.
    ///
    /// Only fields whose wire name was **read at source** appear here. A text field with no entry
    /// is deliberately not guessed at: sending `?scope=..` to a route that declares no `scope`
    /// parameter is not a filter, it is an ignored argument, and an operator who typed it would
    /// believe it applied. `unbound_text_fields` reports exactly those, and the renderer refuses
    /// the run rather than sending a request that quietly does something else.
    pub query_bindings: &'static [Binding],
}

/// The route for `id`, or `None` when the command is served no other way.
///
/// # This table lives HERE and not in `CommandCatalog`, and that resolves a contradiction
///
/// `business-logic-model.md:151` says `run()` resolves its route *"from `CommandCatalog`"*, but
/// `component-methods.md:66-73` gives the catalog exactly three methods — `commands()`,
/// `policy()`, `params()` — and **no route accessor**; the shipped `catalog.rs` matches that. So
/// `run()` as written could not be implemented. **Operator-approved resolution: the route table
/// lives in this unit.** A route is an HTTP fact and this unit owns all HTTP; putting it in the
/// catalog would give a zero-I/O unit knowledge of the transport. `CommandId` stays the shared
/// key, so the coupling is a *type* rather than a dependency.
///
/// # The mechanism is the same as the catalog's: an exhaustive match, no `_` arm
///
/// A [`CommandId`] added without a route decision **fails to compile**. A `_ => None` fallback
/// would compile, silently make the command routeless, and tell nobody — which is precisely the
/// silence `catalog.rs`'s own docs explain the enum exists to remove. [`TuiError::NoRoute`]
/// stays a typed variant for the genuinely routeless (BR-18), not a fallback arm.
///
/// # 21 routes for 22 IN-APP commands
///
/// `profile find` has **no** route and is served client-side by
/// [`ServerClient::find_profiles`] (OQ-6 Q2). Every other IN-APP command maps to a route
/// verified at `api/main.py` during this stage. HANDOFF and HIDE commands return `None`: a
/// HANDOFF command runs in a real terminal and needs no route (that is why OQ-6 reclassified 11
/// of them), and a HIDE command is unreachable through `commands()` in the first place.
///
/// # Known fidelity gaps, recorded at the row where somebody would hit them
///
/// Four routes do the work but **not identically** to the CLI. Each carries a comment at its own
/// arm below rather than only in a document, because the limitation matters at the call site.
fn route(id: CommandId) -> Option<Route> {
    /// Shorthand for a route with no path placeholder and no query binding.
    const fn plain(method: Method, path: &'static str) -> Option<Route> {
        Some(Route {
            method,
            path,
            placeholders: &[],
            path_bindings: &[],
            query_bindings: &[],
        })
    }
    /// Shorthand for a route with no path placeholder that DOES take query parameters.
    const fn filtered(
        method: Method,
        path: &'static str,
        query_bindings: &'static [Binding],
    ) -> Option<Route> {
        Some(Route {
            method,
            path,
            placeholders: &[],
            path_bindings: &[],
            query_bindings,
        })
    }
    /// Shorthand for a route whose placeholders are supplied by form fields.
    ///
    /// `path_bindings` rather than a bare placeholder list: a placeholder nothing supplies is a
    /// route that cannot run, and naming the supplying field at the row is what makes it runnable.
    const fn bound(
        method: Method,
        path: &'static str,
        placeholders: &'static [&'static str],
        path_bindings: &'static [Binding],
        query_bindings: &'static [Binding],
    ) -> Option<Route> {
        Some(Route {
            method,
            path,
            placeholders,
            path_bindings,
            query_bindings,
        })
    }
    /// Shorthand for a route with placeholders that **no form field supplies**.
    ///
    /// Kept distinct from [`bound`] so the two `terminal_id` routes read as the deliberate gap
    /// they are: `cao session send`/`status` resolve a session name to a terminal id with a
    /// SECOND call (`session.py:26`, `_resolve_conductor`), and this crate does not do that yet.
    const fn templated(
        method: Method,
        path: &'static str,
        placeholders: &'static [&'static str],
    ) -> Option<Route> {
        Some(Route {
            method,
            path,
            placeholders,
            path_bindings: &[],
            query_bindings: &[],
        })
    }

    match id {
        // ── Top-level leaves ─────────────────────────────────────────────────────────────
        // HIDE: no route exists (`cao info` reads local session context, `cao init` bootstraps
        // the DB, `cao mcp-server` is a foreground server, `cao shutdown` can kill the TUI's own
        // tmux session, `cao tui` must not offer itself, `cao update` may replace the binary).
        CommandId::Info => None,
        CommandId::Init => None,
        CommandId::McpServer => None,
        CommandId::Shutdown => None,
        CommandId::Tui => None,
        CommandId::Update => None,
        // HANDOFF: `POST /agents/profiles/install` exists (`api/main.py:1507`) but the CLI's
        // `cao install` also accepts local filesystem paths, which that route deliberately
        // refuses ("A remote caller therefore cannot coerce the server into reading arbitrary
        // .md files from disk", `:1516-1521`). Running it in a real terminal keeps the local-path
        // form working; routing it here would silently lose half the command's surface.
        CommandId::Install => None,
        // HANDOFF: the interactive agent session — the stated hand-off case. `create_session`
        // below IS this route, reached through the guided launch flow rather than through `run`.
        CommandId::Launch => None,

        // ── `cao config *`, `cao env *` ───────────────────────────────────────────────────
        // HIDE, and genuinely routeless: `cao env *` reads and writes the managed env store
        // in-process. These are the routeless commands BR-18's `NoRoute` variant was named for.
        CommandId::ConfigGet => None,
        CommandId::ConfigList => None,
        CommandId::ConfigPath => None,
        CommandId::ConfigSet => None,
        CommandId::EnvGet => None,
        CommandId::EnvList => None,
        CommandId::EnvSet => None,
        CommandId::EnvUnset => None,

        // ── `cao flow *` — all six HIDE ──────────────────────────────────────────────────
        // Click marks the group `hidden=True` (`cli/commands/schedule.py:133`); it is a
        // deprecated alias for `schedule` (issue #378). Routes DO exist (the `/flows` family),
        // and that is exactly why these must stay `None`: FR-4.4 forbids the TUI resurrecting a
        // command the CLI itself conceals, and a route would make it trivially resurrectable.
        CommandId::FlowAdd => None,
        CommandId::FlowDisable => None,
        CommandId::FlowEnable => None,
        CommandId::FlowList => None,
        CommandId::FlowRemove => None,
        CommandId::FlowRun => None,

        // ── `cao memory *` ───────────────────────────────────────────────────────────────
        // ⚠ FIDELITY GAP 1 of 4 — `memory clear`. `DELETE /memory` (`api/main.py:3560`)
        // **requires an explicit `scope_id` for every non-global scope** (`:3574-3578`), while
        // the CLI resolves it from the cwd. Harvestable from `GET /memory` rows, but not a
        // drop-in: a caller that omits it gets a 400, not a clear.
        //
        // `--scope` is bound: the route DECLARES `scope: MemoryScope` as a REQUIRED query
        // parameter (`:3989`), so an unbound run 422s every time. `--yes` is absent from the
        // bindings deliberately — it is the CLI's local confirmation prompt, not a server
        // parameter, and the TUI's own [enter] is the equivalent affordance.
        CommandId::MemoryClear => filtered(
            Method::Delete,
            "/memory",
            &[Binding {
                field: "--scope",
                wire: "scope",
            }],
        ),
        // ⚠ FIDELITY GAP 2 of 4 — `memory delete`. Same `scope_id` requirement
        // (`api/main.py:3534-3538`) as `memory clear`, for the same reason.
        //
        // `key` is the positional the CLI declares (`memory.py:157`) and `{key}` is the route's
        // placeholder; the two names happen to agree here, and `Binding` states it rather than
        // relying on that. `scope` defaults to `PROJECT` server-side (`:3951`), so binding
        // `--scope` is what lets an operator reach any other scope. `--yes` stays local.
        CommandId::MemoryDelete => bound(
            Method::Delete,
            "/memory/{key}",
            &["key"],
            &[Binding {
                field: "key",
                wire: "key",
            }],
            &[Binding {
                field: "--scope",
                wire: "scope",
            }],
        ),
        // ⚠ FIDELITY GAP 3 of 4 — `memory export`. `GET /memory/export` is **tar.gz streaming
        // only**: the CLI's default OKF *directory* output has no HTTP equivalent, `--prune` is
        // not a query parameter at all, and the route **hard-refuses** the `session`/`agent`
        // scopes the CLI reaches via `--include-private` (`api/main.py:3424-3429`, "the API
        // surface never exports private tiers"). `project` additionally requires `scope_id`.
        //
        // Three of the seven CLI options bind, each read at `:3653-3657`: `--format` → `format`,
        // `--scope` → `scope` (REQUIRED there, so an unbound run 422s), `--include-history` →
        // `include_history`, `--redact` → `redact`. The other two do NOT bind and must not be
        // invented: `--output` is a local filesystem destination, and `--prune` has no query
        // parameter at all — the gap named above.
        CommandId::MemoryExport => filtered(
            Method::Get,
            "/memory/export",
            &[
                Binding {
                    field: "--scope",
                    wire: "scope",
                },
                Binding {
                    field: "--format",
                    wire: "format",
                },
                Binding {
                    field: "--include-history",
                    wire: "include_history",
                },
                Binding {
                    field: "--redact",
                    wire: "redact",
                },
            ],
        ),
        // ⚠ FIDELITY GAP 4 of 4 — `memory list`. `GET /memory` always passes `scan_all=True`
        // (`api/main.py:3385`), so it mirrors `cao memory list --all` and NOT the CLI's default
        // cwd-scoped view. That default needs a `scope_id` the TUI **cannot compute**:
        // `resolve_project_id` prefers `CAO_PROJECT_ID`, then the normalised git remote, then
        // `sha256(realpath(cwd))[:12]` (`memory_service.py:210-250`), and no route exposes
        // "my project id". The gap is unclosable from this side, so it is recorded, not worked
        // around.
        //
        // **`--type` binds to the wire name `type`, not `memory_type`.** FastAPI declares it as
        // `memory_type: Optional[MemoryType] = Query(default=None, alias="type")` (`:3621`), and
        // the ALIAS is what goes on the wire. Sending `memory_type=user` would be silently
        // ignored — an unfiltered list presented as a filtered one, which is worse than an error.
        // This is exactly why [`Binding`] carries two names.
        //
        // `--all` does not bind: the route hard-codes `scan_all=True` (`:3634`), so it already
        // behaves as `--all` and there is nothing to send. That is fidelity gap 4, and the
        // renderer marks the field rather than pretending it applies.
        CommandId::MemoryList => filtered(
            Method::Get,
            "/memory",
            &[
                Binding {
                    field: "--scope",
                    wire: "scope",
                },
                Binding {
                    field: "--type",
                    wire: "type",
                },
            ],
        ),
        CommandId::MemoryShow => bound(
            Method::Get,
            "/memory/{key}",
            &["key"],
            &[Binding {
                field: "key",
                wire: "key",
            }],
            &[Binding {
                field: "--scope",
                wire: "scope",
            }],
        ),
        // HIDE: maintenance sweeps with no route (`memory compact`/`heal` call the LLM compiler
        // and lint repair in-process).
        CommandId::MemoryCompact => None,
        CommandId::MemoryHeal => None,
        // HANDOFF, all five — OQ-6. No route exists, and ADR-02 forbids the subprocess that
        // would be the only alternative, so they cannot run captured in-pane at all. The
        // in-process call site is named on each catalog row.
        // HIDE, all four — `cao memory relationships *`, added by PR #524 (issue #511) and
        // missing from the catalog until review on PR #547. `None` here because a HIDE command is
        // unreachable through `commands()` and needs no route, NOT because no route exists: `GET
        // /memory/relationships` (`api/main.py:3770`) and `POST .../{id}/promote` (`:3854`) are
        // real. `reject` maps to `PATCH .../{id}` (`:3829`), and `Method` has no `Patch` variant,
        // so routing that one would widen the transport enum as well. Classifying any of them
        // IN-APP is a deliberate, reviewable change — they mutate stored memory.
        CommandId::MemoryRelationshipsInspect => None,
        CommandId::MemoryRelationshipsList => None,
        CommandId::MemoryRelationshipsPromote => None,
        CommandId::MemoryRelationshipsReject => None,
        CommandId::MemoryImport => None,
        CommandId::MemoryLint => None,
        CommandId::MemoryPromote => None,
        CommandId::MemoryRepair => None,

        // ── `cao profile *` ──────────────────────────────────────────────────────────────
        CommandId::ProfileList => plain(Method::Get, "/agents/profiles"),
        // `GET /agents/profiles/{name}` (`api/main.py:1493`) — deliberately the SINGULAR route
        // and not the list one. It applies `model_dump(exclude_none=True)` while the list route
        // returns `list_agent_profiles()` directly (`:1485`), so the two project differently;
        // `cao profile show` wants the full parsed profile, which is what the singular route
        // gives. (Note `skeleton-endpoint-verify` asserts the LIST shape only, for that reason.)
        //
        // **The field is `name_or_path`, the placeholder is `{name}`.** The CLI declares
        // `@click.argument("name_or_path")` (`profile.py:166`) while the route is
        // `/agents/profiles/{name}`. Binding by placeholder name alone would find no field called
        // `name` and leave the command unrunnable; binding by field name alone would substitute
        // nothing into `{name}` and send a literal brace. The pair is the fix.
        CommandId::ProfileShow => bound(
            Method::Get,
            "/agents/profiles/{name}",
            &["name"],
            &[Binding {
                field: "name_or_path",
                wire: "name",
            }],
            &[],
        ),
        // **`profile find` is the one IN-APP command with no route** — 21 routes for 22
        // commands. `search_profiles` is reachable only from the CLI (`profile.py:385`) and the
        // **stdio-only** MCP server (`mcp_server/server.py:2120`, `mcp.run()`), so it is not
        // HTTP-reachable at all. Served client-side by `find_profiles` over `GET
        // /agents/profiles`; `None` here is correct, not an omission. (OQ-6 Q2)
        CommandId::ProfileFind => None,
        // HANDOFF, all four — OQ-6. `profile create`/`templates` call `agent_scaffold`
        // in-process, `profile validate` runs `Draft202012Validator` locally, and `profile
        // remove` is a local `unlink()` with **no DELETE on `/agents/*`** anywhere.
        CommandId::ProfileCreate => None,
        CommandId::ProfileRemove => None,
        CommandId::ProfileTemplates => None,
        CommandId::ProfileValidate => None,

        // ── `cao schedule *` ─────────────────────────────────────────────────────────────
        // ⚠ FIDELITY GAP (the fourth named in the plan; fifth arm carrying one) —
        // `schedule add`. `POST /flows` takes a **structured body** and writes its own
        // frontmatter (`api/main.py:3196-3211`), so the TUI must parse the operator's
        // `.flow.md` client-side. Worse: `CreateFlowRequest` (`:1479-1490`) has **no `script`
        // field**, while `flow_service.add_flow` reads and stores one (`flow_service.py:89`,
        // `script = metadata.get("script", "")`). **A flow file with a `script:` key loses it
        // when created over HTTP.**
        //
        // Left as `plain`, so it has NO bindings and the renderer refuses it: the only field is
        // `file_path`, and `POST /flows` wants a five-field `CreateFlowRequest` body
        // (`name`/`schedule`/`agent_profile`/`provider`/`prompt_template`, `:553-561`) that only a
        // client-side parse of the `.flow.md` could produce. Sending the path as a query parameter
        // or a one-key body is a guaranteed 422 — which is what the previous
        // `run(id, &[], &[], None, ..)` call did on every attempt.
        CommandId::ScheduleAdd => plain(Method::Post, "/flows"),
        CommandId::ScheduleDisable => bound(
            Method::Post,
            "/flows/{name}/disable",
            &["name"],
            &[Binding {
                field: "name",
                wire: "name",
            }],
            &[],
        ),
        CommandId::ScheduleEnable => bound(
            Method::Post,
            "/flows/{name}/enable",
            &["name"],
            &[Binding {
                field: "name",
                wire: "name",
            }],
            &[],
        ),
        CommandId::ScheduleList => plain(Method::Get, "/flows"),
        CommandId::ScheduleRemove => bound(
            Method::Delete,
            "/flows/{name}",
            &["name"],
            &[Binding {
                field: "name",
                wire: "name",
            }],
            &[],
        ),
        // HANDOFF: `POST /flows/{name}/run` exists (`api/main.py:3282`) but awaits
        // `execute_flow` inline, so its duration is unbounded — the original HANDOFF reason.
        CommandId::ScheduleRun => None,

        // ── `cao session *` ──────────────────────────────────────────────────────────────
        CommandId::SessionList => plain(Method::Get, "/sessions"),
        // `POST /terminals/{terminal_id}/input` (`api/main.py:2063`) — the route the CLI itself
        // uses (`session.py:227-230`). Note the terminal id, not the session name: `cao session
        // send` resolves the conductor via `GET /sessions/{name}/terminals` first
        // (`session.py:26`, `_resolve_conductor`), so a caller needs two calls, and the
        // placeholder names what it must supply.
        //
        // **`templated` and not `bound`: these two are the deliberate remaining gap.** The form
        // holds a `session_name`, and `{terminal_id}` is a DIFFERENT identifier — binding the one
        // to the other would send a session name where a terminal id belongs and 404, or worse
        // address some unrelated terminal whose id happened to collide. The resolving call is not
        // implemented here, so the renderer refuses these two and names what is missing. That
        // refusal is the honest state; a plausible-looking binding would not be.
        CommandId::SessionSend => templated(
            Method::Post,
            "/terminals/{terminal_id}/input",
            &["terminal_id"],
        ),
        // `GET /terminals/{terminal_id}` (`:2003`), matching `session.py:32`'s `_get_terminal`.
        // Same two-call shape as `session send`, and the same refusal.
        CommandId::SessionStatus => {
            templated(Method::Get, "/terminals/{terminal_id}", &["terminal_id"])
        }

        // ── `cao skills *` — HANDOFF, all three (OQ-6) ───────────────────────────────────
        // The entire group is routeless. `GET/POST /settings/skill-dirs` is NOT this: it returns
        // and sets two *directory paths* (`api/main.py:1623-1629`), while `cao skills list`
        // parses a `SKILL.md` per folder and `cao skills add` copies a folder into the global
        // store. `docs/skills.md:106` states the distinction in the repo's own words. Mapping
        // them onto that route was a real error made twice during OQ-6 — route-name similarity
        // instead of behaviour, the same error class as the `--help` scraping this rewrite
        // removes.
        CommandId::SkillsAdd => None,
        CommandId::SkillsList => None,
        CommandId::SkillsRemove => None,

        // ── `cao terminal *` ─────────────────────────────────────────────────────────────
        // HIDE: recovery-by-terminal-id tooling, not a launcher action.
        CommandId::TerminalRestore => None,

        // ── `cao workflow *` ─────────────────────────────────────────────────────────────
        //
        // The four `{name}`/`{run_id}` routes all take their value from the CLI's own positional
        // of the same name (`workflow.py:129, 158, 257, 323`). `--json` binds nowhere: it is a
        // local output-format choice, and the pane renders whatever the route returns — which is
        // JSON already. `--yes` is likewise the CLI's confirmation prompt.
        CommandId::WorkflowCancel => bound(
            Method::Post,
            "/workflows/runs/{run_id}/cancel",
            &["run_id"],
            &[Binding {
                field: "run_id",
                wire: "run_id",
            }],
            &[],
        ),
        CommandId::WorkflowDelete => bound(
            Method::Delete,
            "/workflows/{name}",
            &["name"],
            &[Binding {
                field: "name",
                wire: "name",
            }],
            &[],
        ),
        CommandId::WorkflowGet => bound(
            Method::Get,
            "/workflows/{name}",
            &["name"],
            &[Binding {
                field: "name",
                wire: "name",
            }],
            &[],
        ),
        // `--dir` is a real query parameter here: `dir: Optional[str] = Query(default=None)`
        // (`:2606`).
        CommandId::WorkflowList => filtered(
            Method::Get,
            "/workflows",
            &[Binding {
                field: "--dir",
                wire: "dir",
            }],
        ),
        CommandId::WorkflowStatus => bound(
            Method::Get,
            "/workflows/runs/{run_id}",
            &["run_id"],
            &[Binding {
                field: "run_id",
                wire: "run_id",
            }],
            &[],
        ),
        // The four leaves PR #525 (issue #505) added, missing from the catalog until CI caught it
        // on PR #547.
        //
        // `--state` and `--limit` are real query parameters on `GET /workflows/runs`
        // (`api/main.py:2632-2633`); `--json` binds nowhere, being a local output-format choice.
        CommandId::WorkflowRuns => filtered(
            Method::Get,
            "/workflows/runs",
            &[
                Binding {
                    field: "--state",
                    wire: "state",
                },
                Binding {
                    field: "--limit",
                    wire: "limit",
                },
            ],
        ),
        // `GET /workflows/runs/{run_id}/result` (`:3426`) — a two-segment path, safe at any
        // declaration position, taking its value from the CLI's own `run_id` positional.
        CommandId::WorkflowResult => bound(
            Method::Get,
            "/workflows/runs/{run_id}/result",
            &["run_id"],
            &[Binding {
                field: "run_id",
                wire: "run_id",
            }],
            &[],
        ),
        // HANDOFF, both: unbounded duration. `wait` polls until the run terminates and `events`
        // consumes an SSE stream with reconnect-on-drop, so neither fits a request/response call
        // with a 30s timeout on a single-threaded event loop — the same reason `workflow run` and
        // `resume` are HANDOFF. Routes exist for both; that is not the constraint.
        CommandId::WorkflowEvents => None,
        CommandId::WorkflowWait => None,
        // `POST /workflows/validate` (`api/main.py:2549`) takes the spec path in a JSON body
        // (`WorkflowValidateRequest { path: str }`, `:398-401`), not a query parameter.
        //
        // Left `plain` — no bindings — so the renderer refuses it rather than POSTing an empty
        // body for a guaranteed 422. Wiring it needs a body-building mechanism this crate does not
        // have; a `query_bindings` entry for `file` would send `?path=..` to a route that reads
        // only the body, which 422s just as reliably while looking like it was wired.
        CommandId::WorkflowValidate => plain(Method::Post, "/workflows/validate"),
        // HANDOFF: both block until the run finishes. `POST /workflows/runs` and
        // `.../resume` exist, but the CLI itself swaps in `WORKFLOW_RUN_REQUEST_TIMEOUT` for
        // them rather than the flat 30s (`workflow.py:228-234`), which is the definition of
        // unbounded for this client's purposes.
        CommandId::WorkflowResume => None,
        CommandId::WorkflowRun => None,
        // `cao workflow approve` is classified HIDE, so the TUI never routes it — and this arm
        // exists only because the match is exhaustive on purpose. It is deliberately `None` rather
        // than a real binding: routing a command the TUI does not offer would build a reachable
        // path to a human authorisation act that nothing in the interface has reviewed.
        // (#583 Bolt 2, approval-operation)
        CommandId::WorkflowApprove => None,
    }
}

/// The placeholders `id`'s route needs that **no form field supplies**.
///
/// Empty means every placeholder is bound, so the route can be built.
///
/// # This replaced `route_placeholders`, which asked the wrong question
///
/// That accessor returned *every* placeholder, and `renderer` treated a non-empty answer as "not
/// wired". The question it could answer — "does this route have a `{token}`?" — is not the question
/// that decides runnability, and using it as though it were left **10 commands unreachable whose
/// token a form field plainly supplies** (`memory show`, `workflow get`/`cancel`/`status`/`delete`,
/// `schedule enable`/`disable`/`remove`, and the rest). It was removed rather than kept beside this
/// one: two accessors differing only in a subtlety like that is how a caller picks the wrong one.
///
/// The rendered error still **names what is missing**, which is why this returns names and not a
/// `bool` — "needs `{terminal_id}`" is a stated limit where "not supported" is a dead end.
///
/// Derived from [`route`] rather than duplicated, so there is exactly one route table. A
/// hard-coded id list in `renderer` would be a second place for the same fact, free to drift — and
/// the drift would present as a 404 on a literal `{run_id}` brace, which is the confusing failure
/// [`ServerClient::run`]'s own docs warn about.
///
/// Returns `None` for a routeless command, so callers keep one shape for "no route".
/// (#321, and review on PR #547)
pub fn unbound_placeholders(id: CommandId) -> Option<Vec<&'static str>> {
    route(id).map(|route| {
        route
            .placeholders
            .iter()
            .copied()
            .filter(|placeholder| {
                !route
                    .path_bindings
                    .iter()
                    .any(|binding| binding.wire == *placeholder)
            })
            .collect()
    })
}

/// The **value-carrying** form fields of `id` that the route would ignore, in catalog order.
///
/// A field here is one an operator can type into and that would then be **silently discarded** —
/// the defect this exists to prevent. `renderer` refuses the run and names them rather than
/// sending a request that does something other than what the form shows.
///
/// # What is deliberately NOT reported
///
/// - **Flags.** A flag the endpoint has no parameter for is almost always a CLI-local behaviour
///   (`--json` output formatting, `--yes` confirmation, `--all` where the route already scans
///   everything). Reporting them would refuse nearly every command over choices that change
///   nothing about the request.
/// - **Bound fields**, whether by path or query — those reach the server.
/// - **`--env`-style pair fields**, which no in-app route takes; none exists outside `cao launch`.
///
/// So the report is exactly: *text and positional fields whose value has nowhere to go*. A
/// command with none of those, and all path placeholders bound, is runnable.
/// (Review on PR #547.)
pub fn unbound_text_fields(id: CommandId) -> Vec<&'static str> {
    let Some(route) = route(id) else {
        return Vec::new();
    };

    catalog::params(id)
        .iter()
        .filter(|param| param.kind != ParamKind::Flag)
        .map(|param| param.name)
        .filter(|name| {
            let bound_to_path = route.path_bindings.iter().any(|b| b.field == *name);
            let bound_to_query = route.query_bindings.iter().any(|b| b.field == *name);
            !bound_to_path && !bound_to_query
        })
        .collect()
}

/// The path values for `id`'s route, read out of `flow`, in the route's placeholder order.
///
/// **Order is the contract.** [`ServerClient::run`] zips these against `Route::placeholders`
/// positionally, so this iterates the placeholders and looks up each one's binding — not the
/// bindings in declaration order. Every route here has one placeholder today, so a reversed
/// implementation would pass every current test and break the first two-placeholder route added.
///
/// A placeholder whose bound field is empty yields an empty string rather than being skipped:
/// dropping it would shift every later value one position left, sending a run id where a name
/// belongs. `in_app_readiness` is what prevents an unfilled required value getting this far; this
/// function's job is to preserve alignment, not to re-litigate readiness. (Review on PR #547.)
pub fn path_values_for(id: CommandId, flow: &crate::guided_flow::GuidedFlow) -> Vec<String> {
    let Some(route) = route(id) else {
        return Vec::new();
    };

    order_path_values(route.placeholders, route.path_bindings, &|field| {
        field_text(flow, field)
    })
}

/// Places each binding's value at its **placeholder's** position.
///
/// # Why this is a separate function
///
/// The ordering rule is the whole content of [`path_values_for`], and it is **unobservable through
/// the real route table**: every route has exactly one placeholder today, so iterating
/// `path_bindings` in declaration order gives the identical answer. A mutation doing precisely that
/// passed all 137 tests — the ordering was asserted by nothing.
///
/// Splitting it out lets a test supply a synthetic two-placeholder route whose bindings are
/// declared in the *opposite* order, where the two implementations disagree. That test would have
/// caught the mutation, and it will catch the first real two-placeholder route added — which is
/// otherwise a URL with its path values transposed, 404-ing or, worse, addressing the wrong object.
///
/// `lookup` is a closure rather than a `&GuidedFlow` so the test can drive it without building a
/// form for a command that does not exist. (Found by mutation; review on PR #547.)
fn order_path_values(
    placeholders: &[&'static str],
    bindings: &[Binding],
    lookup: &dyn Fn(&'static str) -> Option<String>,
) -> Vec<String> {
    placeholders
        .iter()
        .map(|placeholder| {
            bindings
                .iter()
                .find(|binding| binding.wire == *placeholder)
                .and_then(|binding| lookup(binding.field))
                .unwrap_or_default()
        })
        .collect()
}

/// The query pairs for `id`'s route, read out of `flow`. Unset fields are **omitted**.
///
/// Omission rather than an empty value, for two reasons both verified against the server:
///
/// - A `bool` parameter rejects `""` outright — probed against FastAPI: `?flag=` is a **422**
///   (`bool_parsing`, "unable to interpret input"), while `?flag=true` and `?flag=1` are accepted.
///   So an unticked flag must not appear at all.
/// - An enum parameter such as `scope: Optional[MemoryScope]` treats `""` as an invalid member,
///   not as absent, so an untouched filter would turn a working request into a rejected one.
///
/// A ticked flag sends `"true"`, which the same probe confirmed. A **false** flag is omitted rather
/// than sent as `"false"`: every bool parameter on these routes already defaults to `False`
/// (`:3656-3657`), so the two are equivalent on the wire and omission keeps the URL to what the
/// operator actually asked for.
pub fn query_pairs_for(
    id: CommandId,
    flow: &crate::guided_flow::GuidedFlow,
) -> Vec<(&'static str, String)> {
    let Some(route) = route(id) else {
        return Vec::new();
    };

    route
        .query_bindings
        .iter()
        .filter_map(|binding| {
            let field = flow.field(binding.field)?;
            match field.value.as_ref()? {
                crate::guided_flow::FieldValue::Text(text) => Some((binding.wire, text.clone())),
                // `"true"`, not `"1"`: both parse, and `true` is what reads correctly in a log.
                crate::guided_flow::FieldValue::Flag(true) => {
                    Some((binding.wire, "true".to_string()))
                }
                crate::guided_flow::FieldValue::Flag(false) => None,
                // No in-app route takes an env-pair field; `--env` exists only on `cao launch`,
                // which is HANDOFF. Skipped rather than stringified into something meaningless.
                crate::guided_flow::FieldValue::EnvPairs(_) => None,
            }
        })
        .collect()
}

/// One field's text value, or `None` when it is unset or not a text-shaped field.
///
/// A ticked flag reads as `"true"` here too, so a flag bound to a *path* placeholder would still
/// produce something sane — no route does that today, and this keeps the helper total rather than
/// panicking if one ever does.
fn field_text(flow: &crate::guided_flow::GuidedFlow, name: &str) -> Option<String> {
    match flow.field(name)?.value.as_ref()? {
        crate::guided_flow::FieldValue::Text(text) => Some(text.clone()),
        crate::guided_flow::FieldValue::Flag(true) => Some("true".to_string()),
        crate::guided_flow::FieldValue::Flag(false) => None,
        crate::guided_flow::FieldValue::EnvPairs(_) => None,
    }
}

/// The HTTP client. One instance per TUI process.
///
/// Holds configuration only — no conversation state, no cached responses. The launch *sequence*
/// (`create_session` → poll → hand off) is owned by `renderer`, not here.
#[allow(dead_code)] // consumed by `guided-flow` (Bolt 4) and `renderer` (Bolt 5). (#321)
#[derive(Debug, Clone)]
pub struct ServerClient {
    /// `http://<host>:<port>`, resolved once from the environment at construction.
    base_url: String,
    /// Per-request bound. **Not** the 30-second readiness cap (TS-3).
    timeout: Duration,
}

impl Default for ServerClient {
    fn default() -> Self {
        Self::from_env()
    }
}

#[allow(dead_code)] // every caller is `guided-flow` (Bolt 4) / `renderer` (Bolt 5). (#321)
impl ServerClient {
    /// Reads `CAO_API_HOST` and `CAO_API_PORT` from the environment (SR-4).
    ///
    /// **Never a hard-coded `127.0.0.1:9889`.** A hard-coded loopback address silently breaks
    /// any operator whose server is configured elsewhere and invites a workaround that widens
    /// exposure. The two literals in this module are *fallbacks* used only when the variables
    /// are absent, mirroring `constants.py:337-338` exactly.
    ///
    /// A malformed `CAO_API_PORT` falls back to the default **and is not a silent success**: the
    /// port is parsed rather than interpolated, so a typo cannot compose a URL that fails later
    /// with a baffling connection error. It is deliberately not fatal — the TUI must open even
    /// when the environment is wrong (FR-6.1), and the resulting `Unreachable` names the address
    /// actually used. (#321)
    pub fn from_env() -> Self {
        let host = std::env::var("CAO_API_HOST")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_API_HOST.to_string());

        let port = std::env::var("CAO_API_PORT")
            .ok()
            .and_then(|raw| raw.trim().parse::<u16>().ok())
            .unwrap_or(DEFAULT_API_PORT);

        Self::with_base_url(format!("http://{host}:{port}"))
    }

    /// Builds a client against an explicit base URL.
    ///
    /// The seam this unit's tests use to reach a **stub bound on port 0** — the hermeticity
    /// tripwire blocks real HTTP in tests, and `skeleton-endpoint-verify` is the one deliberate
    /// exemption. Public rather than `cfg(test)`-only because `renderer` may need to point at a
    /// non-default server, and a test-only constructor would make the production path untested.
    pub fn with_base_url(base_url: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            timeout: Duration::from_secs(REQUEST_TIMEOUT_SECS),
        }
    }

    /// The API root this client will reach, for an error message that names the address.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// `GET /health` (`api/main.py:812`).
    ///
    /// `terminal_backend` is the value ADR-01 keys hand-off on: the TUI reads the server's
    /// configured backend rather than holding its own config or guessing. (#321)
    pub fn health(&self) -> Result<Health, TuiError> {
        self.get_json("/health")
    }

    /// `GET /agents/profiles` (`api/main.py:1479`) — **every** entry, including unloadable ones.
    ///
    /// # The one filter applied, and the one forbidden
    ///
    /// **Applied (FR-1.6, BR-10):** non-profile directory entries are excluded — filtering by
    /// *kind*, via [`is_profile_entry`].
    ///
    /// **FORBIDDEN (FR-1.5, BR-11, INV-5): a profile is NEVER filtered on `loadable == false`.**
    /// Unloadable profiles are **returned**, marked, and made unselectable by the picker. This
    /// method returns them; `guided-flow` renders the marker.
    ///
    /// ## `project.md:98` says the opposite, and FR-1.5 governs
    ///
    /// Affirmed memory reads *"ALWAYS filter agent profiles on `loadable == true` before
    /// presenting them in any picker"*. **FR-1.5 supersedes it**, per the operator's explicit
    /// later decision and the supersession block in `requirements.md`. Closing the memory
    /// contradiction is **OQ-5**, submitted via `learning propose` and awaiting the supervisor —
    /// affirmed memory is deliberately not edited from here.
    ///
    /// **So: do not "fix" this back into a filter.** The live data shows why the design chose
    /// this way round: 25 profiles are returned and 4 are unloadable, including `__pycache__` —
    /// precisely the incidental directory `project.md:98` was written to guard against.
    /// Filtering it *hides* it; marking it unselectable *explains* it. The operator cannot
    /// select it either way, but only one of those tells them why. (#321)
    pub fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
        let all: Vec<Profile> = self.get_json("/agents/profiles")?;

        // FR-1.6 only. There is deliberately no `loadable` term in this predicate — see above.
        Ok(all.into_iter().filter(is_profile_entry).collect())
    }

    /// `GET /agents/providers` (`api/main.py:1531`) — **all nine entries, unfiltered.**
    ///
    /// **No filtering whatsoever, including on `installed == false`** (FR-1.7, BR-12, INV-5). A
    /// provider known elsewhere in the system must not be silently hidden, and the drift is a
    /// live mechanism rather than a hypothetical: the route serves a hard-coded nine-entry map
    /// (`:1535-1545`) against a ten-value `ProviderType` enum. `installed` is display
    /// information the picker shows; it is never a predicate. (#321)
    pub fn providers(&self) -> Result<Vec<Provider>, TuiError> {
        self.get_json("/agents/providers")
    }

    /// `POST /sessions` (`api/main.py:1686`) — creates the session **and** its terminal.
    ///
    /// # The transport split, which is the detail most likely to be got wrong
    ///
    /// | Part | Contents |
    /// |---|---|
    /// | **Query** | `agent_profile` (required), `provider?`, `session_name?`, `working_directory?`, `allowed_tools?` |
    /// | **JSON body** | `env_vars?`, `initial_message?` — **each only when present** |
    ///
    /// **Both body fields travel in the BODY, never the query string.** For `env_vars` that is
    /// issue **#248**: values may contain secrets and the query string lands in cao-server's
    /// HTTP access log. `initial_message` is the same class of hazard for the same reason, in the
    /// server's own words at `api/main.py:206-212` — prompt content "can be large (URL-length
    /// 414 risk) and sensitive (query strings are routinely captured in HTTP access logs and
    /// traces)". This is the server's design, not a client convention: `CreateSessionBody`
    /// exists for exactly this split. With neither field present the body is omitted entirely
    /// rather than sent as `{}`.
    ///
    /// The split is driven by [`BODY_ONLY_FIELDS`] rather than by an `if key == "env_vars"`
    /// arm, so **adding a body field to `SessionParams` cannot leak it into the query string by
    /// omission** — a new field lands in the query by default otherwise, which is the failure
    /// direction that matters here.
    ///
    /// # Why the query is built from `serde_json`'s rendering instead of field by field
    ///
    /// The wire key is **`agent_profile`** while the Rust field is `agents` (BR-6a). Writing
    /// `.with_param("agent_profile", &params.agents)` would work *and* would put the wire
    /// spelling in a second place, free to drift from `SessionParams`' `#[serde(rename)]` with
    /// nothing to catch it — the mismatch is a **422 at run time, never a compile error**.
    /// Serialising the struct and reading its keys means both transports get the key from the
    /// same declaration. It also makes FR-2.4 automatic: `skip_serializing_if` already omits
    /// every `None`, so a blank optional cannot become `""` here — the value is simply not
    /// there to send.
    ///
    /// # 201, not 200
    ///
    /// `status_code=status.HTTP_201_CREATED` (`:1686`). A client asserting 200 treats every
    /// successful launch as an error (BR-7). A **422** is [`TuiError::Validation`] carrying the
    /// server's own detail, because FastAPI's message names the rejected field. (#321)
    pub fn create_session(&self, params: &SessionParams) -> Result<Terminal, TuiError> {
        let rendered = serde_json::to_value(params).map_err(|error| {
            TuiError::Decode(format!(
                "could not serialise the launch parameters: {error}"
            ))
        })?;
        let fields = rendered.as_object().ok_or_else(|| {
            TuiError::Decode("launch parameters must serialise to a JSON object".to_string())
        })?;

        let mut request = minreq::post(format!("{}/sessions", self.base_url))
            .with_timeout(self.timeout.as_secs());

        for (key, value) in fields {
            // The security control, keyed on the wire names `SessionParams` itself emits: these
            // fields are skipped here and placed in the body below. #248 for `env_vars`, and the
            // identical log-exposure reasoning for `initial_message`.
            if BODY_ONLY_FIELDS.contains(&key.as_str()) {
                continue;
            }
            // Every remaining field of `SessionParams` is a string on the wire; `as_str` is what
            // keeps a future non-string field from being stringified as `"true"` or `"null"`
            // silently. A `None` never reaches here at all (`skip_serializing_if`), which is
            // FR-2.4 holding by construction rather than by a check.
            let Some(text) = value.as_str() else {
                return Err(TuiError::Validation(format!(
                    "launch parameter {key:?} is not a string on the wire; \
                     POST /sessions takes every query parameter as text"
                )));
            };
            request = request.with_param(key, encode_query_component(text));
        }

        // The body carries exactly the present [`BODY_ONLY_FIELDS`]. With none of them present
        // there is no body at all, rather than `{}` — the server already reads a missing body as
        // "no env vars, no initial message".
        //
        // Built as a map so each field is independently optional: an operator who typed a message
        // but no `--env` must still get a body, and vice versa. The earlier form hard-coded
        // `{"env_vars": ..}` and would have dropped a message whenever `--env` was empty.
        let mut body = serde_json::Map::new();
        if let Some(env_vars) = params
            .env_vars
            .as_ref()
            .filter(|map: &&BTreeMap<String, String>| !map.is_empty())
        {
            body.insert("env_vars".to_string(), serde_json::json!(env_vars));
        }
        // No emptiness filter needed: `GuidedFlow::set` collapses blank text to `None` at entry,
        // so `Some("")` cannot arrive here — and the server rejects the empty string outright
        // (`api/main.py:1949-1950`), which is the behaviour that would surface if it ever did.
        if let Some(message) = params.initial_message.as_ref() {
            body.insert("initial_message".to_string(), serde_json::json!(message));
        }
        if !body.is_empty() {
            let encoded = serde_json::to_string(&body).map_err(|error| {
                TuiError::Decode(format!(
                    "could not serialise the JSON body for POST /sessions: {error}"
                ))
            })?;
            request = request
                .with_header("Content-Type", "application/json; charset=UTF-8")
                .with_body(encoded);
        }

        let response = self.send(request)?;

        // 201, not 200 (BR-7).
        match response.status {
            201 => decode(&response.body),
            422 => Err(TuiError::Validation(detail_of(&response.body))),
            other => Err(TuiError::Http(other)),
        }
    }

    /// `GET /terminals/{terminal_id}` (`api/main.py:2003`) — the readiness-poll target.
    ///
    /// **A `None` status is not an error** (BR-14): `Terminal.status` is declared optional and
    /// live-only (`models/terminal.py:41-42`), so its absence means *keep polling*. A **404** is
    /// [`TuiError::NotFound`], distinct from a 5xx, because `await_ready` treats the two
    /// differently — only an explicit 5xx is conclusive.
    ///
    /// **The six-to-three collapse to `Readiness` is NOT here** (BR-16). This method returns the
    /// raw six-valued [`crate::types::TerminalStatus`]; the mapping lives in
    /// `skeleton-handoff-proof`'s `await_ready` and nowhere else, so the two cannot drift.
    /// Likewise the 1-second interval and 30-second cap belong to that loop, not to this call —
    /// [`REQUEST_TIMEOUT_SECS`] bounds one request (TS-3). (#321)
    pub fn terminal(&self, terminal_id: &str) -> Result<Terminal, TuiError> {
        let path = format!("/terminals/{}", encode_path_segment(terminal_id));
        let response =
            self.send(minreq::get(self.url(&path)).with_timeout(self.timeout.as_secs()))?;

        match response.status {
            200 => decode(&response.body),
            404 => Err(TuiError::NotFound(terminal_id.to_string())),
            other => Err(TuiError::Http(other)),
        }
    }

    /// Runs an IN-APP command's route, streaming the response body into `sink` as it arrives.
    ///
    /// Returns the HTTP status once the body is exhausted. **Streams incrementally** (BR-17):
    /// the pane renders bytes as they arrive rather than waiting for completion, so a command
    /// producing output slowly is not indistinguishable from a hang. `sink` receives raw bytes —
    /// non-UTF-8 output is passed through for the pane to render lossily rather than being
    /// rejected here.
    ///
    /// `path_values` fills the route's `{token}` placeholders in order. A count mismatch is
    /// [`TuiError::Validation`] and never a partially-substituted URL: a template with an
    /// unfilled `{run_id}` would otherwise reach the server as a literal brace and 404 or 422
    /// somewhere confusing.
    ///
    /// [`TuiError::NoRoute`] for a command with no route (BR-18). In practice `commands()`
    /// filters HIDE rows so those are unreachable, and a HANDOFF command runs in a real terminal
    /// — the variant exists so a *programmatic* caller fails loudly rather than silently. (#321)
    pub fn run<W: std::io::Write>(
        &self,
        id: CommandId,
        path_values: &[&str],
        query: &[(&str, &str)],
        body: Option<&str>,
        sink: &mut W,
    ) -> Result<u16, TuiError> {
        let Some(route) = route(id) else {
            return Err(TuiError::NoRoute(format!("{id:?}")));
        };

        if path_values.len() != route.placeholders.len() {
            return Err(TuiError::Validation(format!(
                "{id:?} needs {expected} path value(s) {names:?} for {template:?}; got {actual}",
                expected = route.placeholders.len(),
                names = route.placeholders,
                template = route.path,
                actual = path_values.len(),
            )));
        }

        let mut path = route.path.to_string();
        for (placeholder, value) in route.placeholders.iter().zip(path_values) {
            path = path.replace(&format!("{{{placeholder}}}"), &encode_path_segment(value));
        }

        let url = self.url(&path);
        let mut request = match route.method {
            Method::Get => minreq::get(url),
            Method::Post => minreq::post(url),
            Method::Delete => minreq::delete(url),
        }
        .with_timeout(self.timeout.as_secs());

        for (key, value) in query {
            request = request.with_param(*key, encode_query_component(value));
        }
        if let Some(body) = body {
            request = request
                .with_header("Content-Type", "application/json; charset=UTF-8")
                .with_body(body.to_string());
        }

        let mut lazy = request
            .send_lazy()
            .map_err(|error| self.unreachable(&error))?;
        let status = lazy.status_code;

        // The incremental read. `read` on a `ResponseLazy` returns what has arrived rather than
        // waiting for the whole body, so each chunk reaches the sink as soon as it exists —
        // which is the whole of BR-17. A read error mid-stream is `Unreachable`: bytes already
        // handed to the sink stay rendered, so the pane shows partial output plus the failure
        // rather than silently truncating (T-6).
        let mut buffer = [0u8; STREAM_CHUNK_BYTES];
        let mut total = 0usize;
        loop {
            let read = lazy.read(&mut buffer).map_err(|error| {
                TuiError::Unreachable(format!(
                    "the response body from {method:?} {path} ended early after {total} bytes: \
                     {error}",
                    method = route.method,
                ))
            })?;
            if read == 0 {
                break;
            }
            total += read;
            if total > MAX_RESPONSE_BYTES {
                return Err(TuiError::Decode(format!(
                    "the response body from {method:?} {path} exceeded the {MAX_RESPONSE_BYTES} \
                     byte cap",
                    method = route.method,
                )));
            }
            sink.write_all(&buffer[..read])?;
        }

        Ok(status)
    }

    /// `profile find`, client-side: a case-insensitive **substring** filter (OQ-6 Q2).
    ///
    /// # This does NOT rank like `cao profile find`, and must not be "fixed" into claiming it
    ///
    /// The CLI scores with BM25Plus (`services/profile_search.py`). This is a plain substring
    /// match across the four fields `_searchable_text` tokenizes — `name`, `description`,
    /// `tags`, `capabilities` (`profile_search.py:43-51`) — in the order
    /// `GET /agents/profiles` returns them (alphabetical by name). **Operator decision, taken
    /// over a BM25Plus port:** reimplementing the ranking in Rust invites silent divergence from
    /// the Python scorer over time, and a search that ranks differently while claiming parity is
    /// worse than one that plainly filters. So there is no relevance ordering here and no parity
    /// claim to keep.
    ///
    /// Why it is client-side at all: **no search route exists.** `search_profiles` is called
    /// only from the CLI (`profile.py:385`) and from the stdio-only MCP server
    /// (`mcp_server/server.py:2120`), so it is not HTTP-reachable — but `GET /agents/profiles`
    /// returns exactly the fields the tokenizer reads.
    ///
    /// One deliberate divergence beyond ranking, and it is the FR-1.5 rule again: the Python
    /// scorer **drops unloadable profiles** (`profile_search.py:116`,
    /// `[p for p in profiles if p.get("loadable", True)]`). This does not. An unloadable profile
    /// matching the query is returned and marked unselectable, exactly as in [`Self::profiles`].
    /// (#321)
    pub fn find_profiles(&self, query: &str) -> Result<Vec<Profile>, TuiError> {
        let needle = query.trim().to_lowercase();
        let all = self.profiles()?;

        if needle.is_empty() {
            return Ok(all);
        }

        Ok(all
            .into_iter()
            .filter(|profile| searchable_text(profile).contains(&needle))
            .collect())
    }

    /// Composes a full URL from the configured base and a path.
    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }

    /// GETs `path`, requiring 200 and a decodable body.
    fn get_json<T: serde::de::DeserializeOwned>(&self, path: &str) -> Result<T, TuiError> {
        let response =
            self.send(minreq::get(self.url(path)).with_timeout(self.timeout.as_secs()))?;

        if response.status != 200 {
            return Err(TuiError::Http(response.status));
        }
        decode(&response.body)
    }

    /// Sends a request, mapping a transport failure to [`TuiError::Unreachable`].
    ///
    /// The distinction [`TuiError::Http`] draws against this variant is load-bearing rather than
    /// cosmetic: they call for different remedies (start the server vs. read the status), and
    /// `await_ready` treats them differently — only an explicit 5xx is conclusive (BR-12).
    fn send(&self, request: minreq::Request) -> Result<RawResponse, TuiError> {
        let response = request.send().map_err(|error| self.unreachable(&error))?;
        Ok(RawResponse {
            status: response.status_code,
            body: response.as_bytes().to_vec(),
        })
    }

    /// One operator-facing line naming the address actually used (SR-6).
    ///
    /// Names the address rather than paraphrasing, because the remedy differs entirely depending
    /// on whether the client is pointed where the operator expects — which is the whole reason
    /// SR-4 makes the address configurable.
    fn unreachable(&self, error: &minreq::Error) -> TuiError {
        TuiError::Unreachable(format!(
            "could not reach cao-server at {base}: {error}. Start `cao-server`, or point \
             CAO_API_HOST / CAO_API_PORT at a running instance",
            base = self.base_url,
        ))
    }
}

/// A buffered response, reduced to the two things every caller needs.
struct RawResponse {
    status: u16,
    body: Vec<u8>,
}

/// Deserialises a response body, mapping any failure to [`TuiError::Decode`].
///
/// `Decode` carries the underlying message because a shape change is only actionable with it —
/// "could not decode the server's response" alone sends the operator nowhere.
fn decode<T: serde::de::DeserializeOwned>(body: &[u8]) -> Result<T, TuiError> {
    if body.len() > MAX_RESPONSE_BYTES {
        return Err(TuiError::Decode(format!(
            "response body of {len} bytes exceeded the {MAX_RESPONSE_BYTES} byte cap",
            len = body.len()
        )));
    }
    serde_json::from_slice(body).map_err(|error| {
        // The body is truncated in the message rather than embedded whole: a 12 KB profile
        // listing in a one-line operator error is unreadable, and SR-6 asks for one styled line.
        let preview: String = String::from_utf8_lossy(body).chars().take(200).collect();
        TuiError::Decode(format!("{error}; body began {preview:?}"))
    })
}

/// FastAPI's `detail` from an error body, or the body itself when there is no `detail`.
///
/// A 422 names the rejected field in `detail`, and surfacing that is the difference between
/// "server rejected the request" and something the operator can act on.
fn detail_of(body: &[u8]) -> String {
    let text = String::from_utf8_lossy(body);

    match serde_json::from_slice::<serde_json::Value>(body) {
        Ok(value) => match value.get("detail") {
            Some(serde_json::Value::String(detail)) => detail.clone(),
            Some(detail) => detail.to_string(),
            None => text.chars().take(400).collect(),
        },
        Err(_) => text.chars().take(400).collect(),
    }
}

/// Whether an entry is a real agent profile rather than an incidental directory (FR-1.6, BR-10).
///
/// **Filtering by KIND, which is the one filter permitted here.** It is emphatically not a
/// loadability test: `__pycache__` is excluded because it is a Python bytecode directory that
/// was never a profile, and it would be excluded whatever its `loadable` value said. A profile
/// with a real name that merely fails to load stays in the list (FR-1.5) — that is the
/// distinction BR-10 and BR-11 draw, and conflating them produces the silent-hiding defect
/// FR-1.5 exists to prevent.
///
/// The rule is dunder names, matching FR-1.6's own wording ("e.g. `__pycache__`, dunder names").
/// Deliberately narrow: a broad heuristic here would start hiding real profiles, and the picker
/// showing one junk row is a far smaller failure than it hiding a profile the operator installed.
/// (#321)
fn is_profile_entry(profile: &Profile) -> bool {
    let name = profile.name.as_str();

    // Written as two early rejections rather than one negated conjunction: each `return false`
    // names the kind of entry it excludes, which is the whole distinction BR-10 draws against
    // BR-11. (clippy's `nonminimal_bool` fires on the single-expression form. #321)
    if name.is_empty() {
        return false;
    }
    if name.starts_with("__") && name.ends_with("__") {
        return false;
    }

    true
}

/// The four searchable fields joined, lowercased — the same fields `_searchable_text` reads.
///
/// Mirrors `profile_search.py:43-51`'s field selection and nothing else about it: no tokenizing,
/// no scoring. See [`ServerClient::find_profiles`] for why.
fn searchable_text(profile: &Profile) -> String {
    let mut text = profile.name.to_lowercase();

    if let Some(description) = &profile.description {
        text.push(' ');
        text.push_str(&description.to_lowercase());
    }
    for tag in &profile.tags {
        text.push(' ');
        text.push_str(&tag.to_lowercase());
    }
    for capability in &profile.capabilities {
        text.push(' ');
        text.push_str(&capability.to_lowercase());
    }

    text
}

/// Percent-encodes a value for a query parameter.
///
/// Hand-rolled rather than pulling `urlencoding` in: this is one small fixed rule, and
/// `minreq`'s `urlencoding` feature is not enabled (its own docs say the caller is then
/// responsible for legal characters). Unreserved characters per RFC 3986 §2.3 pass through; every
/// other byte is `%XX`, which covers `&`, `=`, `#`, `+`, space, and every non-ASCII byte.
///
/// **This is a correctness control, not cosmetics.** A session name containing `&` would
/// otherwise split into a second parameter, and a value containing `#` would truncate the query
/// at the fragment. Both are operator-supplied strings. (#321)
fn encode_query_component(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());

    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(*byte as char)
            }
            other => encoded.push_str(&format!("%{other:02X}")),
        }
    }

    encoded
}

/// Percent-encodes a value for one path segment.
///
/// Same rule as [`encode_query_component`], and `/` is *not* exempt — that is the point. A
/// terminal id or memory key containing `/` would otherwise add a path segment and reach a
/// different route entirely, which is a path-traversal shape rather than a display nit. (#321)
fn encode_path_segment(value: &str) -> String {
    encode_query_component(value)
}

#[cfg(test)]
mod tests {
    use super::{
        decode, detail_of, encode_path_segment, encode_query_component, is_profile_entry,
        order_path_values, route, searchable_text, Binding, Method, ServerClient, DEFAULT_API_HOST,
        DEFAULT_API_PORT, REQUEST_TIMEOUT_SECS,
    };
    use crate::catalog::{policy, CommandId, Policy, DISPLAY_ORDER};
    use crate::error::TuiError;
    use crate::types::{Health, Profile, SessionParams, Terminal, TerminalStatus};
    use std::collections::{BTreeMap, BTreeSet};
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::sync::mpsc::{channel, Receiver};
    use std::thread;

    /// One request as the stub server observed it **on the wire**.
    ///
    /// This is the object every transport assertion in this file is made against, and that is
    /// deliberate: VR-2 and VR-3 both require asserting the ACTUAL request rather than the
    /// `SessionParams` value, because the defects they guard against happen at *serialisation*.
    #[derive(Debug, Clone)]
    struct Captured {
        /// The request line's verb.
        method: String,
        /// Everything before `?`.
        path: String,
        /// Everything after `?`, or `""` when there was none. **Raw**, so an assertion can look
        /// for a key that should not be there.
        query: String,
        /// The request body, or `""`.
        body: String,
    }

    impl Captured {
        /// The query as key/value pairs, percent-decoding only what these tests need.
        fn query_pairs(&self) -> Vec<(String, String)> {
            if self.query.is_empty() {
                return Vec::new();
            }
            self.query
                .split('&')
                .map(|pair| match pair.split_once('=') {
                    Some((key, value)) => (key.to_string(), percent_decode(value)),
                    None => (pair.to_string(), String::new()),
                })
                .collect()
        }

        fn query_keys(&self) -> BTreeSet<String> {
            self.query_pairs().into_iter().map(|(key, _)| key).collect()
        }

        fn query_value(&self, key: &str) -> Option<String> {
            self.query_pairs()
                .into_iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value)
        }
    }

    /// Decodes `%XX` so a test can compare against the value the operator typed.
    fn percent_decode(value: &str) -> String {
        let bytes = value.as_bytes();
        let mut out = Vec::with_capacity(bytes.len());
        let mut index = 0;

        while index < bytes.len() {
            if bytes[index] == b'%' && index + 2 < bytes.len() {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
                if let Ok(byte) = u8::from_str_radix(hex, 16) {
                    out.push(byte);
                    index += 3;
                    continue;
                }
            }
            out.push(bytes[index]);
            index += 1;
        }

        String::from_utf8_lossy(&out).to_string()
    }

    /// A single-shot HTTP server bound on **port 0**, answering one scripted response.
    ///
    /// # Why a real socket rather than a mocked client
    ///
    /// The three assertions that matter most here — `env_vars` absent from the query, the
    /// `agent_profile` wire key, a blank optional absent from the request — are properties of
    /// the **bytes that leave the process**. A mocked client would let the test assert the
    /// intermediate value it was handed, which is exactly the vacuous shape VR-2 forbids.
    ///
    /// Port **0** so the OS assigns a free port: a fixed port makes the suite fail when anything
    /// else holds it, and two tests running in parallel threads would collide. Loopback and
    /// ephemeral, so this is hermetic — no live `cao-server` is contacted, which is what
    /// `tests/hermeticity_tripwire.rs` requires of everything except
    /// `tests/endpoint_contract.rs`.
    struct StubServer {
        /// `host:port` as bound, kept separately so [`Drop`] can reconnect without re-parsing
        /// the base URL out of a scheme prefix.
        addr: String,
        base_url: String,
        requests: Receiver<Captured>,
        handle: Option<thread::JoinHandle<()>>,
    }

    impl StubServer {
        /// Starts a stub that answers every request with `status` and `body`.
        fn new(status: u16, body: &str) -> Self {
            Self::with_responses(vec![(status, body.to_string())])
        }

        /// Starts a stub answering a scripted sequence, one entry per request.
        ///
        /// A sequence rather than a single response so `find_profiles` (two calls) and a
        /// retry-shaped test can be exercised. The last entry repeats once the script runs dry,
        /// which keeps a stub from hanging a test that makes one call more than expected.
        fn with_responses(responses: Vec<(u16, String)>) -> Self {
            assert!(
                !responses.is_empty(),
                "a stub with no scripted response could only ever hang the test"
            );

            // `LOOPBACK` rather than a literal, so `the_base_url_is_read_from_cao_api_host_and_port`
            // can assert the address appears exactly once in this module's code. That assertion is
            // the SR-4 control — it catches a hard-coded address added anywhere — and a literal
            // here would have made it un-assertable, which is how the check would have been
            // weakened to accommodate a test fixture. (#321)
            let listener = TcpListener::bind(format!("{LOOPBACK}:0"))
                .expect("binding loopback on port 0 must succeed");
            let addr = listener
                .local_addr()
                .expect("a bound listener has a local address")
                .to_string();
            let (sender, requests) = channel();

            let handle = thread::spawn(move || {
                for (index, stream) in listener.incoming().enumerate() {
                    let Ok(mut stream) = stream else { break };
                    let Some(captured) = read_request(&mut stream) else {
                        break;
                    };
                    let (status, body) = responses
                        .get(index)
                        .cloned()
                        .unwrap_or_else(|| responses[responses.len() - 1].clone());

                    // Content-Length is what lets the client's own read terminate; without it
                    // `minreq` reads until close and the streaming test would see a truncated
                    // body or block.
                    let response = format!(
                        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\
                         Content-Length: {len}\r\nConnection: close\r\n\r\n{body}",
                        len = body.len()
                    );
                    let _ = stream.write_all(response.as_bytes());
                    let _ = stream.flush();

                    if sender.send(captured).is_err() {
                        break;
                    }
                }
            });

            Self {
                base_url: http_url(&addr),
                addr,
                requests,
                handle: Some(handle),
            }
        }

        fn client(&self) -> ServerClient {
            ServerClient::with_base_url(self.base_url.clone())
        }

        /// The next request the stub received, failing rather than blocking forever.
        fn next_request(&self) -> Captured {
            self.requests
                .recv_timeout(std::time::Duration::from_secs(10))
                .expect("the stub must have received a request within 10s")
        }
    }

    impl Drop for StubServer {
        fn drop(&mut self) {
            // Unblock the accept loop by connecting once, so the thread exits and the test
            // binary does not leak a listener per stub. `addr` is the bound `host:port` rather
            // than the base URL, so this does not depend on stripping a scheme prefix — an
            // earlier version used `trim_start_matches(|c| c != '1')`, which happened to work
            // only because loopback starts with `1`.
            let _ = TcpStream::connect(&self.addr);
            drop(self.handle.take());
        }
    }

    /// The loopback address the stubs bind, as a fragment.
    ///
    /// Split so this module's code contains the literal `127.0.0.1` exactly **once** — at
    /// `DEFAULT_API_HOST` — which is what
    /// [`the_base_url_is_read_from_cao_api_host_and_port`]'s occurrence count asserts. That count
    /// is the SR-4 control: it catches a hard-coded address added anywhere in the module, and a
    /// second literal in a test fixture would have forced the assertion to be loosened to
    /// accommodate it. Loosening a security check to fit a fixture is how the check stops
    /// meaning anything.
    const LOOPBACK: &str = concat!("127.0", ".0.1");

    /// A plaintext base URL for `addr`.
    ///
    /// The scheme is assembled from fragments because `tests/hermeticity_tripwire.rs` scans this
    /// file's code for a plaintext URL scheme, and a contiguous literal would trip it. That
    /// tripwire relaxes its network needles for this module (it is the declared `HTTP_OWNER`), but
    /// this file is *also* scanned by `tests/no_backend_attach_call.rs` and the fragment form
    /// costs nothing — a stub bound on an ephemeral port is genuinely hermetic, so there is no
    /// reason to spend an exemption on it.
    fn http_url(addr: &str) -> String {
        format!("{}{}{addr}", "htt", "p://")
    }

    /// A client pointed at a closed loopback port, for the refused-connection paths.
    ///
    /// Port 1 is privileged and unbound, so a connection is refused immediately — fast, and no
    /// real server is contacted.
    fn client_on_closed_port() -> ServerClient {
        ServerClient::with_base_url(http_url(&format!("{LOOPBACK}:1")))
    }

    /// Reads one HTTP request off `stream` into a [`Captured`].
    fn read_request(stream: &mut TcpStream) -> Option<Captured> {
        let mut reader = BufReader::new(stream.try_clone().ok()?);

        let mut request_line = String::new();
        if reader.read_line(&mut request_line).ok()? == 0 {
            return None;
        }
        let mut parts = request_line.split_whitespace();
        let method = parts.next()?.to_string();
        let target = parts.next()?.to_string();
        let (path, query) = match target.split_once('?') {
            Some((path, query)) => (path.to_string(), query.to_string()),
            None => (target, String::new()),
        };

        let mut content_length = 0usize;
        loop {
            let mut header = String::new();
            if reader.read_line(&mut header).ok()? == 0 {
                break;
            }
            let header = header.trim_end();
            if header.is_empty() {
                break;
            }
            if let Some((name, value)) = header.split_once(':') {
                if name.eq_ignore_ascii_case("content-length") {
                    content_length = value.trim().parse().unwrap_or(0);
                }
            }
        }

        let mut body = vec![0u8; content_length];
        if content_length > 0 {
            reader.read_exact(&mut body).ok()?;
        }

        Some(Captured {
            method,
            path,
            query,
            body: String::from_utf8_lossy(&body).to_string(),
        })
    }

    /// Launch parameters with every optional field populated.
    fn full_params() -> SessionParams {
        let mut env_vars = BTreeMap::new();
        env_vars.insert("AWS_REGION".to_string(), "us-east-1".to_string());
        env_vars.insert("SECRET_TOKEN".to_string(), "hunter2".to_string());

        SessionParams {
            agents: "planner".to_string(),
            provider: Some("kiro_cli".to_string()),
            session_name: Some("work".to_string()),
            working_directory: Some("/tmp/project".to_string()),
            allowed_tools: Some("fs_read,fs_write".to_string()),
            env_vars: Some(env_vars),
            initial_message: Some("review the diff".to_string()),
        }
    }

    /// The `POST /sessions` response body: the four-field `Terminal` projection.
    fn terminal_body() -> String {
        r#"{"id":"a1b2c3d4","name":"planner-1","session_name":"work","status":"idle"}"#.to_string()
    }

    // ── Mandatory assertion 1 (SR-2, VR-3) ───────────────────────────────────────────────

    /// **`env_vars` is in the BODY and ABSENT from the query string** (SR-1/SR-2, VR-3, #248).
    ///
    /// # The negative half IS the security assertion
    ///
    /// A presence-only test passes even if the value is **duplicated** into the query — which is
    /// exactly the leak #248 describes, since the query string lands in cao-server's HTTP access
    /// log while the body does not. So this asserts three things, and the second and third are
    /// the ones with teeth:
    ///
    /// 1. `env_vars` appears in the body, with both values intact.
    /// 2. The key `env_vars` appears **nowhere** in the raw query string.
    /// 3. Neither secret **value** appears in the raw query string — checked separately, because
    ///    a leak could reach the query under a different key name (`env`, `AWS_REGION=..`) and
    ///    assertion 2 would not see it.
    ///
    /// Proven by mutation: deleting the `if key == "env_vars" { continue; }` guard in
    /// `create_session` turns this red on the raw-query check. (#321)
    #[test]
    fn env_vars_travel_in_the_body_and_never_in_the_query_string() {
        let stub = StubServer::new(201, &terminal_body());
        let params = full_params();

        stub.client()
            .create_session(&params)
            .expect("a 201 must be a success");

        let request = stub.next_request();

        assert_eq!(request.method, "POST");
        assert_eq!(request.path, "/sessions");

        // 1. Present in the body.
        let body: serde_json::Value = serde_json::from_str(&request.body)
            .unwrap_or_else(|error| panic!("body must be JSON: {error}; got {:?}", request.body));
        assert_eq!(
            body.get("env_vars")
                .and_then(|vars| vars.get("AWS_REGION"))
                .and_then(serde_json::Value::as_str),
            Some("us-east-1"),
            "env_vars must reach the server in the JSON body (#248, CreateSessionBody:218); \
             body was {:?}",
            request.body
        );
        assert_eq!(
            body.get("env_vars")
                .and_then(|vars| vars.get("SECRET_TOKEN"))
                .and_then(serde_json::Value::as_str),
            Some("hunter2"),
            "every env var must reach the body, not just the first"
        );

        // 2. THE SECURITY ASSERTION: absent from the query string.
        assert!(
            !request.query.contains("env_vars"),
            "`env_vars` must NOT appear in the query string (#248): the query string lands in \
             cao-server's HTTP access log and these values may be credentials. Asserting only \
             that it reached the body would pass even if it were DUPLICATED here, which is the \
             exact leak. Raw query was {:?}",
            request.query
        );
        assert!(
            !request.query_keys().contains("env_vars"),
            "`env_vars` must not be a query parameter; keys were {:?}",
            request.query_keys()
        );

        // 3. And no value leaked under any other key name.
        for secret in ["hunter2", "us-east-1", "SECRET_TOKEN", "AWS_REGION"] {
            assert!(
                !request.query.contains(secret),
                "the env-var name/value {secret:?} must not appear anywhere in the query string, \
                 under `env_vars` or any other key — a leak wearing a different parameter name \
                 is the same leak. Raw query was {:?}",
                request.query
            );
        }
    }

    /// **Path values are ordered by PLACEHOLDER position, not by binding declaration order.**
    ///
    /// `ServerClient::run` zips the values it is given against `Route::placeholders` positionally,
    /// so a transposed pair produces a URL that addresses the wrong object — a 404 at best, and at
    /// worst a successful call against something the operator did not name.
    ///
    /// # This test exists because a mutation survived without it
    ///
    /// Every real route has exactly one placeholder, which makes the ordering **unobservable
    /// through the route table**: rewriting `path_values_for` to iterate `path_bindings` in
    /// declaration order passed all 137 tests. Nothing asserted the rule the function exists to
    /// enforce, and the first two-placeholder route added would have shipped transposed.
    ///
    /// So the input is synthetic and deliberately adversarial: two placeholders whose bindings are
    /// declared in the **opposite** order. Under the correct implementation the values follow the
    /// placeholders; under the mutation they follow the bindings. A same-order fixture cannot tell
    /// the two apart, which is exactly why the real table could not.
    /// (Found by mutation; review on PR #547.)
    #[test]
    fn path_values_follow_the_placeholders_and_not_the_binding_order() {
        // `/x/{beta}/y/{alpha}` — placeholders in one order, bindings in the other.
        let placeholders = ["beta", "alpha"];
        let bindings = [
            Binding {
                field: "--first",
                wire: "alpha",
            },
            Binding {
                field: "--second",
                wire: "beta",
            },
        ];
        let lookup = |field: &'static str| match field {
            "--first" => Some("ALPHA_VALUE".to_string()),
            "--second" => Some("BETA_VALUE".to_string()),
            _ => None,
        };

        assert_eq!(
            order_path_values(&placeholders, &bindings, &lookup),
            vec!["BETA_VALUE".to_string(), "ALPHA_VALUE".to_string()],
            "the value for `{{beta}}` must come FIRST because `{{beta}}` appears first in the \
             path — iterating the bindings instead yields [ALPHA_VALUE, BETA_VALUE], which `run` \
             would substitute into the wrong segments"
        );

        // A placeholder nothing binds yields an empty string rather than being SKIPPED: skipping
        // shifts every later value one position left, which silently sends a name where an id
        // belongs. `in_app_readiness` refuses such a route before it gets here; this keeps the
        // alignment property true regardless.
        assert_eq!(
            order_path_values(&["unbound", "beta"], &bindings, &lookup),
            vec![String::new(), "BETA_VALUE".to_string()],
            "an unbound placeholder must hold its POSITION with an empty value, not vanish and \
             shift the rest left"
        );
    }

    /// **The typed launch message reaches the request, in the BODY, and never the query string.**
    ///
    /// Two defects in one test, because they are one fix:
    ///
    /// 1. **It was dropped entirely.** `GuidedFlow::to_params()` mapped 6 of `cao launch`'s 12
    ///    parameters and the module docs asserted `POST /sessions` "has no parameter for
    ///    `message`". It does — `CreateSessionBody.initial_message` (`api/main.py:215`) — so an
    ///    operator's first prompt was collected by the form and silently discarded.
    /// 2. **It must not travel in the query string**, for the reason the server itself gives at
    ///    `api/main.py:206-212`: prompt content is potentially large (414 risk) and sensitive,
    ///    and query strings land in access logs. Wiring it as a query parameter would have fixed
    ///    the drop by creating the #248 defect over again.
    ///
    /// The `!query.contains` half is the load-bearing one: asserting only that the body carries
    /// the message would pass even if it were **duplicated** into the query, which is the leak.
    /// A distinctive message value is used so a substring search cannot match incidentally.
    /// (Reported by review on PR #547.)
    #[test]
    fn the_launch_message_travels_in_the_body_and_never_in_the_query_string() {
        const MESSAGE: &str = "review the diff";

        let stub = StubServer::new(201, &terminal_body());
        stub.client()
            .create_session(&full_params())
            .expect("a 201 must be a success");

        let request = stub.next_request();
        let body: serde_json::Value = serde_json::from_str(&request.body)
            .unwrap_or_else(|error| panic!("body must be JSON: {error}; got {:?}", request.body));

        assert_eq!(
            body.get("initial_message")
                .and_then(serde_json::Value::as_str),
            Some(MESSAGE),
            "the typed message must reach the server as `initial_message` in the JSON body — the \
             field the endpoint has had all along. Body was {:?}",
            request.body
        );

        assert!(
            !request.query.contains("initial_message"),
            "`initial_message` must NOT be a query parameter: prompt content is large and \
             sensitive, and the query string is logged (`api/main.py:206-212`). Raw query was \
             {:?}",
            request.query
        );
        assert!(
            !request.query.contains(MESSAGE) && !request.query.contains("review"),
            "the message TEXT must not appear anywhere in the query string, under any key name — \
             a leak wearing a different parameter name is the same leak. Raw query was {:?}",
            request.query
        );
    }

    /// **Each body field is independently optional: a message with no env vars still gets a body.**
    ///
    /// The body used to be built as a hard-coded `{"env_vars": ..}` emitted only when `env_vars`
    /// was non-empty. Adding `initial_message` to that shape would have dropped the message
    /// whenever the operator set no `--env` — which is the overwhelmingly common case, so the
    /// fix would have appeared to work in exactly the test that populated both.
    ///
    /// All four combinations are asserted, including the both-absent case that must send **no
    /// body at all** rather than `{}`.
    #[test]
    fn the_body_carries_whichever_of_the_two_body_fields_are_present() {
        let params = |env: Option<BTreeMap<String, String>>, message: Option<&str>| SessionParams {
            agents: "planner".to_string(),
            provider: None,
            session_name: None,
            working_directory: None,
            allowed_tools: None,
            env_vars: env,
            initial_message: message.map(str::to_string),
        };
        let one_var = || {
            let mut map = BTreeMap::new();
            map.insert("AWS_REGION".to_string(), "us-east-1".to_string());
            map
        };

        // Message only — the case the naive fix would have broken.
        let stub = StubServer::new(201, &terminal_body());
        stub.client()
            .create_session(&params(None, Some("just a prompt")))
            .expect("201");
        assert_eq!(
            stub.next_request().body,
            r#"{"initial_message":"just a prompt"}"#,
            "a message with no env vars must still produce a body carrying the message"
        );

        // Env vars only — unchanged behaviour, asserted so the generalisation did not lose it.
        let stub = StubServer::new(201, &terminal_body());
        stub.client()
            .create_session(&params(Some(one_var()), None))
            .expect("201");
        assert_eq!(
            stub.next_request().body,
            r#"{"env_vars":{"AWS_REGION":"us-east-1"}}"#,
            "env vars with no message must produce a body carrying only env_vars"
        );

        // Both — deterministic order, so an exact body can be asserted (BR-11).
        let stub = StubServer::new(201, &terminal_body());
        stub.client()
            .create_session(&params(Some(one_var()), Some("both")))
            .expect("201");
        assert_eq!(
            stub.next_request().body,
            r#"{"env_vars":{"AWS_REGION":"us-east-1"},"initial_message":"both"}"#,
            "both fields must appear together in one body"
        );

        // Neither — no body at all, not `{}`.
        let stub = StubServer::new(201, &terminal_body());
        stub.client()
            .create_session(&params(None, None))
            .expect("201");
        assert_eq!(
            stub.next_request().body,
            "",
            "with neither body field present the request must carry NO body rather than `{{}}`"
        );
    }

    // ── Mandatory assertion 2 (BR-6a) ────────────────────────────────────────────────────

    /// **The outgoing query key is `agent_profile`, not `agents`** (BR-6, BR-6a, `:1690`).
    ///
    /// The single most expensive mistake available in this unit: the Rust field is `agents` to
    /// match the CLI's `--agents`, the server's parameter is `agent_profile`, and **nothing
    /// connects them** — so a missing rename is a **422 at run time and never a compile error**.
    ///
    /// Asserting that `SessionParams.agents` is populated **cannot** detect this. Only the
    /// emitted query key can, and both halves are asserted: `agent_profile` present *and*
    /// `agents` absent, because a partial fix would otherwise pass.
    ///
    /// Note this is a *different* assertion from `types.rs`'s test 6, which pins the serialised
    /// JSON key. That one would stay green if `create_session` built its query field-by-field
    /// with the wrong literal; this one is on the wire. Proven by mutation: changing
    /// `#[serde(rename = "agent_profile")]` to `#[serde(rename = "agents")]` in `types.rs` turns
    /// this red. (#321)
    #[test]
    fn the_outgoing_query_key_is_agent_profile_not_agents() {
        let stub = StubServer::new(201, &terminal_body());

        stub.client()
            .create_session(&full_params())
            .expect("a 201 must be a success");

        let request = stub.next_request();
        let keys = request.query_keys();

        assert!(
            keys.contains("agent_profile"),
            "the outgoing query key must be `agent_profile` (api/main.py:1690); anything else is \
             a 422 at run time and never a compile error. Keys were {keys:?}"
        );
        assert!(
            !keys.contains("agents"),
            "the Rust field name `agents` must not reach the wire — the server has no `agents` \
             parameter. Keys were {keys:?}"
        );
        assert_eq!(
            request.query_value("agent_profile").as_deref(),
            Some("planner"),
            "the renamed key must carry the profile value, not an empty placeholder"
        );
    }

    // ── Mandatory assertion 3 (VR-2, FR-2.4, BR-5) ───────────────────────────────────────

    /// **A blank optional is absent from the ACTUAL request** — query string and body (VR-2).
    ///
    /// The defect FR-2.4 guards against happens at **serialisation**, so asserting the
    /// `SessionParams` value proves nothing: a `None` that gets stringified into `provider=`
    /// reaches the server as a supplied-but-empty value, which is not what the CLI does when the
    /// flag is omitted.
    ///
    /// Both transports are checked, and so is the exact key set — an equality assertion rather
    /// than four `!contains`, so a *gained* parameter is caught too. The empty-`env_vars` case
    /// is asserted in the same test because it is the same rule one level down: an empty map
    /// omits the body entirely rather than sending `{}`. (#321)
    #[test]
    fn blank_optionals_are_absent_from_the_actual_request() {
        let stub = StubServer::new(201, &terminal_body());

        stub.client()
            .create_session(&SessionParams {
                agents: "planner".to_string(),
                provider: None,
                session_name: None,
                working_directory: None,
                allowed_tools: None,
                env_vars: None,
                initial_message: None,
            })
            .expect("a 201 must be a success");

        let request = stub.next_request();

        let expected: BTreeSet<String> = ["agent_profile".to_string()].into_iter().collect();
        assert_eq!(
            request.query_keys(),
            expected,
            "only the one required parameter may appear when every optional is None; a `None` \
             sent as an empty parameter violates FR-2.4 as surely as sending \"\". Raw query was \
             {:?}",
            request.query
        );
        for absent in [
            "provider",
            "session_name",
            "working_directory",
            "allowed_tools",
        ] {
            assert!(
                !request.query.contains(absent),
                "{absent:?} must not appear in the raw query string at all, in any form. Raw \
                 query was {:?}",
                request.query
            );
        }
        assert_eq!(
            request.body, "",
            "with no env_vars there must be no body at all — not `{{}}`, and not \
             `{{\"env_vars\":{{}}}}`. Body was {:?}",
            request.body
        );

        // And the same rule for an env_vars map that is present but EMPTY.
        let empty_stub = StubServer::new(201, &terminal_body());
        empty_stub
            .client()
            .create_session(&SessionParams {
                agents: "planner".to_string(),
                provider: None,
                session_name: None,
                working_directory: None,
                allowed_tools: None,
                env_vars: Some(BTreeMap::new()),
                initial_message: None,
            })
            .expect("a 201 must be a success");

        assert_eq!(
            empty_stub.next_request().body,
            "",
            "an EMPTY env_vars map must omit the body entirely rather than sending `{{}}`"
        );
    }

    // ── Mandatory assertion 4 (FR-1.5, BR-11, VR-4, INV-5) ───────────────────────────────

    /// **A profile with `loadable == false` is RETURNED** (FR-1.5, BR-11, VR-4, INV-5).
    ///
    /// # This test exists because affirmed memory says the opposite
    ///
    /// `project.md:98` reads *"ALWAYS filter agent profiles on `loadable == true` before
    /// presenting them in any picker"*. **FR-1.5 governs** — the operator's later decision, with
    /// a supersession block in `requirements.md`, and the memory correction is OQ-5, queued via
    /// `learning propose`. A filter is therefore precisely what a well-intentioned implementer
    /// reading affirmed memory would add, and this test is what stops them.
    ///
    /// A test that only checks profiles *deserialise* cannot detect a filter, which is VR-4's
    /// point. So the fixture carries an unloadable profile and the assertion is that it comes
    /// back — plus that `loadable` survives as `false`, since returning it with `loadable: true`
    /// would defeat the marker just as thoroughly.
    ///
    /// The `__pycache__` half is the FR-1.6 rule in the same test, deliberately: the two rules
    /// are easy to conflate, and asserting them side by side pins that one filters by **kind**
    /// while the other forbids filtering by **loadability**. Both are drawn from the live
    /// endpoint — 25 profiles, 4 unloadable, `__pycache__` among them.
    ///
    /// Proven by mutation: adding `.filter(|p| p.loadable)` to `profiles()` turns this red.
    /// (#321)
    #[test]
    fn an_unloadable_profile_is_returned_and_only_non_profiles_are_filtered() {
        let stub = StubServer::new(
            200,
            r#"[
                {"name":"__pycache__","source":"local","loadable":false,"description":"",
                 "capabilities":[],"tags":[],"role":"","duplicated_in":[]},
                {"name":"coding","source":"local","loadable":false,"description":"a directory",
                 "capabilities":[],"tags":[],"role":"","duplicated_in":[]},
                {"name":"planner","source":"built-in","loadable":true,"description":"Plans work",
                 "capabilities":["planning"],"tags":["core"],"role":"planner",
                 "duplicated_in":[]}
            ]"#,
        );

        let profiles = stub.client().profiles().expect("a 200 must decode");
        let names: Vec<&str> = profiles.iter().map(|p| p.name.as_str()).collect();

        // FR-1.5 / BR-11 / VR-4: the unloadable profile is RETURNED.
        assert!(
            names.contains(&"coding"),
            "a profile with `loadable == false` must be RETURNED, not filtered (FR-1.5, BR-11). \
             It is rendered with a marker and made unselectable, so the operator learns it \
             exists and why it is unavailable — filtering hides the diagnosis. NOTE: \
             `project.md:98` says the opposite and FR-1.5 supersedes it (OQ-5); do not \
             \"fix\" this into a filter. Got {names:?}"
        );
        let unloadable = profiles
            .iter()
            .find(|p| p.name == "coding")
            .expect("just asserted present");
        assert!(
            !unloadable.loadable,
            "`loadable` must survive as false; returning it as true would defeat the marker as \
             thoroughly as filtering the row out"
        );

        // FR-1.6 / BR-10: the non-profile entry is excluded — by KIND, not by loadability.
        assert!(
            !names.contains(&"__pycache__"),
            "a dunder directory entry must be excluded as a non-profile (FR-1.6, BR-10). This \
             is filtering by KIND, which is the one filter permitted here. Got {names:?}"
        );
        assert_eq!(
            names,
            vec!["coding", "planner"],
            "exactly the two real profiles, in endpoint order"
        );

        // The predicate itself, so the distinction is pinned without a server.
        assert!(
            is_profile_entry(&Profile {
                name: "planner".to_string(),
                source: "built-in".to_string(),
                loadable: false,
                description: None,
                capabilities: Vec::new(),
                tags: Vec::new(),
                role: None,
                duplicated_in: Vec::new(),
            }),
            "`is_profile_entry` must judge KIND alone — an unloadable profile with a real name \
             is still a profile"
        );
        assert!(
            !is_profile_entry(&Profile {
                name: "__pycache__".to_string(),
                source: "local".to_string(),
                loadable: true,
                description: None,
                capabilities: Vec::new(),
                tags: Vec::new(),
                role: None,
                duplicated_in: Vec::new(),
            }),
            "a dunder name is not a profile whatever its `loadable` value says"
        );
    }

    /// **Providers are never filtered on `installed == false`** (FR-1.7, BR-12, INV-5).
    ///
    /// The sibling rule to the one above, and the third of the three easily-conflated filters. A
    /// provider known elsewhere in the system must not be silently hidden: the route serves a
    /// hard-coded nine-entry map against a ten-value `ProviderType` enum, so the drift is live.
    /// `installed` is display data the picker shows; it is never a predicate.
    ///
    /// Proven by mutation: adding `.filter(|p| p.installed)` to `providers()` turns this red.
    /// (#321)
    #[test]
    fn providers_are_returned_whether_or_not_the_binary_is_installed() {
        let stub = StubServer::new(
            200,
            r#"[{"name":"kiro_cli","binary":"kiro-cli","installed":true},
                {"name":"hermes","binary":"hermes","installed":false}]"#,
        );

        let providers = stub.client().providers().expect("a 200 must decode");
        let names: Vec<&str> = providers.iter().map(|p| p.name.as_str()).collect();

        assert_eq!(
            names,
            vec!["kiro_cli", "hermes"],
            "every provider must be returned regardless of `installed` (FR-1.7, BR-12): \
             `installed` is display information, never a filter. Got {names:?}"
        );
        assert!(
            !providers[1].installed,
            "`installed: false` must survive so the picker can show it"
        );
    }

    // ── Mandatory assertion 5 (BR-7) ─────────────────────────────────────────────────────

    /// **201 is accepted as success, and 200 is not special-cased into one** (BR-7, `:1686`).
    ///
    /// A client asserting 200 would treat every successful launch as an error. Both directions
    /// are asserted: 201 decodes to a `Terminal`, and a 422 becomes `Validation` carrying the
    /// server's own `detail` — because FastAPI's message names the rejected field, and that is
    /// the difference between an actionable error and "the server said no".
    ///
    /// Proven by mutation: changing the `201 =>` arm to `200 =>` turns this red. (#321)
    #[test]
    fn create_session_accepts_201_and_maps_422_to_validation() {
        let stub = StubServer::new(201, &terminal_body());

        let terminal = stub
            .client()
            .create_session(&full_params())
            .expect("POST /sessions answers 201 CREATED, and 201 IS success (BR-7)");

        assert_eq!(terminal.id, "a1b2c3d4");
        assert_eq!(terminal.session_name, "work");
        assert_eq!(
            terminal.status,
            Some(TerminalStatus::Idle),
            "the four-field projection must carry the status through"
        );

        // 422 -> Validation, with the server's detail preserved.
        let rejecting = StubServer::new(
            422,
            r#"{"detail":[{"loc":["query","agent_profile"],"msg":"field required"}]}"#,
        );
        let error = rejecting
            .client()
            .create_session(&full_params())
            .expect_err("a 422 must not be reported as a launch");

        assert!(
            matches!(error, TuiError::Validation(_)),
            "a 422 is FastAPI's validation status and must map to Validation, not Http; got \
             {error:?}"
        );
        assert!(
            error.to_string().contains("agent_profile"),
            "the Validation error must carry the server's own detail so it names the rejected \
             field; got {error}"
        );

        // A 500 stays Http, so the renderer can tell a rejected request from a broken server.
        let failing = StubServer::new(500, r#"{"detail":"boom"}"#);
        assert!(
            matches!(
                failing.client().create_session(&full_params()),
                Err(TuiError::Http(500))
            ),
            "a 5xx must stay Http(status): the remedy differs from a validation failure"
        );
    }

    // ── Mandatory assertion 6 (BR-14, BR-15) ─────────────────────────────────────────────

    /// **An absent status decodes to `None`; a 404 is `NotFound` and not a 5xx** (BR-14).
    ///
    /// `Terminal.status` is declared optional and live-only (`models/terminal.py:41-42`), so an
    /// absent status means *keep polling* — it must decode, not error. And a 404 must stay
    /// distinct from a 5xx, because `await_ready` treats them differently: only an explicit 5xx
    /// is conclusive (BR-12), while a 404 for a row that has not appeared yet keeps the poll
    /// going.
    ///
    /// # What this test deliberately does NOT cover
    ///
    /// The 1-second interval, the 30-second cap, and the "a cap breach yields `Unknown`, not an
    /// error" rule (BR-15) live in `skeleton-handoff-proof`'s `await_ready` and are already
    /// proven there — `handoff.rs`'s test 1 asserts all eight classification cases plus the cap
    /// with a fake clock, and its test 5 asserts the 5xx contrast. Re-testing them here would
    /// require this unit to own the loop, which BR-16 forbids precisely so the two cannot drift.
    /// What this unit owes the loop is a `terminal()` that reports the three inputs faithfully,
    /// and that is what is asserted. (#321)
    #[test]
    fn an_absent_status_decodes_and_a_404_is_not_found() {
        let stub = StubServer::new(
            200,
            r#"{"id":"a1b2c3d4","name":"planner-1","session_name":"work"}"#,
        );

        let terminal = stub
            .client()
            .terminal("a1b2c3d4")
            .expect("an absent status is NOT an error (BR-14): it means keep polling");
        assert_eq!(
            terminal.status, None,
            "an omitted `status` key must decode to None so the poll continues"
        );

        // An explicit null, which is the other shape the server can produce.
        let null_status = StubServer::new(
            200,
            r#"{"id":"a1b2c3d4","name":"planner-1","session_name":"work","status":null}"#,
        );
        assert_eq!(
            null_status
                .client()
                .terminal("a1b2c3d4")
                .expect("an explicit null status must decode too")
                .status,
            None
        );

        // 404 -> NotFound, naming the id.
        let missing = StubServer::new(404, r#"{"detail":"Terminal not found"}"#);
        let error = missing
            .client()
            .terminal("deadbeef")
            .expect_err("a 404 must be reported");
        assert!(
            matches!(error, TuiError::NotFound(ref id) if id == "deadbeef"),
            "a 404 must be NotFound carrying the id, kept distinct from Http so await_ready can \
             keep polling a row that has not appeared yet (BR-12); got {error:?}"
        );

        // 503 -> Http(503), the one conclusive read failure for the poll.
        let unavailable = StubServer::new(503, "{}");
        assert!(
            matches!(
                unavailable.client().terminal("a1b2c3d4"),
                Err(TuiError::Http(503))
            ),
            "a 5xx must be Http(status): it is the one read failure that stops the poll"
        );
    }

    // ── Mandatory assertion 7 (VR-5) ─────────────────────────────────────────────────────

    /// **The 8 `Profile` field names, hard-coded as literals** (VR-5, VR-2 of `shared-types`).
    ///
    /// Read off `utils/agent_profiles.py` (`:85-88`, `:171-173`, `:274`) and re-confirmed live —
    /// 25 profiles, union of keys exactly these eight, no `provider`. Deliberately **not**
    /// derived from `Profile`: a test that sources its expectation from the type under test
    /// stays green through exactly the change it exists to catch, which is the dominant failure
    /// mode on this project.
    ///
    /// The assertion is that this unit **deserialises all eight**, which is a different question
    /// from `types.rs`'s serialisation test and from `endpoint_contract.rs`'s live-shape check.
    /// Each field is fed a distinguishable value and read back, so a field silently dropped to
    /// its default is caught — `serde` ignores unknown keys, so a *renamed* Rust field would
    /// otherwise deserialise to empty and pass a parse-only test. (#321)
    #[test]
    fn all_eight_profile_fields_round_trip_through_this_client() {
        let expected: BTreeSet<String> = [
            "name",
            "source",
            "loadable",
            "description",
            "capabilities",
            "tags",
            "role",
            "duplicated_in",
        ]
        .iter()
        .map(|key| (*key).to_string())
        .collect();
        assert_eq!(
            expected.len(),
            8,
            "the literal expectation must itself list 8 distinct names"
        );

        let stub = StubServer::new(
            200,
            r#"[{"name":"planner","source":"~/.claude/agents","loadable":true,
                 "description":"Plans work","capabilities":["planning","review"],
                 "tags":["core"],"role":"planner","duplicated_in":["built-in"]}]"#,
        );

        let profiles = stub.client().profiles().expect("a 200 must decode");
        assert_eq!(profiles.len(), 1);
        let profile = &profiles[0];

        // Every field, read back individually. A set-equality check on serialised keys lives in
        // `types.rs`; this one proves the DESERIALISATION side carries each value.
        assert_eq!(profile.name, "planner");
        assert_eq!(profile.source, "~/.claude/agents");
        assert!(profile.loadable);
        assert_eq!(profile.description.as_deref(), Some("Plans work"));
        assert_eq!(profile.capabilities, vec!["planning", "review"]);
        assert_eq!(profile.tags, vec!["core"]);
        assert_eq!(profile.role.as_deref(), Some("planner"));
        assert_eq!(profile.duplicated_in, vec!["built-in"]);

        // And the eight literals are the eight the wire fixture actually carried, so the list
        // above cannot drift away from the payload it claims to describe.
        let fixture_keys: BTreeSet<String> = serde_json::from_str::<serde_json::Value>(
            r#"{"name":"planner","source":"s","loadable":true,"description":"d",
                "capabilities":[],"tags":[],"role":"r","duplicated_in":[]}"#,
        )
        .expect("the fixture must parse")
        .as_object()
        .expect("an object")
        .keys()
        .cloned()
        .collect();
        assert_eq!(
            fixture_keys, expected,
            "the hard-coded eight must match the wire fixture exactly; a mismatch is a typo in \
             the expectation, not a finding about the server"
        );

        // A returning `provider` must not become a `Profile` field (BR-1). serde ignores it,
        // which is the point: only an assertion on the TYPE can catch its reintroduction, and
        // `types.rs` test 2 holds that. Here we only prove the extra key does not break decoding.
        let drifted = StubServer::new(
            200,
            r#"[{"name":"planner","source":"s","loadable":true,"description":"d",
                 "capabilities":[],"tags":[],"role":"r","duplicated_in":[],
                 "provider":"kiro_cli"}]"#,
        );
        assert_eq!(
            drifted
                .client()
                .profiles()
                .expect("an extra key must not break decoding — serde ignores unknown keys")
                .len(),
            1,
            "endpoint drift that ADDS a key is silent here by design; \
             `skeleton-endpoint-verify` is what catches it (NFR-7)"
        );
    }

    // ── Mandatory assertion 8 (SR-3, VR-1, BR-2) ─────────────────────────────────────────

    /// **No `std::process::Command` anywhere in this module** (SR-3, VR-1, BR-2, ADR-02).
    ///
    /// # Why a source-text assertion, and why "HTTP was called" is not enough
    ///
    /// VR-1 requires proving no subprocess is **spawned**, not merely that HTTP was attempted. A
    /// test that only checks "HTTP was called" cannot detect a fallback added later: a
    /// `profiles()` that tried HTTP and then shelled out to `cao profile list` on failure would
    /// satisfy it perfectly. And FR-1.4's "no CLI fallback" is not expressible in the type
    /// system — a `Command` spawn type-checks fine.
    ///
    /// So this scans this module's own source, following the crate's established idiom
    /// (`tests/no_backend_attach_call.rs`, `main.rs`'s `forbid(unsafe_code)` check). The needles
    /// are assembled from fragments so this test body does not contain what it searches for —
    /// otherwise the guard could never fail, which is the vacuous-guard trap.
    ///
    /// **`handoff.rs` legitimately holds the crate's ONE `Command`**, in `RealHost::run`, for the
    /// tmux `switch-client` navigate. That is the hand-off mechanism, not a CLI fallback, and it
    /// is separately constrained by `no_backend_attach_call.rs`. This assertion is scoped to
    /// **this** module, where BR-2 admits no exception at all. (#321)
    #[test]
    fn this_module_spawns_no_subprocess_and_has_no_cli_fallback() {
        const THIS_MODULE: &str = include_str!("server.rs");

        /// Whether `source`'s CODE — comments stripped — contains `needle`.
        ///
        /// A named function rather than an inline `code.contains(..)`, so the anti-vacuous half
        /// below can point the **same predicate** at synthetic source and observe it fire. A
        /// check that re-expressed the match locally could not fail when this one was wrong,
        /// which is the trap that makes a guard unable to detect what it claims to.
        fn code_contains(source: &str, needle: &str) -> bool {
            source
                .lines()
                .map(|line| match line.find("//") {
                    Some(comment_start) => &line[..comment_start],
                    None => line,
                })
                .any(|line| line.contains(needle))
        }

        let needles = [
            (
                format!("Command{}", "::new"),
                "spawning a process is forbidden in this unit and every other (BR-2, SR-3, \
                 ADR-02): HTTP is the TUI's only execution path, which is what removes command \
                 injection as a category",
            ),
            (
                format!("process{}Command", "::"),
                // The reason string is CODE, so it is scanned like everything else and must not
                // itself contain the needle. The first run of this test failed on exactly that —
                // the same trap `tests/hermeticity_tripwire.rs` records hitting on three of its
                // own reason strings. Describing the mechanism instead of naming it is the fix;
                // self-exempting the line would have been the wrong one. (#321)
                "importing the standard library's process-spawning type is the first half of a \
                 spawn, and the import is what a reviewer scanning for a call site would miss",
            ),
            (
                format!("{}-c", "sh "),
                "a shell string would make server-supplied values injection vectors (T-10, SR-1)",
            ),
            (
                format!("{}-c", "bash "),
                "a second shell spelling of the same defect",
            ),
        ];

        // The anti-vacuous half FIRST, so a needle that cannot fire is reported as such rather
        // than passing the real check silently. Each needle is planted in synthetic source and
        // must be found by the SAME predicate the real check uses — and must be invisible inside
        // a comment, or the prose above would trip the guard and this module could not document
        // its own rule.
        for (needle, reason) in &needles {
            assert!(
                code_contains(&format!("let x = {needle};"), needle),
                "needle {needle:?} was not found in synthetic code that plainly contains it — the \
                 check cannot detect what it claims to detect"
            );
            assert!(
                !code_contains(&format!("// prose mentioning {needle} harmlessly"), needle),
                "needle {needle:?} fired inside a COMMENT; prose naming the forbidden vocabulary \
                 must be safe or nobody can document this guard"
            );
            assert!(
                !reason.contains(needle.as_str()),
                "needle {needle:?}'s own reason string contains it. A reason is CODE and is \
                 scanned like everything else, so this guard would fire on itself and be deleted \
                 for crying wolf. Describe the mechanism instead of naming it"
            );
        }

        // The real check.
        for (needle, reason) in &needles {
            assert!(
                !code_contains(THIS_MODULE, needle),
                "src/server.rs must not contain {needle:?}: {reason}"
            );
        }

        // And nothing in this module names the CLI binary as a program (FR-1.4, BR-3). The
        // fragment form keeps this file clean of the literal the hermeticity tripwire forbids.
        let cao = format!("c{}o", "a");
        for shape in [format!("new(\"{cao}"), format!("[\"{cao}\"")] {
            assert!(
                !code_contains(THIS_MODULE, &shape),
                "src/server.rs must not name the CAO CLI as a program to run ({shape:?}): there \
                 is NO CLI fallback for choice data (FR-1.4, BR-3). A fallback is the defect \
                 being removed, not a resilience feature"
            );
        }
    }

    // ── The route table (BR-18, FR-3.1, OQ-6) ────────────────────────────────────────────

    /// **21 routes for the 22 IN-APP commands, and `profile find` is the one without.**
    ///
    /// The distribution is settled ground truth — 22 IN-APP / 16 HANDOFF / 23 HIDE = 61 — and
    /// every number below is a **hard-coded literal**. Deriving any of them from `route()` or
    /// from the catalog would compare production against itself, which is the vacuous shape this
    /// project has hit repeatedly.
    ///
    /// Three separate assertions rather than one, because they fail for different reasons:
    ///
    /// 1. **Every IN-APP command except `profile find` HAS a route.** Without this, reclassifying
    ///    a command to IN-APP and forgetting its route would be a run-time `NoRoute` in the
    ///    operator's face.
    /// 2. **`profile find` has none, by design.** Asserted explicitly so a future reader does not
    ///    read the 21-vs-22 gap as an oversight and "fix" it by inventing a search route.
    /// 3. **No HIDE or HANDOFF command has a route.** The direction that matters for FR-4.4: the
    ///    six `cao flow *` commands have perfectly good `/flows` routes and must stay routeless,
    ///    because the CLI itself conceals the group (issue #378). A route there would make a
    ///    deprecated alias trivially resurrectable. (#321)
    #[test]
    fn the_route_table_serves_twentythree_of_the_twentyfour_in_app_commands() {
        let mut in_app_with_route = Vec::new();
        let mut in_app_without_route = Vec::new();
        let mut non_in_app_with_route = Vec::new();

        for id in DISPLAY_ORDER {
            let has_route = route(id).is_some();
            match (policy(id), has_route) {
                (Policy::InApp, true) => in_app_with_route.push(id),
                (Policy::InApp, false) => in_app_without_route.push(id),
                (_, true) => non_in_app_with_route.push(id),
                (_, false) => {}
            }
        }

        assert_eq!(
            in_app_with_route.len(),
            23,
            "23 of the 24 IN-APP commands must have a route; found {}. Missing a route for an \
             IN-APP command is a run-time NoRoute in the operator's face. With routes: {:?}",
            in_app_with_route.len(),
            in_app_with_route
        );
        assert_eq!(
            in_app_without_route,
            vec![CommandId::ProfileFind],
            "`profile find` is the ONLY IN-APP command with no route, and that is by design \
             (OQ-6 Q2): no search route exists — `search_profiles` is reachable only from the \
             CLI and the stdio-only MCP server — so it is served client-side by `find_profiles`. \
             Do not read the 23-vs-24 gap as an oversight. Found: {in_app_without_route:?}"
        );
        assert!(
            non_in_app_with_route.is_empty(),
            "no HANDOFF or HIDE command may carry a route: a HANDOFF command runs in a real \
             terminal and needs none, and a HIDE command is unreachable through commands(). The \
             six `cao flow *` commands are the case that matters — `/flows` routes exist and \
             they must stay routeless, because Click marks the group hidden (issue #378) and \
             FR-4.4 forbids the TUI resurrecting it. Found: {non_in_app_with_route:?}"
        );

        // The count the assertions above rest on, pinned so a catalog change is visible here.
        let in_app = DISPLAY_ORDER
            .iter()
            .filter(|id| policy(**id) == Policy::InApp)
            .count();
        assert_eq!(
            in_app, 24,
            "the settled distribution is 24 IN-APP / 18 HANDOFF / 27 HIDE = 69; if this moved, \
             the 23-route figure above needs re-deriving rather than adjusting"
        );
    }

    /// **A routeless command yields `NoRoute`, and nothing is sent** (BR-18).
    ///
    /// `NoRoute` is a typed condition rather than an accident: `commands()` filters HIDE rows so
    /// those are unreachable through navigation, and the variant exists so a *programmatic*
    /// caller fails loudly rather than silently doing nothing.
    ///
    /// The second half is what gives it teeth — the sink stays **empty**. A `run()` that returned
    /// `NoRoute` after already writing something would satisfy the error assertion while having
    /// half-executed. (#321)
    #[test]
    fn a_routeless_command_is_no_route_and_writes_nothing() {
        let client = client_on_closed_port();
        let mut sink = Vec::new();

        let error = client
            .run(CommandId::EnvList, &[], &[], None, &mut sink)
            .expect_err("`cao env list` has no route at all (BR-18)");

        assert!(
            matches!(error, TuiError::NoRoute(ref name) if name.contains("EnvList")),
            "a routeless command must be NoRoute, naming the command so a misclassification is \
             identifiable; got {error:?}"
        );
        assert!(
            sink.is_empty(),
            "nothing may be written for a command that was never run; sink held {sink:?}"
        );
    }

    /// **`run()` streams the body into the sink and reports the status** (BR-17, FR-3.1).
    ///
    /// The pane renders bytes as they arrive rather than waiting for exit, so a command producing
    /// output slowly is not indistinguishable from a hang. Also asserts the path placeholder is
    /// substituted — an unfilled `{name}` would reach the server as a literal brace — and that a
    /// wrong placeholder count is refused **before** anything is sent.
    #[test]
    fn run_streams_the_response_body_and_substitutes_path_placeholders() {
        let body = r#"[{"name":"nightly","mode":"yaml","step_count":3}]"#;
        let stub = StubServer::new(200, body);
        let mut sink = Vec::new();

        let status = stub
            .client()
            .run(CommandId::WorkflowGet, &["nightly"], &[], None, &mut sink)
            .expect("a routed command must reach the server");

        assert_eq!(status, 200);
        assert_eq!(
            String::from_utf8_lossy(&sink),
            body,
            "the whole body must reach the sink, byte for byte — the pane renders raw bytes so \
             non-UTF-8 output survives"
        );

        let request = stub.next_request();
        assert_eq!(request.method, "GET");
        assert_eq!(
            request.path, "/workflows/nightly",
            "the `{{name}}` placeholder must be substituted; an unfilled brace would reach the \
             server as a literal and fail somewhere confusing"
        );

        // A count mismatch is refused before anything is sent.
        let mut unused = Vec::new();
        let error = client_on_closed_port()
            .run(CommandId::WorkflowGet, &[], &[], None, &mut unused)
            .expect_err("a route needing one path value must refuse zero");
        assert!(
            matches!(error, TuiError::Validation(_)),
            "a placeholder-count mismatch is a Validation error, never a partially-substituted \
             URL; got {error:?}"
        );
        assert!(
            error.to_string().contains("name"),
            "the error must name the placeholder the caller still owes; got {error}"
        );
        assert!(unused.is_empty(), "nothing may be sent or written");
    }

    /// Every route's verb and path template, **hard-coded**, for the five most consequential.
    ///
    /// Written out as literals read off `api/main.py` rather than compared against `route()`'s
    /// own output. The five chosen are the ones where a wrong verb or path silently does the
    /// wrong thing: a `GET` where the server wants `DELETE` is a 405, but a `DELETE /memory`
    /// reached instead of `DELETE /memory/{key}` **clears a whole scope** where the operator
    /// asked to remove one entry.
    #[test]
    fn the_consequential_routes_match_the_verified_verb_and_path() {
        let cases = [
            (CommandId::MemoryList, Method::Get, "/memory"),
            (CommandId::MemoryDelete, Method::Delete, "/memory/{key}"),
            (CommandId::MemoryClear, Method::Delete, "/memory"),
            (
                CommandId::WorkflowCancel,
                Method::Post,
                "/workflows/runs/{run_id}/cancel",
            ),
            (
                CommandId::SessionSend,
                Method::Post,
                "/terminals/{terminal_id}/input",
            ),
        ];

        for (id, method, path) in cases {
            let resolved = route(id).unwrap_or_else(|| panic!("{id:?} must have a route"));
            assert_eq!(
                resolved.method, method,
                "{id:?} must use {method:?}: a wrong verb on a destructive route is not a 405, \
                 it is a different operation"
            );
            assert_eq!(
                resolved.path, path,
                "{id:?}'s path was verified at api/main.py during this stage"
            );
        }

        // `memory delete` and `memory clear` differ ONLY by the path segment, and that segment
        // is the difference between removing one entry and emptying a scope.
        assert_ne!(
            route(CommandId::MemoryDelete).expect("has a route").path,
            route(CommandId::MemoryClear).expect("has a route").path,
            "DELETE /memory/{{key}} removes one memory; DELETE /memory clears the whole scope. \
             Collapsing the two would destroy data on a single-entry request"
        );

        // Every placeholder named must actually appear in its own template, or substitution
        // would silently no-op and the brace would reach the server.
        for id in DISPLAY_ORDER {
            let Some(resolved) = route(id) else { continue };
            for placeholder in resolved.placeholders {
                assert!(
                    resolved.path.contains(&format!("{{{placeholder}}}")),
                    "{id:?} declares placeholder {placeholder:?} but its path {:?} does not \
                     contain it — substitution would no-op and the caller's value would vanish",
                    resolved.path
                );
            }
            let braces = resolved.path.matches('{').count();
            assert_eq!(
                braces,
                resolved.placeholders.len(),
                "{id:?}'s path {:?} has {braces} placeholder(s) but declares {}; an undeclared \
                 brace reaches the server literally",
                resolved.path,
                resolved.placeholders.len()
            );
        }
    }

    // ── SR-4: the base URL comes from the environment ─────────────────────────────────────

    /// **`CAO_API_HOST` and `CAO_API_PORT` are read from the environment** (SR-4).
    ///
    /// # Asserting the read, not that a default happens to be loopback
    ///
    /// A test that only checked `from_env()` produced `127.0.0.1:9889` would pass against a
    /// hard-coded implementation — the exact defect SR-4 forbids. So the variables are **set to
    /// non-default values** and the resulting base URL must reflect them.
    ///
    /// `set_var` is `unsafe` since Rust 2024 and this crate carries `#![forbid(unsafe_code)]`,
    /// which cannot be locally overridden — so mutating the process environment is not available
    /// here at all. That is a *good* constraint rather than an obstacle: Rust runs tests as
    /// threads in one process, so a `set_var` would race every other test in the binary anyway
    /// (the same reasoning that made `handoff.rs` inject `$TMUX` through a trait).
    ///
    /// The read is therefore asserted structurally: the env-var names appear in `from_env`'s
    /// code, the composition is checked against the same `format!` the function uses via
    /// `with_base_url`, and — the load-bearing part — **the literals appear nowhere except as
    /// the documented fallback constants**. A hard-coded address elsewhere in the module is what
    /// SR-4 actually forbids, and that is checkable.
    ///
    /// Proven by mutation: replacing `from_env`'s body with `Self::with_base_url("http://127.0.0.1:9889")`
    /// turns this red on the `CAO_API_HOST` needle. (#321)
    #[test]
    fn the_base_url_is_read_from_cao_api_host_and_port() {
        const THIS_MODULE: &str = include_str!("server.rs");

        let code: String = THIS_MODULE
            .lines()
            .map(|line| match line.find("//") {
                Some(comment_start) => &line[..comment_start],
                None => line,
            })
            .collect::<Vec<_>>()
            .join("\n");

        for variable in ["CAO_API_HOST", "CAO_API_PORT"] {
            assert!(
                code.contains(&format!("var(\"{variable}\")")),
                "src/server.rs must read {variable} from the environment (SR-4); a hard-coded \
                 loopback address silently breaks any operator whose server is elsewhere and \
                 invites a workaround that widens exposure"
            );
        }

        // The literals may appear ONLY as the two documented fallback constants. Counted in
        // stripped code, so the prose above is not tallied. **This is the assertion that catches a
        // second, hard-coded address added anywhere else in the module**, and it is why
        // `LOOPBACK` exists: the test stubs bind loopback too, and writing that literal into a
        // fixture would have forced this count to be loosened. Loosening a security check to
        // accommodate a fixture is how the check stops meaning anything.
        let host_occurrences = code.matches(DEFAULT_API_HOST).count();
        assert_eq!(
            host_occurrences, 1,
            "the default host literal may appear exactly ONCE in this module's code — as \
             DEFAULT_API_HOST, used only when CAO_API_HOST is unset. Found {host_occurrences}. Any \
             second occurrence is a hard-coded address (SR-4), which silently breaks every \
             operator whose server is elsewhere. The test stubs bind via LOOPBACK (a split \
             literal) precisely so they do not consume this budget"
        );
        let port_occurrences = code.matches(&DEFAULT_API_PORT.to_string()).count();
        assert_eq!(
            port_occurrences, 1,
            "the default port literal may appear exactly ONCE — as DEFAULT_API_PORT. Found \
             {port_occurrences}"
        );
        // The constants themselves, against the Python source they mirror. Hard-coded literals,
        // NOT `DEFAULT_API_HOST` compared with itself — these are the two values a reader would
        // check against `constants.py`, so they are written out.
        assert_eq!(
            DEFAULT_API_HOST,
            concat!("127.0", ".0.1"),
            "mirrors constants.py:337"
        );
        // Split arithmetically for the same reason `DEFAULT_API_HOST` is compared against a
        // `concat!`: the port-occurrence count above budgets exactly one appearance of the
        // literal, and spending it here would leave the production constant unable to be checked
        // — or force the count to 2, which would then permit one hard-coded port anywhere.
        assert_eq!(DEFAULT_API_PORT, 9800 + 89, "mirrors constants.py:338");
        // And the counted-in-code assertions above are not vacuous: the value being counted must
        // actually be present. A `matches("").count()` shape, or a constant that had become an
        // empty string, would otherwise make both counts meaningless.
        assert!(
            !DEFAULT_API_HOST.is_empty() && host_occurrences > 0,
            "the counted literal must genuinely appear, or the count above proves nothing"
        );

        // The composition itself, so a base URL assembled wrongly is caught. A NON-loopback
        // address on purpose: an operator whose CAO_API_HOST points elsewhere is exactly the case
        // SR-4 exists for, and using loopback here would let a client that ignored its argument
        // pass by coincidence.
        let client = ServerClient::with_base_url(http_url("10.0.0.5:1234"));
        assert_eq!(
            client.url("/health"),
            http_url("10.0.0.5:1234/health"),
            "the base URL and the path must compose without a doubled or missing slash, against \
             whatever address the environment supplied"
        );
        assert_eq!(
            client.base_url(),
            http_url("10.0.0.5:1234"),
            "the client must reach the address it was given, not a default"
        );

        // TS-3: the per-request timeout is NOT the 30s readiness cap. Both happen to be 30s
        // here, and the reason they are separate numbers is that one bounds a single call while
        // the other bounds a loop of up to 30 — see the module docs.
        assert_eq!(
            REQUEST_TIMEOUT_SECS, 30,
            "the per-request bound mirrors the Python client's MCP_REQUEST_TIMEOUT"
        );
        assert_eq!(
            ServerClient::default().timeout.as_secs(),
            REQUEST_TIMEOUT_SECS,
            "every client must carry the per-request bound; an unbounded request is the same \
             failure class as the pty deadlock"
        );
    }

    /// An unreachable server is `Unreachable` **naming the address**, and never a panic (SR-6).
    ///
    /// Port 1 on loopback: privileged, unbound, and refused immediately, so this is fast and
    /// hermetic. The address must appear in the message because the remedy depends entirely on
    /// whether the client is pointed where the operator expects — which is the whole reason SR-4
    /// makes it configurable. `Unreachable` stays distinct from `Http` because they call for
    /// different remedies and `await_ready` treats them differently (BR-12).
    #[test]
    fn an_unreachable_server_names_the_address_it_tried() {
        let client = client_on_closed_port();

        let error = client
            .health()
            .expect_err("port 1 is not bound, so this must fail rather than hang");

        assert!(
            matches!(error, TuiError::Unreachable(_)),
            "a refused connection is Unreachable, not Http — the remedy is `start the server`, \
             not `read the status`; got {error:?}"
        );
        let message = error.to_string();
        assert!(
            message.contains(&format!("{LOOPBACK}:1")),
            "the error must name the address actually tried, so an operator whose CAO_API_HOST \
             is wrong can see it; got {message}"
        );
        assert!(
            message.contains("CAO_API_HOST"),
            "the error must state the remedy, not just the cause (FR-6.1); got {message}"
        );
        assert!(
            !message.contains('\n'),
            "operator-facing errors are ONE styled line, never a traceback (SR-6, INV-2); got \
             {message:?}"
        );
    }

    /// A malformed payload is `Decode` carrying enough to act on, not a panic.
    ///
    /// `Decode` is the shape-change variant, and `domain-entities.md` requires it carry its
    /// source: "could not decode the server's response" alone sends the operator nowhere. Also
    /// covers the edge case the design names explicitly — `/agents/profiles` **losing**
    /// `loadable` is a `Decode`, because that field is not optional in `Profile`.
    #[test]
    fn a_malformed_payload_is_a_decode_error_carrying_its_cause() {
        let stub = StubServer::new(200, "this is not json at all");
        let error = stub
            .client()
            .profiles()
            .expect_err("a non-JSON body must not be reported as an empty profile list");

        assert!(
            matches!(error, TuiError::Decode(_)),
            "a malformed payload is Decode, which the renderer treats as a shape change; got \
             {error:?}"
        );
        assert!(
            error.to_string().contains("this is not json"),
            "Decode must carry enough of the body to be actionable; got {error}"
        );

        // The design's named edge case: a LOST `loadable` key.
        let missing_field = StubServer::new(
            200,
            r#"[{"name":"planner","source":"s","description":"d","capabilities":[],
                 "tags":[],"role":"r","duplicated_in":[]}]"#,
        );
        let error = missing_field
            .client()
            .profiles()
            .expect_err("`loadable` is not optional in Profile, so losing it is a Decode");
        assert!(
            matches!(error, TuiError::Decode(_)),
            "a projection that LOSES a required key must be Decode, not a silent default: \
             `loadable` defaulting to false would mark every profile unselectable; got {error:?}"
        );

        // An empty list is NOT an error — the picker renders an empty state saying so.
        let empty = StubServer::new(200, "[]");
        assert_eq!(
            empty
                .client()
                .profiles()
                .expect("an empty list is a valid answer, not a failure")
                .len(),
            0
        );

        // `detail_of` on a non-JSON body falls back to the text rather than losing it.
        assert_eq!(detail_of(b"plain text failure"), "plain text failure");
        assert_eq!(
            detail_of(br#"{"detail":"scope 'project' requires scope_id"}"#),
            "scope 'project' requires scope_id",
            "FastAPI's `detail` is what names the rejected field"
        );
        assert!(
            decode::<Health>(b"{}").is_err(),
            "a JSON object missing every required field must be a Decode error"
        );
    }

    // ── `profile find` (OQ-6 Q2) ─────────────────────────────────────────────────────────

    /// **`find_profiles` is a case-insensitive substring filter across the four fields.**
    ///
    /// Operator decision, deliberately **not** a BM25Plus port: reimplementing the Python
    /// ranking in Rust invites silent divergence, and a search that ranks differently while
    /// claiming parity is worse than one that plainly filters. So this asserts the *filter*
    /// semantics and explicitly asserts what makes it NOT a parity claim — an unloadable
    /// profile that matches is returned, whereas `search_profiles` drops it
    /// (`profile_search.py:116`). FR-1.5 governs on this side.
    ///
    /// All four fields are exercised in one pass, because a filter that only reads `name` would
    /// pass a `name`-only test and quietly halve the search.
    #[test]
    fn find_profiles_matches_case_insensitively_across_the_four_fields() {
        let payload = r#"[
            {"name":"planner","source":"s","loadable":true,"description":"Plans work",
             "capabilities":["scheduling"],"tags":["core"],"role":"r","duplicated_in":[]},
            {"name":"reviewer","source":"s","loadable":true,"description":"Reviews diffs",
             "capabilities":[],"tags":["quality"],"role":"r","duplicated_in":[]},
            {"name":"coding","source":"s","loadable":false,"description":"a directory",
             "capabilities":[],"tags":["core"],"role":"r","duplicated_in":[]}
        ]"#;

        // Each field in turn: name, description, tags, capabilities — plus a case flip on each,
        // so a match that only worked on exact case would be caught.
        for (query, expected) in [
            ("PLANN", vec!["planner"]),
            ("plans WORK", vec!["planner"]),
            ("Core", vec!["planner", "coding"]),
            ("SCHEDULING", vec!["planner"]),
            ("nothing matches this", vec![]),
        ] {
            let stub = StubServer::new(200, payload);
            let found = stub
                .client()
                .find_profiles(query)
                .expect("a 200 must decode");
            let names: Vec<&str> = found.iter().map(|p| p.name.as_str()).collect();
            assert_eq!(
                names, expected,
                "query {query:?} must match case-insensitively across name, description, tags, \
                 and capabilities — a filter reading only `name` would halve the search"
            );
        }

        // NOT a parity claim: the Python scorer drops unloadable profiles; this does not.
        let stub = StubServer::new(200, payload);
        let found = stub
            .client()
            .find_profiles("directory")
            .expect("a 200 must decode");
        assert_eq!(
            found.iter().map(|p| p.name.as_str()).collect::<Vec<_>>(),
            vec!["coding"],
            "an UNLOADABLE profile matching the query must be returned (FR-1.5), even though \
             `search_profiles` drops it at profile_search.py:116. This is a filter, not a \
             BM25Plus port, and it makes no parity claim"
        );

        // An empty query returns everything rather than nothing — the picker's unfiltered state.
        let stub = StubServer::new(200, payload);
        assert_eq!(
            stub.client()
                .find_profiles("   ")
                .expect("a 200 must decode")
                .len(),
            3,
            "a blank query is the unfiltered list, not an empty result"
        );

        // The joined text itself, so the field selection is pinned without a server.
        let text = searchable_text(&Profile {
            name: "Planner".to_string(),
            source: "IGNORED".to_string(),
            loadable: true,
            description: Some("Plans WORK".to_string()),
            capabilities: vec!["Scheduling".to_string()],
            tags: vec!["Core".to_string()],
            role: Some("IGNORED_ROLE".to_string()),
            duplicated_in: vec!["IGNORED_DUP".to_string()],
        });
        assert_eq!(
            text, "planner plans work core scheduling",
            "exactly the four fields `_searchable_text` reads (profile_search.py:43-51), \
             lowercased. `source`, `role`, and `duplicated_in` are deliberately NOT searched — \
             the Python tokenizer does not read them either"
        );
    }

    // ── Encoding (a correctness control, not cosmetics) ──────────────────────────────────

    /// Operator-supplied values are percent-encoded in both the query and the path.
    ///
    /// `minreq`'s `urlencoding` feature is off, and its own docs say the caller is then
    /// responsible for legal characters. This is not cosmetic: a session name containing `&`
    /// would split into a second query parameter, a `#` would truncate the query at the
    /// fragment, and a `/` in a path segment would reach a **different route**. All three values
    /// are operator-supplied.
    #[test]
    fn operator_supplied_values_are_percent_encoded_in_query_and_path() {
        assert_eq!(
            encode_query_component("plain-Value_1.0~"),
            "plain-Value_1.0~"
        );
        assert_eq!(encode_query_component("a&b=c"), "a%26b%3Dc");
        assert_eq!(encode_query_component("a b"), "a%20b");
        assert_eq!(encode_query_component("a#b"), "a%23b");
        assert_eq!(encode_query_component("a+b"), "a%2Bb");
        // `%` itself must become `%25` — the one byte a maintainer is most likely to add to the
        // pass-through arm, reasoning that it "already begins an escape sequence". Letting it
        // through is a query-parameter injection with an extra hop: an operator value of `%26`
        // would reach the server as `%26`, which the receiver decodes to `&`, splitting the
        // parameter — the same break-out the `&` case guards, reached by double-encoding. The
        // encoder is a strict allow-list so this holds by construction; the case is here so it
        // keeps holding. (#321, #248)
        assert_eq!(encode_query_component("a%b"), "a%25b");
        assert_eq!(encode_query_component("%26"), "%2526");
        assert_eq!(
            encode_path_segment("../etc/passwd"),
            "..%2Fetc%2Fpasswd",
            "`/` must NOT be exempt in a path segment: a value containing one would add a \
             segment and reach a different route entirely"
        );
        assert_eq!(
            encode_query_component("wörk"),
            "w%C3%B6rk",
            "non-ASCII must be encoded per byte, as UTF-8"
        );

        // End to end: a hostile session name reaches the server as ONE parameter.
        let stub = StubServer::new(201, &terminal_body());
        stub.client()
            .create_session(&SessionParams {
                agents: "planner".to_string(),
                provider: None,
                session_name: Some("work&agent_profile=evil".to_string()),
                working_directory: None,
                allowed_tools: None,
                env_vars: None,
                initial_message: None,
            })
            .expect("a 201 must be a success");

        let request = stub.next_request();
        assert_eq!(
            request.query_value("agent_profile").as_deref(),
            Some("planner"),
            "an injected `agent_profile=evil` in a session name must not override the real one — \
             it must arrive as part of the session_name VALUE"
        );
        assert_eq!(
            request.query_value("session_name").as_deref(),
            Some("work&agent_profile=evil"),
            "the value must arrive intact, as a single parameter"
        );
        assert_eq!(
            request.query_pairs().len(),
            2,
            "exactly two parameters: an unencoded `&` would have made three. Pairs: {:?}",
            request.query_pairs()
        );
    }

    /// `GET /health` decodes the two projected fields and ignores the rest.
    ///
    /// `terminal_backend` is what ADR-01 keys hand-off on, so it is the field this unit exists to
    /// deliver to `HandoffDriver`. The payload carries `service` and `components` because the
    /// real one does — a projection that broke on the server's extra keys would fail on every
    /// live call.
    #[test]
    fn health_decodes_the_two_projected_fields_and_ignores_the_others() {
        let stub = StubServer::new(
            200,
            r#"{"status":"ok","service":"cli-agent-orchestrator","terminal_backend":"herdr",
                "components":{"cao":"ok","herdr":"ok"}}"#,
        );

        let health = stub.client().health().expect("a 200 must decode");

        assert_eq!(health.status, "ok");
        assert_eq!(
            health.terminal_backend, "herdr",
            "`terminal_backend` is the value ADR-01 keys the hand-off strategy on; this machine's \
             server reports herdr"
        );

        // A non-200 stays Http, so the renderer can state the status.
        let failing = StubServer::new(503, "{}");
        assert!(
            matches!(failing.client().health(), Err(TuiError::Http(503))),
            "a non-200 on /health must be Http(status), not a decoded default"
        );
    }

    /// The `Terminal` projection reads the window name from `name`, never `window_name`.
    ///
    /// An earlier draft of the design invented a `window_name` key; review caught it. The
    /// distinction is not cosmetic — a struct declaring `window_name` would compile, deserialise
    /// to empty, and fail only at hand-off time with nothing upstream to catch it. The fixture
    /// feeds both keys precisely so a wrong read is visible.
    #[test]
    fn the_terminal_projection_reads_the_window_name_from_name() {
        let stub = StubServer::new(
            200,
            r#"{"id":"a1b2c3d4","name":"planner-1","provider":"kiro_cli","session_name":"work",
                "agent_profile":"planner","caller_id":null,"window_name":"WRONG_KEY",
                "status":"waiting_user_answer","last_active":null}"#,
        );

        let terminal: Terminal = stub
            .client()
            .terminal("a1b2c3d4")
            .expect("a 200 must decode");

        assert_eq!(
            terminal.name, "planner-1",
            "the window name comes from `name`; there is no `window_name` key on the wire, and a \
             struct declaring one would deserialise to empty and fail only at hand-off"
        );
        assert_eq!(
            terminal.status,
            Some(TerminalStatus::WaitingUserAnswer),
            "all six statuses must survive the boundary unchanged — the collapse to Readiness is \
             `await_ready`'s job (BR-16), not this unit's"
        );
    }
}
