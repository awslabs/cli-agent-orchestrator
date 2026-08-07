//! `renderer` (Bolt 5): the shell — geometry, focus order, the key map, and **the `launch()`
//! orchestration**. The DAG root, and the last unit (issue #321).
//!
//! # FR-3.2 is this unit's defining obligation
//!
//! **The results pane must have a production caller.** The superseded implementation built a
//! captured-output pane and never invoked it from production code — design defect #3, and the
//! reason this rewrite exists. That defect recurs *precisely here*, in the unit that is supposed
//! to drive the pane, so [`Renderer::launch`] and [`Renderer::run_in_app`] call
//! [`ResultsPane::attach`], [`ResultsPane::push_bytes`], [`ResultsPane::complete`] and
//! [`ResultsPane::refuse`] from production code. A test-only caller would reproduce the original
//! defect exactly while looking green — which is why
//! `the_results_pane_has_production_callers_in_launch_and_the_in_app_path` asserts on
//! **production call sites** rather than on a harness driving the pane.
//!
//! # This is the ONLY orchestrator
//!
//! `component-methods.md:112` names it explicitly, because a 2.7 reviewer found the
//! `await_ready -> handoff` sequencing unspecified. The other five units are mechanisms: the
//! catalog is a table, the flow is field state, the pane is a buffer, the client is HTTP, and the
//! driver moves a view. None of them sequences anything, and none holds a reference back here —
//! [`Renderer::render`] **pulls**, which is what keeps the graph acyclic despite five
//! dependencies.
//!
//! # Infallible by design, with exactly one exception
//!
//! `render()`, `on_key()`, `launch()` and `resize()` cannot fail, and that is a design property
//! rather than an omission: **a UI that raises has nowhere to raise to.** Every outcome is a
//! rendered state. [`Renderer::run`] is the sole fallible method, and only for an unrecoverable
//! **startup** condition ([`Fatal`]) — not for a server that happens to be down, which is a
//! rendered state (FR-6.1).
//!
//! # Two seams, following `handoff.rs`'s idiom rather than inventing one
//!
//! [`ServerApi`] and [`handoff::Host`] are injected. `handoff.rs` records why: a trait per
//! boundary means the sequencing is exercised with no I/O at all, and — the part that matters for
//! a UI unit — **a test can observe what was called**. FR-3.2 is a statement about call sites, so
//! it is only provable if the calls are observable.
//!
//! # The in-app gap, stated rather than half-wired (operator decision at the 3.5 plan gate)
//!
//! `ServerClient::run` needs `path_values`, `query` and `body`. **Measured from `route()` at this
//! stage: of the 22 IN-APP commands, 9 have a placeholder-free route, 12 carry placeholders
//! (`{name}`, `{run_id}`, `{key}`, `{terminal_id}`), and 1 (`profile find`) has no route at all.**
//! Nothing in the crate maps an arbitrary command's form fields to a route's path values, and no
//! artifact specifies how.
//!
//! The operator's ruling was **wire what works, state the gap**: the 9 placeholder-free routes run
//! for real, and the other 13 render a stated "not yet wired" error that names exactly what is
//! missing. Inventing a field-to-path-value mapping here would be the "invent a mechanism the
//! design never specified" failure, and a visible gap beats a picker that half-works — which is
//! the "passed CI but partially worked" class this rewrite exists to eliminate. (#321)
//!
//! Note the plan recorded "15 carry placeholders". That was not re-derived from `route()`; the
//! measured figure is 12 templated plus 1 routeless. See [`in_app_readiness`].

use std::io::{IsTerminal, Write};
use std::time::Duration;

use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::widgets::{Paragraph, Widget, Wrap};
use ratatui::Terminal as RatatuiTerminal;

use crate::catalog::{self, Command, CommandId, Policy};
use crate::error::TuiError;
use crate::guided_flow::{self, Field, FieldKind, GuidedFlow, PickerState, UNLOADABLE_MARKER};
use crate::handoff::{HandoffDriver, Host, ServerRead};
use crate::results_pane::{PaneState, ResultsPane};
use crate::types::{Health, Profile, Provider, Readiness, SessionParams, Terminal, TerminalStatus};

/// The minimum terminal the two-column layout needs (NFR-6). Below either bound the layout
/// **stacks** — degraded but fully usable, never an error state.
const MIN_COLS: u16 = 80;
/// See [`MIN_COLS`]. 24 rows is the second half of NFR-6's 80x24 floor.
const MIN_ROWS: u16 = 24;

/// How long [`Renderer::await_pickers`] waits for each picker answer.
///
/// Deliberately **not** the 30-second readiness cap and not `ServerClient`'s per-request timeout:
/// this bounds a channel receive, and an unbounded `recv()` here would hang the event loop on a
/// hung fetch — the same failure class as the pty deadlock the harness exists to prevent.
const PICKER_DRAIN_TIMEOUT: Duration = Duration::from_millis(50);

/// Restores terminal modes on every return path from the interactive loop.
///
/// `Drop` is intentionally best-effort: an error is already being returned when unwinding most
/// failure paths, and replacing it with a second cleanup error would hide the actionable cause.
/// The explicit `show_cursor()` before a successful return remains fallible. (#321)
struct TerminalRestore;

impl Drop for TerminalRestore {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(std::io::stdout(), LeaveAlternateScreen);
    }
}

// ## The `allow(dead_code)` attributes in this module, and why they are `allow` and not `expect`
//
// This is a **binary** crate, so `pub` does not exempt an item from `dead_code` — there is no
// downstream crate that could use it. The event loop reaches the interactive surface indirectly
// through key dispatch, while focused tests also call orchestration methods directly.
//
// **`allow`, not `expect`**, for the reason `types.rs` and `guided_flow.rs` both record after
// measuring it: the `cfg(test)` build *does* use these, so under `--all-targets` an
// `#[expect(dead_code)]` is unfulfilled and `-D warnings` promotes
// `unfulfilled_lint_expectations` to an error. `expect` would fail the gate outright.
//
// **Per-item, never a module-level `#![allow(dead_code)]`**: a module-wide allow would cover every
// item added here in future, silently and permanently, and would hide the next genuinely orphaned
// method — which, in the unit whose defining requirement is "this component had no caller", is the
// exact lint that must keep working. (#321)

/// The unrecoverable **startup** condition — the only thing this unit can fail with.
///
/// # Why a unit-local `thiserror` type and not an eighth [`TuiError`] variant
///
/// `results_pane.rs` and `guided_flow.rs` each recorded this decision for their own single
/// condition, and the reasoning transfers with one addition specific to this unit:
///
/// 1. **[`TuiError`] IS the operator-facing boundary contract**, by its own documentation —
///    `renderer` matches on it to choose a *rendered state*. `Fatal` is the one condition that
///    can never be rendered, because it means the terminal could not be put into a state where
///    rendering is possible at all. Putting it in the enum whose purpose is what-to-render would
///    weaken both meanings.
/// 2. **The signature is the documentation.** `run() -> Result<(), Fatal>` says the only thing
///    that can go wrong is startup. `Result<(), TuiError>` would admit seven variants, six of
///    which this method must handle as rendered states rather than propagate — and a reader could
///    not tell which from the type.
/// 3. **It carries no `std::io::Error`,** so it is `PartialEq` and a test reads as the
///    requirement.
///
/// The trade-off is stated rather than hidden: `renderer` now handles three error types
/// (`TuiError`, `guided_flow::Error`, `results_pane::NotRunning`) plus its own. That is the cost
/// of the one-type rule being about *integration boundaries*, which none of the three cross.
/// (#321)
#[derive(Debug, thiserror::Error, Clone, PartialEq, Eq)]
#[error("cao-tui cannot start: {0}")]
pub struct Fatal(pub String);

/// Which region has keyboard focus. Focus order follows FR-2.1's step order.
///
/// A closed enum with an exhaustive `match` in [`Renderer::on_key`], following `catalog.rs`'s
/// idiom: a sixth region is a compile error rather than a region that silently cannot be reached
/// by `Tab` — which, for a keyboard-only UI (NFR-3), is the same as not existing. (#321)
#[allow(dead_code)] // every variant is constructed by `on_key`'s focus ring. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    /// The navigable command list. `Enter` selects.
    CommandList,
    /// The always-visible required fields.
    RequiredFields,
    /// The collapsed-by-default optional section header (FR-2.3).
    OptionalSection,
    /// The results pane. `[k]` cancels here, and **only while running**.
    Results,
}

#[allow(dead_code)] // the ring is walked by `on_key`, whose caller is the event loop. (#321)
impl Focus {
    /// The focus ring, in FR-2.1's order. Wraps.
    ///
    /// A `const` array rather than an `impl` with four arms so the *order* is readable as data —
    /// FR-2.1 specifies a step order, and a chain of `match` arms makes it something a reader has
    /// to reconstruct.
    const ORDER: [Self; 4] = [
        Self::CommandList,
        Self::RequiredFields,
        Self::OptionalSection,
        Self::Results,
    ];

    /// The index of `self` in [`Self::ORDER`].
    ///
    /// Infallible without an `unwrap`: every variant is in the array, and the fallback is
    /// `Self::CommandList`'s index rather than a panic, because a UI that panics on a focus
    /// lookup is worse than one that returns to the first region.
    fn position(self) -> usize {
        Self::ORDER
            .iter()
            .position(|region| *region == self)
            .unwrap_or(0)
    }

    /// `Tab`: the next region, wrapping to the first.
    fn next(self) -> Self {
        Self::ORDER[(self.position() + 1) % Self::ORDER.len()]
    }

    /// `Shift-Tab`: the previous region, wrapping to the last.
    ///
    /// `+ len - 1` rather than `- 1` because the index is a `usize` and `0 - 1` underflows. The
    /// arithmetic is the reason this is a named method and not inlined twice.
    fn previous(self) -> Self {
        Self::ORDER[(self.position() + Self::ORDER.len() - 1) % Self::ORDER.len()]
    }
}

/// The two layouts NFR-6 specifies.
#[allow(dead_code)] // both variants are constructed by `LayoutMode::of`. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LayoutMode {
    /// >= 80x24: command list and form on the left, results lower-right.
    TwoColumn,
    /// Below either bound: a single stacked column, results below the form.
    ///
    /// **Degraded but fully usable, never an error** (NFR-6). Long lines wrap rather than
    /// truncate, which is `Wrap { trim: false }` on every paragraph here.
    Stacked,
}

#[allow(dead_code)] // called by `render`, `draw`, and `results_height`. (#321)
impl LayoutMode {
    /// NFR-6's rule, in one place so the two-column and stacked branches cannot drift.
    ///
    /// `<` on **either** dimension stacks. An `&&` here would keep the two-column layout at
    /// 200x10, where the results region would round down to zero rows and render nothing — a
    /// blank region, which is the failure `render()` must never produce.
    fn of(cols: u16, rows: u16) -> Self {
        if cols >= MIN_COLS && rows >= MIN_ROWS {
            Self::TwoColumn
        } else {
            Self::Stacked
        }
    }
}

/// Whether an IN-APP command can actually be run in-pane, and what is missing when it cannot.
///
/// # This type IS the stated gap (operator decision)
///
/// Modelled as a three-variant enum rather than a `bool` so the renderer **cannot** run a command
/// whose path values it does not have: the only way to reach [`ServerClient::run`] is through
/// [`Self::Runnable`], and the other two variants carry the sentence the operator reads. A `bool`
/// would have made "and what do I pass for `{run_id}`?" a question answered at the call site,
/// under time pressure, by inventing a mapping.
///
/// Derived from `route()`'s own `placeholders` rather than from a second list of command ids —
/// there is exactly one route table, and a hard-coded id list here would be a place for the two to
/// disagree silently. (#321)
#[allow(dead_code)] // constructed by `in_app_readiness`; matched by `run_in_app`. (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InAppReadiness {
    /// Every path placeholder is bound to a form field and no filled field would be ignored.
    ///
    /// **21 of the 24 IN-APP commands reach this on an empty form, measured** — up from 9. The earlier count was
    /// not a property of the routes; it was the consequence of `renderer` calling
    /// `run(id, &[], &[], None, ..)` and therefore treating any route with a `{token}` as
    /// unreachable, even the 10 whose token a form field plainly supplies.
    Runnable,
    /// The route needs a path value **no form field supplies**.
    ///
    /// Carries the unbound placeholder names so the rendered error says *which* values are missing
    /// rather than "not supported" — the difference between a stated gap and a dead end. Only
    /// `cao session send` and `cao session status` remain here, both needing the session-name →
    /// terminal-id resolution call the CLI makes first (`session.py:26`).
    NotWired {
        /// The `{token}` names still unsupplied.
        placeholders: Vec<&'static str>,
    },
    /// The operator filled a field the route would **silently discard**.
    ///
    /// # Why this variant exists, and why it is keyed on FILLED fields
    ///
    /// The reported defect was that `run_in_app` sent no query and no body at all, so
    /// `memory list --scope X` ran as a scan-all and `memory clear`'s required `--scope` was
    /// dropped. Most of those are now bound. What remains are fields with genuinely nowhere to go:
    /// `memory export --output` (a local path), `schedule add`'s `file_path` and
    /// `workflow validate`'s `file` (both need a JSON body this crate cannot yet build).
    ///
    /// **Keyed on what the operator actually typed, not on what the command declares.** Refusing
    /// `memory export` outright because `--output` *could* be filled would break a command that
    /// works perfectly without it — trading a silent drop for a false blocker. The dishonesty is
    /// specifically a *value* going nowhere, so that is the trigger.
    /// (Reported by review on PR #547.)
    Ignored {
        /// The filled field names that would not reach the server, in catalog order.
        fields: Vec<&'static str>,
    },
    /// The command has no HTTP route at all (`profile find`, measured: exactly one).
    NoRoute,
}

/// Which reads this unit needs from the server, taken through a trait for `handoff.rs`'s reasons.
///
/// # Why this is a new trait rather than a reuse of [`ServerRead`]
///
/// [`ServerRead`] is `handoff.rs`'s two reads (`health`, `terminal`) and is **deliberately not
/// widened**: adding `create_session` and `run` to it would give the hand-off driver a surface it
/// has no business holding, and `handoff.rs`'s own docs say one trait per boundary. So this trait
/// is a superset by *inheritance* — `ServerApi: ServerRead` — which means one production type
/// (`ServerClient`) and one fake satisfy both, and the driver still only sees the two reads it
/// needs.
///
/// The `run` method takes `&mut dyn Write` rather than a generic `W: Write`, and that is forced
/// rather than stylistic: a generic method makes the trait not object-safe, and more importantly a
/// **fake cannot be written for it** without repeating the generic — which is how the incremental
/// forwarding test would end up unable to observe intermediate state. `ServerClient::run` is
/// generic; the impl below monomorphises it at `&mut dyn Write`, which costs one vtable per chunk
/// and buys the only proof PR-1 can have. (#321)
pub trait ServerApi: ServerRead {
    /// `GET /agents/profiles` — every entry, unloadable ones included (FR-1.1, FR-1.5).
    fn profiles(&self) -> Result<Vec<Profile>, TuiError>;

    /// `GET /agents/providers` — unfiltered (FR-1.2, FR-1.7).
    fn providers(&self) -> Result<Vec<Provider>, TuiError>;

    /// `POST /sessions`, expecting **201** (`launch()` step 2).
    fn create_session(&self, params: &SessionParams) -> Result<Terminal, TuiError>;

    /// An IN-APP route, streaming into `sink` as bytes arrive (BR-17, PR-1).
    fn run(
        &self,
        id: CommandId,
        path_values: &[&str],
        query: &[(&str, &str)],
        body: Option<&str>,
        sink: &mut dyn Write,
    ) -> Result<u16, TuiError>;
}

/// A rendered banner: the one styled line (or few) the operator reads above the panes.
///
/// **Every error in this unit becomes one of these**, which is what makes "every outcome is a
/// rendered state" a mechanism rather than an intention. There is deliberately no error *setter*
/// on [`ResultsPane`] — it has exactly four methods, none of which sets an error — so
/// `launch()`'s step-2 failure lands here instead. (An earlier design draft said "steps 2 and 4";
/// it was corrected at 3.1, and a developer chasing the overstatement would hunt for a missing
/// pane method or invent one.)
#[allow(dead_code)] // constructed by `launch`/`run_in_app`; read by `render`. (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Banner {
    /// `error`, `warning`, or `info` — **as TEXT** (NFR-3). Never colour alone.
    pub severity: &'static str,
    /// WHAT failed.
    pub what: String,
    /// WHY — the cause, naming the address when there is one (FR-6.1, SR-1).
    pub why: String,
    /// WHAT TO DO — the remedy, and `[r] retry` when a retry is available (FR-6.3).
    pub remedy: String,
}

#[allow(dead_code)] // every constructor's caller is on the interactive surface. (#321)
impl Banner {
    /// A hard error: cause and remedy, never a traceback (SR-1).
    fn error(what: impl Into<String>, why: impl Into<String>, remedy: impl Into<String>) -> Self {
        Self {
            severity: "error",
            what: what.into(),
            why: why.into(),
            remedy: remedy.into(),
        }
    }

    /// A warning the flow **continues past** — `Readiness::Unknown`'s arm (ADR-04).
    fn warning(what: impl Into<String>, why: impl Into<String>, remedy: impl Into<String>) -> Self {
        Self {
            severity: "warning",
            what: what.into(),
            why: why.into(),
            remedy: remedy.into(),
        }
    }

    /// A non-error progress state rendered before blocking I/O begins.
    fn info(what: impl Into<String>, why: impl Into<String>, remedy: impl Into<String>) -> Self {
        Self {
            severity: "info",
            what: what.into(),
            why: why.into(),
            remedy: remedy.into(),
        }
    }

    /// The display lines, severity first so no state is conveyed by colour alone (NFR-3).
    fn lines(&self) -> Vec<String> {
        vec![
            format!("[{}] {}", self.severity, self.what),
            format!("  cause: {}", self.why),
            format!("  remedy: {}", self.remedy),
        ]
    }
}

/// What `launch()` was doing when it failed, so `[r]` retries **that** rather than restarting.
///
/// FR-6.3 requires retry *in place* with form values preserved. Retry needs to know which step to
/// resume, and recording it here — rather than re-deriving it from the banner text — is what keeps
/// the retry honest when the message wording changes.
#[allow(dead_code)] // constructed by `launch`/`run_in_app`/`populate_pickers`. (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Retryable {
    /// The picker fetches. Both return to `Loading` and are re-issued.
    Pickers,
    /// `launch()`. Re-runs the whole four-step sequence with the form untouched.
    Launch,
    /// An in-app run of this command.
    InApp(CommandId),
}

/// Work queued by a keypress and executed only after the event loop draws its pending state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PendingAction {
    Launch,
    InApp(CommandId),
}

/// One rendered frame, as a data structure rather than as terminal side effects.
///
/// # Why a `Frame` value and not a direct `ratatui::Frame` draw
///
/// `component-methods.md:120` gives `render()` the return type `Frame`, and the reason it is
/// worth honouring is testability: "never blank" (SR-2) is a property of *content*, and the only
/// way to assert it without a terminal is to have the content in hand. [`Renderer::draw`] renders
/// this value into a ratatui buffer, so the widget path and the assertion path see the same data.
///
/// **"Blank" needs a definition or the test is vacuous** (`frontend-components.md:167`): a frame
/// is blank when every rendered string is empty or whitespace. So [`Self::is_blank`] asks "is
/// there at least one non-whitespace glyph", not `frame != Frame::default()` — the latter passes
/// while the screen is visually empty. (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    /// The layout NFR-6 chose for the current size.
    pub layout: LayoutMode,
    /// App title plus the server-status indicator, textual.
    pub header: Vec<String>,
    /// The navigable command list — **HIDE rows already excluded** by `commands()` (FR-4.3).
    pub command_list: Vec<String>,
    /// The required fields, always visible.
    pub required_fields: Vec<String>,
    /// The optional section: its header always, its fields only when expanded (FR-2.3).
    pub optional_section: Vec<String>,
    /// The two pickers' states, each stating cause and remedy when failed (FR-6.1).
    pub pickers: Vec<String>,
    /// The results pane's own lines, pulled from the pane.
    pub results: Vec<String>,
    /// The banner, when there is one.
    pub banner: Vec<String>,
    /// Gating reason | key hints (FR-6.2, NFR-3).
    pub footer: Vec<String>,
}

#[allow(dead_code)] // `is_blank` is SR-2's predicate, asserted by this module's tests. (#321)
impl Frame {
    /// Every line in the frame, in render order.
    fn all_lines(&self) -> impl Iterator<Item = &String> {
        self.header
            .iter()
            .chain(&self.command_list)
            .chain(&self.required_fields)
            .chain(&self.optional_section)
            .chain(&self.pickers)
            .chain(&self.results)
            .chain(&self.banner)
            .chain(&self.footer)
    }

    /// Is there **no** non-whitespace glyph anywhere in the frame?
    ///
    /// The definition `frontend-components.md:167` insists on, because a blank screen is
    /// indistinguishable from a hang (SR-2) and a weaker definition makes the guard vacuous.
    pub fn is_blank(&self) -> bool {
        !self.all_lines().any(|line| !line.trim().is_empty())
    }
}

/// Starts the two picker reads and returns their shared update feed.
type PickerLauncher<'a> = Box<dyn Fn(&mut GuidedFlow) -> guided_flow::PickerFeed + 'a>;

/// The shell. One instance per TUI process.
///
/// Borrows its two seams for the process's lifetime, following `HandoffDriver`'s shape: the
/// lifetime is what lets [`HandoffDriver::new`] be called per launch without re-resolving the
/// backend, and it keeps this type free of `Arc` for state that is never shared across threads
/// (the event loop is single-threaded — TS-1; only picker population is concurrent).
#[allow(dead_code)] // the interactive fields are read by `on_key`/`launch`/`run_in_app`. (#321)
pub struct Renderer<'a, S: ServerApi, H: Host> {
    server: &'a S,
    host: &'a H,
    flow: GuidedFlow,
    pane: ResultsPane,
    /// The command list, pulled once — it is a compile-time constant filtered by policy.
    commands: Vec<Command>,
    /// Index into [`Self::commands`]. Not a `CommandId`, because the list is what `Up`/`Down`
    /// move through and a selected-but-not-confirmed row is a real state.
    cursor: usize,
    /// Index into the selected command's fields. Each form region clamps it to its own slice.
    field_cursor: usize,
    focus: Focus,
    /// `false` by default (FR-2.3): the optional section is collapsed but **present**.
    optional_expanded: bool,
    cols: u16,
    rows: u16,
    banner: Option<Banner>,
    /// What `[r]` would retry. `None` when there is nothing to retry.
    retryable: Option<Retryable>,
    /// Set by Enter, consumed immediately after the next draw.
    pending_action: Option<PendingAction>,
    /// The live picker feed, when one is in flight.
    picker_feed: Option<guided_flow::PickerFeed>,
    /// Production launches both picker reads concurrently. Tests may omit this and use the
    /// deterministic injected `ServerApi` path below.
    picker_launcher: Option<PickerLauncher<'a>>,
    /// The last `health()` answer, for the header's server indicator.
    ///
    /// `Option<Result<..>>` rather than `Result`: "not asked yet" is a third state, and rendering
    /// it as a failure would report the server down before anything was tried.
    health: Option<Result<Health, String>>,
    /// Set while an in-app run or a launch is in flight, so quitting confirms first (Step 10).
    running: bool,
    /// `true` once `[q]` was pressed while something was running — the confirm prompt.
    confirm_quit: bool,
    /// In-progress text for the focused field, so a TRAILING space survives between keystrokes.
    ///
    /// `(field_cursor, text)`. `GuidedFlow::set` stores a trimmed value by an affirmed rule, which
    /// made a trailing space unmakeable: the renderer appends one character and re-`set`s, so the
    /// space was trimmed before the next character arrived. Keyed by cursor index so moving to
    /// another field cannot inherit this one's partial text. (#321)
    edit_buffer: Option<(usize, String)>,
    /// Set by `[q]` (or a confirmed quit) and read by the event loop.
    should_quit: bool,
}

#[allow(dead_code)] // `main` reaches `new`/`run`/`render`; the rest await the event loop. (#321)
impl<'a, S: ServerApi, H: Host> Renderer<'a, S, H> {
    /// Builds the shell. **Performs no I/O and resolves no backend.**
    ///
    /// FR-6.1 in its consequential form: `HandoffDriver`'s backend is a `OnceCell` resolved
    /// lazily, and this constructor does not touch it. An eager resolve here would turn FR-6.1's
    /// *rendered* server-down state into a **startup crash** — the TUI must open with the server
    /// down. That is why `health` starts as `None` rather than being fetched, and why the driver
    /// is constructed per launch rather than held as a field.
    pub fn new(server: &'a S, host: &'a H, cols: u16, rows: u16) -> Self {
        Self {
            server,
            host,
            flow: GuidedFlow::new(),
            pane: ResultsPane::new(),
            // FR-4.3: `commands()` filters HIDE rows, so a hidden command is ABSENT from
            // navigation rather than disabled. There is no other list.
            commands: catalog::commands(),
            cursor: 0,
            field_cursor: 0,
            focus: Focus::CommandList,
            optional_expanded: false,
            cols,
            rows,
            banner: None,
            retryable: None,
            pending_action: None,
            picker_feed: None,
            picker_launcher: None,
            health: None,
            running: false,
            confirm_quit: false,
            edit_buffer: None,
            should_quit: false,
        }
    }

    /// Installs the production concurrent picker source.
    ///
    /// Kept as a constructor modifier so the orchestration fake need not be `Sync`: production
    /// owns an `Arc<ServerClient>`, while unit tests use `RefCell` call records on one thread.
    pub fn with_concurrent_pickers<P>(mut self, source: std::sync::Arc<P>) -> Self
    where
        P: guided_flow::PickerSource + Send + Sync + 'static,
    {
        self.picker_launcher = Some(Box::new(move |flow| {
            flow.populate_pickers(std::sync::Arc::clone(&source))
        }));
        self
    }

    /// The event loop. **The only fallible method** (`Fatal`, startup conditions only).
    ///
    /// Everything after startup is a rendered state: a server that is down, a picker that failed,
    /// a hand-off that was refused. `Fatal` is reserved for a terminal that cannot be put into a
    /// renderable state at all, which is the one condition under which rendering an error is not
    /// available as an answer.
    ///
    pub fn run(&mut self) -> Result<(), Fatal> {
        if self.cols == 0 || self.rows == 0 {
            // The one genuine startup failure: a zero-area terminal cannot render *anything*, so
            // the honest answer is one styled line and a non-zero exit rather than a frame nobody
            // can see. Not a `TuiError` — see `Fatal`'s docs.
            return Err(Fatal(format!(
                "the terminal reports a {cols}x{rows} area, which has no room to render. \
                 Resize the terminal and start again",
                cols = self.cols,
                rows = self.rows,
            )));
        }

        self.tick();

        // Integration tests and shell probes capture stdout through a pipe. There is no terminal
        // to put into raw mode in that case, so `main` prints one populated textual frame and
        // exits. A real terminal takes the interactive path below. (#321)
        if !std::io::stdout().is_terminal() {
            return Ok(());
        }

        enable_raw_mode().map_err(|error| Fatal(format!("could not enable raw mode: {error}")))?;
        let mut stdout = std::io::stdout();
        execute!(stdout, EnterAlternateScreen)
            .map_err(|error| Fatal(format!("could not enter the alternate screen: {error}")))?;
        let _restore = TerminalRestore;

        let backend = CrosstermBackend::new(stdout);
        let mut terminal = RatatuiTerminal::new(backend)
            .map_err(|error| Fatal(format!("could not initialise the terminal: {error}")))?;

        while !self.should_quit {
            self.tick();
            terminal
                .draw(|frame| self.draw(frame.area(), frame.buffer_mut()))
                .map_err(|error| Fatal(format!("could not draw the terminal: {error}")))?;

            // The draw above is load-bearing: every network operation gets one visible pending
            // frame before it can block the single-threaded event loop. (#321)
            if self.run_pending_action() {
                continue;
            }

            if !crossterm::event::poll(Duration::from_millis(50))
                .map_err(|error| Fatal(format!("could not poll terminal input: {error}")))?
            {
                continue;
            }

            match crossterm::event::read()
                .map_err(|error| Fatal(format!("could not read terminal input: {error}")))?
            {
                Event::Key(key)
                    if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) =>
                {
                    self.on_key_event(key.code, key.modifiers);
                }
                Event::Resize(cols, rows) => self.resize(cols, rows),
                Event::FocusGained
                | Event::FocusLost
                | Event::Key(_)
                | Event::Mouse(_)
                | Event::Paste(_) => {}
            }
        }

        terminal
            .show_cursor()
            .map_err(|error| Fatal(format!("could not restore the cursor: {error}")))?;
        Ok(())
    }

    /// One event-loop iteration's non-input work: apply whatever the pickers have answered.
    ///
    /// Non-blocking by construction (`drain_picker_updates` uses `try_recv`), which is INV-5 of
    /// `guided-flow`: picker state changes on the same thread that mutates fields, so no lock is
    /// needed. Returns how many answers were applied, so a caller can tell whether a redraw is
    /// warranted.
    pub fn tick(&mut self) -> usize {
        let Some(feed) = self.picker_feed.as_ref() else {
            return 0;
        };
        self.flow.drain_picker_updates(feed)
    }

    /// Renders the current state. **Infallible, and never blank** (SR-2).
    ///
    /// Pull-based: every region is read from its owner at render time and nothing is cached, so no
    /// component needs a reference back here (the acyclicity property). The header is
    /// unconditional — that alone makes [`Frame::is_blank`] false even with the server down, both
    /// pickers failed and no command selected, which is the total-failure case the never-blank
    /// test exercises.
    pub fn render(&self) -> Frame {
        let layout = LayoutMode::of(self.cols, self.rows);

        Frame {
            layout,
            header: self.header_lines(),
            command_list: self.command_list_lines(),
            required_fields: self.field_lines(true),
            optional_section: self.optional_section_lines(),
            pickers: self.picker_lines(),
            results: self.results_lines(),
            banner: self.banner.as_ref().map(Banner::lines).unwrap_or_default(),
            footer: self.footer_lines(),
        }
    }

    /// Title plus the server indicator, textual (NFR-3).
    fn header_lines(&self) -> Vec<String> {
        let status = match &self.health {
            None => "server: not checked yet".to_string(),
            Some(Ok(health)) => format!(
                "server: {} (backend {})",
                health.status, health.terminal_backend
            ),
            // Cause AND remedy, even in the one-line indicator (FR-6.1): "server: error" would
            // tell the operator nothing they could act on.
            //
            // **`{why}` already carries its own label**, so this no longer prefixes "unreachable"
            // unconditionally. It used to, and the result contradicted itself: with auth enabled
            // every route answers 401, and the header read `server: unreachable — cao-server
            // returned HTTP 401` — a server that answered, described as unreachable, with an
            // implied "start the server" remedy that cannot help. `server_failure_summary` writes
            // the right label for each variant. (Reported by review on PR #547.)
            Some(Err(why)) => format!("server: {why}"),
        };

        vec![format!("cao-tui {}", env!("CARGO_PKG_VERSION")), status]
    }

    /// The command list. `Hidden` rows are **absent**, not disabled (FR-4.3, SR-3).
    ///
    /// The focus marker is a structural `>` rather than a colour, per NFR-3 item 1 — a screen
    /// whose focus is only a hue is unnavigable for anyone who cannot see it, and item 7 makes
    /// visible focus a hard requirement.
    fn command_list_lines(&self) -> Vec<String> {
        self.commands
            .iter()
            .enumerate()
            .map(|(index, command)| {
                let marker = if index == self.cursor && self.focus == Focus::CommandList {
                    ">"
                } else {
                    " "
                };
                let selected = if self.flow.current() == Some(command.id) {
                    "*"
                } else {
                    " "
                };
                format!(
                    "{marker}{selected} {} — {}",
                    render_command_path(command),
                    command.summary
                )
            })
            .collect()
    }

    /// The form's fields, split at [`GuidedFlow::guided_field_count`].
    ///
    /// `required` selects the guided prefix (FR-2.1's three steps) rather than literally
    /// `field.required`, because FR-2.3's split is about the *guided surface* and only `--agents`
    /// is actually required — a literal filter would put `--provider` and `--session-name` behind
    /// the collapsed section, which is not FR-2.1's step order.
    fn field_lines(&self, guided: bool) -> Vec<String> {
        let split = self.flow.guided_field_count();
        let fields = self.flow.fields();
        let slice = if guided {
            &fields[..split.min(fields.len())]
        } else {
            &fields[split.min(fields.len())..]
        };

        let offset = if guided { 0 } else { split.min(fields.len()) };
        slice
            .iter()
            .enumerate()
            .map(|(index, field)| {
                let focused = match self.focus {
                    Focus::RequiredFields => guided && self.field_cursor == offset + index,
                    Focus::OptionalSection => {
                        !guided && self.optional_expanded && self.field_cursor == offset + index
                    }
                    Focus::CommandList | Focus::Results => false,
                };
                render_field(field, focused, self.flow.current())
            })
            .collect()
    }

    /// The optional section: **the header is always present** (FR-2.3, BR-10).
    ///
    /// Hidden-by-default is not the same as absent. The header states the count and the key that
    /// expands it, so every one of the nine remaining parameters is reachable without leaving the
    /// form — which is what makes NFR-3's keyboard-only requirement hold for them.
    fn optional_section_lines(&self) -> Vec<String> {
        let hidden = self.field_lines(false);
        if hidden.is_empty() {
            return Vec::new();
        }

        let marker = if self.focus == Focus::OptionalSection {
            ">"
        } else {
            " "
        };
        let glyph = if self.optional_expanded { "▾" } else { "▸" };
        let mut lines = vec![format!(
            "{marker} {glyph} optional ({count}) — [enter] expand/collapse",
            count = hidden.len()
        )];

        if self.optional_expanded {
            lines.extend(hidden);
        }
        lines
    }

    /// Both pickers, each state rendered distinctly.
    ///
    /// `Loaded(vec![])` and `Failed` are **different** and render differently: an empty list from
    /// a machine with no profiles is a valid answer, while a failure states cause and remedy
    /// (FR-6.1). Conflating them tells the operator something is broken when nothing is.
    ///
    /// An unloadable profile is **listed with its textual marker** and is unselectable (FR-1.5,
    /// NFR-3 item 4) — the operator learns the profile exists *and why it is unavailable*.
    /// Filtering it would hide the diagnosis, which is what
    /// `inception/practices-discovery/discovered-rules.md:29` would have had us do; FR-1.5
    /// supersedes that rule per the operator's later decision.
    fn picker_lines(&self) -> Vec<String> {
        let mut lines = Vec::new();

        // ONE LINE PER CHOICE, not a comma-joined paragraph.
        //
        // 25 profiles joined by ", " wrapped into an unreadable block, and the operator could not
        // pick a name out of it. The count stays on its own header line so it is still visible at a
        // glance, and each choice is indented beneath it. (#321)
        match self.flow.agent_choices() {
            PickerState::Loading => lines.push("agents: loading…".to_string()),
            PickerState::Loaded(profiles) if profiles.is_empty() => {
                lines.push("agents: none found on this machine".to_string());
            }
            PickerState::Loaded(profiles) => {
                lines.push(format!("agents ({}):", profiles.len()));
                for profile in profiles {
                    // The unselectable marker travels WITH its row (FR-1.5): an unloadable profile
                    // is shown and explained, never filtered out.
                    if profile.loadable {
                        lines.push(format!("  {}", profile.name));
                    } else {
                        lines.push(format!("  {} [{UNLOADABLE_MARKER}]", profile.name));
                    }
                }
            }
            PickerState::Failed(error) => {
                // `[ctrl+r]`, not `[r]`: a plain `r` is TEXT in a field, so naming it here would
                // promise an affordance that types instead of retrying — the `[c] clear` failure
                // again, where a documented key did nothing. (#321)
                lines.push(format!(
                    "agents: unavailable — {error}. Press [ctrl+r] to retry"
                ));
            }
        }

        match self.flow.provider_choices() {
            PickerState::Loading => lines.push("providers: loading…".to_string()),
            PickerState::Loaded(providers) if providers.is_empty() => {
                lines.push("providers: none reported".to_string());
            }
            PickerState::Loaded(providers) => {
                lines.push(format!("providers ({}):", providers.len()));
                for provider in providers {
                    // `installed` is DISPLAY information, never a filter (FR-1.7): the endpoint
                    // serves a hard-coded nine-entry map against a ten-value enum, so hiding an
                    // uninstalled provider would hide real drift.
                    if provider.installed {
                        lines.push(format!("  {}", provider.name));
                    } else {
                        lines.push(format!("  {} (not installed)", provider.name));
                    }
                }
            }
            PickerState::Failed(error) => lines.push(format!(
                "providers: unavailable — {error}. Press [ctrl+r] to retry"
            )),
        }

        lines
    }

    /// The pane's region, as the operator sees it, plus its state as text.
    ///
    /// # Why this renders the pane rather than reading `ResultsPane::lines()`
    ///
    /// `lines()` returns the **ring buffer** — the captured command output. It is deliberately not
    /// the whole of what the pane displays: the HANDOFF outcome line, the refusal reason, the
    /// copyable argv, the truncation marker, the `empty` notice and the footer exit code all come
    /// from the pane's own `Widget` impl and are **absent from `lines()`**.
    ///
    /// Reading only `lines()` would report an *empty* results region after a successful hand-off,
    /// precisely the "empty pane reads as a failed run" defect the pane's `complete()` branch exists
    /// to prevent. The pane is therefore rendered into an off-screen [`Buffer`] and the rows are
    /// read back. That keeps
    /// the pull-based property (the pane renders *itself*; nothing here reimplements its branches)
    /// and guarantees the frame and the screen cannot disagree — there is one renderer, used twice.
    /// A `Buffer` is a plain data structure, so this needs no terminal. (#321)
    fn results_lines(&self) -> Vec<String> {
        let mut lines = vec![format!("results [{}]", pane_state_word(self.pane.state()))];

        let area = Rect::new(0, 0, self.cols.max(1), self.results_height().max(2));
        let mut buffer = Buffer::empty(area);
        (&self.pane).render(area, &mut buffer);

        for row in 0..area.height {
            let text: String = (0..area.width)
                .map(|column| buffer[(column, row)].symbol())
                .collect();
            // Trailing cell padding is not content; a row of spaces is not a line the operator
            // sees, and keeping it would make `is_blank` weaker than its definition requires.
            let text = text.trim_end().to_string();
            if !text.is_empty() {
                lines.push(text);
            }
        }

        lines
    }

    /// Gating reason, then key hints (FR-6.2, NFR-3).
    ///
    /// The gating reason is **always textual** — never a greyed control with no explanation, which
    /// is the exact failure FR-6.2 names. It comes from `GuidedFlow::blocked_reason()` so the
    /// wording lives with the rule that produces it.
    fn footer_lines(&self) -> Vec<String> {
        let mut lines = Vec::new();

        if self.confirm_quit {
            // Step 10: the run continues server-side, so a silent exit would misrepresent it.
            lines.push(
                "a command is still running — press [ctrl+c] again to quit anyway, [esc] to \
                 stay. The run continues on the server either way"
                    .to_string(),
            );
        }

        match self.flow.blocked_reason() {
            Some(reason) => lines.push(reason),
            None => lines.push("ready — [enter] run".to_string()),
        }

        // The hint must name the key that ACTUALLY works in the current focus. While a text
        // field holds the plain letters, `[r]` and `[q]` are characters, so advertising them would
        // promise an affordance that types instead of acting — the `[c] clear` failure again,
        // where a documented key did nothing. Ctrl+R and Ctrl+C work in EVERY focus, so they are
        // what a text-entry focus advertises. (#321)
        lines.push(if self.focus_is_text_entry() {
            "[tab] focus · [←] commands · [enter] select/run · [ctrl+r] retry · [ctrl+c] quit".to_string()
        } else {
            "[tab] focus · [←] commands · [enter] run · [k] stop following · [ctrl+r] retry · [q] quit"
                .to_string()
        });
        lines
    }

    /// Handles one key. **Infallible; unhandled keys are IGNORED, never errors.**
    ///
    /// Returns whether the key was consumed, so a caller can tell a no-op from an action. The
    /// exhaustive `match` on [`Focus`] is what makes a fifth region a compile error rather than a
    /// region `Tab` silently skips.
    pub fn on_key(&mut self, key: KeyCode) -> bool {
        // The quit confirmation intercepts before anything else: while it is up, `q` means
        // "yes, quit" and `esc` means "stay". Any other key falls through to normal handling,
        // because a modal that swallows every key is a UI the operator cannot escape.
        if self.confirm_quit {
            match key {
                KeyCode::Char('q') => {
                    self.should_quit = true;
                    self.confirm_quit = false;
                    return true;
                }
                KeyCode::Esc => {
                    self.confirm_quit = false;
                    return true;
                }
                _ => {}
            }
        }

        match key {
            KeyCode::Tab => {
                self.focus = self.focus.next();
                return true;
            }
            KeyCode::BackTab => {
                self.focus = self.focus.previous();
                return true;
            }
            // LEFT ARROW: back to the command list from anywhere, in one keystroke.
            //
            // An operator reported being STUCK after `memory list`: Tab, `k` and `q` all appeared
            // to do nothing. Tab was in fact working — but nothing moves focus to `Results` after a
            // run, so focus sat on the form while the filled pane dominated the screen, and with no
            // visible focus indicator the TUI looked frozen. `k` and `q` are TEXT in a form field,
            // so they were silently absorbed into a field the operator could not see.
            //
            // Tab's cycle is four regions and requires knowing which one you are in; Left is
            // unconditional and needs no such model. It is also not otherwise bound, so it costs no
            // existing affordance. Clears the edit buffer for the same reason `select_at_cursor`
            // does — leaving partial text keyed to a cursor the operator has left would resurrect it
            // on return. (#321)
            // The guard is `!self.focus_is_text_entry()` and NOT an inline
            // `matches!(a | b if cond)`: in Rust an `if` guard on a multi-alternative pattern
            // applies to EVERY alternative, so `RequiredFields | OptionalSection if expanded`
            // was false whenever `optional_expanded` was false — i.e. in the default state right
            // after selecting a command. `[q]` then quit the TUI instead of reaching the field,
            // so an agent name containing `q` could not be typed at all. Found by the §12a
            // reviewer; the sole keyboard test typed "planner", which has no `q`. (#321)
            KeyCode::Left => {
                self.focus = Focus::CommandList;
                self.edit_buffer = None;
                return true;
            }
            KeyCode::Char('q') if !self.focus_is_text_entry() => {
                self.request_quit();
                return true;
            }
            // A plain `[r]` is NOT retry. Retry is **Ctrl+R**, handled in the event loop, because
            // `r` must remain typeable in a text field: `"herder"` and `"moderator"` are valid
            // agent names, and the operator is most likely reading a failure banner while that
            // field is focused. Operator decision at the 3.5 gate, after the §12a reviewer found
            // the guard defect. (#321)
            _ => {}
        }

        match self.focus {
            Focus::CommandList => self.on_key_command_list(key),
            Focus::RequiredFields => self.on_key_form(key, true),
            Focus::OptionalSection if self.optional_expanded => self.on_key_form(key, false),
            Focus::OptionalSection => match key {
                KeyCode::Enter | KeyCode::Char(' ') => {
                    self.optional_expanded = true;
                    self.field_cursor = self.flow.guided_field_count();
                    true
                }
                _ => false,
            },
            // `[k]` cancels **only while running**, and `cancel()`'s `NotRunning` is NOT
            // surfaced — `handle_key` swallows it and reports `false`, so pressing `[k]` with
            // nothing running does nothing visible. A key that does not apply is not an
            // operator-facing error.
            Focus::Results => {
                let page = self.results_height().into();
                self.pane.handle_key(key, page)
            }
        }
    }

    /// The single input entry point, modifiers included.
    ///
    /// Extracted from `run()`'s event loop so it is REACHABLE FROM A TEST: the loop itself needs a
    /// real terminal, so a dispatch living only inside it could never be exercised, and Ctrl+R
    /// would be an affordance asserted nowhere. (#321)
    ///
    /// **Ctrl-modified keys are COMMANDS even while a text field is focused**, and are dispatched
    /// to dedicated methods rather than rewritten into printable `KeyCode`s. The previous code
    /// rewrote Ctrl+C into `Char('q')`, which worked only because `[q]` was — wrongly — a command
    /// key in every focus. Now that a plain `q` is text in a field, that rewrite would make Ctrl+C
    /// **type a `q`** instead of quitting.
    fn on_key_event(&mut self, code: KeyCode, modifiers: KeyModifiers) -> bool {
        if modifiers.contains(KeyModifiers::CONTROL) {
            return match code {
                KeyCode::Char('c') => {
                    self.request_quit();
                    true
                }
                KeyCode::Char('r') => self.retry(),
                _ => false,
            };
        }
        self.on_key(code)
    }

    /// Quit, or ask first when a command is still running (step 10).
    ///
    /// Shared by the plain `[q]` key and by Ctrl+C from the event loop, so the two cannot drift
    /// apart — and so Ctrl+C keeps working while a text field is focused, where a plain `q` is
    /// deliberately text. **Confirms rather than exits while running**, because the run continues
    /// server-side and a silent exit would misrepresent what happened. (#321)
    fn request_quit(&mut self) {
        // A SECOND request while the prompt is up means "yes, quit anyway". Without this, Ctrl+C
        // would re-arm the prompt forever and the footer's "press [ctrl+c] again to quit anyway"
        // would be a lie — and in a text-entry focus, where a plain `q` is a character, Ctrl+C is
        // the only key that can answer the prompt at all. (#321)
        if self.confirm_quit {
            self.confirm_quit = false;
            self.should_quit = true;
            return;
        }
        if self.running {
            self.confirm_quit = true;
        } else {
            self.should_quit = true;
        }
    }

    /// Is the focused region one where a printable character is TEXT, not a command key?
    ///
    /// Written as an explicit `match` rather than an inline
    /// `matches!(self.focus, A | B if cond)`, because that form binds the guard to **every**
    /// alternative — which is the defect this replaced: `RequiredFields` was only protected while
    /// `optional_expanded` happened to be true, so in the default state after selecting a command
    /// `[q]` quit the TUI instead of entering a `q`. Here each arm carries its own condition, so
    /// `RequiredFields` is unconditional and only `OptionalSection` depends on expansion. (#321)
    fn focus_is_text_entry(&self) -> bool {
        match self.focus {
            // Always text: this is where `--agents` is typed, and it is the operator's first
            // action after selecting a command.
            Focus::RequiredFields => true,
            // Text only once expanded; collapsed, it is a single activatable control.
            Focus::OptionalSection => self.optional_expanded,
            Focus::CommandList | Focus::Results => false,
        }
    }

    /// The command list's keys: move the cursor, `Enter` selects.
    ///
    /// Selecting resets the form (`GuidedFlow::select`) and starts the two picker fetches
    /// concurrently, which is where NFR-1's 500 ms p95 is spent.
    fn on_key_command_list(&mut self, key: KeyCode) -> bool {
        match key {
            KeyCode::Up => {
                self.cursor = self.cursor.saturating_sub(1);
                true
            }
            KeyCode::Down => {
                self.cursor = (self.cursor + 1).min(self.commands.len().saturating_sub(1));
                true
            }
            KeyCode::Enter => {
                self.select_at_cursor();
                true
            }
            _ => false,
        }
    }

    /// Selects the command under the cursor and populates the pickers.
    ///
    /// `select()`'s `Err(Error::Hidden)` is unreachable here by construction — `self.commands`
    /// came from `commands()`, which filters `Hidden` — and is rendered as a banner rather than
    /// swallowed, because a programmatic reachability defect should be visible rather than silent.
    fn select_at_cursor(&mut self) {
        let Some(command) = self.commands.get(self.cursor).copied() else {
            return;
        };

        if let Err(error) = self.flow.select(command.id) {
            self.banner = Some(Banner::error(
                format!("cannot open a form for {}", render_command_path(&command)),
                error.to_string(),
                "pick another command; this one is not offered in the TUI",
            ));
            return;
        }

        self.banner = None;
        self.focus = Focus::RequiredFields;
        self.field_cursor = 0;
        // The buffer is keyed by cursor index, so moving BETWEEN fields invalidates it for free.
        // A command change is the one case that needs an explicit clear: the cursor resets to 0,
        // so field 0 of the NEW form would otherwise inherit the old form's partial text. (#321)
        self.edit_buffer = None;
        self.optional_expanded = false;
        self.populate_pickers();
    }

    /// Routes form keys to the focused field while keeping execution on an explicit `Enter`.
    ///
    /// Text editing is intentionally small and predictable: printable characters append,
    /// Backspace removes one character, Space toggles flags, and Up/Down move within the current
    /// form region. All validation still happens at [`GuidedFlow::set`], the form's sole mutation
    /// boundary, so keyboard input cannot bypass env parsing or unloadable-profile refusal. (#321)
    fn on_key_form(&mut self, key: KeyCode, guided: bool) -> bool {
        let split = self.flow.guided_field_count();
        let len = self.flow.fields().len();
        let (start, end) = if guided {
            (0, split.min(len))
        } else {
            (split.min(len), len)
        };

        if start == end {
            return if key == KeyCode::Enter {
                self.run_selected()
            } else {
                false
            };
        }

        self.field_cursor = self.field_cursor.clamp(start, end - 1);
        match key {
            KeyCode::Up => {
                self.field_cursor = self.field_cursor.saturating_sub(1).max(start);
                true
            }
            KeyCode::Down => {
                self.field_cursor = (self.field_cursor + 1).min(end - 1);
                true
            }
            KeyCode::Enter => self.run_selected(),
            KeyCode::Char(' ') if self.focused_field_kind() == Some(FieldKind::Flag) => {
                self.toggle_focused_flag()
            }
            KeyCode::Char(character) if !character.is_control() => {
                self.edit_focused_field(Some(character))
            }
            KeyCode::Backspace => self.edit_focused_field(None),
            KeyCode::Esc if !guided => {
                self.optional_expanded = false;
                true
            }
            _ => false,
        }
    }

    /// Runs the selected command through the policy-specific production path.
    fn run_selected(&mut self) -> bool {
        let Some(id) = self.flow.current() else {
            return false;
        };

        match (id, catalog::policy(id)) {
            (CommandId::Launch, Policy::Handoff) => {
                self.pending_action = Some(PendingAction::Launch);
                self.running = true;
                self.banner = Some(Banner::info(
                    "creating the session",
                    "the launch request is ready",
                    "waiting for cao-server",
                ));
            }
            (_, Policy::InApp) => {
                self.pending_action = Some(PendingAction::InApp(id));
                self.running = true;
                self.banner = Some(Banner::info(
                    format!("starting {id:?}"),
                    "the route is ready",
                    "waiting for cao-server",
                ));
            }
            (_, Policy::Handoff) => {
                self.banner = Some(Banner::error(
                    format!("{id:?} is not yet wired for hand-off execution"),
                    "`launch()` is the only specified session-creation path; this command has no \
                     artifact-defined terminal creation mapping",
                    "run it from the CLI for now",
                ));
            }
            (_, Policy::Hidden) => return false,
        }
        true
    }

    /// Executes work queued by [`Self::run_selected`] after its pending frame has been drawn.
    ///
    /// # Why `running` is cleared HERE and not only inside each path
    ///
    /// Both `launch()` and `run_in_app()` are synchronous: when they return, nothing is in
    /// flight. So the flag's lifetime is exactly this call, and clearing it on the way out makes
    /// that structural instead of a per-branch obligation.
    ///
    /// It used to be per-branch, and it leaked. `run_selected` set `running = true`, but
    /// `launch()`'s `Incomplete` early return and `run_in_app()`'s `NotWired`/`NoRoute` arms
    /// returned without resetting it — so `cao launch` with `--agents` empty, `[enter]`, then
    /// `[q]` produced the quit prompt's *"the run continues on the server either way"* for a
    /// request that was never sent. Misreporting system state is the failure mode this crate
    /// treats as the worst available, and the per-branch form gives every future early return a
    /// fresh chance to reintroduce it. (Reported by review on PR #547.)
    ///
    /// The paths still clear the flag at their own network boundaries, which is deliberate
    /// rather than redundant: `launch()` must be false before it renders a hand-off outcome, and
    /// this is the backstop for every arm that returns earlier.
    fn run_pending_action(&mut self) -> bool {
        match self.pending_action.take() {
            Some(PendingAction::Launch) => self.launch(),
            Some(PendingAction::InApp(id)) => self.run_in_app(id),
            None => return false,
        }
        // Unconditional: reached by every arm above, including the ones that returned early.
        self.running = false;
        true
    }

    fn focused_field_kind(&self) -> Option<FieldKind> {
        self.flow
            .fields()
            .get(self.field_cursor)
            .map(|field| field.kind)
    }

    fn toggle_focused_flag(&mut self) -> bool {
        let value = self
            .flow
            .fields()
            .get(self.field_cursor)
            .and_then(|field| field.value.as_ref())
            .and_then(|value| match value {
                guided_flow::FieldValue::Flag(value) => Some(!value),
                _ => None,
            })
            .unwrap_or(true);
        self.set_focused_field(if value { "true" } else { "false" })
    }

    fn edit_focused_field(&mut self, character: Option<char>) -> bool {
        if self.focused_field_kind() == Some(FieldKind::Flag) {
            return false;
        }

        let Some(field) = self.flow.fields().get(self.field_cursor) else {
            return false;
        };

        // Reads from the EDIT BUFFER, not from the stored field, when one is live for this field.
        //
        // `GuidedFlow::set` stores a trimmed value — an affirmed rule (BR-7/BR-8), and a test pins
        // that `"  planner  "` is stored as `"planner"` rather than rejected. But the renderer
        // appends ONE character per keystroke and calls `set()` each time, so a trailing space was
        // trimmed away before the next character arrived: `"code"` + `' '` round-tripped to
        // `"code"`, and the operator got `"codereview"` instead of `"code review"`. Typing a space
        // was impossible in any text field.
        //
        // The buffer keeps the in-progress text verbatim so a trailing space survives long enough
        // to be typed past, while `set()` keeps storing the trimmed value — so the wire never sees
        // padding and BR-7/BR-8 are untouched. An operator hit this on `profile find`. (#321)
        let mut value = match &self.edit_buffer {
            Some((index, buffered)) if *index == self.field_cursor => buffered.clone(),
            _ => field_value_text(field),
        };
        match character {
            Some(character) => value.push(character),
            None => {
                value.pop();
            }
        }

        // A value that is entirely whitespace is not worth buffering: it collapses to None either
        // way, and holding it would make the field render as non-empty when it is not.
        self.edit_buffer = if value.trim().is_empty() {
            None
        } else {
            Some((self.field_cursor, value.clone()))
        };

        self.set_focused_field(&value)
    }

    fn set_focused_field(&mut self, value: &str) -> bool {
        let Some(name) = self
            .flow
            .fields()
            .get(self.field_cursor)
            .map(|field| field.name)
        else {
            return false;
        };

        match self.flow.set(name, value) {
            Ok(()) => {
                self.banner = None;
                true
            }
            Err(error) => {
                self.banner = Some(Banner::error(
                    format!("cannot set {name}"),
                    error.to_string(),
                    "correct the value and try again",
                ));
                true
            }
        }
    }

    /// Starts both picker fetches concurrently (FR-1.1, FR-1.2, NFR-1).
    ///
    /// Also refreshes the header's server indicator. The `health()` read is what turns a failed
    /// picker into a *diagnosable* failure: the operator sees the address the client is pointed
    /// at, which is the whole reason `CAO_API_HOST`/`CAO_API_PORT` are configurable (SR-4).
    fn populate_pickers(&mut self) {
        self.health = Some(
            self.server
                .health()
                // `server_failure_summary`, not `to_string()`: an auth rejection needs to read as
                // an auth rejection. See that function for why the raw `Display` was wrong here.
                .map_err(|error: TuiError| server_failure_summary(&error)),
        );
        self.retryable = Some(Retryable::Pickers);

        if let Some(launch) = self.picker_launcher.as_ref() {
            self.picker_feed = Some(launch(&mut self.flow));
            return;
        }

        // Deterministic fallback for injected single-threaded fakes. Production always installs
        // `picker_launcher` in `main`, so its two reads take the concurrent path above.
        let profiles = self.server.profiles();
        let providers = self.server.providers();
        self.flow
            .apply_picker_update(guided_flow::PickerUpdate::Agents(profiles));
        self.flow
            .apply_picker_update(guided_flow::PickerUpdate::Providers(providers));
    }

    /// Waits for the in-flight picker answers, bounded. Returns how many arrived.
    ///
    /// Bounded per receive rather than by one overall budget, for `guided-flow`'s reason: an
    /// unbounded `recv()` would hang the event loop on a fetch that never reports.
    pub fn await_pickers(&mut self) -> usize {
        let Some(feed) = self.picker_feed.as_ref() else {
            return 0;
        };
        self.flow.await_pickers(feed, PICKER_DRAIN_TIMEOUT)
    }

    /// `[r]`: retry **in place**, with form values preserved (FR-6.3).
    ///
    /// # Preserving the form is the requirement, not a nicety
    ///
    /// Losing typed input on a transient failure pushes the operator back to the CLI — the
    /// opposite of this intent's purpose. So retry re-issues the *operation* and touches
    /// `self.flow`'s field values nowhere. The one thing it does reset is the pickers, back to
    /// `Loading`, because their previous answer was the failure being retried.
    ///
    /// Note what is deliberately absent: no `select()` call, which resets the form, and no
    /// `GuidedFlow::new()`. Both would compile and both would silently discard the operator's
    /// input while the retry looked correct. (#321)
    fn retry(&mut self) -> bool {
        let Some(retryable) = self.retryable.clone() else {
            return false;
        };

        self.banner = None;

        match retryable {
            Retryable::Pickers => {
                self.populate_pickers();
                true
            }
            Retryable::Launch => {
                self.launch();
                true
            }
            Retryable::InApp(id) => {
                self.run_in_app(id);
                true
            }
        }
    }

    /// **The `launch()` orchestration — four steps, each ending in a rendered state.**
    ///
    /// Infallible: every branch renders. This is the sequence a 2.7 reviewer found unspecified,
    /// and this unit is the only place it exists.
    ///
    /// ```text
    /// 1. GuidedFlow::to_params()
    ///      Err(Incomplete{missing}) -> "blocked: {missing} required" [FR-6.2]; STOP
    /// 2. ServerClient::create_session(params)          // expects 201, not 200
    ///      Err(Http(5xx)) | Err(Unreachable) -> hard error; STOP     [FR-6.1]
    ///      Err(Validation(d))                -> the rejected field;  STOP
    /// 3. HandoffDriver::await_ready(terminal.id)       // THREE-way, never two [FR-5.4]
    ///      Ready          -> continue to 4
    ///      Unknown        -> WARNING, then CONTINUE to 4             [ADR-04]
    ///      Failed(status) -> hard error; STOP
    /// 4. HandoffDriver::handoff(terminal)
    ///      Ok(Outcome)          -> ResultsPane::complete(exit, Some(line))   // FR-3.2
    ///      Err(Refused{r, cmd}) -> ResultsPane::refuse(r, cmd)      [FR-5.3]  // FR-3.2
    /// ```
    ///
    /// **Step 3's `Unknown` arm CONTINUES**, and that is the whole reason `Readiness` is a
    /// three-state type rather than a `Result`: a two-state return forces `Unknown` into the error
    /// arm, the exact conflation ADR-04 exists to prevent. The operator's ruling was verbatim
    /// *"just warning message, don't really do anything, user should retry if not succeed"*.
    /// Folding it into the error arm compiles, looks tidier, and burns a healthy launch.
    ///
    /// **Step 2's failure renders a banner, not a pane method.** `ResultsPane` has exactly four
    /// methods and none is an error setter — see [`Banner`].
    pub fn launch(&mut self) {
        self.retryable = Some(Retryable::Launch);
        self.banner = None;

        // Step 1. `Incomplete` carries the field list, so the stated reason names the field
        // without re-deriving it (FR-6.2, BR-19).
        let params = match self.flow.to_params() {
            Ok(params) => params,
            Err(guided_flow::Error::Incomplete { missing }) => {
                self.banner = Some(Banner::error(
                    blocked_reason(&missing),
                    "the CLI requires these parameters, so the request would be rejected",
                    "fill them in and press [enter] again",
                ));
                return;
            }
            Err(error) => {
                self.banner = Some(Banner::error(
                    "cannot build the launch request",
                    error.to_string(),
                    "select `cao launch` and fill in the form",
                ));
                return;
            }
        };

        // A pending state before the first network call (PR-3, `frontend-components.md:102`): a
        // frozen UI is indistinguishable from a crash. This is renderer-owned frame state, NOT a
        // fifth pane method — the pane has no stream at this point.
        self.running = true;
        self.pane.attach(Policy::Handoff);

        // Step 2. 201, not 200 — `create_session` enforces that; the branch here is on the error
        // vocabulary, which `error.rs` says exists to choose a rendered state.
        let terminal = match self.server.create_session(&params) {
            Ok(terminal) => terminal,
            Err(error) => {
                self.running = false;
                self.banner = Some(create_session_banner(&error));
                return;
            }
        };

        // Step 3. Three-way. The driver is constructed HERE rather than held as a field, so the
        // backend `OnceCell` is resolved on first use and a server that was down at startup does
        // not prevent the TUI from opening (FR-6.1).
        let driver = HandoffDriver::new(self.server, self.host);

        match driver.await_ready(&terminal.id) {
            Readiness::Ready => {}
            // ADR-04: warn, then CONTINUE. Not an error arm.
            Readiness::Unknown => {
                self.banner = Some(Banner::warning(
                    "the agent did not report ready within 30 seconds",
                    "cao-server gave no conclusive status; unknown is not the same as failed",
                    "handing off anyway — if the new window is not ready, retry with [r]",
                ));
            }
            Readiness::Failed(status) => {
                self.running = false;
                self.banner = Some(Banner::error(
                    "the agent failed to launch",
                    format!("cao-server reports terminal status {status:?}"),
                    "check the agent profile and the provider, then retry with [r]",
                ));
                return;
            }
        }

        // Step 4. **FR-3.2 site one**: both arms terminate in a named `ResultsPane` call from
        // production code.
        self.running = false;
        match driver.handoff(&terminal) {
            Ok(outcome) => {
                let line = format!(
                    "launched in new window · session: {} · window: {}",
                    outcome.session, outcome.window
                );
                // HANDOFF's completion shape: the structured outcome line. The command's output
                // went to the new window, so the pane's own buffer is expected to be empty and
                // `complete` deliberately ignores emptiness on this branch.
                self.pane.complete(0, Some(line));
            }
            Err(refused) => {
                // FR-5.3: the reason plus the copyable exact argv, and nothing implying the
                // hand-off happened (SR-4).
                self.pane
                    .refuse(refused.reason.clone(), refused.manual_command.clone());
            }
        }
    }

    /// **The in-app run path — FR-3.2 site two.** `attach` → `push_bytes` → `complete`.
    ///
    /// The `Write` adapter is the resolution of a design-vs-code gap recorded at the plan gate:
    /// `business-logic-model.md` describes `attach(stream, policy)`, but there is **no `Stream`
    /// type in the crate** — `run()` writes to a `Write` sink and `attach()` takes only a policy,
    /// with bytes arriving via `push_bytes`. Bridging them is a better fit than the design
    /// describes: `Write::write` is called *as bytes arrive*, so incrementality is structural
    /// (PR-1) rather than something a reviewer has to verify, and `run()` returns the status code,
    /// so `complete()`'s exit code comes from the same call rather than a second source.
    ///
    /// The adapter is [`PaneSink`], not `&mut self.pane` directly, for one reason: `ResultsPane`
    /// already implements `Write`, so passing it would work — but then nothing observes the
    /// forwarding, and PR-1's test could not distinguish incremental from buffered. `PaneSink`
    /// wraps the pane and counts writes, which is what makes the incrementality assertion possible
    /// without changing the production path. (#321)
    /// `profile find`, served client-side (OQ-6 Q2) — the one routeless IN-APP command.
    ///
    /// Reads the query from the form's own field rather than inventing a second input, and renders
    /// through the SAME pane path every other IN-APP command uses, so FR-3.2's production-caller
    /// obligation holds here too: `attach` -> `push_bytes` -> `complete`.
    ///
    /// A no-match is `exit 0` with a stated "no profiles matched", NOT an error: the search worked
    /// and the answer is empty. Reporting a successful empty search as a failure is the same
    /// misrepresentation as an empty HANDOFF pane reading as a failed run.
    fn run_profile_find(&mut self) {
        // `cao profile find` takes a positional KEYWORD, so accept either spelling rather than
        // guessing one.
        let query = self
            .flow
            .fields()
            .iter()
            .find(|field| {
                field.name == "keyword" || field.name == "--keyword" || field.name == "KEYWORD"
            })
            .map(field_value_text)
            .unwrap_or_default()
            .to_lowercase();

        self.running = true;
        self.pane.attach(Policy::InApp);

        // Filters `profiles()` rather than calling `ServerClient::find_profiles`: the renderer holds
        // an injected `S: ServerApi`, and that trait exposes `profiles()`. Widening the trait for one
        // command would force every test double to implement a method only this path uses. The
        // substring rule is identical either way, and this keeps the injection seam intact.
        let outcome = self.server.profiles();
        self.running = false;

        match outcome {
            Ok(all) => {
                let profiles: Vec<&Profile> = all
                    .iter()
                    .filter(|profile| profile_searchable_text(profile).contains(&query))
                    .collect();
                let mut rendered = String::new();
                if profiles.is_empty() {
                    rendered.push_str(&format!("no profiles matched {query:?}\n"));
                } else {
                    for profile in &profiles {
                        // The unselectable marker travels with the row (FR-1.5): an unloadable
                        // profile is SHOWN and explained, never filtered out silently.
                        let marker = if profile.loadable {
                            ""
                        } else {
                            "  [not loadable]"
                        };
                        rendered.push_str(&format!("{}{marker}\n", profile.name));
                    }
                }
                self.pane.push_bytes(rendered.as_bytes());
                self.pane.complete(0, None);
            }
            Err(error) => {
                self.pane.push_bytes(error.to_string().as_bytes());
                self.pane.complete(1, None);
                self.banner = Some(Banner::error(
                    "could not read the profile list".to_string(),
                    error.to_string(),
                    "check that cao-server is reachable, then retry with [ctrl+r]",
                ));
            }
        }
    }

    pub fn run_in_app(&mut self, id: CommandId) {
        self.retryable = Some(Retryable::InApp(id));
        self.banner = None;

        // Belt and braces against a programmatic caller: `commands()` already excludes HIDE rows,
        // so the operator cannot select one, but a caller holding a `CommandId` from elsewhere can
        // reach here. A HIDE command must not run.
        let policy = catalog::policy(id);
        if policy != Policy::InApp {
            self.banner = Some(Banner::error(
                format!("{id:?} does not run in-app"),
                format!("its run policy is {policy:?}"),
                match policy {
                    Policy::Handoff => "run it with [enter] on the launch form instead",
                    _ => "this command is not offered in the TUI",
                },
            ));
            return;
        }

        // The stated gap. `NotWired`, `Ignored` and `NoRoute` render an error that NAMES what is
        // missing — the operator can see the limit rather than meeting a picker that half-works.
        match in_app_readiness(id, &self.flow) {
            InAppReadiness::Runnable => {}
            InAppReadiness::NotWired { placeholders } => {
                self.banner = Some(Banner::error(
                    format!("{id:?} is not yet wired for in-app execution"),
                    format!(
                        "its route needs the path value(s) {placeholders:?}, which no form field \
                         supplies — the CLI resolves them with a second call this TUI does not \
                         make yet"
                    ),
                    "run it from the CLI for now; this is a stated limit, not a failure",
                ));
                return;
            }
            // The honesty gate. Enter is refused rather than firing a request that would drop what
            // the operator typed — the reported defect was a form that taught operators their
            // input mattered when it did not. Naming the fields is what makes this a stated limit
            // rather than a dead end. (Review on PR #547.)
            InAppReadiness::Ignored { fields } => {
                self.banner = Some(Banner::error(
                    format!("{id:?} cannot send {fields:?} in-app"),
                    format!(
                        "you filled in {fields:?}, and this route has no parameter for them, so \
                         running it here would silently ignore what you typed"
                    ),
                    "clear those fields to run without them, or run it from the CLI",
                ));
                return;
            }
            // `profile find` is the ONE routeless IN-APP command, and the operator's OQ-6 Q2
            // decision was to serve it CLIENT-SIDE: a case-insensitive substring filter over
            // `GET /agents/profiles`, deliberately not a BM25Plus port. `ServerClient::find_profiles`
            // implements it — but nothing called it from production, so this arm reported "no HTTP
            // route" and the approved behaviour was unreachable. That is design defect #3's shape
            // again: working code with no production caller. (#321)
            InAppReadiness::NoRoute if id == CommandId::ProfileFind => {
                self.run_profile_find();
                return;
            }
            InAppReadiness::NoRoute => {
                self.banner = Some(Banner::error(
                    format!("{id:?} has no HTTP route"),
                    "cao-server serves it no other way, and ADR-02 forbids the subprocess that \
                     would be the only alternative"
                        .to_string(),
                    "run it from the CLI",
                ));
                return;
            }
        }

        self.running = true;
        // FR-3.2 site two, call one: `attach` from PRODUCTION code.
        self.pane.attach(Policy::InApp);

        // The block scopes `sink`'s mutable borrow of the pane, so `complete()` below can borrow it
        // again. Not a stylistic block: without it the borrow lives to the end of the function and
        // the `complete` call does not compile.
        // Buffers through `JsonSink` so a JSON body can be pretty-printed, then pushes the
        // rendered text into the pane in ONE write.
        //
        // This is the one place PR-1's incrementality is deliberately traded for readability, and
        // it costs nothing measurable: zero of the 21 routed IN-APP commands uses
        // `StreamingResponse` (measured at 3.6), so there is no incremental arrival to lose. A
        // non-JSON body passes through unchanged. `PaneSink` remains the streaming path and is
        // still what `push_bytes` is exercised through elsewhere. (#321)
        // The form's values, resolved through the route's own bindings. Owned `String`s first
        // because `run` takes `&[&str]`/`&[(&str, &str)]` and the values come out of the form by
        // value; the borrowed views are built from these and must outlive the call.
        //
        // This replaced `run(id, &[], &[], None, ..)` — literally no path values and no query.
        // That call was why `memory list --scope X` ran as a scan-all and `memory clear`'s
        // REQUIRED `--scope` was dropped into a guaranteed 422. Readiness has already established
        // that every placeholder is bound and that nothing filled would be ignored, so what is
        // built here is complete by construction rather than by hoping. (Review on PR #547.)
        let path_values: Vec<String> = crate::server::path_values_for(id, &self.flow);
        let query_pairs: Vec<(&'static str, String)> =
            crate::server::query_pairs_for(id, &self.flow);
        let path_refs: Vec<&str> = path_values.iter().map(String::as_str).collect();
        let query_refs: Vec<(&str, &str)> = query_pairs
            .iter()
            .map(|(wire, value)| (*wire, value.as_str()))
            .collect();

        let mut sink = JsonSink::new();
        let status = self
            .server
            .run(id, &path_refs, &query_refs, None, &mut sink);
        self.pane.push_bytes(sink.rendered().as_bytes());

        self.running = false;
        match status {
            // The exit code comes from `run()`'s own return value — the same call that streamed
            // the bytes, which is what the `Write`-adapter resolution buys.
            Ok(code) => self.pane.complete(i32::from(code >= 400), None),
            Err(error) => {
                // T-6: bytes already handed to the pane stay rendered, so the operator sees the
                // partial output AND the failure. Completing with a non-zero code rather than
                // leaving the pane `running` forever is what keeps the state honest.
                self.pane.complete(1, None);
                self.banner = Some(Banner::error(
                    format!("{id:?} did not complete"),
                    error.to_string(),
                    "the output above is what arrived before the failure. Retry with [r]",
                ));
            }
        }
    }

    /// Re-layouts for a new terminal size. **Infallible, and the pane keeps its buffer.**
    ///
    /// The pane's ring buffer and scroll offset are untouched here, which is the edge case
    /// `business-logic-model.md:191` names: a resize mid-run must not discard output or move the
    /// operator's position. Wrapping is display-only (`Wrap { trim: false }`), so a narrower
    /// terminal re-wraps the same buffer rather than truncating it.
    pub fn resize(&mut self, cols: u16, rows: u16) {
        self.cols = cols;
        self.rows = rows;
    }

    /// The results region's height, for `PageUp`/`PageDown`.
    ///
    /// `max(1)` because a zero page height would make paging a no-op, and a key that silently does
    /// nothing is the defect the `[c]`-vs-`[k]` amendment was written about.
    fn results_height(&self) -> u16 {
        match LayoutMode::of(self.cols, self.rows) {
            LayoutMode::TwoColumn => (self.rows / 2).max(1),
            LayoutMode::Stacked => (self.rows / 3).max(1),
        }
    }

    /// Has the operator asked to quit, and been confirmed if a command was running?
    pub fn should_quit(&self) -> bool {
        self.should_quit
    }

    /// Is a quit confirmation on screen (Step 10)?
    pub fn awaiting_quit_confirmation(&self) -> bool {
        self.confirm_quit
    }

    /// The focused region.
    pub fn focus(&self) -> Focus {
        self.focus
    }

    /// The banner, when one is rendered.
    pub fn banner(&self) -> Option<&Banner> {
        self.banner.as_ref()
    }

    /// The results pane, for a caller that needs to read its state.
    pub fn pane(&self) -> &ResultsPane {
        &self.pane
    }

    /// The form, for a caller that needs to read or fill field values.
    pub fn flow_mut(&mut self) -> &mut GuidedFlow {
        &mut self.flow
    }

    /// The form, read-only.
    pub fn flow(&self) -> &GuidedFlow {
        &self.flow
    }

    /// Moves the cursor to `id`, for a caller driving the list programmatically.
    ///
    /// Returns whether the command is in the navigable list — `false` for a HIDE command, which is
    /// FR-4.3 observable: a hidden command is not merely unselectable, it is **not there**.
    pub fn focus_command(&mut self, id: CommandId) -> bool {
        match self.commands.iter().position(|command| command.id == id) {
            Some(index) => {
                self.cursor = index;
                true
            }
            None => false,
        }
    }

    /// Renders [`Self::render`]'s frame into a ratatui buffer.
    ///
    /// Separated from `render()` so "never blank" is assertable on the frame value without a
    /// terminal, and so the widget path and the assertion path see the same data rather than two
    /// implementations of the layout.
    ///
    /// `Wrap { trim: false }` everywhere is NFR-6's "wrap, never truncate", and `trim: false`
    /// specifically preserves leading whitespace so the banner's indented `cause:`/`remedy:` lines
    /// stay readable when they wrap.
    pub fn draw(&self, area: Rect, buf: &mut Buffer) {
        if area.is_empty() {
            return;
        }

        let frame = self.render();

        let [header, main, footer] = Layout::vertical([
            Constraint::Length(frame.header.len().max(1) as u16),
            Constraint::Min(1),
            Constraint::Length(frame.footer.len().max(1) as u16),
        ])
        .areas(area);

        paragraph(&frame.header).render(header, buf);
        paragraph(&frame.footer).render(footer, buf);

        let mut left: Vec<String> = Vec::new();
        left.extend(frame.command_list.iter().cloned());
        left.extend(frame.required_fields.iter().cloned());
        left.extend(frame.optional_section.iter().cloned());
        left.extend(frame.pickers.iter().cloned());
        left.extend(frame.banner.iter().cloned());

        match frame.layout {
            LayoutMode::TwoColumn => {
                let [form_area, results_area] =
                    Layout::horizontal([Constraint::Percentage(60), Constraint::Percentage(40)])
                        .areas(main);
                paragraph(&left).render(form_area, buf);
                // The pane renders ITSELF (it is a `Widget`), so its buffer, scroll position and
                // state wording come from the pane rather than from a copy here.
                self.render_results(results_area, buf);
            }
            LayoutMode::Stacked => {
                // NFR-6: a single column, results BELOW the form. Degraded, fully usable.
                let [form_area, results_area] = Layout::vertical([
                    Constraint::Min(1),
                    Constraint::Length(
                        self.results_height()
                            .min(main.height.saturating_sub(1))
                            .max(1),
                    ),
                ])
                .areas(main);
                paragraph(&left).render(form_area, buf);
                self.render_results(results_area, buf);
            }
        }
    }

    /// Renders the pane, or its state word when the area is too small for the pane itself.
    ///
    /// The fallback matters for NFR-6: `ResultsPane::render` returns early on an empty area, so a
    /// one-row region at 70x20 would render nothing there. Writing the state word instead keeps
    /// the region populated — a blank region is the same defect as a blank frame, one region down.
    fn render_results(&self, area: Rect, buf: &mut Buffer) {
        if area.is_empty() {
            return;
        }
        if area.height < 2 {
            buf.set_string(
                area.x,
                area.y,
                format!("results [{}]", pane_state_word(self.pane.state())),
                Style::default(),
            );
            return;
        }
        (&self.pane).render(area, buf);
    }
}

/// A `Paragraph` over owned lines, wrapping rather than truncating (NFR-6).
fn paragraph(lines: &[String]) -> Paragraph<'_> {
    Paragraph::new(lines.join("\n")).wrap(Wrap { trim: false })
}

/// The pane's state as a word, so no state is conveyed by colour alone (NFR-3).
fn pane_state_word(state: PaneState) -> &'static str {
    match state {
        PaneState::Collapsed => "collapsed",
        PaneState::Running => "running",
        PaneState::Complete => "complete",
        PaneState::Empty => "empty",
        PaneState::Cancelled => "cancelled",
        PaneState::Refused => "refused",
    }
}

/// `cao session list`, from a catalog row. `None` parent means a top-level leaf.
fn render_command_path(command: &Command) -> String {
    match command.parent {
        Some(parent) => format!("cao {parent} {}", command.leaf_name),
        None => format!("cao {}", command.leaf_name),
    }
}

/// One form field as a line. A positional is rendered **without** a `--` prefix (BR-4).
///
/// `Field::is_positional` is what decides, rather than a second check on the name — the field kind
/// is the entity's own statement of the distinction, and `message` acquiring a `--` is exactly
/// what BR-4 forbids.
///
/// `command` is taken so a field the endpoint has no parameter for can say so in the line itself
/// (`crate::guided_flow::NOT_SENT_MARKER`). Five `cao launch` flags are in that position; before this they
/// rendered identically to the wired ones, so an operator ticking `--auto-approve` had no way to
/// learn it would not be applied until an approval prompt appeared in the new window. The
/// *command* is what makes the claim safe to print — `--async` also exists on `cao session send`,
/// where "not sent" would be false. (Reported by review on PR #547.)
fn render_field(field: &Field, focused: bool, command: Option<CommandId>) -> String {
    let marker = if focused { ">" } else { " " };
    let label = if field.is_positional() {
        format!("<{}>", field.name.trim_start_matches('-'))
    } else {
        field.name.to_string()
    };
    let requirement = if field.required { " (required)" } else { "" };
    let value = match &field.value {
        None => "—".to_string(),
        Some(guided_flow::FieldValue::Text(text)) => text.clone(),
        Some(guided_flow::FieldValue::Flag(set)) => set.to_string(),
        Some(guided_flow::FieldValue::EnvPairs(pairs)) => pairs
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .collect::<Vec<_>>()
            .join(" "),
    };
    let kind = match field.kind {
        FieldKind::Flag => "flag",
        FieldKind::Text => "text",
        FieldKind::Positional => "positional",
    };
    let unwirable = if guided_flow::is_unwirable_launch_flag(command, field.name) {
        format!("  [{}]", guided_flow::NOT_SENT_MARKER)
    } else {
        String::new()
    };

    format!("{marker} {label}{requirement} [{kind}]: {value}{unwirable}")
}

/// The four fields Python's `_searchable_text` tokenizes, lowercased for a case-insensitive match.
///
/// `name`, `description`, `tags`, `capabilities` — matching `profile_search.py:43-51`. Deliberately
/// NOT a BM25Plus port (OQ-6 Q2): reimplementing the ranking invites silent divergence from the
/// Python scorer, and a search that ranks differently while claiming parity is worse than one that
/// plainly filters. (#321)
fn profile_searchable_text(profile: &Profile) -> String {
    let mut text = String::new();
    text.push_str(&profile.name);
    text.push(' ');
    if let Some(description) = profile.description.as_deref() {
        text.push_str(description);
    }
    for tag in &profile.tags {
        text.push(' ');
        text.push_str(tag);
    }
    for capability in &profile.capabilities {
        text.push(' ');
        text.push_str(capability);
    }
    text.to_lowercase()
}

/// Converts the focused value back to the text accepted by [`GuidedFlow::set`].
fn field_value_text(field: &Field) -> String {
    match &field.value {
        None => String::new(),
        Some(guided_flow::FieldValue::Text(text)) => text.clone(),
        Some(guided_flow::FieldValue::Flag(value)) => value.to_string(),
        Some(guided_flow::FieldValue::EnvPairs(pairs)) => pairs
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .collect::<Vec<_>>()
            .join(" "),
    }
}

/// FR-6.2's stated gating reason, naming every unset required field.
///
/// Derived from the `Incomplete` payload rather than by asking `missing()` a second time, which is
/// what `Error::Incomplete` carrying `Vec<Field>` is for (BR-19). Matches
/// `GuidedFlow::blocked_reason`'s wording deliberately: the footer and the banner must not
/// disagree about why the run is blocked.
#[allow(dead_code)] // called by `launch`'s step-1 arm. (#321)
fn blocked_reason(missing: &[Field]) -> String {
    let names: Vec<&str> = missing.iter().map(|field| field.name).collect();
    format!("blocked: {} required", names.join(", "))
}

/// One line describing a failed server read, **labelled by what actually went wrong**.
///
/// # Why this exists rather than `error.to_string()`
///
/// The header prefixed every failure with "unreachable". CAO's auth is opt-in and off by default,
/// so that was true for the common case — but with auth enabled `get_current_scopes` answers
/// **401** for a missing or invalid token and `require_any_scope` answers **403** for insufficient
/// scope (`security/auth.py:435-440`), and this client sends no `Authorization` header at all. The
/// operator therefore saw `server: unreachable — cao-server returned HTTP 401`: a server that
/// plainly answered, described as unreachable, with a "start cao-server / check the address"
/// remedy that cannot possibly work. A self-contradicting diagnosis is worse than a vague one,
/// because it sends the reader somewhere else entirely.
///
/// 401 and 403 are kept **separate**, because the actions differ: 401 means no usable credential
/// was presented, 403 means the credential is real but lacks the scope. Collapsing them into
/// "auth failed" would leave the operator guessing which.
///
/// This does not add authentication — a bearer-token passthrough is a larger change with its own
/// configuration surface. It makes the existing failure legible, which is the part that was
/// actively misleading. (Reported by review on PR #547.)
fn server_failure_summary(error: &TuiError) -> String {
    match error {
        TuiError::Http(401) => "authentication required — cao-server answered HTTP 401 and \
                                cao-tui sends no credential; run it against a server with auth \
                                disabled, or use the CLI, which does"
            .to_string(),
        TuiError::Http(403) => "not authorised — cao-server answered HTTP 403, so the credential \
                                it saw lacks the scope this read needs"
            .to_string(),
        // Every other status: the server answered, so "unreachable" is still the wrong word.
        TuiError::Http(code) => format!("cao-server answered HTTP {code}"),
        // The genuinely-unreachable case. `Unreachable`'s own `Display` names the address tried
        // and the `CAO_API_HOST` remedy, so it is passed through rather than paraphrased.
        TuiError::Unreachable(detail) => format!("unreachable — {detail}"),
        other => other.to_string(),
    }
}

/// The rendered state for each `create_session` failure (`launch()` step 2).
///
/// One arm per error variant this call can produce, which is what `error.rs` means by "this enum
/// IS the boundary contract": `Unreachable` and `Http` call for different remedies (start the
/// server vs. read the status), and `Validation` names a rejected *field*, whose remedy is editing
/// the form. Collapsing them into one message would throw away the distinction the error type
/// exists to carry.
#[allow(dead_code)] // called by `launch`'s step-2 arm. (#321)
fn create_session_banner(error: &TuiError) -> Banner {
    match error {
        // FR-6.1: cause, address, remedy. The `Unreachable` message already names the address
        // actually tried and the `CAO_API_HOST` remedy, so it is passed through rather than
        // paraphrased — a paraphrase would drop the address.
        TuiError::Unreachable(detail) => Banner::error(
            "could not create the session",
            detail.clone(),
            "start `cao-server`, or point CAO_API_HOST / CAO_API_PORT at a running instance, \
             then press [r] to retry — your entries are preserved",
        ),
        TuiError::Validation(detail) => Banner::error(
            "cao-server rejected the launch request",
            detail.clone(),
            "fix the named field and press [enter] again",
        ),
        // Auth, named, and placed BEFORE the `>= 500` arm. The guard already excludes 401/403, so
        // the order is not what makes this correct — it is what makes it readable: a reader
        // scanning for "how is 401 handled" should not have to evaluate a numeric guard first.
        //
        // Retry is deliberately NOT the remedy: `[r]` re-issues the identical credential-free
        // request, so offering it would loop the operator through a failure that cannot change.
        // The same misdiagnosis the header carried. (Review on PR #547.)
        TuiError::Http(401) => Banner::error(
            "cao-server requires authentication",
            "it answered HTTP 401, and cao-tui sends no credential".to_string(),
            "retrying will not help — use the CLI, which authenticates, or point \
             CAO_API_HOST / CAO_API_PORT at a server with auth disabled",
        ),
        TuiError::Http(403) => Banner::error(
            "cao-server refused the launch",
            "it answered HTTP 403, so the credential it saw lacks the scope this needs".to_string(),
            "retrying will not help — grant the write scope, or run `cao launch` from the CLI",
        ),
        TuiError::Http(code) if *code >= 500 => Banner::error(
            "cao-server failed while creating the session",
            format!("it answered HTTP {code}"),
            "check the cao-server log, then press [r] to retry",
        ),
        other => Banner::error(
            "could not create the session",
            other.to_string(),
            "press [r] to retry; your entries are preserved",
        ),
    }
}

/// Whether `id`'s route can be built from `flow`'s current values — **the stated in-app gap**.
///
/// Derived from `ServerClient`'s own route table via [`crate::server::unbound_placeholders`] and
/// [`crate::server::unbound_text_fields`], so there is exactly one source of truth for which
/// routes need what. A hard-coded id list here would be a second place for the same fact, free to
/// drift silently — and the drift would present as a 404 on a literal `{run_id}` brace.
///
/// # It takes the form, and that is the whole correction
///
/// It used to take only the `CommandId` and answer "does this route have placeholders?" — which
/// made every `{token}` route unreachable regardless of whether a field supplied the value, and
/// said nothing at all about a *filled* field the request would drop. Both halves of the reported
/// defect lived in that signature.
///
/// The order of the checks is deliberate: an unsupplied path value is a harder blocker than an
/// ignored one, and reporting "needs `{terminal_id}`" is more useful than listing the four fields
/// that would also be dropped along the way.
///
/// **Measured after the fix: 21 `Runnable`, 2 `NotWired`, 1 `NoRoute`** (on an empty form) —
/// `the_in_app_gap_is_twentyone_runnable_two_not_wired_and_one_routeless` pins the real numbers
/// rather than a remembered figure. (#321, and review on PR #547)
#[allow(dead_code)] // called by `run_in_app`; asserted directly by the gap test. (#321)
pub fn in_app_readiness(id: CommandId, flow: &GuidedFlow) -> InAppReadiness {
    let Some(unbound) = crate::server::unbound_placeholders(id) else {
        return InAppReadiness::NoRoute;
    };
    if !unbound.is_empty() {
        return InAppReadiness::NotWired {
            placeholders: unbound,
        };
    }

    // Only fields the operator actually FILLED. An unbound field left blank costs nothing, so
    // refusing on it would block commands that work.
    let ignored: Vec<&'static str> = crate::server::unbound_text_fields(id)
        .into_iter()
        .filter(|name| flow.field(name).is_some_and(|field| field.value.is_some()))
        .collect();
    if !ignored.is_empty() {
        return InAppReadiness::Ignored { fields: ignored };
    }

    InAppReadiness::Runnable
}

/// The `Write` adapter: `ServerClient::run`'s sink, forwarding into `ResultsPane::push_bytes`.
///
/// # Why this exists when `ResultsPane` already implements `Write`
///
/// Passing `&mut pane` would compile and work. It would also make PR-1 unprovable: nothing would
/// observe *when* bytes reached the pane, so a buffered implementation and an incremental one
/// would be indistinguishable from the outside. This wrapper forwards each `write` straight
/// through — no buffering, no line splitting — and counts the forwards, which is the observation
/// the incremental test needs.
///
/// `flush` is a no-op for the same reason it is on the pane: the `vte` parser emits into the ring
/// buffer eagerly, so nothing is held back. **This is deliberately not a `BufWriter` or a
/// `LineWriter`** — `strip-ansi-escapes` 0.2.1 wraps its sink in `std::io::LineWriter`, which
/// withholds a newline-less `Loading 50%` rather than exposing it immediately. (#321)
#[allow(dead_code)] // constructed by `run_in_app`, FR-3.2's second site. (#321)
/// Buffers a response body so a JSON one can be PRETTY-PRINTED before it reaches the pane.
///
/// # Why buffering is acceptable here, and only here
///
/// PR-1 requires INCREMENTAL rendering: bytes appear as they arrive, so a slow command is never
/// mistaken for a hang. Pretty-printing needs the WHOLE body, so it trades that away — and the
/// trade is only defensible because it costs nothing for these routes. Measured at 3.6 by resolving
/// all 57 route decorators and scanning each body: **zero of the 21 routed IN-APP commands uses
/// `StreamingResponse`.** They all return buffered JSON, so there is no incremental arrival to
/// preserve.
///
/// Non-JSON output is passed through UNCHANGED rather than mangled: if the body does not parse, the
/// raw bytes go to the pane exactly as [`PaneSink`] would have sent them. A formatter that garbles
/// what it cannot parse is worse than none.
///
/// The 10 MB response cap in `server.rs` bounds this buffer — it is not unbounded growth. (#321)
struct JsonSink {
    body: Vec<u8>,
}

impl JsonSink {
    fn new() -> Self {
        Self { body: Vec::new() }
    }

    /// The text to render: indented JSON when it parses, the raw body otherwise.
    fn rendered(&self) -> String {
        let raw = String::from_utf8_lossy(&self.body);
        match serde_json::from_slice::<serde_json::Value>(&self.body) {
            Ok(value) => {
                serde_json::to_string_pretty(&value).unwrap_or_else(|_| raw.clone().into_owned())
            }
            Err(_) => raw.into_owned(),
        }
    }
}

impl Write for JsonSink {
    fn write(&mut self, chunk: &[u8]) -> std::io::Result<usize> {
        self.body.extend_from_slice(chunk);
        Ok(chunk.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

struct PaneSink<'p> {
    pane: &'p mut ResultsPane,
    writes: usize,
}

#[allow(dead_code)] // see the struct. (#321)
impl<'p> PaneSink<'p> {
    /// Wraps the pane. No buffer, by construction.
    fn new(pane: &'p mut ResultsPane) -> Self {
        Self { pane, writes: 0 }
    }
}

impl Write for PaneSink<'_> {
    /// Forwards the whole slice immediately and reports it all consumed.
    ///
    /// Reporting a short write would make the caller re-send bytes the parser has already seen,
    /// which would duplicate output rather than lose it — the failure that looks like success.
    fn write(&mut self, chunk: &[u8]) -> std::io::Result<usize> {
        self.writes += 1;
        self.pane.push_bytes(chunk);
        Ok(chunk.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// `ServerApi` for the production client. The only production implementor.
///
/// `health` and `terminal` come from the [`ServerRead`] impl below; the four here are the rest of
/// the surface this unit needs. Each forwards to the inherent method of the same name — the
/// qualified call is what keeps the trait method from recursing into itself, which is a genuine
/// hazard when the names match.
impl ServerApi for crate::server::ServerClient {
    fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
        crate::server::ServerClient::profiles(self)
    }

    fn providers(&self) -> Result<Vec<Provider>, TuiError> {
        crate::server::ServerClient::providers(self)
    }

    fn create_session(&self, params: &SessionParams) -> Result<Terminal, TuiError> {
        crate::server::ServerClient::create_session(self, params)
    }

    fn run(
        &self,
        id: CommandId,
        path_values: &[&str],
        query: &[(&str, &str)],
        body: Option<&str>,
        mut sink: &mut dyn Write,
    ) -> Result<u16, TuiError> {
        // `&mut sink`, not `sink`: the inherent method is `run<W: Write>(.., sink: &mut W)`, so
        // passing the trait object directly infers `W = dyn Write`, which is unsized and does not
        // compile. Re-borrowing the binding makes `W = &mut dyn Write` — itself `Sized` and
        // `Write`, via `impl<W: Write + ?Sized> Write for &mut W` — which is why the parameter is
        // `mut`. This is the whole cost of the object-safe trait method, and it is what lets a fake
        // observe each chunk (PR-1). (#321)
        crate::server::ServerClient::run(self, id, path_values, query, body, &mut sink)
    }
}

/// `ServerRead` for the production client, so one type satisfies both traits.
///
/// This is what makes `ServerApi: ServerRead` work end to end: `HandoffDriver` takes a
/// `&S: ServerRead`, and `launch()` hands it the same `&S` it holds as a `ServerApi`. Without
/// this impl the driver would need its own client and the two could disagree about the address.
impl ServerRead for crate::server::ServerClient {
    fn health(&self) -> Result<Health, TuiError> {
        crate::server::ServerClient::health(self)
    }

    fn terminal(&self, terminal_id: &str) -> Result<Terminal, TuiError> {
        crate::server::ServerClient::terminal(self, terminal_id)
    }
}

/// `TerminalStatus` is named in `launch()`'s step-3 failure banner; this keeps the import honest.
///
/// Rust does not warn on a type used only inside a `format!` of a matched binding, so the
/// re-export makes the dependency explicit to a reader rather than implicit in a `{status:?}`.
#[allow(dead_code)] // documentation of the step-3 payload's type; see above. (#321)
type Step3FailurePayload = TerminalStatus;

#[cfg(test)]
mod tests {
    use super::{
        in_app_readiness, Fatal, Focus, Frame, InAppReadiness, JsonSink, LayoutMode, PaneSink,
        Renderer, Retryable, ServerApi, MIN_COLS, MIN_ROWS,
    };
    use crate::catalog::{self, CommandId, Policy};
    use crate::error::TuiError;
    use crate::guided_flow::PickerState;
    use crate::handoff::{Host, ServerRead};
    use crate::results_pane::PaneState;
    use crate::types::{Health, Profile, Provider, SessionParams, Terminal, TerminalStatus};
    use crossterm::event::{KeyCode, KeyModifiers};
    use std::cell::{Cell, RefCell};
    use std::collections::VecDeque;
    use std::io::Write;
    use std::time::Duration;

    /// **This module's own source text**, embedded at compile time.
    ///
    /// FR-3.2 is a claim about **where** the pane is called from, and that is a property of the
    /// source rather than of any runtime value. Embedding it is how the assertion can distinguish a
    /// production call site from a test one — see
    /// [`the_results_pane_has_production_callers_in_launch_and_the_in_app_path`]. `include_str!`
    /// rather than a filesystem read for `main.rs`'s reason: the assertion must not depend on the
    /// working directory a runner happens to use, and a read that silently found nothing would
    /// pass. (#321)
    const THIS_MODULE_SOURCE: &str = include_str!("renderer.rs");

    /// One scripted `terminal()` answer, modelled as data because `TuiError` is not `Clone`.
    ///
    /// Same shape as `handoff.rs`'s `Reply`, deliberately: the readiness loop being driven here is
    /// that unit's, so a different fake shape would only invite the two to diverge.
    enum Reply {
        /// A successful read carrying this status (`None` = the field was absent).
        Status(Option<TerminalStatus>),
        /// An HTTP error status.
        Http(u16),
    }

    /// What `create_session` should answer.
    enum SessionAnswer {
        Created(Terminal),
        Unreachable(String),
        Validation(String),
        Http(u16),
    }

    /// What `run` should do: the chunks to emit, then the status — or a failure.
    enum RunAnswer {
        /// Emit each chunk as a separate `write`, then report this status.
        Chunks(Vec<&'static str>, u16),
        /// Emit these chunks, then fail mid-stream (T-6: the bytes stay rendered).
        Truncated(Vec<&'static str>, String),
    }

    /// A fake server. **Records every call**, because FR-3.2 and the three-way branch are both
    /// statements about what was called rather than about a returned value.
    struct FakeServer {
        backend: String,
        health: RefCell<Option<TuiError>>,
        profiles: RefCell<Result<Vec<Profile>, String>>,
        providers: RefCell<Result<Vec<Provider>, String>>,
        session: RefCell<VecDeque<SessionAnswer>>,
        terminal_script: RefCell<VecDeque<Reply>>,
        run_answer: RefCell<Option<RunAnswer>>,
        create_session_calls: Cell<usize>,
        create_session_params: RefCell<Vec<SessionParams>>,
        run_calls: RefCell<Vec<CommandId>>,
        /// The path values each `run` received, one entry per call.
        ///
        /// **These were discarded (`_path_values`), and that is why the reported defect was
        /// invisible to this suite.** `run_in_app` passed `&[]` for every command, and a fake that
        /// ignores the argument cannot tell an empty slice from a correct one — so tests asserting
        /// "the command reached the server" passed while the request it built was unusable.
        /// (Review on PR #547.)
        run_path_values: RefCell<Vec<Vec<String>>>,
        /// The query pairs each `run` received, one entry per call. Recorded for the same reason.
        run_queries: RefCell<Vec<Vec<(String, String)>>>,
        /// The byte counts of each `write` the sink received, in order.
        ///
        /// This is the observation PR-1 needs: a buffered implementation produces one write of the
        /// whole body, an incremental one produces several. A total byte count could not tell them
        /// apart.
        run_write_sizes: RefCell<Vec<usize>>,
    }

    impl FakeServer {
        /// A server that answers everything successfully. Individual tests break one thing.
        fn healthy() -> Self {
            Self {
                backend: "herdr".to_string(),
                health: RefCell::new(None),
                profiles: RefCell::new(Ok(vec![profile("planner", true)])),
                providers: RefCell::new(Ok(vec![provider("kiro_cli", true)])),
                session: RefCell::new(VecDeque::new()),
                terminal_script: RefCell::new(VecDeque::new()),
                run_answer: RefCell::new(None),
                create_session_calls: Cell::new(0),
                create_session_params: RefCell::new(Vec::new()),
                run_calls: RefCell::new(Vec::new()),
                run_path_values: RefCell::new(Vec::new()),
                run_queries: RefCell::new(Vec::new()),
                run_write_sizes: RefCell::new(Vec::new()),
            }
        }

        /// A server that cannot be reached at all — FR-6.1's condition.
        fn unreachable() -> Self {
            let server = Self::healthy();
            let message = unreachable_message();
            *server.health.borrow_mut() = Some(TuiError::Unreachable(message.clone()));
            *server.profiles.borrow_mut() = Err(message.clone());
            *server.providers.borrow_mut() = Err(message.clone());
            server
                .session
                .borrow_mut()
                .push_back(SessionAnswer::Unreachable(message));
            server
        }

        /// A server that ANSWERS, with an HTTP error status. Distinct from [`Self::unreachable`],
        /// which is the no-answer case — the whole point of the auth arms is that the two must not
        /// be described the same way.
        fn answering_http(status: u16) -> Self {
            let server = Self::healthy();
            *server.health.borrow_mut() = Some(TuiError::Http(status));
            *server.profiles.borrow_mut() = Err(format!("cao-server returned HTTP {status}"));
            *server.providers.borrow_mut() = Err(format!("cao-server returned HTTP {status}"));
            server
                .session
                .borrow_mut()
                .push_back(SessionAnswer::Http(status));
            server
        }

        fn with_session(self, answer: SessionAnswer) -> Self {
            self.session.borrow_mut().push_back(answer);
            self
        }

        fn with_terminal_script(self, script: Vec<Reply>) -> Self {
            *self.terminal_script.borrow_mut() = script.into();
            self
        }

        fn with_run(self, answer: RunAnswer) -> Self {
            *self.run_answer.borrow_mut() = Some(answer);
            self
        }

        fn with_backend(mut self, backend: &str) -> Self {
            self.backend = backend.to_string();
            self
        }
    }

    impl ServerRead for FakeServer {
        /// `TuiError` is not `Clone`, so a recorded failure is re-minted **as its own variant**.
        ///
        /// It used to re-mint everything as `Unreachable`:
        /// `Some(other) => Err(TuiError::Unreachable(other.to_string()))`. That made the variant
        /// unobservable through this fake — an injected `Http(401)` arrived at the renderer as
        /// `Unreachable`, so **no test could distinguish the two**, and the header bug of labelling
        /// an answering server "unreachable" was invisible to the whole suite by construction. A
        /// fake that flattens the distinction under test is worse than no fake.
        /// (Reported by review on PR #547.)
        fn health(&self) -> Result<Health, TuiError> {
            match self.health.borrow().as_ref() {
                Some(TuiError::Unreachable(message)) => Err(TuiError::Unreachable(message.clone())),
                Some(TuiError::Http(status)) => Err(TuiError::Http(*status)),
                Some(TuiError::Validation(detail)) => Err(TuiError::Validation(detail.clone())),
                Some(TuiError::NotFound(what)) => Err(TuiError::NotFound(what.clone())),
                Some(TuiError::Decode(detail)) => Err(TuiError::Decode(detail.clone())),
                Some(TuiError::NoRoute(what)) => Err(TuiError::NoRoute(what.clone())),
                // `Io` carries a `std::io::Error`, which cannot be cloned or reconstructed
                // faithfully. No test injects one; if one ever does, this states why it cannot be
                // replayed rather than silently mislabelling it as something else.
                Some(other @ TuiError::Io(_)) => panic!(
                    "FakeServer cannot re-mint {other:?}: `Io` wraps a non-cloneable \
                     `std::io::Error`. Inject a different variant, or extend this fake \
                     deliberately rather than flattening it into another variant"
                ),
                None => Ok(Health {
                    status: "ok".to_string(),
                    terminal_backend: self.backend.clone(),
                }),
            }
        }

        fn terminal(&self, terminal_id: &str) -> Result<Terminal, TuiError> {
            let reply = self
                .terminal_script
                .borrow_mut()
                .pop_front()
                // Once the script runs dry the server answers `Processing` forever, which is what
                // lets the 30-second cap be reached without a real clock.
                .unwrap_or(Reply::Status(Some(TerminalStatus::Processing)));

            match reply {
                Reply::Status(status) => Ok(Terminal {
                    id: terminal_id.to_string(),
                    name: "planner-1".to_string(),
                    session_name: "work".to_string(),
                    status,
                }),
                Reply::Http(code) => Err(TuiError::Http(code)),
            }
        }
    }

    impl ServerApi for FakeServer {
        fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
            match self.profiles.borrow().as_ref() {
                Ok(profiles) => Ok(profiles.clone()),
                Err(message) => Err(TuiError::Unreachable(message.clone())),
            }
        }

        fn providers(&self) -> Result<Vec<Provider>, TuiError> {
            match self.providers.borrow().as_ref() {
                Ok(providers) => Ok(providers.clone()),
                Err(message) => Err(TuiError::Unreachable(message.clone())),
            }
        }

        fn create_session(&self, params: &SessionParams) -> Result<Terminal, TuiError> {
            self.create_session_calls
                .set(self.create_session_calls.get() + 1);
            self.create_session_params.borrow_mut().push(params.clone());

            match self.session.borrow_mut().pop_front() {
                Some(SessionAnswer::Created(terminal)) => Ok(terminal),
                Some(SessionAnswer::Unreachable(message)) => Err(TuiError::Unreachable(message)),
                Some(SessionAnswer::Validation(detail)) => Err(TuiError::Validation(detail)),
                Some(SessionAnswer::Http(code)) => Err(TuiError::Http(code)),
                None => Ok(terminal("t-1")),
            }
        }

        fn run(
            &self,
            id: CommandId,
            path_values: &[&str],
            query: &[(&str, &str)],
            _body: Option<&str>,
            sink: &mut dyn Write,
        ) -> Result<u16, TuiError> {
            self.run_calls.borrow_mut().push(id);
            // Recorded, not ignored: see the field docs. Owned copies, because the borrows do not
            // outlive this call.
            self.run_path_values
                .borrow_mut()
                .push(path_values.iter().map(|value| value.to_string()).collect());
            self.run_queries.borrow_mut().push(
                query
                    .iter()
                    .map(|(key, value)| (key.to_string(), value.to_string()))
                    .collect(),
            );

            match self.run_answer.borrow_mut().take() {
                Some(RunAnswer::Chunks(chunks, status)) => {
                    for chunk in chunks {
                        self.run_write_sizes.borrow_mut().push(chunk.len());
                        sink.write_all(chunk.as_bytes())
                            .expect("the pane sink is infallible");
                    }
                    Ok(status)
                }
                Some(RunAnswer::Truncated(chunks, message)) => {
                    for chunk in chunks {
                        self.run_write_sizes.borrow_mut().push(chunk.len());
                        sink.write_all(chunk.as_bytes())
                            .expect("the pane sink is infallible");
                    }
                    Err(TuiError::Unreachable(message))
                }
                None => Ok(200),
            }
        }
    }

    /// The local machine, faked. Same shape as `handoff.rs`'s, for the same three reasons: `$TMUX`
    /// is read through the trait so no test mutates a process-global, the clock is fakeable so the
    /// 30-second cap is provable in microseconds, and **spawning is observable** so a test can
    /// assert the refusal path spawned nothing.
    struct FakeHost {
        tmux: Option<String>,
        now: Cell<Duration>,
        spawned: RefCell<Vec<Vec<String>>>,
    }

    impl FakeHost {
        fn inside_tmux() -> Self {
            Self::new(Some("/private/tmp/tmux-504/default,1,0".to_string()))
        }

        /// Models a process outside tmux: `$TMUX` unset.
        fn outside_tmux() -> Self {
            Self::new(None)
        }

        fn new(tmux: Option<String>) -> Self {
            Self {
                tmux,
                now: Cell::new(Duration::ZERO),
                spawned: RefCell::new(Vec::new()),
            }
        }
    }

    impl Host for FakeHost {
        fn tmux_env(&self) -> Option<String> {
            self.tmux.clone()
        }

        fn now(&self) -> Duration {
            self.now.get()
        }

        fn sleep(&self, duration: Duration) {
            // Advancing the fake clock is what makes the readiness cap reachable in microseconds
            // instead of thirty real seconds per assertion.
            self.now.set(self.now.get() + duration);
        }

        fn run(&self, argv: &[&str]) -> Result<(), String> {
            self.spawned
                .borrow_mut()
                .push(argv.iter().map(|part| (*part).to_string()).collect());
            Ok(())
        }
    }

    /// The operator-facing message a real `Unreachable` carries, for a fixture.
    ///
    /// # The URL scheme is ASSEMBLED, and that is required rather than stylistic
    ///
    /// `tests/hermeticity_tripwire.rs` scans this file for a `http:` literal as a **catch-all for
    /// an HTTP client its needle list does not name**, and BR-1 permits only `src/server.rs` to
    /// name one. A fixture containing the contiguous scheme trips it — correctly, because a static
    /// scan cannot tell a fixture string from a call. Found by running the tripwire, not by review.
    ///
    /// Assembling it is the same technique the tripwire uses on its own needle definitions, and it
    /// is the right fix: the alternative would be loosening a catch-all that exists to cover the
    /// clients the list cannot enumerate.
    ///
    /// The message mirrors `ServerClient::unreachable`'s wording, because the FR-6.1 tests assert
    /// the operator sees the **address** and the `CAO_API_HOST` remedy — a paraphrased fixture
    /// would let a renderer that dropped either one still pass.
    fn unreachable_message() -> String {
        let scheme = format!("http{}", ":");
        format!(
            "could not reach cao-server at {scheme}\
             //127.0.0.1:9889: connection refused. Start `cao-server`, or point CAO_API_HOST / \
             CAO_API_PORT at a running instance"
        )
    }

    fn profile(name: &str, loadable: bool) -> Profile {
        Profile {
            name: name.to_string(),
            source: "builtin".to_string(),
            loadable,
            description: Some(String::new()),
            capabilities: Vec::new(),
            tags: Vec::new(),
            role: Some(String::new()),
            duplicated_in: Vec::new(),
        }
    }

    fn provider(name: &str, installed: bool) -> Provider {
        Provider {
            name: name.to_string(),
            binary: name.to_string(),
            installed,
        }
    }

    fn terminal(id: &str) -> Terminal {
        Terminal {
            id: id.to_string(),
            name: "planner-1".to_string(),
            session_name: "work".to_string(),
            status: Some(TerminalStatus::Idle),
        }
    }

    /// A shell with `cao launch` selected and `--agents` filled in, ready to launch.
    ///
    /// The pickers are populated as a side effect of `select`, which is the production path — a
    /// helper that set the fields directly would bypass `GuidedFlow::set`'s validation and prove
    /// less than it appears to.
    fn ready_to_launch<'a>(
        server: &'a FakeServer,
        host: &'a FakeHost,
    ) -> Renderer<'a, FakeServer, FakeHost> {
        let mut shell = Renderer::new(server, host, 100, 40);
        assert!(
            shell.focus_command(CommandId::Launch),
            "`cao launch` must be in the navigable command list"
        );
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--agents", "planner")
            .expect("`planner` is a loadable profile in the fake's answer");
        shell
    }

    /// The production half of this module's source: everything before `#[cfg(test)]`.
    ///
    /// The split is what makes the FR-3.2 assertion mean what it says. Without it, a test-only
    /// caller of `attach`/`complete`/`refuse` would satisfy a whole-file `contains()` — which is
    /// **exactly the defect being guarded against**, since the predecessor's pane was called from
    /// its tests and nowhere else.
    fn production_source() -> &'static str {
        let marker = "#[cfg(test)]";
        let (production, _) = THIS_MODULE_SOURCE.split_once(marker).expect(
            "this module must carry a #[cfg(test)] marker for the region split to mean anything",
        );
        production
    }

    /// Strips `//`-comments, so a method named in prose is not counted as a call site.
    ///
    /// Same rule and same caveat as the two tripwires': not a lexer, and it can only ever *weaken*
    /// the check by hiding real code — it removes text rather than adding it, so it cannot
    /// manufacture a pass for a call that is present.
    fn code_only(source: &str) -> String {
        source
            .lines()
            .map(|line| match line.find("//") {
                Some(comment_start) => &line[..comment_start],
                None => line,
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// Removes **all** whitespace, so a call-site search is independent of how rustfmt broke the
    /// line.
    ///
    /// rustfmt renders the refusal arm as `self.pane\n    .refuse(..)`, so a `contains("self.\
    /// pane.refuse(")` search found nothing **even though the call was there** — a guard that
    /// fails for a formatting reason gets "fixed" by weakening it, which is how a real check gets
    /// lost. Collapsing whitespace makes the assertion about the *call* rather than about the
    /// line break.
    ///
    /// It cannot manufacture a false positive: a `.refuse(` can only follow a receiver
    /// expression, so joining two lines cannot invent a method call that was not written. And it
    /// runs on already-comment-stripped text, so prose is out of scope before this sees it.
    fn dense(source: &str) -> String {
        source.chars().filter(|c| !c.is_whitespace()).collect()
    }

    // ── Test 1 — FR-3.2, THE DEFINING TEST ───────────────────────────────────────────────────

    /// **Test 1 (FR-3.2): the results pane has PRODUCTION callers, and they are `launch()` and
    /// the in-app run path.**
    ///
    /// # Why this test asserts on source text rather than only on behaviour
    ///
    /// FR-3.2 is an **anti-requirement about a call site**: the predecessor built a
    /// captured-output pane, tested it thoroughly, and never invoked it from production code. Every
    /// behavioural test it had passed. So a test that merely drives `launch()` and observes the
    /// pane's state proves the pane *works when called* — which the predecessor's suite also
    /// proved. **A test-only caller reproduces the original defect exactly while looking green.**
    ///
    /// The property that distinguishes the two is *which region of the file the call is in*, and
    /// that is only visible in the source. So the file is split at `#[cfg(test)]` and each pane
    /// method is required to appear on the **production** side, inside a named production function.
    ///
    /// # The behavioural half is here too, and both halves are necessary
    ///
    /// The source half alone could be satisfied by a call in dead production code. So the second
    /// half runs `launch()` and `run_in_app()` and observes the pane actually reaching each
    /// terminal state — which proves the production call sites are *reached*, not merely present.
    /// Neither half is sufficient:
    ///
    /// - Source only: a call inside an unreachable production branch passes.
    /// - Behaviour only: a test-only caller passes. **This is the predecessor's exact defect.**
    ///
    /// # Proven by mutation
    ///
    /// Deleting `self.pane.complete(0, Some(line))` from `launch()`'s success arm and replacing it
    /// with a discarded local turns this red on the source half *and* on the state assertion. See
    /// the summary's mutation log for the observed output. (#321)
    #[test]
    fn the_results_pane_has_production_callers_in_launch_and_the_in_app_path() {
        let production = dense(&code_only(production_source()));

        // Half one: each pane method is called from PRODUCTION code, not from a test.
        // Whitespace-collapsed, so the assertion is about the CALL and not about how rustfmt broke
        // the line — see `dense`, which the first draft of this test needed and did not have.
        for (call, why) in [
            (
                "self.pane.attach(",
                "the pane must be attached from production code — the predecessor's pane was \
                 attached only by its own tests, which is design defect #3",
            ),
            (
                "self.pane.complete(",
                "a run must be completed from production code, or the pane never leaves `running`",
            ),
            (
                "self.pane.refuse(",
                "FR-5.3's refusal must reach the pane from production code, or a refused hand-off \
                 renders nothing",
            ),
            (
                "self.pane.push_bytes(",
                "bytes must reach the pane from production code — via `PaneSink`, which is the \
                 `Write` adapter `ServerClient::run` writes into",
            ),
        ] {
            assert!(
                production.contains(call),
                "FR-3.2: `{call}..)` does not appear in this module's PRODUCTION code. {why}. \
                 A call from the test module would satisfy a whole-file search and reproduce the \
                 original defect exactly while looking green"
            );
        }

        // Half one, continued: the calls are inside the two named orchestration methods, so a
        // future refactor cannot satisfy the check above by parking them in a helper nothing calls.
        let launch_body = production
            .split_once("pubfnlaunch(&mutself)")
            .expect("`launch` must exist in production code")
            .1
            .split_once("pubfnrun_in_app")
            .expect("`run_in_app` must follow `launch`")
            .0;
        assert!(
            launch_body.contains("self.pane.complete(")
                && launch_body.contains("self.pane.refuse("),
            "FR-3.2: `launch()`'s own body must reach BOTH `complete()` (the success arm) and \
             `refuse()` (the FR-5.3 arm). Found neither or only one, which means step 4 of the \
             orchestration does not terminate in a pane call"
        );

        let in_app_body = production
            .split_once("pubfnrun_in_app(&mutself,id:CommandId)")
            .expect("`run_in_app` must exist in production code")
            .1
            .split_once("pubfnresize")
            .expect("`resize` must follow `run_in_app`")
            .0;
        assert!(
            in_app_body.contains("self.pane.attach(") && in_app_body.contains("self.pane.complete("),
            "FR-3.2: the in-app path must `attach()` then `complete()` from its own body — that is \
             the path `business-logic-model.md:207` records as the one the predecessor never wired"
        );

        // Half two: the production call sites are REACHED. A call in dead code would pass above.
        //
        // 2a — the hand-off success arm reaches `complete()` with the structured outcome line.
        let server = FakeServer::healthy().with_session(SessionAnswer::Created(terminal("t-1")));
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "the hand-off success arm must leave the pane `complete`; the backend is herdr, which \
             navigates itself, so `handoff()` returns an Outcome. Pane: {:?}",
            shell.pane()
        );
        // The RENDERED region, not the ring buffer: the outcome line comes from the pane's own
        // `Widget` impl and is absent from `lines()`. Asserting on `lines()` here reported an empty
        // pane after a successful hand-off — the defect this frame accessor was corrected for.
        let rendered = shell.render().results.join("\n");
        assert!(
            rendered.contains("launched in new window") && rendered.contains("session: work"),
            "HANDOFF's completion shape is the STRUCTURED OUTCOME LINE, because the command's \
             output went to the new window and an empty pane would read as a failed run. Got: \
             {rendered:?}"
        );

        // 2b — the refusal arm reaches `refuse()` with the copyable argv (FR-5.3).
        let server = FakeServer::healthy()
            .with_backend("tmux")
            .with_session(SessionAnswer::Created(terminal("t-2")));
        // `$TMUX` unset: there is no client whose view could be moved, so the hand-off is refused.
        // A designed outcome, not a malfunction.
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert_eq!(
            shell.pane().state(),
            PaneState::Refused,
            "the FR-5.3 refusal arm must leave the pane `refused`. Pane: {:?}",
            shell.pane()
        );
        let rendered = shell.render().results.join("\n");
        assert!(
            rendered.contains("$TMUX is unset"),
            "the refusal must carry the reason. Got: {rendered:?}"
        );
        assert!(
            rendered.contains("tmux") && rendered.contains("work:planner-1"),
            "SR-4/FR-5.3: the refusal must render the EXACT argv for the operator to copy, \
             naming the target. Got: {rendered:?}"
        );
        assert!(
            host.spawned.borrow().is_empty(),
            "the refusal path must spawn NOTHING — it hands the operator a command instead. \
             Spawned: {:?}",
            host.spawned.borrow()
        );

        // 2c — the in-app path reaches `attach` → `push_bytes` → `complete`.
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["one\n", "two\n"], 200));
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        // `session list` is placeholder-free — one of the 9 measured `Runnable` routes.
        shell.run_in_app(CommandId::SessionList);
        assert_eq!(
            server.run_calls.borrow().as_slice(),
            &[CommandId::SessionList],
            "the in-app path must call `ServerClient::run` exactly once, for the command asked for"
        );
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "an in-app run that produced output must leave the pane `complete` with the captured \
             output, per the IN-APP branch of `complete()`. Pane: {:?}",
            shell.pane()
        );
        assert_eq!(
            shell.pane().lines(),
            vec!["one", "two"],
            "the bytes `run()` wrote into the sink must be what the pane holds — that is the \
             `Write` adapter forwarding into `push_bytes`"
        );
    }

    // ── Test 2 — the THREE-WAY readiness branch, all three arms ──────────────────────────────

    /// **Test 2 (FR-5.4, ADR-04): the readiness branch has THREE arms, and `Unknown` CONTINUES.**
    ///
    /// # A test covering only Ready and Failed cannot detect the defect
    ///
    /// The mistake this guards is folding `Unknown` into the error arm — which compiles, reads
    /// tidier, and burns a healthy launch. `Ready`/`Failed`-only coverage stays green through it,
    /// because both of those arms behave identically either way. So all three are asserted, and the
    /// `Unknown` case asserts the property that distinguishes them: **the flow reached step 4.**
    ///
    /// "Reached step 4" is observed rather than inferred: the pane leaves `running` only if
    /// `handoff()` was called, since `complete`/`refuse` are the only transitions out of it and both
    /// are in step 4. A banner assertion alone would not do it — a `Failed` arm renders a banner
    /// too.
    ///
    /// # Proven by mutation
    ///
    /// Replacing the `Unknown` arm's warning-then-continue with an early `return` after the banner
    /// turns the `Unknown` case red — the pane stays `Running` and never reaches `Complete`. The
    /// `Ready` and `Failed` cases stay green, which is the point. (#321)
    #[test]
    fn the_readiness_branch_has_three_arms_and_unknown_continues_to_the_handoff() {
        // Arm 1 — `Ready`: the terminal reports `idle`, so the hand-off runs immediately.
        let server = FakeServer::healthy()
            .with_session(SessionAnswer::Created(terminal("t-ready")))
            .with_terminal_script(vec![Reply::Status(Some(TerminalStatus::Idle))]);
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "`Ready` must continue to step 4 and complete the hand-off"
        );
        assert!(
            shell.banner().is_none(),
            "a `Ready` readiness needs no banner at all; got {:?}",
            shell.banner()
        );

        // Arm 2 — `Unknown`: the cap elapses with no conclusive status. **WARN, THEN CONTINUE.**
        //
        // The script is left empty, so the fake answers `Processing` forever and the fake clock
        // advances one second per poll until the 30-second cap. The whole 30 polls run in
        // microseconds because the clock is injected (`handoff.rs`'s reason for the `Host` seam).
        let server = FakeServer::healthy()
            .with_session(SessionAnswer::Created(terminal("t-unknown")))
            .with_terminal_script(vec![Reply::Status(Some(TerminalStatus::Processing))]);
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();

        let banner = shell.banner().expect(
            "`Unknown` must render a WARNING — a timeout the operator is not told about is \
                     a silent 30-second stall",
        );
        assert_eq!(
            banner.severity, "warning",
            "ADR-04: an unresolved readiness is a WARNING, not an error. `Unknown` is not the same \
             as failed, and rendering it as an error is the conflation the three-state type exists \
             to prevent. Banner: {banner:?}"
        );
        // THE assertion that distinguishes continue from stop. `complete()` is only reachable from
        // step 4, so a `Complete` pane proves the hand-off was performed after the warning.
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "**`Unknown` must CONTINUE to step 4.** The operator's ruling was verbatim: \"just \
             warning message, don't really do anything, user should retry if not succeed\". A pane \
             still in `running` means the warning arm returned early — which is `Unknown` folded \
             into the error arm, the exact conflation ADR-04 exists to prevent. Pane: {:?}",
            shell.pane()
        );

        // Arm 3 — `Failed`: an explicit error status stops the flow.
        let server = FakeServer::healthy()
            .with_session(SessionAnswer::Created(terminal("t-failed")))
            .with_terminal_script(vec![Reply::Status(Some(TerminalStatus::Error))]);
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();

        let banner = shell.banner().expect("`Failed` must render a hard error");
        assert_eq!(
            banner.severity, "error",
            "an explicit `TerminalStatus::Error` is genuine breakage and renders as an error"
        );
        assert_ne!(
            shell.pane().state(),
            PaneState::Complete,
            "`Failed` must STOP before step 4 — a hand-off to a terminal that failed to launch \
             would move the operator's view to a broken agent. Pane: {:?}",
            shell.pane()
        );
        assert!(
            host.spawned.borrow().is_empty(),
            "the `Failed` arm must spawn nothing: it never reaches the hand-off. Spawned: {:?}",
            host.spawned.borrow()
        );

        // Arm 3b — a 5xx during the poll is the ONE conclusive read failure (BR-12), and it must
        // also stop. Included because `Failed` has two sources and only one of them is a status.
        let server = FakeServer::healthy()
            .with_session(SessionAnswer::Created(terminal("t-5xx")))
            .with_terminal_script(vec![Reply::Http(503)]);
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert_eq!(
            shell.banner().map(|banner| banner.severity),
            Some("error"),
            "a 5xx during the readiness poll settles as `Failed` (BR-12) and renders a hard error"
        );
    }

    // ── Test 3 — FR-6.2, the stated gating reason ────────────────────────────────────────────

    /// **Test 3 (FR-6.2): `Incomplete` renders a stated reason NAMING the field.**
    ///
    /// Never a greyed control with no explanation — that is the exact failure FR-6.2 names. The
    /// assertion is on the field **name as the CLI spells it** (`--agents`), because a message
    /// saying "a required field is missing" is the greyed control with extra words.
    ///
    /// It also asserts the flow **stopped**: `create_session` must not have been called. A step-1
    /// refusal that still issued the HTTP request would send an invalid launch to the server and
    /// render the server's 422 instead of the local reason — a worse error, one round trip later.
    #[test]
    fn an_incomplete_form_states_the_missing_field_and_never_reaches_the_server() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));
        // Deliberately NOT setting `--agents`: it is `cao launch`'s only required parameter.

        shell.launch();

        let banner = shell
            .banner()
            .expect("FR-6.2: a blocked run must render a STATED reason, never a silent no-op");
        assert!(
            banner.what.contains("--agents"),
            "FR-6.2: the reason must name the field in the CLI's own spelling. \"a required field \
             is missing\" is the greyed control with extra words. Got: {:?}",
            banner.what
        );
        assert_eq!(
            banner.what, "blocked: --agents required",
            "the wording must match `GuidedFlow::blocked_reason`'s, or the footer and the banner \
             disagree about why the same run is blocked"
        );
        assert_eq!(
            server.create_session_calls.get(),
            0,
            "step 1 must STOP. Issuing the request anyway would render the server's 422 instead of \
             the local reason — a worse error, one round trip later"
        );

        // The footer carries it too, since that is where the operator looks before pressing enter.
        let frame = shell.render();
        assert!(
            frame
                .footer
                .iter()
                .any(|line| line.contains("blocked: --agents required")),
            "the gating reason must be in the footer as text. Footer: {:?}",
            frame.footer
        );
    }

    // ── Test 4 — FR-6.1, the TUI starts with the server down ─────────────────────────────────

    /// **Test 4 (FR-6.1): the TUI STARTS with the server down, and renders a populated state.**
    ///
    /// # The assertion is `Ok` from `run()`, not merely "no panic"
    ///
    /// `HandoffDriver`'s backend is a lazily-resolved `OnceCell` for exactly this requirement: an
    /// eager resolve in the constructor would turn FR-6.1's *rendered* error state into a **startup
    /// crash**. So the test constructs against an unreachable server, calls `run()`, and requires
    /// `Ok(())` — a `Fatal` here would mean the TUI does not open, which is the defect.
    ///
    /// It then requires the frame to be **populated and diagnostic**: cause, address, and remedy
    /// (FR-6.1, NFR-3 item 5). "error" alone would satisfy a weaker test and tell the operator
    /// nothing they could act on.
    #[test]
    fn the_tui_starts_and_renders_a_diagnostic_state_with_the_server_down() {
        let server = FakeServer::unreachable();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert_eq!(
            shell.run(),
            Ok(()),
            "FR-6.1: the TUI must START when cao-server is down. A `Fatal` here means the backend \
             was resolved eagerly, which turns a rendered error state into a startup crash"
        );

        // Selecting a command issues the reads that fail, which is when the operator learns.
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));

        let frame = shell.render();
        assert!(
            !frame.is_blank(),
            "a blank screen is indistinguishable from a hang (SR-2). Frame: {frame:?}"
        );

        let all = frame
            .header
            .iter()
            .chain(&frame.pickers)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n");
        assert!(
            all.contains("unreachable") || all.contains("unavailable"),
            "the state must say WHAT failed. Got: {all:?}"
        );
        assert!(
            all.contains("9889"),
            "FR-6.1 requires the ADDRESS: the remedy depends entirely on whether the client is \
             pointed where the operator expects, which is why CAO_API_HOST/CAO_API_PORT are \
             configurable (SR-4). Got: {all:?}"
        );
        assert!(
            all.contains("CAO_API_HOST") || all.contains("Start `cao-server`"),
            "FR-6.1 requires the REMEDY, not just the cause (NFR-3 item 5). Got: {all:?}"
        );
        assert!(
            all.contains("[ctrl+r]"),
            "FR-6.3 requires the retry affordance to be stated in place — and it must name \
             `[ctrl+r]`, the key that WORKS. A plain `[r]` is text in a field, so advertising it \
             would promise an affordance that types instead of retrying, which is the `[c] clear` \
             failure again. Got: {all:?}"
        );

        // And the pickers are `Failed`, not `Loaded(vec![])` — an empty list would claim the
        // machine has no profiles, which is a different and false statement.
        assert!(
            matches!(shell.flow().agent_choices(), PickerState::Failed(_)),
            "a failed fetch must be `Failed`, never `Loaded(vec![])`: conflating them tells the \
             operator the server has no profiles when it is simply unreachable"
        );
    }

    // ── Test 5 — FR-6.3, retry preserves form state ───────────────────────────────────────────

    /// **Test 5 (FR-6.3): retry preserves the typed field VALUES, not merely the fact of retrying.**
    ///
    /// # Asserting "retry was attempted" is the vacuous version
    ///
    /// `frontend-components.md:177` names this explicitly: assert the values survive. A test that
    /// only counted `create_session` calls would stay green through a retry that called
    /// `GuidedFlow::new()` or `select()` first — both of which compile, both of which reset the
    /// form, and both of which silently discard the operator's input while the retry looks correct.
    /// Losing typed input on a transient failure pushes the operator back to the CLI, which is the
    /// opposite of this intent's purpose.
    ///
    /// So the assertion is on the **values read back after the retry**, and on the params the
    /// second `create_session` call actually received — which is the only place a silently-cleared
    /// field would show up.
    ///
    /// The first call fails `Unreachable` and the second succeeds, so the test also proves retry
    /// **resumes the failed step** rather than restarting the TUI.
    #[test]
    fn a_retry_preserves_every_typed_field_value() {
        let server = FakeServer::healthy()
            // Attempt 1 fails; attempt 2 succeeds. Both answers are queued up front.
            .with_session(SessionAnswer::Unreachable(unreachable_message()))
            .with_session(SessionAnswer::Created(terminal("t-retry")));
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);

        // Four fields across three kinds — text, positional, and flag — so a reset that spared one
        // kind cannot pass.
        shell
            .flow_mut()
            .set("--session-name", "my-session")
            .expect("a plain text value is accepted");
        shell
            .flow_mut()
            .set("--working-directory", "/tmp/work")
            .expect("a plain text value is accepted");
        shell
            .flow_mut()
            .set("message", "hello agent")
            .expect("the positional takes a text value");
        shell
            .flow_mut()
            .set("--yolo", "true")
            .expect("a flag takes true/false");

        shell.launch();
        assert_eq!(
            shell.banner().map(|banner| banner.severity),
            Some("error"),
            "attempt 1 must render the unreachable error"
        );

        // The retry affordance, pressed as the operator would press it.
        // Ctrl+R, not a plain `[r]`: a plain `r` is TEXT while a required field is focused, and
        // after `select()` that is exactly where focus sits. Operator decision at the 3.5 gate.
        assert!(
            shell.retry(),
            "FR-6.3: retry must be handled — an unhandled retry affordance is one that does nothing"
        );

        assert_eq!(
            server.create_session_calls.get(),
            2,
            "the retry must re-issue the failed step, not restart the TUI"
        );

        // Half one: the form still holds every value.
        for (name, expected) in [
            ("--agents", "planner"),
            ("--session-name", "my-session"),
            ("--working-directory", "/tmp/work"),
            ("message", "hello agent"),
        ] {
            let field = shell
                .flow()
                .field(name)
                .unwrap_or_else(|| panic!("{name} must still be in the form after a retry"));
            assert_eq!(
                field.value,
                Some(crate::guided_flow::FieldValue::Text(expected.to_string())),
                "FR-6.3: {name} must survive the retry with its typed value. A retry that called \
                 `select()` or `GuidedFlow::new()` first would clear it and still look correct — \
                 which is why this asserts the VALUE and not that a retry happened. Field: \
                 {field:?}"
            );
        }
        assert_eq!(
            shell
                .flow()
                .field("--yolo")
                .map(|field| field.value.clone()),
            Some(Some(crate::guided_flow::FieldValue::Flag(true))),
            "FR-6.3: a flag must survive the retry too — a reset that spared only text fields \
             would pass a text-only assertion"
        );

        // Half two: the values the SECOND request carried. This is where a cleared field shows up
        // even if the form somehow still displayed it.
        let params = server.create_session_params.borrow();
        let second = params
            .get(1)
            .expect("the retry must have issued a second create_session");
        assert_eq!(
            second.agents, "planner",
            "the retry must send the same profile"
        );
        assert_eq!(
            second.session_name.as_deref(),
            Some("my-session"),
            "the retry must send the preserved session name, not omit it. Params: {second:?}"
        );
        assert_eq!(
            second.working_directory.as_deref(),
            Some("/tmp/work"),
            "the retry must send the preserved working directory. Params: {second:?}"
        );
        // And the first request carried the same thing, so "preserved" means unchanged rather than
        // coincidentally re-derived.
        assert_eq!(
            params.first().map(|first| first.session_name.clone()),
            Some(Some("my-session".to_string())),
            "attempt 1 must have carried the same values, or 'preserved' is not what was tested"
        );

        // The retry succeeded, so the pane reached step 4.
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "the successful retry must complete the hand-off. Pane: {:?}",
            shell.pane()
        );
    }

    // ── Test 6 — NFR-6 / NFR-3, sub-80x24 stacks and stays reachable ──────────────────────────

    /// **Test 6 (NFR-6, NFR-3): below 80x24 the layout STACKS, and every region stays reachable.**
    ///
    /// # Degraded but usable, not an error
    ///
    /// NFR-6 makes the small terminal a *layout*, not a failure. So the test asserts three things
    /// at 70x20 — the exact size `tech-stack-decisions.md` TS-3 names:
    ///
    /// 1. The layout is `Stacked`.
    /// 2. The frame is **not blank** and carries no error banner — a "terminal too small" error
    ///    would be the failure NFR-6 forbids.
    /// 3. Every focus region is still reachable by `Tab` alone (NFR-3), and the results region
    ///    still renders content rather than collapsing to nothing.
    ///
    /// Boundary cases are included because `>=` vs `>` on either dimension is a one-character
    /// mutation: 80x24 is exactly two-column, and 79x24 and 80x23 both stack.
    ///
    /// The companion integration test in `tests/pty.rs` proves the production binary also paints
    /// and accepts keyboard input in a real 70x20 terminal.
    #[test]
    fn below_eighty_by_twentyfour_the_layout_stacks_and_stays_keyboard_reachable() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);

        // The boundary, asserted in both directions so `>=` cannot silently become `>`.
        shell.resize(MIN_COLS, MIN_ROWS);
        assert_eq!(
            shell.render().layout,
            LayoutMode::TwoColumn,
            "exactly {MIN_COLS}x{MIN_ROWS} is the minimum two-column size, not one short of it"
        );
        shell.resize(MIN_COLS - 1, MIN_ROWS);
        assert_eq!(
            shell.render().layout,
            LayoutMode::Stacked,
            "one column short of the minimum must stack"
        );
        shell.resize(MIN_COLS, MIN_ROWS - 1);
        assert_eq!(
            shell.render().layout,
            LayoutMode::Stacked,
            "one row short of the minimum must stack — the rule is `<` on EITHER dimension, so an \
             `&&` here would keep the two-column layout at 200x10 where the results region rounds \
             down to nothing"
        );

        // TS-3's named size.
        shell.resize(70, 20);
        let frame = shell.render();
        assert_eq!(
            frame.layout,
            LayoutMode::Stacked,
            "70x20 must stack (NFR-6)"
        );
        assert!(
            !frame.is_blank(),
            "the stacked layout is DEGRADED BUT USABLE, never blank. Frame: {frame:?}"
        );
        assert!(
            shell.banner().is_none(),
            "a small terminal is not an error state — a \"terminal too small\" banner is exactly \
             the failure NFR-6 forbids. Banner: {:?}",
            shell.banner()
        );
        assert!(
            !frame.command_list.is_empty()
                && !frame.required_fields.is_empty()
                && !frame.footer.is_empty(),
            "stacking must not drop a region: the form, the list and the footer all stay. Frame: \
             {frame:?}"
        );
        assert!(
            !frame.results.is_empty(),
            "the results region must still render at 70x20 — a region that collapses to nothing is \
             a blank area, which is the same defect as a blank frame one level down. Frame: {frame:?}"
        );

        // NFR-3: every region is reachable by `Tab` alone, with no mouse anywhere.
        let mut seen = vec![shell.focus()];
        for _ in 0..Focus::ORDER.len() {
            assert!(
                shell.on_key(KeyCode::Tab),
                "`Tab` must be handled in every region, or a region becomes unreachable"
            );
            seen.push(shell.focus());
        }
        for region in Focus::ORDER {
            assert!(
                seen.contains(&region),
                "NFR-3: {region:?} is not reachable by Tab alone at 70x20. Every action must be \
                 keyboard-reachable and there is no mouse dependency anywhere. Reached: {seen:?}"
            );
        }
        // And it wraps rather than dead-ending at the last region.
        assert_eq!(
            seen.first(),
            seen.last(),
            "the focus ring must WRAP: {} Tabs from the first region must return to it, or the \
             operator gets stuck at the end. Reached: {seen:?}",
            Focus::ORDER.len()
        );

        // Shift-Tab retreats, so the ring is navigable in both directions.
        let before = shell.focus();
        assert!(shell.on_key(KeyCode::BackTab));
        assert_eq!(
            shell.focus(),
            before.previous(),
            "Shift-Tab must retreat one region"
        );

        // A resize mid-run keeps the pane's buffer and scroll position
        // (`business-logic-model.md:191`).
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(
            vec!["line-one\n", "line-two\n", "line-three\n"],
            200,
        ));
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.run_in_app(CommandId::SessionList);
        let before = shell.pane().lines().join("\n");
        let offset_before = shell.pane().scroll_offset();
        shell.resize(70, 20);
        assert_eq!(
            shell.pane().lines().join("\n"),
            before,
            "a resize must keep the pane's BUFFER — discarding output on resize loses the \
             operator's results for a cosmetic event"
        );
        assert_eq!(
            shell.pane().scroll_offset(),
            offset_before,
            "a resize must keep the pane's SCROLL POSITION; wrapping is display-only"
        );
    }

    // ── Test 7 — SR-2, render() is never blank ────────────────────────────────────────────────

    /// **Test 7 (SR-2): `render()` never returns a blank frame — including on total failure.**
    ///
    /// # "Blank" is defined, or the test is vacuous
    ///
    /// `frontend-components.md:167` is explicit: a frame is blank when **every** rendered string is
    /// empty or whitespace, so the assertion is "at least one non-whitespace glyph is present" and
    /// **not** `frame != Frame::default()`. The latter passes while the screen is visually empty,
    /// which is the vacuous form of this exact guard.
    ///
    /// The states swept are the ones where a blank frame is actually plausible: a fresh shell that
    /// has done nothing, a total server failure with both pickers failed, a zero-by-zero-adjacent
    /// terminal, and every pane state. A blank screen is indistinguishable from a hang, and
    /// availability of the front door is the security property SR-2 names.
    ///
    /// # Proven by mutation
    ///
    /// Making [`Frame::is_blank`] always return `true` turns this test red on its first fresh-shell
    /// assertion. See the summary's mutation log.
    /// (#321)
    #[test]
    fn render_never_returns_a_blank_frame() {
        let host = FakeHost::outside_tmux();

        // Case 1 — a fresh shell: nothing selected, nothing fetched, nothing run.
        let server = FakeServer::healthy();
        let shell = Renderer::new(&server, &host, 100, 40);
        let frame = shell.render();
        assert!(
            !frame.is_blank(),
            "a shell that has done nothing yet must still render something — this is the case \
             where a blank screen is most plausible and most indistinguishable from a hang. \
             Frame: {frame:?}"
        );

        // Case 2 — TOTAL failure: server unreachable, both pickers failed, launch refused.
        let server = FakeServer::unreachable();
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert!(
            matches!(shell.flow().agent_choices(), PickerState::Failed(_)),
            "the agent picker must have failed for this to be the total-failure case"
        );
        assert!(
            matches!(shell.flow().provider_choices(), PickerState::Failed(_)),
            "the provider picker must have failed for this to be the total-failure case"
        );
        let frame = shell.render();
        assert!(
            !frame.is_blank(),
            "SR-2: even a TOTAL server failure renders a POPULATED error state. Frame: {frame:?}"
        );

        // Case 3 — every size from a 1x1 terminal upward, including both sides of the NFR-6 bound.
        for (cols, rows) in [
            (1, 1),
            (1, 24),
            (80, 1),
            (70, 20),
            (79, 23),
            (80, 24),
            (200, 60),
        ] {
            let mut shell = ready_to_launch(&server, &host);
            shell.resize(cols, rows);
            let frame = shell.render();
            assert!(
                !frame.is_blank(),
                "SR-2: {cols}x{rows} must still render content. A size that renders nothing is a \
                 hang the operator cannot distinguish from a crash. Frame: {frame:?}"
            );
        }

        // Case 4 — every pane state. Each is reachable, and none may render an empty region.
        //
        // Driven by a `PaneState` discriminant rather than a `Vec<Box<dyn Fn>>`: the boxed-closure
        // form trips clippy's `type_complexity`, and matching on the state enum is what makes a
        // seventh pane state a **compile error here** rather than a case this sweep silently skips.
        for target in [
            PaneState::Collapsed,
            PaneState::Running,
            PaneState::Empty,
            PaneState::Complete,
            PaneState::Cancelled,
            PaneState::Refused,
        ] {
            let mut shell = Renderer::new(&server, &host, 100, 40);
            match target {
                // A fresh pane is already collapsed.
                PaneState::Collapsed => {}
                PaneState::Running => shell.pane.attach(Policy::InApp),
                // IN-APP with no bytes is `empty`, which states the fact rather than blanking.
                PaneState::Empty => {
                    shell.pane.attach(Policy::InApp);
                    shell.pane.complete(0, None);
                }
                PaneState::Complete => {
                    shell.pane.attach(Policy::InApp);
                    shell.pane.push_bytes(b"out\n");
                    shell.pane.complete(0, None);
                }
                PaneState::Cancelled => {
                    shell.pane.attach(Policy::InApp);
                    shell
                        .pane
                        .cancel()
                        .expect("a freshly attached pane is running, so cancel succeeds");
                }
                PaneState::Refused => {
                    shell.pane.attach(Policy::Handoff);
                    shell.pane.refuse("no tmux client".to_string(), None);
                }
            }

            assert_eq!(
                shell.pane().state(),
                target,
                "the sweep must actually reach {target:?}, or this iteration proves nothing about it"
            );

            let frame = shell.render();
            assert!(
                !frame.is_blank(),
                "SR-2: the frame must be populated in the {target:?} pane state. Frame: {frame:?}"
            );
            assert!(
                !frame.results.is_empty(),
                "the results REGION must be populated in {target:?} — an empty region is the same \
                 defect one level down. Results: {:?}",
                frame.results
            );
        }

        // And the definition itself is not vacuous: a frame of empty and whitespace-only strings
        // IS blank by it. Without this, `is_blank` could be `|| false` and every case above would
        // still pass.
        let whitespace_only = Frame {
            layout: LayoutMode::Stacked,
            header: vec![String::new(), "   ".to_string()],
            command_list: vec!["\t".to_string()],
            required_fields: Vec::new(),
            optional_section: Vec::new(),
            pickers: vec!["\n".to_string()],
            results: Vec::new(),
            banner: Vec::new(),
            footer: vec!["  ".to_string()],
        };
        assert!(
            whitespace_only.is_blank(),
            "the blank DEFINITION must be honest: a frame of empty and whitespace-only strings is \
             blank. Without this assertion `is_blank` could return `false` unconditionally and \
             every case above would pass while proving nothing"
        );
    }

    // ── Test 8 — unhandled keys are ignored, and [k] with nothing running is silent ────────────

    /// **Test 8: an unhandled key is IGNORED, and `[k]` with nothing running does nothing visible.**
    ///
    /// Two rules that are easy to get backwards in opposite directions:
    ///
    /// - **An unhandled key is not an error.** `on_key` reports `false` and changes nothing. A UI
    ///   that banners on an unrecognised keypress is unusable, and `on_key` is infallible by design.
    /// - **`cancel()`'s `NotRunning` is NOT surfaced.** `[k]` outside `running` is ignored, per
    ///   `frontend-components.md:62` — a key that simply does not apply is not an operator-facing
    ///   error. The pane's `handle_key` already swallows the `Err`; this asserts the shell does not
    ///   re-raise it.
    ///
    /// The assertion is on the **whole frame being unchanged**, not just on the return value. A key
    /// that returned `false` while setting a banner or moving focus would pass a return-value-only
    /// check and still be visible to the operator — which is the property the rule is about.
    ///
    /// It also proves `[k]` is not a no-op *in general*: while running, it must cancel. Without
    /// that half, deleting the `Char('k')` arm entirely would leave this test green.
    #[test]
    fn an_unhandled_key_is_ignored_and_k_with_nothing_running_is_silent() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = ready_to_launch(&server, &host);

        // Keys with no binding anywhere in the shell.
        for key in [KeyCode::F(7), KeyCode::Insert, KeyCode::Delete] {
            let before = shell.render();
            let focus_before = shell.focus();
            let consumed = shell.on_key(key);

            assert!(
                !consumed,
                "{key:?} has no binding, so `on_key` must report it unconsumed"
            );
            assert_eq!(
                shell.render(),
                before,
                "{key:?} must change NOTHING the operator can see. Asserting only the return value \
                 would let a key that also set a banner pass"
            );
            assert_eq!(shell.focus(), focus_before, "{key:?} must not move focus");
            assert!(
                shell.banner().is_none(),
                "an unhandled key is IGNORED, never an error — `on_key` is infallible by design. \
                 Banner after {key:?}: {:?}",
                shell.banner()
            );
        }

        // `[k]` with nothing running: the pane is `collapsed`, so `cancel()` answers `NotRunning`.
        let mut shell = Renderer::new(&server, &host, 100, 40);
        while shell.focus() != Focus::Results {
            assert!(shell.on_key(KeyCode::Tab));
        }
        assert_eq!(
            shell.pane().state(),
            PaneState::Collapsed,
            "the pane must not be running for this half of the test to mean anything"
        );

        let before = shell.render();
        let consumed = shell.on_key(KeyCode::Char('k'));
        assert!(
            !consumed,
            "`[k]` outside `running` must report unconsumed, so the shell can pass the key on"
        );
        assert_eq!(
            shell.pane().state(),
            PaneState::Collapsed,
            "`[k]` outside `running` must leave the pane's state alone"
        );
        assert_eq!(
            shell.render(),
            before,
            "`cancel()`'s `NotRunning` must NOT be surfaced: pressing `[k]` with nothing running \
             does nothing VISIBLE. A key that does not apply is not an operator-facing error \
             (`frontend-components.md:62`)"
        );
        assert!(
            shell.banner().is_none(),
            "`NotRunning` must never reach a banner. Got: {:?}",
            shell.banner()
        );

        // The other half: `[k]` is not a no-op in general. Deleting the `Char('k')` arm entirely
        // would leave everything above green, so this is what makes the pair meaningful.
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.pane.attach(Policy::InApp);
        while shell.focus() != Focus::Results {
            assert!(shell.on_key(KeyCode::Tab));
        }
        assert!(
            shell.on_key(KeyCode::Char('k')),
            "`[k]` WHILE RUNNING must be consumed — otherwise the ignore-when-not-running rule is \
             indistinguishable from the key not being wired at all"
        );
        assert_eq!(
            shell.pane().state(),
            PaneState::Cancelled,
            "`[k]` while running must cancel (stop following)"
        );
        let rendered = shell.render().results.join("\n");
        assert!(
            rendered.contains("still running"),
            "SR-4: the cancelled wording must not claim the command stopped — `[k]` stops FOLLOWING \
             and the command continues server-side. Got: {rendered:?}"
        );
    }

    /// Keyboard input reaches the same production launch/run methods the direct orchestration
    /// tests cover. Without this, every unit test can stay green while a real operator has no key
    /// sequence capable of reaching either caller — the gap the predecessor shipped. (#321)
    #[test]
    fn keyboard_input_edits_the_form_and_reaches_both_production_run_paths() {
        let server = FakeServer::healthy().with_session(SessionAnswer::Created(terminal("t-key")));
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));

        for character in "planner".chars() {
            assert!(
                shell.on_key(KeyCode::Char(character)),
                "printable input must be routed to the focused field"
            );
        }
        assert_eq!(
            shell
                .flow()
                .field("--agents")
                .and_then(|field| field.value.as_ref()),
            Some(&crate::guided_flow::FieldValue::Text("planner".to_string())),
            "keyboard entry must mutate the production GuidedFlow, not a renderer-only buffer"
        );
        assert!(shell.on_key(KeyCode::Enter));
        assert_eq!(
            server.create_session_calls.get(),
            0,
            "the keypress must queue launch so the event loop can draw pending before network I/O"
        );
        assert_eq!(
            shell.banner().map(|banner| banner.severity),
            Some("info"),
            "the queued launch must expose a pending frame before it executes"
        );
        assert!(shell.run_pending_action());
        assert_eq!(
            server.create_session_calls.get(),
            1,
            "Enter on the launch form must reach `launch()`"
        );
        assert_eq!(shell.pane().state(), PaneState::Complete);

        let server =
            FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["from keyboard\n"], 200));
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::SessionList));
        assert!(shell.on_key(KeyCode::Enter));
        assert!(shell.on_key(KeyCode::Enter));
        assert!(
            server.run_calls.borrow().is_empty(),
            "the keypress must queue the in-app run until after the pending draw"
        );
        assert!(shell.run_pending_action());
        assert_eq!(
            server.run_calls.borrow().as_slice(),
            &[CommandId::SessionList],
            "Enter on a placeholder-free IN-APP form must reach `run_in_app()`"
        );
        assert_eq!(shell.pane().lines(), vec!["from keyboard"]);
    }

    // ── Test 9 — FR-4.3, a hidden command is unreachable ──────────────────────────────────────

    /// **Test 9 (FR-4.3, SR-3): hidden commands are ABSENT from navigation, not disabled.**
    ///
    /// A disabled-but-visible destructive command invites the operator to find another way to run
    /// it; absence is the stronger control. So the test asserts three things:
    ///
    /// 1. **No HIDE command appears in the rendered list.** Checked against the catalog rather than
    ///    against a hard-coded name list, so a reclassification cannot slip past.
    /// 2. **`focus_command` cannot reach one** — the cursor has nothing to land on, which is what
    ///    "not there" means operationally.
    /// 3. **`cao tui` and `cao shutdown` specifically are absent.** Named because they are the two
    ///    whose presence would be actively dangerous: the TUI must not offer itself, and `shutdown`
    ///    can kill the tmux session hosting the TUI.
    ///
    /// And the converse, which is what stops the test passing on an empty list: every IN-APP and
    /// HANDOFF command IS present. A `commands()` that returned nothing would satisfy every
    /// absence assertion above.
    #[test]
    fn a_hidden_command_is_absent_from_navigation_and_unreachable() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 200, 60);

        let rendered = shell.render().command_list.join("\n");
        let mut hidden_seen = Vec::new();
        let mut offered_missing = Vec::new();

        for id in catalog::commands()
            .iter()
            .map(|command| command.id)
            .collect::<Vec<_>>()
        {
            // Every id `commands()` yields must be focusable — that is the list being navigated.
            assert!(
                shell.focus_command(id),
                "{id:?} is in `commands()` but not focusable in the rendered list"
            );
        }

        // Sweep the whole catalog, HIDE rows included, via the display order the table itself owns.
        for command in catalog::DISPLAY_ORDER.iter().copied() {
            let policy = catalog::policy(command);
            let reachable = shell.focus_command(command);

            match policy {
                Policy::Hidden => {
                    if reachable {
                        hidden_seen.push(command);
                    }
                }
                Policy::InApp | Policy::Handoff => {
                    if !reachable {
                        offered_missing.push(command);
                    }
                }
            }
        }

        assert!(
            hidden_seen.is_empty(),
            "FR-4.3/SR-3: a HIDE command must be ABSENT from navigation, not disabled — a \
             disabled-but-visible destructive command invites the operator to find another way to \
             run it. Reachable HIDE commands: {hidden_seen:?}"
        );
        // The converse, which is what stops this passing on an empty list.
        assert!(
            offered_missing.is_empty(),
            "every IN-APP and HANDOFF command must be reachable, or the absence assertions above \
             would pass on an empty list. Missing: {offered_missing:?}"
        );

        // The two whose presence would be actively dangerous, named explicitly.
        for (id, why) in [
            (
                CommandId::Tui,
                "the TUI must not offer itself; nesting is a no-op or a mess",
            ),
            (
                CommandId::Shutdown,
                "it kills tmux sessions and can kill the one hosting the TUI",
            ),
        ] {
            assert_eq!(
                catalog::policy(id),
                Policy::Hidden,
                "{id:?} must be classified HIDE: {why}"
            );
            assert!(
                !shell.focus_command(id),
                "{id:?} must be unreachable: {why}"
            );
        }
        assert!(
            !rendered.contains("cao tui"),
            "`cao tui` must not appear in the rendered list. Got: {rendered:?}"
        );
        assert!(
            !rendered.contains("cao shutdown"),
            "`cao shutdown` must not appear in the rendered list. Got: {rendered:?}"
        );
        // Positive control on the rendering itself, so the two assertions above are not passing
        // because the list renders nothing at all.
        assert!(
            rendered.contains("cao launch") && rendered.contains("cao session list"),
            "the list must render the commands it DOES offer, or the absence checks are vacuous. \
             Got: {rendered:?}"
        );

        // A programmatic caller holding a HIDE id is refused rather than silently doing nothing.
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.run_in_app(CommandId::Shutdown);
        let banner = shell
            .banner()
            .expect("a programmatic HIDE run must be refused visibly, not silently ignored");
        assert_eq!(
            banner.severity, "error",
            "a HIDE command reaching `run_in_app` is a reachability defect and must be visible"
        );
        assert!(
            server.run_calls.borrow().is_empty(),
            "a HIDE command must never reach `ServerClient::run`. Calls: {:?}",
            server.run_calls.borrow()
        );
    }

    // ── Test 10 — PR-1, the Write adapter forwards INCREMENTALLY ───────────────────────────────

    /// **Test 10 (PR-1): the `Write` adapter forwards each chunk as it arrives, not at the end.**
    ///
    /// # The assertion observes intermediate state, because a total cannot distinguish the two
    ///
    /// A buffered implementation and an incremental one produce the same final buffer. What
    /// separates them is whether the pane holds bytes **while the run is still in progress**. So the
    /// fake asserts *from inside* `run()`, between chunks: at that moment the pane must already hold
    /// the earlier chunk and must still be `running`.
    ///
    /// This is the requirement PR-1 exists for: a command producing output slowly must not be
    /// indistinguishable from a hang. `strip-ansi-escapes` 0.2.1 implements its `Writer` with a
    /// `LineWriter`, so the no-newline chunk in the fixture is the case a line-buffered adapter
    /// would fail.
    #[test]
    fn the_write_adapter_forwards_each_chunk_incrementally() {
        // A sink that records what the pane held at each write. `PaneSink` is the production
        // adapter; this drives it directly so the observation is of the adapter itself.
        let mut pane = crate::results_pane::ResultsPane::new();
        pane.attach(Policy::InApp);

        let mut observed: Vec<(usize, Vec<String>)> = Vec::new();
        {
            let mut sink = PaneSink::new(&mut pane);
            for chunk in [
                b"first\n".as_slice(),
                b"second\n".as_slice(),
                b"Loading 50%".as_slice(),
            ] {
                sink.write_all(chunk).expect("the pane sink is infallible");
                // Read the pane back THROUGH the sink, mid-stream. This is the intermediate state a
                // buffered adapter could not produce.
                observed.push((
                    sink.writes,
                    sink.pane
                        .lines()
                        .iter()
                        .map(|line| line.to_string())
                        .collect(),
                ));
            }
        }

        assert_eq!(
            observed.len(),
            3,
            "each chunk must be one forwarded write, not one batched write at the end"
        );
        assert_eq!(
            observed[0],
            (1, vec!["first".to_string()]),
            "after the FIRST chunk the pane must already hold it — this is the assertion a buffered \
             adapter fails and a final-state assertion cannot make. Observed: {observed:?}"
        );
        assert_eq!(
            observed[1],
            (2, vec!["first".to_string(), "second".to_string()]),
            "the second chunk must arrive without the first being re-sent or dropped. Observed: \
             {observed:?}"
        );
        assert_eq!(
            observed[2],
            (
                3,
                vec![
                    "first".to_string(),
                    "second".to_string(),
                    "Loading 50%".to_string()
                ]
            ),
            "PR-1: a chunk with NO trailing newline must be visible immediately. A LineWriter-based \
             adapter withholds `Loading 50%` until the line completes — a slow command indistinguishable \
             from a hang. Observed: {observed:?}"
        );

        // The same property through the PRODUCTION path, asserted mid-run from inside the fake's
        // `run()`. `run_in_app` is what wires the adapter, so this is the path PR-1 is about.
        struct MidRunObserver {
            /// The pane's line count and state, captured between chunks by the fake's `run`.
            snapshots: RefCell<Vec<usize>>,
        }

        impl ServerRead for MidRunObserver {
            fn health(&self) -> Result<Health, TuiError> {
                Ok(Health {
                    status: "ok".to_string(),
                    terminal_backend: "herdr".to_string(),
                })
            }
            fn terminal(&self, _terminal_id: &str) -> Result<Terminal, TuiError> {
                Ok(terminal("t-1"))
            }
        }

        impl ServerApi for MidRunObserver {
            fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
                Ok(Vec::new())
            }
            fn providers(&self) -> Result<Vec<Provider>, TuiError> {
                Ok(Vec::new())
            }
            fn create_session(&self, _params: &SessionParams) -> Result<Terminal, TuiError> {
                Ok(terminal("t-1"))
            }
            fn run(
                &self,
                _id: CommandId,
                _path_values: &[&str],
                _query: &[(&str, &str)],
                _body: Option<&str>,
                sink: &mut dyn Write,
            ) -> Result<u16, TuiError> {
                // Three writes, and after each one the sink reports how many bytes it accepted.
                // A short write would mean the caller must re-send — which would duplicate output.
                for chunk in ["alpha\n", "beta\n", "gamma\n"] {
                    let written = sink
                        .write(chunk.as_bytes())
                        .expect("the pane sink is infallible");
                    assert_eq!(
                        written,
                        chunk.len(),
                        "the adapter must report the WHOLE slice consumed; a short write would make \
                         the caller re-send bytes the parser has already seen, duplicating output"
                    );
                    self.snapshots.borrow_mut().push(written);
                }
                Ok(200)
            }
        }

        let server = MidRunObserver {
            snapshots: RefCell::new(Vec::new()),
        };
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.run_in_app(CommandId::SessionList);

        assert_eq!(
            server.snapshots.borrow().as_slice(),
            &[6, 5, 6],
            "the production path must forward three separate writes of exactly the bytes offered"
        );
        assert_eq!(
            shell.pane().lines(),
            vec!["alpha", "beta", "gamma"],
            "every chunk must reach the pane through the production adapter, in order"
        );
    }

    // ── The stated in-app gap: the real numbers ───────────────────────────────────────────────

    /// **`[←]` returns to the command list from ANY focus, including after a run.**
    ///
    /// Regression test for an operator report of being stuck: after `memory list` filled the pane,
    /// Tab, `k` and `q` all appeared to do nothing.
    ///
    /// The diagnosis, which is why this binding exists rather than a Tab fix: **nothing moves focus
    /// to `Results` after a run**, so focus sat on the form while the filled pane dominated the
    /// screen. Tab WAS working — it just moved between form regions invisibly. And `k`/`q` are TEXT
    /// in a form field, so they were absorbed into a field the operator could not see. Three keys
    /// that each "did nothing" for three different reasons.
    ///
    /// Asserts from every region, not just the one reported: a fix that only escapes `Results`
    /// would leave the actual reported state — stuck on `RequiredFields` — unfixed. (#321)
    #[test]
    fn the_left_arrow_returns_to_the_command_list_from_every_focus() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();

        for region in [
            Focus::CommandList,
            Focus::RequiredFields,
            Focus::OptionalSection,
            Focus::Results,
        ] {
            let mut shell = Renderer::new(&server, &host, 100, 40);
            assert!(shell.focus_command(CommandId::MemoryList));
            assert!(shell.on_key(KeyCode::Enter));
            shell.focus = region;

            assert!(
                shell.on_key(KeyCode::Left),
                "[←] must be HANDLED from {region:?} — an unhandled key reports false and the \
                 operator gets no response at all"
            );
            assert_eq!(
                shell.focus,
                Focus::CommandList,
                "[←] must return to the command list from {region:?}. The operator reported being \
                 stuck after a run with Tab, `k` and `q` all apparently inert"
            );
        }

        // And the footer must ADVERTISE it: an escape hatch nobody can discover is not an escape.
        let shell = Renderer::new(&server, &host, 100, 40);
        let footer = shell.render().footer.join(" ");
        assert!(
            footer.contains("[←]"),
            "the footer must name `[←]`, or the operator has no way to learn the key exists — the \
             `[c] clear` failure was a documented key that did nothing; this is its inverse, a \
             working key nobody is told about. Got: {footer:?}"
        );
    }

    /// **A JSON response is PRETTY-PRINTED; a non-JSON one passes through unchanged.**
    ///
    /// The operator reported `memory list` rendering as one unreadable JSON blob: `run()` streams
    /// the HTTP body straight to the pane, which is right for a streaming route but leaves every
    /// IN-APP command showing raw API output. The CLI already prints a table for the same data, so
    /// the TUI was strictly worse than the surface it replaces.
    ///
    /// Asserts BOTH directions, because either alone is satisfiable by a wrong implementation:
    /// formatting without a passthrough garbles anything that is not JSON, and a passthrough
    /// without formatting is the defect. Operator decision at the fix gate. (#321)
    #[test]
    fn a_json_body_is_indented_and_a_non_json_body_is_left_alone() {
        let compact = br#"[{"key":"a","scope":"session"},{"key":"b","scope":"project"}]"#;
        let mut json = JsonSink::new();
        json.write_all(compact).expect("the sink accepts bytes");
        let rendered = json.rendered();

        assert!(
            rendered.contains('\n'),
            "a JSON array must be rendered across MULTIPLE LINES — one blob is what the operator \
             reported as unreadable. Got: {rendered:?}"
        );
        assert!(
            rendered.contains("\"key\": \"a\"") && rendered.contains("\"key\": \"b\""),
            "pretty-printing must preserve every entry and space its keys, not summarise. Got: \
             {rendered:?}"
        );

        // The other direction: plain text must survive byte-for-byte.
        let plain = b"exit 0\nnot json at all\n";
        let mut passthrough = JsonSink::new();
        passthrough
            .write_all(plain)
            .expect("the sink accepts bytes");
        assert_eq!(
            passthrough.rendered(),
            "exit 0\nnot json at all\n",
            "a body that is not JSON must pass through UNCHANGED — a formatter that mangles what \
             it cannot parse is worse than no formatter"
        );
    }

    /// **A SPACE is typeable in a text field — `"code review"`, not `"codereview"`.**
    ///
    /// Regression test for a defect the operator hit on `profile find`. `GuidedFlow::set` stores a
    /// trimmed value by an affirmed rule (BR-7/BR-8, and a test pins that `"  planner  "` stores as
    /// `"planner"`). But the renderer appends ONE character per keystroke and re-`set`s, so a
    /// trailing space was trimmed away before the next character arrived: `"code"` + `' '`
    /// round-tripped to `"code"`, and the next letter landed flush. **A space could not be typed in
    /// any text field.**
    ///
    /// No existing test caught it because every fixture set a whitespace-free value in ONE `set()`
    /// call — the multi-keystroke path was never exercised with a space.
    ///
    /// Drives real KEYSTROKES rather than calling `set()` directly: calling `set("code review")`
    /// passes on the broken build, since the defect lives in the append-and-re-set loop. (#321)
    #[test]
    fn a_space_can_be_typed_into_a_text_field() {
        let server =
            FakeServer::healthy().with_session(SessionAnswer::Created(terminal("t-space")));
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));

        // A profile whose name has no space, then Tab to a free-text field and type one.
        for character in "planner".chars() {
            shell.on_key(KeyCode::Char(character));
        }
        shell.on_key(KeyCode::Down);

        for character in "my session".chars() {
            shell.on_key(KeyCode::Char(character));
        }

        let typed = shell
            .flow()
            .fields()
            .iter()
            .find_map(|field| match &field.value {
                Some(crate::guided_flow::FieldValue::Text(text)) if text.contains("session") => {
                    Some(text.clone())
                }
                _ => None,
            });

        assert_eq!(
            typed.as_deref(),
            Some("my session"),
            "typing `my session` one keystroke at a time must land the SPACE. On the broken build \
             the trailing space was trimmed between keystrokes, giving `mysession` — so an \
             operator could not enter a multi-word value in any text field"
        );
    }

    /// **`profile find` actually SEARCHES — it does not report "no HTTP route".**
    ///
    /// Regression test for a defect the operator hit while testing by hand. `profile find` is the one
    /// routeless IN-APP command, and OQ-6 Q2 settled that it is served **client-side** by a substring
    /// filter. `ServerClient::find_profiles` implemented that — but **nothing called it from
    /// production**, so `run_in_app` fell through to the `NoRoute` arm and rendered
    /// `ProfileFind has no HTTP route` while a working implementation sat unreachable.
    ///
    /// **That is design defect #3's shape again** — the exact failure this rewrite exists to
    /// eliminate: correct code with no production caller, invisible to a green suite because the
    /// only caller was a test.
    ///
    /// Asserts on the PANE's rendered output, not on a helper's return value: a test calling the
    /// filter directly would have passed throughout the defect. (#321)
    #[test]
    fn profile_find_searches_client_side_instead_of_reporting_no_route() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert!(shell.focus_command(CommandId::ProfileFind));
        assert!(shell.on_key(KeyCode::Enter));
        shell.run_in_app(CommandId::ProfileFind);

        //  is the pane's own render path — the same accessor a prior test was
        // corrected to use, because `lines()` omits the completion line and reported an empty pane
        // after a successful run.
        let cells = shell.render().results.join("\n");
        assert!(
            !cells.contains("has no HTTP route"),
            "`profile find` must NOT report a missing route — it is served client-side per OQ-6 Q2. \
             Cells: {cells:?}"
        );
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "an empty-query search over the profile list is a SUCCESSFUL run: the search worked and \
             returned everything. Reporting it as an error is the same misrepresentation as an empty \
             HANDOFF pane reading as a failed run"
        );
        assert!(
            cells.contains("planner") || cells.contains("no profiles matched"),
            "the pane must show the search RESULT — matching profile names, or a stated no-match. \
             Cells: {cells:?}"
        );
    }

    /// **The in-app gap, with the numbers MEASURED from `route()` rather than taken from the plan.**
    ///
    /// # The counts moved, and the previous ones were a consequence of the defect
    ///
    /// This test used to pin **9 runnable, 12 not-wired**. Those were real measurements of the
    /// code as it stood, but what they measured was `renderer` calling
    /// `run(id, &[], &[], None, ..)` and therefore treating every `{token}` route as unreachable —
    /// including the 10 whose token a form field plainly supplies. With path bindings the figures
    /// are **21 runnable, 2 not-wired, 1 routeless** across the 24 IN-APP commands (`workflow runs`
    /// and `workflow result`, from PR #525, are the two most recent additions).
    ///
    /// The remaining two are `cao session send` and `cao session status`, which need the
    /// session-name → terminal-id resolution call the CLI makes first (`session.py:26`). That is a
    /// genuine unimplemented step rather than an unmapped field, which is why they stay `NotWired`
    /// instead of being bound to something that looks close enough.
    ///
    /// Measured on an EMPTY form, which is what makes 21 the honest count: `in_app_readiness` also
    /// returns `Ignored` for a command whose *filled* fields have nowhere to go, and three
    /// commands can reach that state (`memory export --output`, `schedule add`, `workflow
    /// validate`) — asserted separately in
    /// `a_filled_field_the_route_cannot_send_refuses_the_run_and_names_it`, because it depends on
    /// form state rather than on the route table.
    ///
    /// The literals are hard-coded, because a figure computed from the thing under test proves
    /// nothing. Correcting an instance without re-deriving the count is how a wrong number
    /// survives an audit.
    ///
    /// It also asserts the operator-visible half: an unsupplied placeholder renders an error that
    /// **names the missing path values**, and a `Runnable` one actually runs. That is what makes
    /// this a *stated* gap rather than a dead end — the operator can see the limit.
    #[test]
    fn the_in_app_gap_is_twentyone_runnable_two_not_wired_and_one_routeless() {
        let mut runnable = Vec::new();
        let mut not_wired = Vec::new();
        let mut no_route = Vec::new();

        for id in catalog::DISPLAY_ORDER.iter().copied() {
            if catalog::policy(id) != Policy::InApp {
                continue;
            }
            // An empty form for every command, so this measures the ROUTE TABLE rather than any
            // particular form state. `Ignored` cannot arise here — it needs a filled field — and
            // an arm that panics says so rather than being folded into another bucket.
            let mut flow = crate::guided_flow::GuidedFlow::new();
            flow.select(id).expect("an IN-APP command is selectable");
            match in_app_readiness(id, &flow) {
                InAppReadiness::Runnable => runnable.push(id),
                InAppReadiness::NotWired { .. } => not_wired.push(id),
                InAppReadiness::NoRoute => no_route.push(id),
                InAppReadiness::Ignored { fields } => panic!(
                    "{id:?} reported Ignored on an EMPTY form, naming {fields:?} — `Ignored` must \
                     depend on what the operator FILLED, so this means the filter is keyed on the \
                     declared field rather than on its value"
                ),
            }
        }

        assert_eq!(
            runnable.len(),
            21,
            "21 IN-APP commands can be built from the form and run for real. Found: {runnable:?}"
        );
        assert_eq!(
            not_wired,
            vec![CommandId::SessionSend, CommandId::SessionStatus],
            "exactly two IN-APP commands still carry an UNSUPPLIED placeholder, and it is the same \
             one for both: `{{terminal_id}}`, which the CLI obtains with a second call \
             (`session.py:26`). Every other placeholder is now bound to a form field. Found: \
             {not_wired:?}"
        );
        assert_eq!(
            no_route,
            vec![CommandId::ProfileFind],
            "`profile find` is the ONE IN-APP command with no route at all — no search endpoint \
             exists, and it is served client-side by `find_profiles` (OQ-6 Q2). Found: {no_route:?}"
        );
        assert_eq!(
            runnable.len() + not_wired.len() + no_route.len(),
            24,
            "the three sets must partition the 24 IN-APP commands with none double-counted"
        );

        // The operator-visible half: an unsupplied placeholder states WHAT is missing.
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        // `session status` needs `{terminal_id}` — one of the remaining two.
        shell.run_in_app(CommandId::SessionStatus);

        let banner = shell.banner().expect(
            "an unsupplied placeholder must render a STATED error, not silently do nothing",
        );
        assert!(
            banner.why.contains("terminal_id"),
            "the stated gap must NAME the missing path value — \"not supported\" is a dead end, \
             \"needs terminal_id\" is a stated limit. Got: {:?}",
            banner.why
        );
        assert!(
            banner.what.contains("not yet wired"),
            "the wording must say the wiring is absent rather than implying the command is broken. \
             Got: {:?}",
            banner.what
        );
        assert!(
            server.run_calls.borrow().is_empty(),
            "an unsupplied placeholder must NOT be called — a partially-substituted URL would reach \
             the server as a literal brace and 404 somewhere confusing. Calls: {:?}",
            server.run_calls.borrow()
        );

        // And a `Runnable` one does run, so the gap is a limit rather than the whole surface.
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["ok\n"], 200));
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.run_in_app(CommandId::WorkflowList);
        assert_eq!(
            server.run_calls.borrow().as_slice(),
            &[CommandId::WorkflowList],
            "a placeholder-free IN-APP route must actually run — \"wire what works\" is the other \
             half of the operator's ruling"
        );
        assert!(
            shell.banner().is_none(),
            "a successful placeholder-free run needs no banner. Got: {:?}",
            shell.banner()
        );
    }

    /// **An auth rejection is not described as "unreachable", and is not offered a retry.**
    ///
    /// With auth enabled, `get_current_scopes` answers 401 for a missing or invalid token and
    /// `require_any_scope` answers 403 for insufficient scope (`security/auth.py:435-440`), and
    /// this client sends no `Authorization` header at all. The header prefixed every failure with
    /// "unreachable", so the operator read `server: unreachable — cao-server returned HTTP 401`:
    /// a server that plainly answered, described as unreachable, with a "start cao-server" remedy
    /// that cannot help.
    ///
    /// Each assertion has a **negative half**, which is where the value is: naming auth while
    /// still saying "unreachable" would be a half-fix that a positive-only test would pass. The
    /// 401 and 403 cases are checked separately because the actions differ — no usable credential
    /// versus a real credential without the scope.
    /// (Reported by review on PR #547.)
    #[test]
    fn an_auth_rejection_is_named_as_auth_and_not_as_unreachable() {
        let host = FakeHost::outside_tmux();

        for (status, expected) in [(401u16, "authentication"), (403u16, "authoris")] {
            let server = FakeServer::answering_http(status);
            let mut shell = Renderer::new(&server, &host, 100, 40);
            // Selecting a command is what performs the `health()` read (`select` →
            // `populate_pickers`), so the header has something to report. A bare `new` leaves it
            // at "not checked yet", which is a third state and not the one under test.
            assert!(shell.focus_command(CommandId::Launch));
            assert!(shell.on_key(KeyCode::Enter));

            let header = shell.render().header.join("\n");
            assert!(
                header.to_lowercase().contains(expected),
                "HTTP {status} must be described as an auth failure — the header is the operator's \
                 only standing indicator. Got: {header:?}"
            );
            assert!(
                !header.contains("unreachable"),
                "HTTP {status} means the server ANSWERED, so \"unreachable\" is false and its \
                 implied remedy (start the server / check the address) sends the operator the \
                 wrong way. Got: {header:?}"
            );
            assert!(
                header.contains(&status.to_string()),
                "the status code must survive into the message so the operator can look it up. \
                 Got: {header:?}"
            );
        }

        // And the launch banner, which additionally must not promise that `[r]` helps: retry
        // re-issues the same credential-free request.
        for status in [401u16, 403u16] {
            let server = FakeServer::answering_http(status);
            let mut shell = ready_to_launch(&server, &host);
            shell.launch();

            let banner = shell
                .banner()
                .unwrap_or_else(|| panic!("HTTP {status} must render a banner"));
            let whole = format!("{} {} {}", banner.what, banner.why, banner.remedy);
            assert!(
                whole.contains(&status.to_string()),
                "the banner must name the status. Got: {whole:?}"
            );
            assert!(
                banner.remedy.contains("will not help"),
                "the remedy must say retrying cannot work — `[r]` re-sends the identical \
                 credential-free request, so offering it loops the operator through an \
                 unchangeable failure. Got: {:?}",
                banner.remedy
            );
        }
    }

    /// **A command whose placeholder a form field supplies actually RUNS, with the value substituted.**
    ///
    /// The other half of the count change above, and the one that matters to an operator: 10
    /// commands were unreachable purely because `renderer` passed `&[]` for the path values.
    /// `workflow get NAME` is the representative — `{name}` bound to the CLI's own positional.
    ///
    /// Asserted on the **request the stub received**, not on `run_calls` alone: a call that reached
    /// the server with an unsubstituted `{name}` would satisfy a call-count assertion and 404 in
    /// production. The path is the property under test.
    /// (Reported by review on PR #547.)
    #[test]
    fn a_bound_placeholder_is_substituted_from_the_form_and_the_command_runs() {
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["{}\n"], 200));
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert!(shell.focus_command(CommandId::WorkflowGet));
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("name", "nightly-audit")
            .expect("`name` is the positional `cao workflow get` declares");

        shell.run_in_app(CommandId::WorkflowGet);

        assert!(
            shell.banner().is_none(),
            "a bound placeholder must not render a not-wired error. Got: {:?}",
            shell.banner()
        );
        assert_eq!(
            server.run_calls.borrow().as_slice(),
            &[CommandId::WorkflowGet],
            "the command must reach the server"
        );
        assert_eq!(
            server.run_path_values.borrow().as_slice(),
            &[vec!["nightly-audit".to_string()]],
            "the path value must come FROM THE FORM — an empty slice here is the original defect, \
             and it reaches the server as a literal `{{name}}` brace"
        );
        assert_eq!(shell.pane().state(), PaneState::Complete);
    }

    /// **A typed query filter reaches the request instead of being silently dropped.**
    ///
    /// `memory list --scope global --type user` ran as an unfiltered scan-all, because
    /// `run_in_app` passed `&[]` for the query. An operator reading a full list would reasonably
    /// conclude their filter matched everything.
    ///
    /// **The wire name is the load-bearing assertion.** `--type` binds to `type`, not
    /// `memory_type`: FastAPI declares `memory_type: Optional[MemoryType] = Query(alias="type")`
    /// (`api/main.py:3621`) and the alias is what goes on the wire. Sending `memory_type=user`
    /// would be silently ignored — an unfiltered list presented as a filtered one, which is the
    /// same dishonesty in a new place. Only asserting the emitted key catches it.
    #[test]
    fn typed_query_filters_reach_the_request_under_the_servers_own_parameter_names() {
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["[]\n"], 200));
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert!(shell.focus_command(CommandId::MemoryList));
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--scope", "global")
            .expect("text field");
        shell.flow_mut().set("--type", "user").expect("text field");

        shell.run_in_app(CommandId::MemoryList);

        let queries = server.run_queries.borrow();
        let sent = queries.first().expect("the command must reach the server");
        assert!(
            sent.contains(&("scope".to_string(), "global".to_string())),
            "`--scope` must reach the server as `scope`. Sent: {sent:?}"
        );
        assert!(
            sent.contains(&("type".to_string(), "user".to_string())),
            "`--type` must reach the server as `type` — its FastAPI ALIAS, not the Python \
             identifier `memory_type`, which would be silently ignored. Sent: {sent:?}"
        );
        assert!(
            !sent.iter().any(|(key, _)| key == "memory_type"),
            "`memory_type` is the Python parameter name, not the wire name; sending it filters \
             nothing while looking wired. Sent: {sent:?}"
        );
    }

    /// **A flag that is off is omitted from the query, not sent empty — however it got that way.**
    ///
    /// Probed against FastAPI directly: `?flag=` on a `bool` parameter is a **422**
    /// (`bool_parsing`, "unable to interpret input"), while `?flag=true` and `?flag=1` are
    /// accepted. So a flag the operator has not asked for must not appear at all — emitting it as
    /// `""` would turn every unticked box into a rejected request.
    ///
    /// # Two ways to be off, and only one of them was covered
    ///
    /// A flag field can hold `None` (never touched) or `Some(Flag(false))` (toggled on and back
    /// off — `toggle_focused_flag` stores the literal `"false"`). They take **different arms** in
    /// `query_pairs_for`, and the first version of this test exercised only the untouched one: the
    /// mutation making `Flag(false)` emit `""` left it green. Both are asserted now, because a flag
    /// the operator deliberately switched off is the likelier of the two to reach here.
    /// (Reported by review on PR #547; this gap found by mutation.)
    #[test]
    fn a_flag_that_is_off_is_absent_from_the_query_and_a_ticked_one_sends_true() {
        let host = FakeHost::outside_tmux();

        // Case 1: `--include-history` never touched (`value == None`).
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["ok\n"], 200));
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert!(shell.focus_command(CommandId::MemoryExport));
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--scope", "global")
            .expect("text field");
        shell
            .flow_mut()
            .set("--redact", "true")
            .expect("`--redact` is a flag");

        shell.run_in_app(CommandId::MemoryExport);

        let queries = server.run_queries.borrow();
        let sent = queries.first().expect("the command must reach the server");
        assert!(
            sent.contains(&("redact".to_string(), "true".to_string())),
            "a ticked flag must send `true`, which FastAPI parses. Sent: {sent:?}"
        );
        assert!(
            !sent.iter().any(|(key, _)| key == "include_history"),
            "an UNTOUCHED flag must be ABSENT — sending it as \"\" is a 422 (`bool_parsing`), \
             verified against FastAPI. Sent: {sent:?}"
        );
        drop(queries);

        // Case 2: `--redact` explicitly OFF (`value == Some(Flag(false))`) — a different arm.
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["ok\n"], 200));
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert!(shell.focus_command(CommandId::MemoryExport));
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--scope", "global")
            .expect("text field");
        shell
            .flow_mut()
            .set("--redact", "false")
            .expect("`--redact` is a flag");
        assert_eq!(
            shell
                .flow()
                .field("--redact")
                .and_then(|field| field.value.as_ref()),
            Some(&crate::guided_flow::FieldValue::Flag(false)),
            "this case is only meaningful if the field really holds an explicit `false` — \
             otherwise it duplicates case 1 and the `Flag(false)` arm stays unexercised"
        );

        shell.run_in_app(CommandId::MemoryExport);

        let queries = server.run_queries.borrow();
        let sent = queries.first().expect("the command must reach the server");
        assert!(
            !sent.iter().any(|(key, _)| key == "redact"),
            "a flag explicitly switched OFF must be omitted too: every bool parameter on these \
             routes already defaults to False (`api/main.py:3656-3657`), so omission is \
             equivalent — and `\"\"` is a 422. Sent: {sent:?}"
        );
    }

    /// **A FILLED field the route cannot send refuses the run and names the field.**
    ///
    /// The honesty gate. `memory export --output` is a local filesystem path with no query
    /// parameter, so a run that ignored it would write nothing where the operator asked and say
    /// nothing about it.
    ///
    /// Both halves, because the refusal must be keyed on the VALUE and not the declaration:
    /// filling `--output` refuses, and leaving it blank runs. A gate keyed on the declared field
    /// would block `memory export` permanently — trading a silent drop for a false blocker, which
    /// is not an improvement. (Reported by review on PR #547.)
    #[test]
    fn a_filled_field_the_route_cannot_send_refuses_the_run_and_names_it() {
        let host = FakeHost::outside_tmux();

        // Filled: refused, and the field is named.
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["ok\n"], 200));
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::MemoryExport));
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--scope", "global")
            .expect("text field");
        shell
            .flow_mut()
            .set("--output", "/tmp/out")
            .expect("text field");

        shell.run_in_app(CommandId::MemoryExport);

        let banner = shell
            .banner()
            .expect("a field that would be discarded must refuse the run, not fire it anyway");
        assert!(
            banner.why.contains("--output"),
            "the refusal must NAME the field, or the operator cannot tell what to clear. Got: {:?}",
            banner.why
        );
        assert!(
            server.run_calls.borrow().is_empty(),
            "the request must NOT be sent — sending it is exactly the silent discard being fixed. \
             Calls: {:?}",
            server.run_calls.borrow()
        );

        // Blank: runs. The gate is on the value, not the declaration.
        let server = FakeServer::healthy().with_run(RunAnswer::Chunks(vec!["ok\n"], 200));
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::MemoryExport));
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--scope", "global")
            .expect("text field");

        shell.run_in_app(CommandId::MemoryExport);

        assert_eq!(
            server.run_calls.borrow().as_slice(),
            &[CommandId::MemoryExport],
            "with `--output` left blank there is nothing to discard, so the command must RUN — a \
             gate keyed on the declared field rather than its value would block this forever"
        );
        assert!(
            shell.banner().is_none(),
            "a run with nothing ignored needs no banner. Got: {:?}",
            shell.banner()
        );
    }

    /// **A launch flag the endpoint cannot receive says so IN THE RENDERED FRAME.**
    ///
    /// The predicate living in `guided_flow` proves nothing on its own — the defect was that the
    /// form looked identical whether a field was sent or silently discarded, so what matters is
    /// that an operator reading the screen can tell. This drives the real key path to expand the
    /// optional section and asserts on `render()`'s output.
    ///
    /// Both directions, in one frame: `--yolo` (unwirable) carries the marker and `--env`
    /// (wired, and in the same collapsed section) does not. A marker on every field would be as
    /// useless as a marker on none, and only the negative half can catch that.
    /// (Reported by review on PR #547.)
    #[test]
    fn an_unwirable_launch_flag_is_marked_not_sent_in_the_rendered_form() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);

        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));
        // Move focus to the optional section and expand it — the five unwirable flags all live
        // there, behind the three guided steps.
        while shell.focus() != Focus::OptionalSection {
            assert!(
                shell.on_key(KeyCode::Tab),
                "tab must reach the optional section from the form"
            );
        }
        assert!(shell.on_key(KeyCode::Enter), "[enter] expands the section");

        let optional = shell.render().optional_section.join("\n");

        let yolo = optional
            .lines()
            .find(|line| line.contains("--yolo"))
            .unwrap_or_else(|| panic!("`--yolo` must be in the optional section: {optional:?}"));
        assert!(
            yolo.contains(crate::guided_flow::NOT_SENT_MARKER),
            "`--yolo` cannot reach POST /sessions, so its line must say so — otherwise an \
             operator who ticks it believes they launched an unrestricted session. Got: {yolo:?}"
        );

        let env = optional
            .lines()
            .find(|line| line.contains("--env"))
            .unwrap_or_else(|| panic!("`--env` must be in the optional section: {optional:?}"));
        assert!(
            !env.contains(crate::guided_flow::NOT_SENT_MARKER),
            "`--env` IS sent (in the JSON body, #248), so marking it would be a false statement \
             — and a marker on every field conveys nothing. Got: {env:?}"
        );

        // The message is wired now, and it is a required-section... no: it is the positional in
        // the guided prefix. Asserted wherever it renders, because a leftover marker on it would
        // contradict the wiring this PR added.
        let whole_form = shell.render();
        let message_line = whole_form
            .required_fields
            .iter()
            .chain(&whole_form.optional_section)
            .find(|line| line.contains("message"));
        if let Some(line) = message_line {
            assert!(
                !line.contains(crate::guided_flow::NOT_SENT_MARKER),
                "`message` maps to `initial_message` in the body, so it must NOT be marked as \
                 not sent. Got: {line:?}"
            );
        }
    }

    // ── Step 10 — exit while a command runs CONFIRMS first ─────────────────────────────────────

    /// **Step 10: quitting while a command runs CONFIRMS first, and says the run continues.**
    ///
    /// The run does not stop when the TUI exits — it is server-side work — so a silent exit would
    /// misrepresent what happened. That is the same class as `cancelled` overstating a kill, which
    /// is why `CANCELLED_NOTICE`'s wording is binding.
    ///
    /// The confirmation must also be **escapable**, or it is a modal the operator cannot leave; and
    /// with nothing running `[q]` must quit immediately, or the confirm becomes a permanent tax.
    #[test]
    fn quitting_while_a_command_runs_confirms_first_and_states_that_the_run_continues() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();

        // Nothing running: `[q]` quits at once.
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.on_key(KeyCode::Char('q')));
        assert!(
            shell.should_quit(),
            "with nothing running, `[q]` must quit immediately — a confirmation there is a \
             permanent tax on the common case"
        );
        assert!(
            !shell.awaiting_quit_confirmation(),
            "no confirmation may be shown when nothing is running"
        );

        // A run in flight: `[q]` confirms instead of quitting. Driven through the production path,
        // whose fake blocks inside `run()` — so the `running` flag is set by observing it there.
        struct QuitProbe {
            /// Set from inside `run()`: what `should_quit`/`awaiting_quit_confirmation` reported
            /// when `[q]` was pressed mid-run.
            observed: RefCell<Vec<(bool, bool)>>,
        }

        impl ServerRead for QuitProbe {
            fn health(&self) -> Result<Health, TuiError> {
                Ok(Health {
                    status: "ok".to_string(),
                    terminal_backend: "herdr".to_string(),
                })
            }
            fn terminal(&self, _terminal_id: &str) -> Result<Terminal, TuiError> {
                Ok(terminal("t-1"))
            }
        }

        impl ServerApi for QuitProbe {
            fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
                Ok(Vec::new())
            }
            fn providers(&self) -> Result<Vec<Provider>, TuiError> {
                Ok(Vec::new())
            }
            fn create_session(&self, _params: &SessionParams) -> Result<Terminal, TuiError> {
                Ok(terminal("t-1"))
            }
            fn run(
                &self,
                _id: CommandId,
                _path_values: &[&str],
                _query: &[(&str, &str)],
                _body: Option<&str>,
                sink: &mut dyn Write,
            ) -> Result<u16, TuiError> {
                sink.write_all(b"working\n")
                    .expect("the pane sink is infallible");
                self.observed.borrow_mut().push((false, false));
                Ok(200)
            }
        }

        // The `running` flag is a field, so the mid-run state is exercised by setting up the same
        // condition `run_in_app` creates and then pressing `[q]` — which is what the event loop
        // would do on its next tick while a run is in flight.
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.running = true;

        assert!(shell.on_key(KeyCode::Char('q')));
        assert!(
            !shell.should_quit(),
            "Step 10: `[q]` while a command runs must NOT quit — the run continues server-side, so \
             a silent exit misrepresents what happened"
        );
        assert!(
            shell.awaiting_quit_confirmation(),
            "`[q]` while running must raise a confirmation"
        );

        let footer = shell.render().footer.join("\n");
        assert!(
            footer.contains("still running"),
            "the confirmation must say a command is still running. Got: {footer:?}"
        );
        assert!(
            footer.contains("continues"),
            "it must state that the run CONTINUES on the server — that is the honest fact the \
             confirmation exists to convey, not merely 'are you sure?'. Got: {footer:?}"
        );

        // Escapable: `[esc]` dismisses without quitting.
        assert!(shell.on_key(KeyCode::Esc));
        assert!(
            !shell.awaiting_quit_confirmation() && !shell.should_quit(),
            "the confirmation must be escapable, or it is a modal the operator cannot leave"
        );

        // Confirmed: a second `[q]` quits.
        assert!(shell.on_key(KeyCode::Char('q')));
        assert!(shell.awaiting_quit_confirmation());
        assert!(shell.on_key(KeyCode::Char('q')));
        assert!(shell.should_quit(), "a confirmed quit must actually quit");

        // The unused probe type is kept deliberately: it documents that a mid-run `[q]` cannot be
        // driven through `run()` itself, because `run_in_app` is synchronous and returns before
        // any key can be read. Naming that here is why the flag is set directly above.
        let _ = QuitProbe {
            observed: RefCell::new(Vec::new()),
        };
    }

    /// **A run that was never sent must not make `[q]` claim one is still in flight.**
    /// Regression test for a review finding on PR #547.
    ///
    /// `run_selected` sets `running = true` to buy the pending frame, but three arms returned
    /// without clearing it: `launch()`'s `Incomplete` early return, and `run_in_app()`'s
    /// `NotWired` and `NoRoute` arms. The flag then leaked into `request_quit`, so `[q]` raised
    /// *"the run continues on the server either way"* — about a request that never left the
    /// process.
    ///
    /// The repro is the reviewer's, driven through the real key handler rather than by setting
    /// the flag: select `cao launch`, leave `--agents` empty, `[enter]` (blocked banner), quit.
    ///
    /// **Quit is driven by Ctrl+C, not a bare `[q]`**, and that is not a workaround. Both forms
    /// funnel into the same [`Self::request_quit`], but after selecting a command the focus is a
    /// text field, where a printable `q` is deliberately TEXT (NFR-3 — see
    /// `a_q_or_r_typed_into_a_required_field_is_text_and_not_a_command_key`). Ctrl+C is the
    /// binding that reaches the quit path from any focus, so it is what an operator sitting in a
    /// half-filled form would actually press. Written with a bare `q` first, this test failed
    /// for that reason and not for the one it exists to catch.
    ///
    /// Every one of the three arms is exercised, because the leak was per-arm and fixing one
    /// proves nothing about the others. Each asserts BOTH halves — the blocking banner really
    /// rendered (so the arm under test was the one reached) and the quit went through outright.
    /// Asserting only `should_quit` would pass against a build where the banner never appeared,
    /// which is a different defect wearing the same green.
    #[test]
    fn quitting_is_immediate_after_a_run_that_was_blocked_before_it_was_sent() {
        let host = FakeHost::outside_tmux();

        // Arm 1: `launch()`'s `Incomplete` return — `--agents` left empty.
        let server = FakeServer::healthy().with_session(SessionAnswer::Created(terminal("t-none")));
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));
        assert!(shell.on_key(KeyCode::Enter), "Enter must queue the launch");
        assert!(
            shell.run_pending_action(),
            "the queued launch must execute on the next tick"
        );
        assert_eq!(
            server.create_session_calls.get(),
            0,
            "the required-field gate must stop this BEFORE any request — if it were sent, a \
             running flag would be honest and this test would be asserting the wrong thing"
        );
        assert_eq!(
            shell.banner().map(|banner| banner.severity),
            Some("error"),
            "the blocked launch must render the missing-field banner"
        );
        assert!(shell.on_key_event(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(
            !shell.awaiting_quit_confirmation(),
            "nothing was sent, so quitting must not claim a run is still in flight"
        );
        assert!(
            shell.should_quit(),
            "quitting after a blocked launch must exit outright"
        );

        // Arms 2 and 3: `run_in_app`'s `NotWired` (a route needing path values no form field
        // supplies) and `Ignored` (a filled field the route cannot send). Both render a stated
        // limit and send nothing.
        //
        // `MemoryShow` used to stand in for `NotWired` here and no longer can — its `{key}` is now
        // bound to the form's positional, so it RUNS. That is the fix working, and the test moved
        // to commands still on the refusing arms rather than being weakened to accommodate it.
        // `ScheduleAdd` reaches `Ignored` once its `file_path` is filled below.
        for (id, fill) in [
            (CommandId::SessionSend, None),
            // `Ignored` is keyed on a FILLED field, so this one has to be filled to reach that arm
            // at all — which is itself the property that test asserts.
            (
                CommandId::ScheduleAdd,
                Some(("file_path", "nightly.flow.md")),
            ),
        ] {
            let server = FakeServer::healthy();
            let mut shell = Renderer::new(&server, &host, 100, 40);
            assert!(shell.focus_command(id), "{id:?} must be selectable");
            assert!(shell.on_key(KeyCode::Enter));
            if let Some((field, value)) = fill {
                shell
                    .flow_mut()
                    .set(field, value)
                    .expect("the field is declared by this command");
            }
            assert!(shell.on_key(KeyCode::Enter));
            assert!(shell.run_pending_action());
            assert!(
                server.run_calls.borrow().is_empty(),
                "{id:?} must not reach the server for this test to mean anything"
            );
            assert_eq!(
                shell.banner().map(|banner| banner.severity),
                Some("error"),
                "{id:?} must render its stated limit"
            );
            assert!(shell.on_key_event(KeyCode::Char('c'), KeyModifiers::CONTROL));
            assert!(
                !shell.awaiting_quit_confirmation() && shell.should_quit(),
                "quitting after {id:?} was refused must exit outright — nothing is running"
            );
        }
    }

    /// **No command can leave `running` set once its dispatch returns — checked for ALL of them.**
    ///
    /// The three-arm test above pins the reported repro. This one closes the class: it drives the
    /// full `select → [enter] → [enter] → dispatch` cycle for **every non-HIDE command in the
    /// catalog** and asserts the flag is clear afterwards. A future arm that early-returns from
    /// `launch()` or `run_in_app()` fails here without anyone remembering to extend a list.
    ///
    /// This is the assertion that actually earns the unconditional reset in
    /// [`Self::run_pending_action`]: removing that one line reddens this test across many
    /// commands at once, whereas a per-arm reset can only ever be as complete as the arms
    /// someone thought to enumerate.
    ///
    /// Worth recording why there is **no companion test for the `[r]` retry path**: one was
    /// written, and it could not fail. `retry()` calls `launch()`/`run_in_app()` directly, and
    /// both set `running = true` only *after* their gates — `launch()` at its `create_session`
    /// boundary, `run_in_app()` after `in_app_readiness`. The pre-gate setter is
    /// `run_selected`'s alone, and that is only on the `[enter]` path. So a blocked retry never
    /// sets the flag, a reset there is unreachable, and its test passed with the reset deleted.
    /// Proven by mutation rather than by reading, and the resets were removed rather than kept as
    /// reassurance. (Reported by review on PR #547.)
    #[test]
    fn no_dispatched_command_leaves_the_running_flag_set() {
        let host = FakeHost::outside_tmux();

        for command in catalog::commands() {
            let id = command.id;
            let server = FakeServer::healthy()
                .with_session(SessionAnswer::Created(terminal("t-sweep")))
                .with_run(RunAnswer::Chunks(vec!["ok\n"], 200));
            let mut shell = Renderer::new(&server, &host, 100, 40);

            assert!(
                shell.focus_command(id),
                "{id:?} is offered by `commands()` so it must be focusable"
            );
            // First Enter selects the command; the second runs it. A command with no fields
            // treats the second as the run too, which `run_selected` handles.
            shell.on_key(KeyCode::Enter);
            shell.on_key(KeyCode::Enter);
            shell.run_pending_action();

            assert!(
                !shell.running,
                "{id:?} left `running` set after its dispatch returned. Both dispatch paths are \
                 synchronous, so nothing is in flight here — and a stale flag makes the quit \
                 prompt claim a run continues server-side when none was ever sent"
            );
        }
    }

    /// **A printable character reaches a TEXT FIELD instead of firing a command key
    /// (NFR-3).** Regression test for the §12a reviewer's CRITICAL finding.
    ///
    /// The guard was `!matches!(self.focus, RequiredFields | OptionalSection if optional_expanded)`
    /// — and in Rust an `if` guard on a multi-alternative pattern applies to **every** alternative.
    /// So `RequiredFields` was protected only while `optional_expanded` happened to be true. In the
    /// default state right after selecting a command it is false, so **`[q]` quit the TUI instead
    /// of typing a `q`**, and `[r]` retried instead of typing an `r` whenever a banner showed.
    ///
    /// The operator's first action after selecting `cao launch` is typing an agent name, so an
    /// agent whose name contains `q` or `r` was unenterable — `"qbert"`, `"herder"`,
    /// `"moderator"`. The pre-existing keyboard test typed `"planner"`: no `q`, and no banner
    /// showing, so it passed against the defect.
    ///
    /// Asserted on the FIELD VALUE, not on `should_quit` alone: a fix that stopped quitting without
    /// delivering the character would satisfy the negative half and still lose the keystroke.
    /// (#321)
    #[test]
    fn a_q_or_r_typed_into_a_required_field_is_text_and_not_a_command_key() {
        for name in ["qbert", "herder"] {
            let server =
                FakeServer::healthy().with_session(SessionAnswer::Created(terminal("t-typed")));
            let host = FakeHost::outside_tmux();
            let mut shell = Renderer::new(&server, &host, 100, 40);
            assert!(shell.focus_command(CommandId::Launch));
            assert!(shell.on_key(KeyCode::Enter));

            for character in name.chars() {
                shell.on_key(KeyCode::Char(character));
            }

            assert!(
                !shell.should_quit(),
                "typing {name:?} into a required text field must never quit the TUI"
            );
            assert!(
                !shell.awaiting_quit_confirmation(),
                "typing {name:?} must not raise the quit confirmation either"
            );
            assert_eq!(
                shell
                    .flow()
                    .field("--agents")
                    .and_then(|field| field.value.as_ref()),
                Some(&crate::guided_flow::FieldValue::Text(name.to_string())),
                "every character must reach the field: {name:?} contains a `q` or `r`, which the \
                 broken guard consumed as a command key. Not quitting is only half the fix — the \
                 keystroke must actually land"
            );
        }
    }

    /// **Ctrl+R retries and Ctrl+C quits EVEN WHILE A TEXT FIELD IS FOCUSED, while the plain
    /// letters stay text.** The other half of the §12a CRITICAL finding's fix.
    ///
    /// Both halves are asserted together because either alone is satisfiable by a wrong
    /// implementation: suppressing the plain letters without providing a modified path leaves the
    /// operator with **no way to retry or quit** from the focus where a failure banner is most
    /// likely being read, and providing the modified path without suppressing the letters is the
    /// original defect.
    ///
    /// Dispatched through `on_key_event`, the same entry point `run()`'s event loop uses — a test
    /// calling `retry()` directly would prove the method works while the KEY reached nothing.
    /// (#321)
    #[test]
    fn ctrl_r_retries_and_ctrl_c_quits_while_a_text_field_holds_the_plain_letters() {
        let host = FakeHost::outside_tmux();

        // Ctrl+R retries from RequiredFields, where a plain `r` is text.
        let server = FakeServer::unreachable();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        // Enter SELECTS: it builds the field set and moves focus to RequiredFields. Without it
        // there is no `--agents` field to set — `set()` answers `UnknownField`.
        assert!(shell.on_key(KeyCode::Enter));
        shell
            .flow_mut()
            .set("--agents", "planner")
            .expect("a plain text value is accepted");
        shell.launch();
        assert_eq!(
            server.create_session_calls.get(),
            1,
            "attempt 1 must happen"
        );
        assert_eq!(
            shell.focus,
            Focus::RequiredFields,
            "select() focuses the field"
        );

        assert!(
            shell.on_key_event(KeyCode::Char('r'), KeyModifiers::CONTROL),
            "Ctrl+R must retry even though a plain `r` is text in this focus"
        );
        assert_eq!(
            server.create_session_calls.get(),
            2,
            "FR-6.3: Ctrl+R must re-issue the failed step"
        );

        // ...and the plain letter is still text, not the command.
        let before = server.create_session_calls.get();
        shell.on_key_event(KeyCode::Char('r'), KeyModifiers::NONE);
        assert_eq!(
            server.create_session_calls.get(),
            before,
            "a PLAIN `r` must never retry — it is a character in the agent name"
        );

        // Ctrl+C quits from the same focus, where a plain `q` is text.
        let server = FakeServer::healthy();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));
        assert_eq!(shell.focus, Focus::RequiredFields);

        shell.on_key_event(KeyCode::Char('q'), KeyModifiers::NONE);
        assert!(
            !shell.should_quit(),
            "a PLAIN `q` in a text field must not quit — that was the CRITICAL defect"
        );

        assert!(
            shell.on_key_event(KeyCode::Char('c'), KeyModifiers::CONTROL),
            "Ctrl+C must be handled"
        );
        assert!(
            shell.should_quit(),
            "Ctrl+C must quit from a text field: with a plain `q` now text, it is the ONLY way out \
             of this focus, so losing it would trap the operator"
        );
    }

    /// **The quit confirmation is answerable by the key the footer advertises.**
    ///
    /// The footer says "press [ctrl+c] again to quit anyway", and in a text-entry focus a plain `q`
    /// is a character — so Ctrl+C is the ONLY key that can answer the prompt there. A
    /// `request_quit` that merely re-armed `confirm_quit` would leave the operator pressing the
    /// advertised key forever with a command running: a trap, and a footer that lies.
    ///
    /// Caught while changing that footer text, not by a test that already existed. (#321)
    #[test]
    fn ctrl_c_answers_its_own_quit_confirmation_rather_than_re_arming_it() {
        let server = FakeServer::healthy();
        let host = FakeHost::outside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.running = true;

        assert!(shell.on_key_event(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(
            shell.awaiting_quit_confirmation(),
            "the first Ctrl+C with a run in flight must CONFIRM, not exit — the run continues \
             server-side and a silent exit would misrepresent it"
        );
        assert!(!shell.should_quit(), "the first press must not quit");

        assert!(shell.on_key_event(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(
            shell.should_quit(),
            "the SECOND Ctrl+C must quit: the footer promises it, and with a plain `q` being text \
             it is the only key that can answer the prompt from a field"
        );
        assert!(
            !shell.awaiting_quit_confirmation(),
            "the prompt must clear when it is answered"
        );
    }

    // ── `run()` is the ONE fallible method, and only for a startup condition ───────────────────

    /// **`run()` is the only fallible method, and `Fatal` is reserved for a startup condition.**
    ///
    /// A zero-area terminal is the one condition under which rendering an error is not available as
    /// an answer, so it is the one thing that may fail. Everything else — a server that is down, a
    /// picker that failed, a refused hand-off — is a rendered state.
    ///
    /// The converse is asserted too, and it is the half that matters: with the server **completely
    /// unreachable**, `run()` must return `Ok`. That is FR-6.1's mechanism, and a `Fatal` there
    /// would mean the TUI does not open.
    #[test]
    fn run_is_the_only_fallible_method_and_fatal_is_startup_only() {
        let server = FakeServer::unreachable();
        let host = FakeHost::outside_tmux();

        // A zero-area terminal: genuinely unrenderable, so genuinely `Fatal`.
        for (cols, rows) in [(0, 24), (80, 0), (0, 0)] {
            let mut shell = Renderer::new(&server, &host, cols, rows);
            let outcome = shell.run();
            assert!(
                outcome.is_err(),
                "{cols}x{rows} has no room to render anything, so it is the one startup condition \
                 that may fail"
            );
            let Fatal(reason) = outcome.expect_err("just asserted it is an error");
            assert!(
                reason.contains("Resize"),
                "SR-1: `Fatal` must state the remedy in one line, never a traceback. Got: \
                 {reason:?}"
            );
        }

        // A renderable terminal with a dead server: `Ok`. This is FR-6.1's whole point.
        let mut shell = Renderer::new(&server, &host, 80, 24);
        assert_eq!(
            shell.run(),
            Ok(()),
            "an unreachable server is a RENDERED STATE, never a startup failure — the TUI must open"
        );

        // And the four infallible methods have no `Result` to unwrap: this compiles only because
        // their return types are `()`, `Frame`, `bool` and `()` respectively. A signature change to
        // `Result` would break this function, which is the compile-time half of the claim.
        let mut shell = Renderer::new(&server, &host, 80, 24);
        let _: Frame = shell.render();
        let _: bool = shell.on_key(KeyCode::Tab);
        let _: () = shell.launch();
        let _: () = shell.resize(70, 20);
        let _: () = shell.run_in_app(CommandId::SessionList);
    }

    // ── launch() step 2 — one rendered state per error variant ─────────────────────────────────

    /// **`launch()` step 2 renders a DISTINCT state per `create_session` error variant.**
    ///
    /// `error.rs` says the enum *is* the boundary contract rather than diagnostics: `renderer`
    /// matches on it to choose a rendered state. That claim is only true if the variants actually
    /// produce different messages, so this asserts they do — a single "the server said no" for all
    /// three would make the distinction the type carries worthless.
    ///
    /// - `Unreachable` → cause + address + remedy, retryable (FR-6.1, FR-6.3).
    /// - `Validation` (422) → the **rejected field**, whose remedy is editing the form, not retrying.
    /// - `Http(5xx)` → the server is broken; check its log and retry.
    ///
    /// Every arm must also STOP — no arm may proceed to the readiness poll or the hand-off, which is
    /// asserted via the pane never reaching a terminal state and the host spawning nothing.
    #[test]
    fn each_create_session_error_renders_its_own_state_and_stops_the_launch() {
        let host = FakeHost::outside_tmux();

        // Unreachable — FR-6.1's named case.
        let server =
            FakeServer::healthy().with_session(SessionAnswer::Unreachable(unreachable_message()));
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        let banner = shell
            .banner()
            .expect("an unreachable server must render a state")
            .clone();
        assert_eq!(banner.severity, "error");
        assert!(
            banner.why.contains("9889") && banner.why.contains("connection refused"),
            "the `Unreachable` message must be passed through, not paraphrased — a paraphrase drops \
             the address, and the remedy depends on whether the client is pointed where the \
             operator expects. Got: {:?}",
            banner.why
        );
        assert!(
            banner.remedy.contains("CAO_API_HOST") && banner.remedy.contains("[r]"),
            "the remedy must name the env vars AND the retry key. Got: {:?}",
            banner.remedy
        );
        assert_eq!(
            shell.pane().state(),
            PaneState::Running,
            "step 2 must STOP: the pane stays in the pending state it was attached into, and no \
             hand-off happens. Pane: {:?}",
            shell.pane()
        );
        assert!(host.spawned.borrow().is_empty(), "no spawn may occur");

        // Validation (422) — the remedy is editing a field, NOT retrying the same request.
        let server = FakeServer::healthy().with_session(SessionAnswer::Validation(
            "agent_profile: profile 'planner' could not be loaded".to_string(),
        ));
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        let validation = shell.banner().expect("a 422 must render a state").clone();
        assert!(
            validation.why.contains("agent_profile"),
            "a 422 names the rejected FIELD in the server's own `detail`, and surfacing it is the \
             difference between an actionable error and 'the server said no'. Got: {:?}",
            validation.why
        );
        assert!(
            validation.remedy.contains("fix the named field"),
            "a 422 means THIS REQUEST was wrong, so the remedy is editing a field — not retrying \
             the identical request, which would be rejected identically. Got: {:?}",
            validation.remedy
        );

        // Http(5xx) — the server is broken; a retry is the right remedy here.
        let server = FakeServer::healthy().with_session(SessionAnswer::Http(503));
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        let http = shell.banner().expect("a 5xx must render a state").clone();
        assert!(
            http.why.contains("503"),
            "a 5xx must name the status the server actually returned. Got: {:?}",
            http.why
        );
        assert!(
            http.remedy.contains("log"),
            "a 5xx means the SERVER is broken, so the remedy points at its log. Got: {:?}",
            http.remedy
        );

        // The three are genuinely distinct. Without this, all three could share one message and
        // every assertion above could still pass on a sufficiently generic string.
        let messages = [
            format!("{}|{}", banner.why, banner.remedy),
            format!("{}|{}", validation.why, validation.remedy),
            format!("{}|{}", http.why, http.remedy),
        ];
        for (left, right) in [(0, 1), (0, 2), (1, 2)] {
            assert_ne!(
                messages[left], messages[right],
                "`error.rs` says the enum IS the boundary contract because `renderer` matches on it \
                 to choose a rendered state. Two variants sharing one message makes that claim \
                 false. Got: {messages:?}"
            );
        }
    }

    // ── T-6 — a mid-stream failure keeps the bytes that arrived ────────────────────────────────

    /// **T-6: a run that fails mid-stream keeps the output that arrived and states the failure.**
    ///
    /// Degrade visibly. The bytes already handed to the pane stay rendered, so the operator sees the
    /// partial output **and** the cause — never a silent truncation, and never an empty pane that
    /// reads as "produced nothing".
    ///
    /// The pane must also LEAVE `running`: a pane stuck in `running` after the run died is a spinner
    /// that never stops, which is the frozen-UI-indistinguishable-from-a-crash failure again.
    #[test]
    fn a_mid_stream_failure_keeps_the_partial_output_and_states_the_cause() {
        let server = FakeServer::healthy().with_run(RunAnswer::Truncated(
            vec!["row one\n", "row two\n"],
            "the response body from Get /sessions ended early after 16 bytes: connection reset"
                .to_string(),
        ));
        let host = FakeHost::inside_tmux();
        let mut shell = Renderer::new(&server, &host, 100, 40);

        shell.run_in_app(CommandId::SessionList);

        assert_eq!(
            shell.pane().lines(),
            vec!["row one", "row two"],
            "T-6: the bytes that ARRIVED must stay rendered — a silent truncation hides that any \
             output was produced at all"
        );
        let banner = shell
            .banner()
            .expect("T-6: a mid-stream failure must be stated, not swallowed");
        assert!(
            banner.why.contains("ended early"),
            "the cause must name what happened to the stream. Got: {:?}",
            banner.why
        );
        assert!(
            banner.remedy.contains("above"),
            "the remedy must tell the operator that what they can see is what arrived, or they \
             cannot tell partial output from complete output. Got: {:?}",
            banner.remedy
        );
        assert_ne!(
            shell.pane().state(),
            PaneState::Running,
            "the pane must LEAVE `running` — a pane still running after the run died is a spinner \
             that never stops. Pane: {:?}",
            shell.pane()
        );

        // And `inside_tmux` is the condition under which the tmux hand-off NAVIGATES rather than
        // refusing, which is the other half of `FakeHost`'s two states — asserted here so both are
        // exercised by the suite.
        let server = FakeServer::healthy()
            .with_backend("tmux")
            .with_session(SessionAnswer::Created(terminal("t-nav")));
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert_eq!(
            shell.pane().state(),
            PaneState::Complete,
            "inside tmux with a tmux backend, the hand-off NAVIGATES and completes. Pane: {:?}",
            shell.pane()
        );
        assert_eq!(
            host.spawned.borrow().as_slice(),
            &[vec![
                "tmux".to_string(),
                "switch-client".to_string(),
                "-t".to_string(),
                "work:planner-1".to_string(),
            ]],
            "navigation must be `switch-client` at the `session:window` target. `POST /sessions` \
             always creates a NEW session, so this is a CROSS-session move — `select-window` \
             exits 0 there without moving the client, which made the whole hand-off a silent \
             no-op. A nested attach is still forbidden (tmux refuses it). Spawned: {:?}",
            host.spawned.borrow()
        );
    }

    // ── FR-6.3 — retry resumes the RIGHT operation ─────────────────────────────────────────────

    /// **FR-6.3: `[r]` resumes the operation that failed, not a fixed one.**
    ///
    /// Three operations can fail and each needs its own resumption: the picker fetches, the launch,
    /// and an in-app run. A retry that always re-ran one of them would look correct in whichever
    /// test happened to exercise that one — so all three are driven and the *observable* effect of
    /// each is asserted separately.
    ///
    /// `[r]` with nothing to retry must report unconsumed, or the key claims an affordance that does
    /// not exist.
    #[test]
    fn retry_resumes_the_operation_that_failed_and_is_inert_when_there_is_none() {
        let host = FakeHost::outside_tmux();

        // Nothing has been attempted: `[r]` is inert.
        let server = FakeServer::healthy();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(
            !shell.retry(),
            "Ctrl+R with nothing to retry must report unconsumed — a key that claims an affordance \
             which does not exist is the same defect as `[c] clear`, which was never implemented"
        );
        assert_eq!(
            shell.retryable, None,
            "nothing attempted means nothing to retry"
        );

        // Pickers: selecting a command issues them, so that becomes the retryable operation.
        let server = FakeServer::unreachable();
        let mut shell = Renderer::new(&server, &host, 100, 40);
        assert!(shell.focus_command(CommandId::Launch));
        assert!(shell.on_key(KeyCode::Enter));
        assert_eq!(
            shell.retryable,
            Some(Retryable::Pickers),
            "a failed picker fetch must be what `[r]` resumes at this point"
        );
        assert!(shell.retry());
        assert!(
            matches!(shell.flow().agent_choices(), PickerState::Failed(_)),
            "the retried fetch failed again, which is the honest outcome against a dead server"
        );

        // Launch: attempting it makes the launch the retryable operation.
        let server = FakeServer::healthy()
            .with_session(SessionAnswer::Http(500))
            .with_session(SessionAnswer::Created(terminal("t-ok")));
        let mut shell = ready_to_launch(&server, &host);
        shell.launch();
        assert_eq!(
            shell.retryable,
            Some(Retryable::Launch),
            "after a failed launch, `[r]` must resume the LAUNCH — not re-fetch the pickers, which \
             would leave the operator's failed launch unretried while appearing to do something"
        );
        assert!(shell.retry());
        assert_eq!(
            server.create_session_calls.get(),
            2,
            "the retry must re-issue `create_session`"
        );

        // In-app: the retryable operation carries the COMMAND, so the right one is re-run.
        let server = FakeServer::healthy().with_run(RunAnswer::Truncated(
            vec!["partial\n"],
            "connection reset".to_string(),
        ));
        let mut shell = Renderer::new(&server, &host, 100, 40);
        shell.run_in_app(CommandId::WorkflowList);
        assert_eq!(
            shell.retryable,
            Some(Retryable::InApp(CommandId::WorkflowList)),
            "the retryable operation must carry WHICH command failed, or the retry re-runs a \
             different one — which would render the wrong output under the operator's retry"
        );
        assert!(shell.retry());
        assert_eq!(
            server.run_calls.borrow().as_slice(),
            &[CommandId::WorkflowList, CommandId::WorkflowList],
            "the retry must re-run the SAME command. Calls: {:?}",
            server.run_calls.borrow()
        );
    }
}
