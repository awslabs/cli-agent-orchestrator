//! `guided-flow` (Bolt 4): the guided launch form — field state, required-field gating, and
//! **the two pickers, which is where this unit performs I/O** (issue #321).
//!
//! Implements **FR-2.1, FR-2.2, FR-2.3, FR-2.4, FR-6.2, NFR-1, S-4**. Delivers [`GuidedFlow`].
//!
//! # `GuidedFlow`, never `GuidedForm` (BR-1)
//!
//! Resolved at Q3 of this unit's functional design. `GuidedFlow` is load-bearing in
//! `components.md`, `component-methods.md`, `component-dependency.md`, **the DAG edge block the
//! engine parses**, and the unit's own name `guided-flow`; `GuidedForm` appears only in
//! `interaction-spec.md`, which is gate-approved. Aligning that one artifact is therefore an
//! **amendment note, not a silent edit** — recorded here because a reader arriving from
//! `interaction-spec.md` will otherwise think this module is misnamed.
//!
//! # This unit performs I/O, and that is worth stating loudly (BR-12)
//!
//! `component-dependency.md` records `GuidedFlow → ServerClient` as "sync HTTP, picker
//! population". A developer reading only this unit's *responsibilities* — field state, gating,
//! `SessionParams` — would not know it touches the network at all, and the 2.7 reviewer flagged
//! exactly that omission. It does: [`GuidedFlow::populate_pickers`] fetches the agent and
//! provider lists.
//!
//! **All of it goes through `server-client`.** BR-1 of that unit says it owns every connection,
//! and `tests/hermeticity_tripwire.rs` enforces it mechanically — a second production module
//! naming an HTTP client **fails `cargo test`**. Not "fails the build", which this said before and
//! which overstates it: the tripwire is a test target (TS-2, deliberately absent from the release
//! build), so `cargo build` succeeds and the violation surfaces at test time. CI runs both, so the
//! gate is real either way — but a reader who believes the compiler enforces it will not think to
//! check that the test still runs. Overstating a guard is how the guard that would have caught the
//! thing never gets added. (Reported by review on PR #547.)
//!
//! That is also why the fetch is taken through the
//! [`PickerSource`] trait rather than against `ServerClient` directly: it is the same seam
//! `handoff.rs` uses for `ServerRead`, and it means this unit's own tests exercise the picker
//! logic with **no socket at all**. A test stub bound on a real port would have to name
//! `TcpStream` in this file, which the tripwire would (correctly) reject.
//!
//! # The parameter surface, re-verified at source rather than carried forward
//!
//! Enumerated by walking the Click tree and cross-read against `cli/commands/launch.py` — never
//! by scraping `--help`, which is design defect #1 of the superseded TUI (FR-1.3):
//!
//! | Fact | Verified |
//! |---|---|
//! | `cao launch` parameters | **12** |
//! | Required | **1** — `--agents` (BR-3) |
//! | Text-valued | **7** (6 options + the positional) |
//! | Flags | **5** |
//! | `message` | **POSITIONAL** — no `--` prefix (BR-4) |
//! | `--memory` | **`is_flag=True`** at `launch.py:130-134` (BR-5) |
//!
//! `--memory` is the trap BR-5 names, and it is worth spelling out because every instinct points
//! the wrong way: the name suggests a memory-manager *value*, and `POST /sessions` really does
//! take a `memory_manager` parameter — but the CLI option is **boolean**. Rendering it as a text
//! field produces an invocation the CLI rejects.
//!
//! # CLI spelling here, HTTP spelling in `server-client` (BR-6)
//!
//! The field names are `--allowed-tools` and `--env`. The server's query parameter is
//! `allowed_tools` and its body key is `env_vars`. **The two spellings differ and nothing
//! connects them**, so this unit uses the CLI's and `server-client` maps to the HTTP names —
//! which it already does, via `SessionParams`' own `#[serde(rename)]`. Conflating them produces
//! requests the server rejects, at run time and never at compile time.
//!
//! # Blank means absent, decided at the POINT OF ENTRY (BR-7, BR-8)
//!
//! [`GuidedFlow::set`] trims a text value and stores `None` when nothing is left. Both the empty
//! string and the whitespace-only string collapse, because `"   "` is as much "the operator did
//! not fill this in" as `""` is.
//!
//! **That is what makes FR-2.4 structural rather than a caller discipline.** By the time
//! [`GuidedFlow::to_params`] runs there is no empty string left in the form to leak, so INV-2 —
//! *no empty string ever reaches `SessionParams`* — is a property of how the value was
//! constructed. Collapsing at serialisation instead would leave a window in which a malformed
//! value exists inside the type, which is precisely what SR-1 forbids.
//!
//! # Why `to_params()` maps 7 of the 12 parameters, and what happens to the other 5
//!
//! **This section previously claimed six, and was wrong in a way that lost operator input.** It
//! said `POST /sessions` "has no parameter for `message`" and that the unmapped six were "carried
//! in the field set for the hand-off path that `renderer` builds". Both halves were false:
//! `CreateSessionBody.initial_message` has been on the endpoint all along (`api/main.py:215`),
//! and **no such argv-building path exists** — `launch()` does `create_session` plus tmux
//! navigation and builds no `cao launch` command line. So a typed first prompt was collected and
//! silently discarded. `message` is now mapped to `initial_message` and travels in the JSON body.
//! (Reported by review on PR #547.)
//!
//! The remaining five — `--headless`, `--async`, `--auto-approve`, `--yolo`, `--memory` — really
//! do have no endpoint parameter, and they are **not silently dropped**: [`unwirable_flags`]
//! names them, and the renderer marks each in the form so an operator can see that setting
//! `--yolo` here will not produce a `--yolo` session. Silence was the actual harm — someone who
//! ticks `--auto-approve` and then meets an approval prompt in the new window has been misled by
//! the form, not merely underserved by it.
//!
//! `--memory` is the near-miss worth naming: `POST /sessions` *does* take a `memory_manager`
//! parameter, so it looks wirable. It is not, from here — the CLI option is `is_flag=True`
//! (`launch.py:130-134`) while the endpoint's parameter is a memory-manager **profile name**
//! (`Optional[str]`), and the flag carries no name to send. Mapping presence to some invented
//! default would launch a sidecar the operator never chose. See BR-9 of `server-client` for the
//! mirror-image case: `memory_manager` and `model` exist on the endpoint and stay unused because
//! the CLI exposes no value for them. (#321)

use std::collections::BTreeMap;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::time::Duration;

use thiserror::Error;

use crate::catalog::{self, CommandId, ParamKind, Policy};
use crate::env_guard::{self, EnvDecision, MAX_ENV_VALUE_BYTES};
use crate::error::TuiError;
use crate::server::ServerClient;
use crate::types::{Profile, Provider, SessionParams};

/// The guided step order (FR-2.1, BR-11): command → **agent → provider → session name** → run.
///
/// A prefix, not a total order: any parameter named here is pulled to the front in this sequence,
/// and everything else keeps the CLI's own declaration order behind it. That split is what makes
/// FR-2.1 and FR-2.3 the same mechanism — the three guided steps first, the other nine reachable
/// behind the collapsed section (BR-10) rather than absent.
///
/// Written as a prefix rather than a full 12-name list so it stays correct for the other 60
/// commands, none of which FR-2.1 says anything about. (#321)
pub const GUIDED_STEP_ORDER: [&str; 3] = ["--agents", "--provider", "--session-name"];

/// The expected shape of one `--env` pair, quoted verbatim in the rejection message (BR-23).
///
/// A constant so the error text and the test expectation cannot drift: an error that does not
/// say what was expected forces the operator to guess, which is the failure BR-23 names. (#321)
pub const ENV_PAIR_SHAPE: &str = "KEY=VALUE";

/// The textual marker an unloadable profile carries in the picker (FR-1.5, BR-15).
///
/// **Text, and text that explains** — not a colour, not a dimmed row. The whole reason FR-1.5
/// keeps unloadable profiles in the list is so the operator learns the profile *exists and why it
/// is unavailable*; a marker that only says "unavailable" would populate the list and still leave
/// them guessing.
///
/// Rendering is `renderer`'s job. The string lives here because the refusal it describes is
/// enforced here, in [`GuidedFlow::set`], and the two must say the same thing. (#321)
pub const UNLOADABLE_MARKER: &str = "unloadable — listed so you can see it, but not selectable";

/// The marker a `cao launch` field carries when the endpoint has no parameter for it.
///
/// Same posture as [`UNLOADABLE_MARKER`], for the same reason: the field stays **visible and
/// explained** rather than hidden. Removing the five flags from the form would be the other
/// defensible answer, and it was rejected — an operator who knows `cao launch --yolo` exists and
/// cannot find it in the TUI learns nothing, whereas one who sees it marked learns exactly where
/// the boundary is and that the CLI is the way across it. (Reported by review on PR #547.)
pub const NOT_SENT_MARKER: &str = "not sent — POST /sessions has no parameter for this; run it \
                                   from the CLI if you need it";

/// The `cao launch` parameters that **cannot** reach `POST /sessions`, and are marked as such.
///
/// Each is a Click flag with no endpoint counterpart, re-verified against `api/main.py`'s
/// `POST /sessions` signature (`:1890-1904`) and `CreateSessionBody` (`:219`) rather than carried
/// forward from the earlier claim — which was wrong about `message` and cost the operator their
/// typed prompt.
///
/// - `--headless`, `--async`, `--auto-approve`, `--yolo` — no parameter, no body field. These are
///   CLI-process behaviours (detach, don't wait, skip the confirmation prompt), and the two
///   approval flags are the consequential ones to mark: a session that blocks on approvals when
///   the operator asked for auto-approve is a misled operator, not a missing feature.
/// - `--memory` — the near-miss. `POST /sessions` **does** take `memory_manager`, but the CLI
///   option is `is_flag=True` (`launch.py:130-134`) while the parameter wants a memory-manager
///   profile **name** (`Optional[str]`). A bare flag carries no name to send, and inventing a
///   default would spawn a sidecar the operator never chose.
///
/// `message` is deliberately **absent from this list**: it is wired, to `initial_message` in the
/// JSON body. [`the_unwirable_flags_are_exactly_the_unmapped_launch_parameters`] pins that this
/// list plus the seven mapped fields accounts for all twelve, so a field can be neither dropped
/// nor marked by accident. (#321)
pub const UNWIRABLE_LAUNCH_FLAGS: [&str; 5] = [
    "--headless",
    "--async",
    "--auto-approve",
    "--yolo",
    "--memory",
];

/// Whether `field_name` on `command` is a parameter that cannot reach the endpoint.
///
/// **Takes the command, not just the name**, because the names are not unique across the catalog:
/// `cao session send` also declares `--async` (measured — it is the only overlap), and that one is
/// an in-app concern with nothing to do with `POST /sessions`. Keyed on the name alone, this would
/// stamp "not sent" onto a `session send` field where the claim is simply untrue — trading a
/// silent drop for a confident wrong label, which is no better.
///
/// A function rather than callers matching on [`UNWIRABLE_LAUNCH_FLAGS`] directly, so the renderer
/// asks a question instead of re-implementing the answer — the `renderer`-holds-a-second-copy
/// failure this crate keeps running into. (#321, and review on PR #547)
pub fn is_unwirable_launch_flag(command: Option<CommandId>, field_name: &str) -> bool {
    command == Some(CommandId::Launch) && UNWIRABLE_LAUNCH_FLAGS.contains(&field_name)
}

/// Everything [`GuidedFlow`] can refuse.
///
/// # Why a unit-local `thiserror` type and NOT four more [`TuiError`] variants
///
/// The affirmed practice is one crate-root error type, and `error.rs` instructs later units to
/// "add variants here rather than minting their own top-level error types" — the Python side's
/// `ProviderError`-in-six-modules problem. `results_pane.rs` made the same call for `NotRunning`
/// and recorded three reasons; two of them apply here verbatim, and there is a third that is
/// specific to this unit and is the decisive one.
///
/// 1. **[`TuiError`] IS the operator-facing boundary contract**, by its own documentation:
///    `renderer` matches on it to choose a rendered state and each `Display` string is the one
///    styled line the operator sees. Two of the four variants below are never shown to anybody —
///    [`Error::Hidden`] is unreachable from the UI (INV-3) and [`Error::UnknownField`] is a
///    programming mistake, since the renderer can only address fields the form gave it. Putting
///    variants whose contract is *must never be rendered* into an enum whose purpose is what to
///    render would make both meanings weaker.
/// 2. **[`TuiError`] is not `PartialEq`** — it carries a `std::io::Error` — so
///    `assert_eq!(flow.set(..), Err(Error::Invalid(..)))` would not compile against it. The four
///    conditions here are all equality-comparable, and a test that reads as the requirement is
///    worth more than a `matches!` that reads as plumbing.
/// 3. **The decisive one: [`Error::Incomplete`] carries `Vec<Field>`, a type this unit owns.**
///    Adding it to [`TuiError`] would make the crate-root error type depend on `guided_flow`'s
///    domain model, inverting the dependency — `error.rs` today depends on nothing but `std` and
///    `thiserror`, which is what lets every other module import it freely. A crate-root error
///    that reached back into one unit's entities would have to be edited by every future unit
///    that wanted to report a structured failure.
///
/// The trade-off, stated rather than hidden: `renderer` handles a second error type here. That
/// cost is local and bounded — these four conditions never cross an integration boundary, which
/// is the case the one-type rule is actually about. Picker failures, which *do* cross that
/// boundary, are [`TuiError`] and are deliberately **not** represented here: they arrive from
/// `server-client` already typed and are held in [`PickerState::Failed`]. (#321)
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum Error {
    /// [`GuidedFlow::select`] was called on a `Hidden` command (INV-3).
    ///
    /// **Defensive, and deliberately unreachable from the UI**: `catalog::commands()` already
    /// filters `Hidden` rows, so the renderer can never offer one. It exists so a *programmatic*
    /// caller fails loudly instead of silently building a form for a command the operator must
    /// never be offered — the same reasoning as `TuiError::NoRoute`. (#321)
    #[error("that command is not offered in the TUI")]
    Hidden,

    /// [`GuidedFlow::set`] was given a name that is not in the current field set.
    ///
    /// Names the attempted field, because the plausible cause is a stale name after a
    /// `select()` — the form resets on reselect, so a name valid a moment ago may not be now.
    #[error("no field named {0} in this command's form")]
    UnknownField(String),

    /// The value was rejected: a flag that is not a boolean, a malformed `--env` pair, or an
    /// **unloadable profile** (SR-4, BR-15, BR-23).
    ///
    /// The payload is the whole operator-facing sentence rather than a code, because each cause
    /// needs a different remedy and a shared prefix would be wrong for at least one of them —
    /// the same reasoning `TuiError::Unreachable` records for its open format string.
    #[error("{0}")]
    Invalid(String),

    /// [`GuidedFlow::to_params`] was called with a required field unset.
    ///
    /// **Carries the field list** (BR-19) so the caller does not re-derive it — which is what
    /// makes FR-6.2's stated reason cheap to render at the point of refusal rather than something
    /// the renderer has to reconstruct by asking a second question.
    #[error("{} required field(s) are unset", missing.len())]
    Incomplete {
        /// The unset required fields, in form order.
        missing: Vec<Field>,
    },
}

/// The shape of a field's value.
///
/// [`FieldKind::Positional`] exists as its own variant **specifically so `message` cannot be
/// rendered as `--message`** (BR-4). Modelling it as plain [`FieldKind::Text`] would make the
/// no-prefix rule a convention the renderer must remember; making it a variant means the renderer
/// has to handle it distinctly or not compile. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldKind {
    /// An option that takes a value, e.g. `--session-name`.
    Text,
    /// Boolean presence, e.g. `--yolo`. **`--memory` is one of these** (BR-5).
    Flag,
    /// Text, but a positional argument — **rendered and placed WITHOUT a `--` prefix** (BR-4).
    Positional,
}

/// A field's current value. `None` on the owning [`Field`] means "not filled in".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FieldValue {
    /// The seven text-valued parameters, already trimmed and known non-empty (BR-7, BR-8).
    Text(String),
    /// The five flags.
    Flag(bool),
    /// `--env` only, already parsed and validated by [`GuidedFlow::set`].
    ///
    /// `BTreeMap` for deterministic serialisation order (BR-20) — which is what lets a test
    /// assert an exact request body instead of parsing and re-comparing.
    ///
    /// **These values travel in the request BODY, never the query string** (BR-21, issue
    /// **#248**): the query string lands in cao-server's HTTP access log and an `--env` value may
    /// be a credential. The enforcement is in `server-client`, but the map originates here, so
    /// the citation belongs here too — at the point of the decision to model it as a map at all.
    EnvPairs(BTreeMap<String, String>),
}

/// One form field, mirroring one CLI parameter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Field {
    /// **The CLI's own spelling** (BR-6) — `--agents`, `--allowed-tools`, `--env`. A name with no
    /// `--` prefix is a positional argument (BR-4).
    pub name: &'static str,
    /// Value shape.
    pub kind: FieldKind,
    /// Whether the CLI requires it. For `cao launch`, **`--agents` and nothing else** (BR-3).
    pub required: bool,
    /// `None` means "not filled in".
    ///
    /// **This is the type-level expression of FR-2.4.** An empty or whitespace-only entry
    /// collapses to `None` in [`GuidedFlow::set`] (BR-7, BR-8), so no empty string survives to
    /// serialisation and INV-2 holds by construction rather than by remembering to check.
    pub value: Option<FieldValue>,
}

// ## The `allow(dead_code)` attributes in this module, and why they are `allow` and not `expect`
//
// This is a **binary** crate, so `pub` does not exempt an item from `dead_code` — there is no
// downstream crate that could use it — and every accessor here exists for `renderer` (Bolt 5),
// which does not exist yet. Measured in the bin cfg with the allows stripped: 4 warnings
// (`Field::is_positional`, `PickerState::{failure, is_loading}`, `PickerFeed::expected`, plus the
// `PickerState::Failed` payload).
//
// **`allow`, not `expect`**, for the reason `types.rs` records after measuring both: the
// `cfg(test)` build *does* use all of these, so under `--all-targets` an `#[expect(dead_code)]`
// is unfulfilled, and `-D warnings` promotes `unfulfilled_lint_expectations` to an error. `expect`
// would fail the gate outright.
//
// **Per-item, never a module-level `#![allow(dead_code)]`**: a module-wide allow would cover every
// item added here in future, silently and permanently, and would hide the next genuinely orphaned
// method. Each attribute below names the unit that will consume it, so it can be deleted when that
// unit lands rather than outliving its reason. (#321)

#[allow(dead_code)] // consumed by `renderer` (Bolt 5), which renders the field set. (#321)
impl Field {
    /// Is this a positional argument, which must be rendered without a `--` prefix (BR-4)?
    pub fn is_positional(&self) -> bool {
        self.kind == FieldKind::Positional
    }
}

/// The three states a picker can be in.
///
/// Modelled explicitly because **"empty" and "failed" are different and must render
/// differently**: `Loaded(vec![])` is a valid answer from a machine with no profiles and renders
/// an explicit empty state, while [`PickerState::Failed`] renders cause and remedy. Conflating
/// them would tell the operator something is broken when nothing is.
///
/// No `PartialEq`/`Clone`: [`PickerState::Failed`] carries a [`TuiError`], which carries a
/// `std::io::Error`. Deriving neither is the honest option — the alternative would be flattening
/// the error to a `String` at the moment it arrives, which throws away the variant `renderer`
/// matches on to choose a rendered state. (#321)
#[derive(Debug)]
pub enum PickerState<T> {
    /// The fetch is in flight.
    Loading,
    /// The choices. **May be empty, and that is not a failure.**
    Loaded(Vec<T>),
    /// The fetch failed, carrying `server-client`'s own typed error.
    ///
    /// Its `Display` already names the address tried and the `CAO_API_HOST` remedy, which is
    /// exactly what FR-6.1 requires the field to render. **There is no CLI fallback** (BR-14,
    /// FR-1.4): the TUI states the cause and offers retry, and never shells out — a fallback is
    /// the defect being removed, not resilience.
    ///
    /// `allow(dead_code)` on the payload: the bin cfg constructs it but only [`Self::failure`]
    /// *reads* it, and that accessor's caller is `renderer` (Bolt 5). The lint's own suggestion —
    /// "consider changing the field to be of unit type" — is exactly what must not happen: the
    /// variant `renderer` matches on to choose a rendered state, and the `Display` string that
    /// names the address and the remedy (FR-6.1), both live in this payload. (#321)
    Failed(#[allow(dead_code)] TuiError),
}

impl<T> PickerState<T> {
    // `failure` and `is_loading` are read only by `renderer` (Bolt 5) and by this module's tests;
    // `choices` already has a production caller in `refuse_unloadable_profile`. (#321)
    /// The loaded choices, or `None` while loading or after a failure.
    ///
    /// Deliberately **not** `unwrap_or_default()`-shaped: an empty slice for a *failed* picker is
    /// the conflation this type exists to prevent.
    pub fn choices(&self) -> Option<&[T]> {
        match self {
            Self::Loaded(items) => Some(items),
            Self::Loading | Self::Failed(_) => None,
        }
    }

    /// The failure, when there was one.
    #[allow(dead_code)] // read by `renderer` (Bolt 5) to render cause and remedy. (#321)
    pub fn failure(&self) -> Option<&TuiError> {
        match self {
            Self::Failed(error) => Some(error),
            Self::Loading | Self::Loaded(_) => None,
        }
    }

    /// Is the fetch still in flight?
    #[allow(dead_code)] // read by `renderer` (Bolt 5) to render the loading indicator. (#321)
    pub fn is_loading(&self) -> bool {
        matches!(self, Self::Loading)
    }
}

/// The two reads this unit needs, taken through a trait for the reason `handoff.rs` records.
///
/// `ServerClient` is the one production implementor and BR-1 of `server-client` says it owns every
/// connection in the crate. The trait buys two things:
///
/// - **This unit's tests do no I/O at all.** A stub bound on a real port would have to name a
///   socket type in this file, and `tests/hermeticity_tripwire.rs` would reject it — correctly,
///   because "only `src/server.rs` names an HTTP client" is BR-1 in executable form.
/// - **Failure and latency are controllable**, so BR-13's concurrency and BR-14's no-fallback
///   rule are provable rather than asserted.
///
/// `Send + Sync + 'static` are on the spawn site ([`GuidedFlow::populate_pickers`]) rather than on
/// the trait, so an implementor that is only ever used synchronously is not forced to satisfy
/// bounds it does not need.
pub trait PickerSource {
    /// `GET /agents/profiles` — **every** entry, unloadable ones included (FR-1.5, BR-15).
    fn profiles(&self) -> Result<Vec<Profile>, TuiError>;

    /// `GET /agents/providers` — **unfiltered**, `installed == false` included (FR-1.7, BR-16).
    fn providers(&self) -> Result<Vec<Provider>, TuiError>;
}

impl PickerSource for ServerClient {
    fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
        ServerClient::profiles(self)
    }

    fn providers(&self) -> Result<Vec<Provider>, TuiError> {
        ServerClient::providers(self)
    }
}

/// One picker's result, as it arrives over the channel.
///
/// Two variants rather than a `(name, payload)` pair so the apply step is an exhaustive match: a
/// third picker added later cannot be silently dropped on the floor.
#[derive(Debug)]
pub enum PickerUpdate {
    /// The agent picker's answer.
    Agents(Result<Vec<Profile>, TuiError>),
    /// The provider picker's answer.
    Providers(Result<Vec<Provider>, TuiError>),
}

/// The channel the two concurrent fetches report over (TS-2, BR-13, INV-5).
///
/// # Why a channel and not `Arc<Mutex<GuidedFlow>>`
///
/// "No locking is needed" is a claim that requires a mechanism, and this is it. The two fetches
/// run on their own threads and never touch [`GuidedFlow`]; each sends its result here, and the
/// event loop applies it on its next tick ([`GuidedFlow::drain_picker_updates`]). So the form is
/// mutated only from the event-loop thread and needs no lock — which is INV-5, and it is also
/// why field mutation staying single-threaded is a property of the design rather than a rule
/// somebody has to follow.
///
/// The threads are deliberately **not** joined on the fast path: joining would serialise the pair
/// and defeat BR-13, which is the whole reason they are concurrent. They are bounded instead —
/// `ServerClient` carries a 30-second per-request timeout — and each exits as soon as its `send`
/// completes or the receiver is dropped.
#[derive(Debug)]
pub struct PickerFeed {
    updates: Receiver<PickerUpdate>,
    /// How many updates this feed will produce. Two today; a field rather than a literal so
    /// [`GuidedFlow::await_pickers`] does not encode the count at its call site.
    expected: usize,
}

impl PickerFeed {
    /// How many updates the feed will deliver in total.
    #[allow(dead_code)] // read by `renderer` (Bolt 5)'s drain loop. (#321)
    pub fn expected(&self) -> usize {
        self.expected
    }
}

/// The guided launch form. One instance per TUI process, reset on command selection.
///
/// **Never calls back into `Renderer`** (INV-4): every accessor here is a *pull*, which is what
/// keeps the component graph acyclic. (#321)
#[allow(dead_code)] // every caller is `renderer` (Bolt 5); see the note in `types.rs`. (#321)
#[derive(Debug, Default)]
pub struct GuidedFlow {
    /// The selected command. `None` before any selection.
    current: Option<CommandId>,
    /// The field set, **in FR-2.1's step order** (BR-11).
    ///
    /// A `Vec` and not an `IndexMap`, which is a deliberate deviation from TS-1 — see the note on
    /// [`GuidedFlow::fields`].
    fields: Vec<Field>,
    /// Agent choices, populated over HTTP.
    agent_choices: PickerState<Profile>,
    /// Provider choices, populated over HTTP.
    provider_choices: PickerState<Provider>,
}

/// A fresh picker is `Loading`, never `Loaded(vec![])`.
///
/// Hand-written rather than `#[derive(Default)]` with `#[default]` on the variant, because the
/// derive would sit next to `Loaded` and invite somebody to move the attribute — and
/// `Loaded(vec![])` as the default is precisely the conflation [`PickerState`] exists to prevent:
/// a form that had not fetched anything yet would claim the server has no profiles. (#321)
impl<T> Default for PickerState<T> {
    fn default() -> Self {
        Self::Loading
    }
}

#[allow(dead_code)] // every method's caller is `renderer` (Bolt 5). (#321)
impl GuidedFlow {
    /// An empty form: no command, no fields, both pickers `Loading`.
    pub fn new() -> Self {
        Self::default()
    }

    /// Select a command and build its field set (FR-2.1).
    ///
    /// `Err(Error::Hidden)` for a `Hidden` command (INV-3) — defensive; see [`Error::Hidden`].
    ///
    /// **Selecting resets the form**, and that is deliberate rather than incidental: carrying a
    /// `--session-name` from one command to another would silently apply the operator's input to
    /// a command they did not type it for. Picker choices are *not* reset, because the agent and
    /// provider lists are properties of the server rather than of the command — re-fetching them
    /// on every keystroke-driven reselect would spend two HTTP calls to learn the same answer.
    pub fn select(&mut self, id: CommandId) -> Result<(), Error> {
        if catalog::policy(id) == Policy::Hidden {
            return Err(Error::Hidden);
        }

        self.current = Some(id);
        self.fields = build_fields(id);
        Ok(())
    }

    /// The selected command, or `None`.
    pub fn current(&self) -> Option<CommandId> {
        self.current
    }

    /// The field set, in form order (FR-2.1, BR-11).
    ///
    /// # Deviation from TS-1, stated rather than quietly taken
    ///
    /// `tech-stack-decisions.md` TS-1 selects `IndexMap` for the field set, on the ground that
    /// "FR-2.1 specifies a guided **step order**, so iteration order is semantic. A `HashMap`
    /// would make the form order incidental and non-reproducible."
    ///
    /// A `Vec<Field>` satisfies that reasoning exactly — order is the *only* thing it guarantees
    /// — while `HashMap`, the alternative TS-1 rejects, is what is actually being avoided. Three
    /// reasons the `Vec` is the better fit here, and the decision is reported rather than
    /// buried:
    ///
    /// 1. **The key is already inside the value.** `Field::name` is part of the entity
    ///    (`domain-entities.md`), so `IndexMap<&'static str, Field>` stores it twice and creates
    ///    a drift surface where the two can disagree.
    /// 2. **The collection is at most 12 elements** — `cao launch` is the largest form in the
    ///    catalog. Linear lookup over 12 `&'static str` comparisons is not a cost worth a
    ///    dependency.
    /// 3. **`indexmap` is not in `Cargo.lock`.** Adding it would be new supply-chain surface for
    ///    ordered iteration that `Vec` already provides, and the affirmed practice is that a new
    ///    dependency is a reviewable decision rather than a convenience.
    ///
    /// The design sketch in `domain-entities.md` is marked "Illustrative, not prescriptive", and
    /// nothing downstream observes the container type. (#321)
    pub fn fields(&self) -> &[Field] {
        &self.fields
    }

    /// One field by its CLI name, or `None`.
    pub fn field(&self, name: &str) -> Option<&Field> {
        self.fields.iter().find(|field| field.name == name)
    }

    /// How many leading fields are guided steps; the rest are the collapsed section (FR-2.3).
    ///
    /// Counted from the fields actually present rather than from [`GUIDED_STEP_ORDER`]'s length,
    /// because 51 of the 61 commands have none of the three. **Hidden-by-default is not the same
    /// as absent** (BR-10): everything past this index is still in [`GuidedFlow::fields`] and
    /// still settable, which is what "reachable without leaving the form" means.
    pub fn guided_field_count(&self) -> usize {
        self.fields
            .iter()
            .take_while(|field| GUIDED_STEP_ORDER.contains(&field.name))
            .count()
    }

    /// Set a field's value from operator input.
    ///
    /// # The rules, each of which is a separate way to get this wrong
    ///
    /// - **Text and positional:** trimmed; **empty after trimming stores `None`** (BR-7, BR-8).
    ///   This is the point of entry, and doing it here rather than at serialisation is what makes
    ///   FR-2.4 structural (SR-1). It is also the *clear a field* path: `set(name, "")` returns
    ///   the field to unset.
    /// - **Flags:** `true`/`false` only; anything else is `Err(Error::Invalid)` naming what was
    ///   accepted. A blank value clears the flag, for the same reason a blank clears a text
    ///   field — an operator deleting their input means "not filled in", whatever the kind.
    /// - **`--agents`:** an **unloadable profile is REFUSED** (SR-4, FR-1.5, BR-15). See below.
    /// - **`--env`:** pairs are parsed **and validated here, not in `to_params()`** (SR-2).
    ///
    /// # `--env` cannot be deferred, and the type system is what forces that
    ///
    /// [`GuidedFlow::to_params`] returns `Result<_, Error>` whose only reachable variant there is
    /// [`Error::Incomplete`] — it has **no way to report a malformed pair**. BR-23 requires a
    /// malformed pair to be `Err(Error::Invalid)`, and that is only expressible from here. Stated
    /// explicitly because a developer reading the `set()` pseudo-code alone would reasonably
    /// store the raw string and defer parsing, and would then find `to_params()` unable to
    /// complain.
    pub fn set(&mut self, name: &str, value: &str) -> Result<(), Error> {
        let kind = self
            .field(name)
            .ok_or_else(|| Error::UnknownField(name.to_string()))?
            .kind;

        let trimmed = value.trim();

        // BR-7/BR-8, applied before any kind-specific parsing so the collapse is one rule rather
        // than three: a blank entry of ANY kind is "not filled in". (#321)
        if trimmed.is_empty() {
            self.set_value(name, None);
            return Ok(());
        }

        let parsed = match kind {
            FieldKind::Flag => FieldValue::Flag(parse_flag(name, trimmed)?),
            FieldKind::Text | FieldKind::Positional if name == "--env" => {
                match parse_env_pairs(trimmed)? {
                    // Zero pairs from a non-blank entry is not reachable today — a non-blank
                    // string yields at least one candidate pair, and a candidate that is not a
                    // pair is rejected above. Handled rather than `unreachable!()` because BR-22
                    // is about the empty map being OMITTED, and an `unreachable!()` here would be
                    // a panic at an integration boundary for a case the requirement anticipates.
                    pairs if pairs.is_empty() => {
                        self.set_value(name, None);
                        return Ok(());
                    }
                    pairs => FieldValue::EnvPairs(pairs),
                }
            }
            FieldKind::Text | FieldKind::Positional => {
                if name == "--agents" {
                    self.refuse_unloadable_profile(trimmed)?;
                }
                FieldValue::Text(trimmed.to_string())
            }
        };

        self.set_value(name, Some(parsed));
        Ok(())
    }

    /// **An unloadable profile is not selectable, and that is ENFORCED here** (SR-4, FR-1.5,
    /// BR-15, VR-4).
    ///
    /// # `project.md:98` says the opposite, and FR-1.5 governs
    ///
    /// Affirmed memory reads *"ALWAYS filter agent profiles on `loadable == true` before
    /// presenting them in any picker"*. **FR-1.5 supersedes it**, per the operator's explicit
    /// later decision and the supersession block in `requirements.md`: unloadable profiles are
    /// **populated, marked, and refused on selection**. Closing the memory contradiction is
    /// **OQ-5**, submitted via `learning propose` and awaiting the supervisor — affirmed memory is
    /// deliberately not edited from here. `server.rs` carries the same note on `profiles()`,
    /// which is the other half of this rule.
    ///
    /// **So: do not "fix" this into a filter.** The live data shows why the design chose this way
    /// round — 25 profiles, 4 unloadable, `__pycache__` among them, which is exactly the
    /// incidental directory `project.md:98` was written to guard against. Filtering it *hides*
    /// it; marking it unselectable *explains* it. The operator cannot pick it either way, and
    /// only one of those tells them why.
    ///
    /// # Why an unknown name is accepted
    ///
    /// The refusal is checked against the **loaded** list. While the picker is `Loading` or
    /// `Failed` there is no list, so no refusal is possible and the entry is accepted — because
    /// BR-17 says a picker failure must not disable the rest of the form, and refusing every
    /// agent name when the picker is down would disable the *only required field*, making the
    /// launch unreachable. A name absent from a successfully loaded list is likewise accepted:
    /// the server is the authority on what loads, and the picker's snapshot may be stale.
    fn refuse_unloadable_profile(&self, name: &str) -> Result<(), Error> {
        let Some(profiles) = self.agent_choices.choices() else {
            return Ok(());
        };

        let unloadable = profiles
            .iter()
            .any(|profile| profile.name == name && !profile.loadable);

        if unloadable {
            return Err(Error::Invalid(format!(
                "agent profile {name:?} cannot be loaded, so it cannot be launched \
                 ({UNLOADABLE_MARKER})"
            )));
        }
        Ok(())
    }

    /// Writes a value into the named field. The one mutation point for [`Field::value`].
    fn set_value(&mut self, name: &str, value: Option<FieldValue>) {
        if let Some(field) = self.fields.iter_mut().find(|field| field.name == name) {
            field.value = value;
        }
    }

    /// The unset **required** fields, in form order (BR-18, FR-6.2).
    ///
    /// # The `required &&` term is the whole rule (BR-3, INV-1)
    ///
    /// Dropping it makes every unset field missing, which gates the run on all 12 parameters —
    /// the over-strict gating BR-3 forbids, and a form that refuses launches the CLI would
    /// accept. Inverting `is_none()` makes the gate pass with nothing filled in. Both mutants
    /// compile; `missing_names_the_unset_required_field_and_states_the_reason` and
    /// `can_run_is_true_with_only_the_agents_field_set` are what turn red (VR-3, VR-6).
    pub fn missing(&self) -> Vec<Field> {
        self.fields
            .iter()
            .filter(|field| field.required && field.value.is_none())
            .cloned()
            .collect()
    }

    /// Is the run available (FR-2.2, INV-1)?
    ///
    /// **Gates on required fields ONLY** — for `cao launch` that is `--agents` alone (BR-3).
    ///
    /// The `current.is_some()` term is not redundant, and leaving it out is a trap worth naming:
    /// an empty form has **no fields**, so `missing()` is empty and a bare
    /// `missing().is_empty()` would report the run as available before the operator has chosen a
    /// command at all. The gate is "every required field of a selected command is satisfied", and
    /// the selection is half of that.
    pub fn can_run(&self) -> bool {
        self.current.is_some() && self.missing().is_empty()
    }

    /// The stated reason the run is blocked, or `None` when it is available (BR-18, FR-6.2).
    ///
    /// **Never a greyed control with no explanation** — that is the exact failure FR-6.2 names.
    /// The renderer shows this string; the wording is pinned by
    /// `the_blocked_reason_names_the_field_rather_than_merely_greying_out`.
    pub fn blocked_reason(&self) -> Option<String> {
        if self.current.is_none() {
            return Some("blocked: no command selected".to_string());
        }

        let missing = self.missing();
        if missing.is_empty() {
            return None;
        }

        let names: Vec<&str> = missing.iter().map(|field| field.name).collect();
        Some(format!("blocked: {} required", names.join(", ")))
    }

    /// Build the `POST /sessions` request (FR-2.4, BR-19).
    ///
    /// `Err(Error::Incomplete { missing })` when a required field is unset, **carrying the field
    /// list** so the caller need not re-derive it.
    ///
    /// # Only `cao launch` has a `SessionParams`
    ///
    /// `SessionParams` is the launch request; the other 60 commands have nothing to build. A
    /// caller asking for one from a different form is a programming error and gets
    /// [`Error::Invalid`] rather than a struct assembled from whatever fields happened to share a
    /// name — which is the failure mode a permissive implementation would have.
    ///
    /// # Six of the twelve parameters have nowhere to go, by design
    ///
    /// `message`, `--headless`, `--async`, `--auto-approve`, `--yolo`, and `--memory` have no
    /// `POST /sessions` parameter. They stay in the field set for the hand-off argv `renderer`
    /// builds and are absent from the HTTP request by construction — see the module docs, and
    /// BR-9 of `server-client` for the mirror-image case.
    ///
    /// **`env_vars` is already a `BTreeMap` here.** `set()` parsed and validated it (SR-2), so
    /// this method only moves it. It travels in the body, never the query string — issue
    /// **#248**, enforced in `server-client`.
    pub fn to_params(&self) -> Result<SessionParams, Error> {
        if self.current != Some(CommandId::Launch) {
            return Err(Error::Invalid(format!(
                "only `cao launch` builds a session request; the current form is {:?}",
                self.current
            )));
        }

        let missing = self.missing();
        if !missing.is_empty() {
            return Err(Error::Incomplete { missing });
        }

        // Unwrap-free: `missing` is empty, so the one required field has a value. Sourced through
        // the same accessor as every optional rather than a special case, so a `--agents` that
        // somehow held a non-`Text` value would fail the required check rather than silently
        // becoming an empty profile name.
        let agents = self.text_of("--agents").ok_or_else(|| Error::Incomplete {
            missing: vec![Field {
                name: "--agents",
                kind: FieldKind::Text,
                required: true,
                value: None,
            }],
        })?;

        Ok(SessionParams {
            agents,
            provider: self.text_of("--provider"),
            session_name: self.text_of("--session-name"),
            working_directory: self.text_of("--working-directory"),
            allowed_tools: self.text_of("--allowed-tools"),
            env_vars: self.env_pairs_of("--env"),
            // `"message"` with no `--` prefix: it is `cao launch`'s trailing POSITIONAL argument
            // (`launch.py:94`), and `text_of` looks fields up by the catalog's exact spelling.
            // Asking for `"--message"` here would find nothing and silently drop the prompt
            // again, which is the whole defect this line fixes — so
            // `the_typed_launch_message_reaches_the_request` pins the field name.
            initial_message: self.text_of("message"),
        })
    }

    /// A text field's value, or `None` when unset or not a text value.
    ///
    /// Returning `None` for a blank field is the whole of FR-2.4 on this side: `SessionParams`
    /// skips serialising `None`, so the key is **omitted** from the request rather than sent as
    /// `""` or `null`.
    fn text_of(&self, name: &str) -> Option<String> {
        match self.field(name).and_then(|field| field.value.as_ref()) {
            Some(FieldValue::Text(text)) => Some(text.clone()),
            _ => None,
        }
    }

    /// The `--env` map, or `None`.
    ///
    /// **An empty map yields `None`** (BR-22), so the request body is omitted entirely rather
    /// than carrying `{}`. Unreachable via `set()` today — a blank entry already stores `None` —
    /// and handled anyway, because "empty means omitted" is the requirement rather than a
    /// consequence of how the map got there.
    fn env_pairs_of(&self, name: &str) -> Option<BTreeMap<String, String>> {
        match self.field(name).and_then(|field| field.value.as_ref()) {
            Some(FieldValue::EnvPairs(pairs)) if !pairs.is_empty() => Some(pairs.clone()),
            _ => None,
        }
    }

    /// Start both picker fetches **concurrently** (BR-12, BR-13, NFR-1).
    ///
    /// Two independent GETs on two threads, reporting over one channel. They are concurrent
    /// because **NFR-1 measures the pair**: running them in sequence doubles the latency the
    /// requirement is written against, for no benefit — neither call's input depends on the
    /// other's output.
    ///
    /// Both pickers move to [`PickerState::Loading`] immediately, so the form has a truthful
    /// state to render before either answer arrives. Apply the answers with
    /// [`GuidedFlow::drain_picker_updates`] (non-blocking, for an event loop) or
    /// [`GuidedFlow::await_pickers`] (bounded, for a synchronous caller or a test).
    ///
    /// Failures are **not** raised from here: they arrive as [`PickerState::Failed`] and each
    /// field renders cause and remedy while the rest of the form stays usable (BR-14, BR-17).
    /// A picker that could take the whole form down would make an unreachable server a launch
    /// blocker rather than a stated condition.
    pub fn populate_pickers<S>(&mut self, source: Arc<S>) -> PickerFeed
    where
        S: PickerSource + Send + Sync + 'static,
    {
        self.agent_choices = PickerState::Loading;
        self.provider_choices = PickerState::Loading;

        let (sender, updates) = mpsc::channel();

        spawn_fetch(Arc::clone(&source), sender.clone(), |source| {
            PickerUpdate::Agents(source.profiles())
        });
        spawn_fetch(source, sender, |source| {
            PickerUpdate::Providers(source.providers())
        });

        PickerFeed {
            updates,
            expected: 2,
        }
    }

    /// Apply one picker answer.
    ///
    /// **Stores what arrived, unmodified.** No `loadable` filter (FR-1.5, BR-15) and no
    /// `installed` filter (FR-1.7, BR-16) — `server-client` already declines to filter either,
    /// and adding one here would undo it one layer up.
    pub fn apply_picker_update(&mut self, update: PickerUpdate) {
        match update {
            PickerUpdate::Agents(Ok(profiles)) => {
                self.agent_choices = PickerState::Loaded(profiles);
            }
            PickerUpdate::Agents(Err(error)) => {
                self.agent_choices = PickerState::Failed(error);
            }
            PickerUpdate::Providers(Ok(providers)) => {
                self.provider_choices = PickerState::Loaded(providers);
            }
            PickerUpdate::Providers(Err(error)) => {
                self.provider_choices = PickerState::Failed(error);
            }
        }
    }

    /// Apply every answer that has already arrived, without blocking. Returns how many.
    ///
    /// The event-loop entry point (TS-2, INV-5): called once per tick, so picker state changes on
    /// the same thread that mutates fields and no lock is needed.
    pub fn drain_picker_updates(&mut self, feed: &PickerFeed) -> usize {
        let mut applied = 0;
        while let Ok(update) = feed.updates.try_recv() {
            self.apply_picker_update(update);
            applied += 1;
        }
        applied
    }

    /// Apply up to [`PickerFeed::expected`] answers, waiting at most `timeout` **for each**.
    ///
    /// The bounded form, for a synchronous caller and for tests. A per-receive deadline rather
    /// than one overall budget keeps the wait self-terminating even if a thread never reports:
    /// an unbounded `recv()` here would hang the TUI on a hung fetch, which is the same failure
    /// class as the pty deadlock the harness exists to prevent.
    ///
    /// Returns how many answers were applied; fewer than expected means at least one timed out
    /// and that picker is still `Loading`.
    pub fn await_pickers(&mut self, feed: &PickerFeed, timeout: Duration) -> usize {
        let mut applied = 0;
        for _ in 0..feed.expected {
            match feed.updates.recv_timeout(timeout) {
                Ok(update) => {
                    self.apply_picker_update(update);
                    applied += 1;
                }
                Err(_) => break,
            }
        }
        applied
    }

    /// The agent picker's state.
    pub fn agent_choices(&self) -> &PickerState<Profile> {
        &self.agent_choices
    }

    /// The provider picker's state.
    pub fn provider_choices(&self) -> &PickerState<Provider> {
        &self.provider_choices
    }
}

/// Runs one fetch on its own thread and reports the result.
///
/// Free function rather than an inline closure so both spawns are demonstrably the same shape —
/// the asymmetry a hand-written second spawn invites is exactly how one of a concurrent pair ends
/// up sequential.
fn spawn_fetch<S, F>(source: Arc<S>, sender: Sender<PickerUpdate>, fetch: F)
where
    S: PickerSource + Send + Sync + 'static,
    F: FnOnce(&S) -> PickerUpdate + Send + 'static,
{
    std::thread::spawn(move || {
        // A failed send means the receiver is gone — the operator selected another command or
        // closed the TUI. Dropping the answer is correct there, and it is the only outcome
        // `send` can report, so there is nothing to log. (#321)
        let _ = sender.send(fetch(&source));
    });
}

/// Builds `id`'s field set in form order (FR-2.1, BR-11).
///
/// The guided steps first, in [`GUIDED_STEP_ORDER`], then everything else in the CLI's own
/// declaration order. `catalog::params` supplies the parameters; this function only orders and
/// widens them into [`Field`]s.
fn build_fields(id: CommandId) -> Vec<Field> {
    let params = catalog::params(id);

    let mut ordered: Vec<Field> = Vec::with_capacity(params.len());
    for step in GUIDED_STEP_ORDER {
        if let Some(param) = params.iter().find(|param| param.name == step) {
            ordered.push(field_of(param));
        }
    }
    for param in &params {
        if !GUIDED_STEP_ORDER.contains(&param.name) {
            ordered.push(field_of(param));
        }
    }
    ordered
}

/// Widens one catalog parameter into a form field.
///
/// **The positional case is derived from the absence of a `--` prefix**, which is how the catalog
/// itself documents the distinction (`Param::name`: "A name with no `--` prefix is a positional
/// argument"). Deriving it beats a second hard-coded list of positionals, which would be a place
/// for the two to disagree — and BR-4's whole point is that `message` must never acquire a `--`.
fn field_of(param: &catalog::Param) -> Field {
    let kind = match param.kind {
        ParamKind::Flag => FieldKind::Flag,
        ParamKind::Text if param.name.starts_with("--") => FieldKind::Text,
        // A flag is always an option in Click, so this arm is the text-valued positional —
        // `message` for `cao launch`, `key`/`value`/`name` for the config and profile commands.
        ParamKind::Text => FieldKind::Positional,
    };

    Field {
        name: param.name,
        kind,
        required: param.required,
        value: None,
    }
}

/// Parses a flag's value.
///
/// `true`/`false` only, and the error **names what was accepted** — an error that does not say
/// what was expected forces the operator to guess, the same rule BR-23 states for `--env`.
fn parse_flag(name: &str, value: &str) -> Result<bool, Error> {
    value.parse::<bool>().map_err(|_| {
        Error::Invalid(format!(
            "{name} is a flag: expected `true` or `false`, got {value:?}"
        ))
    })
}

/// Parses and validates `--env` pairs (BR-23, SR-2, SR-3, issue **#248**).
///
/// # One pair per LINE, not per whitespace-separated token
///
/// An env value may legitimately contain spaces (`--env GREETING=hello world`), so splitting on
/// whitespace would reject or truncate a valid value. Splitting on newlines keeps a single-line
/// entry working as one pair — the ordinary case — while letting a multi-line field carry
/// several. Blank lines are skipped rather than rejected, so a trailing newline is not an error.
///
/// # This mirrors the CLI's REJECT, not the client's silent DROP
///
/// There are two env policies in the Python source and they behave differently, so it matters
/// which one a front door reproduces:
///
/// - **`launch.py::_parse_env_pairs` (`:60-90`) REJECTS** — it raises `ClickException` for a
///   missing `=`, a key outside `[A-Za-z_][A-Za-z0-9_]*`, a blocked prefix, or a value at or
///   above the byte cap. This is the CLI's front door, and it is what this function mirrors.
/// - **`clients/tmux.py::_merge_extra_env` DROPS with a warning** — the later stage, which runs
///   **server-side** and never errors, because failing a launch there would be a behaviour change.
///   This crate no longer mirrors it: `env_guard::merge` existed for that purpose and was deleted
///   for having no reachable caller, since the TUI sends `env_vars` over HTTP and cao-server
///   performs that merge itself. (Review on PR #547.)
///
/// This unit is the front door, so it rejects: loudly, at entry, naming the cause (SR-3,
/// deny-by-default). The policy constants and the allowlist-before-prefix ordering are **not**
/// re-implemented here — [`env_guard::decide`] owns them, and that ordering is itself a security
/// behaviour (every allowlisted key starts with `CLAUDE`, so a prefix-first test drops
/// `CLAUDE_CODE_USE_BEDROCK` and breaks Bedrock authentication).
///
/// The key-shape check is the one rule `env_guard` does not carry, because the Python side splits
/// it the same way: shape belongs to the CLI parser, policy belongs to the client.
fn parse_env_pairs(raw: &str) -> Result<BTreeMap<String, String>, Error> {
    let mut pairs = BTreeMap::new();

    for line in raw.lines() {
        let entry = line.trim();
        if entry.is_empty() {
            continue;
        }

        let Some((key, value)) = entry.split_once('=') else {
            return Err(Error::Invalid(format!(
                "--env expects {ENV_PAIR_SHAPE}, got {entry:?}; did you forget the `=`?"
            )));
        };

        if !is_valid_env_key(key) {
            return Err(Error::Invalid(format!(
                "--env key must match [A-Za-z_][A-Za-z0-9_]* (got {key:?}); \
                 the shape is {ENV_PAIR_SHAPE}"
            )));
        }

        // Deny-by-default, at entry, using the guard that already owns the policy (SR-3).
        //
        // Each arm's message ends with `EnvDecision::warning()`, and that is what gives that
        // method its **production caller**. It had none: it was written for `env_guard::merge`,
        // which was deleted for the same reason (it mirrored a merge cao-server performs itself),
        // and the reasons this front door prints were written out a second time here. Two copies
        // of one policy explanation drift, and the drift is silent — the message an operator reads
        // would stop matching the rule that rejected them. Now the guard states the decision and
        // this unit adds the front-door context. (Reported by review on PR #547.)
        let decision = env_guard::decide(key, value);
        if let Some(reason) = decision.warning() {
            return Err(Error::Invalid(match &decision {
                EnvDecision::DropBlocked { key } => format!(
                    "{reason}. --env key {key:?} uses a prefix reserved for provider env \
                     ({}); it would cause a nested-session failure",
                    env_guard::BLOCKED_PREFIXES.join(", ")
                ),
                EnvDecision::DropOversized { .. } => format!(
                    "{reason}. The {MAX_ENV_VALUE_BYTES}-byte cap is the tmux argv limit \
                     (PR #246)"
                ),
                // `warning()` returns `None` for `Keep`, so this arm is unreachable — stated
                // rather than silently folded into one of the drops above.
                EnvDecision::Keep => unreachable!(
                    "EnvDecision::warning() returns None for Keep, so this branch cannot be \
                     entered; a warning implies a drop"
                ),
            }));
        }

        // Later pairs override earlier ones on a key collision, matching `_merge_extra_env`'s
        // dict-assignment semantics (`tmux.py:105-128`). (#321)
        pairs.insert(key.to_string(), value.to_string());
    }

    Ok(pairs)
}

/// Is `key` a POSIX-shaped environment variable name?
///
/// `[A-Za-z_][A-Za-z0-9_]*`, ASCII only — verbatim from `launch.py:72-79`, including the
/// `key.isascii()` term. Stricter than "is an identifier" only in forbidding non-ASCII, which is
/// the point: a name the shell cannot export is not a name worth forwarding.
fn is_valid_env_key(key: &str) -> bool {
    let mut chars = key.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphabetic() || first == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

#[cfg(test)]
mod tests {
    use super::{
        build_fields, is_unwirable_launch_flag, is_valid_env_key, Error, Field, FieldKind,
        FieldValue, GuidedFlow, PickerSource, PickerUpdate, ENV_PAIR_SHAPE, GUIDED_STEP_ORDER,
        UNLOADABLE_MARKER, UNWIRABLE_LAUNCH_FLAGS,
    };
    use crate::catalog::{self, CommandId};
    use crate::error::TuiError;
    use crate::types::{Profile, Provider};
    use std::collections::BTreeSet;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    /// How long a bounded wait in these tests will tolerate. Generous, because it only bounds a
    /// failure: every fetch here is in-process and answers immediately.
    const WAIT: Duration = Duration::from_secs(5);

    /// A profile fixture. `loadable` is the parameter because it is the axis every FR-1.5
    /// assertion turns on.
    fn profile(name: &str, loadable: bool) -> Profile {
        Profile {
            name: name.to_string(),
            source: "~/.claude/agents".to_string(),
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
            binary: name.replace('_', "-"),
            installed,
        }
    }

    /// A [`PickerSource`] with no I/O, and the reason this unit's tests bind no socket.
    ///
    /// `handoff.rs` set the precedent with `FakeServer`: taking the reads through a trait means
    /// the logic is exercised with nothing running. Here it also keeps the crate's HTTP-ownership
    /// guard intact — a stub on a real port would have to name a socket type in this file, and
    /// `tests/hermeticity_tripwire.rs` would reject it, correctly.
    ///
    /// `entered` is not bookkeeping: it is how
    /// [`the_two_picker_fetches_run_concurrently`] observes overlap without a sleep-and-hope
    /// timing assertion.
    struct FakeSource {
        profiles: Vec<Profile>,
        providers: Vec<Provider>,
        profiles_fail: Option<String>,
        providers_fail: Option<String>,
        /// How many fetches have entered a call. **Monotonic — never decremented.**
        ///
        /// An enter/exit counter was the first attempt and it is racy in a way worth recording,
        /// because it fails in the *flattering* direction only sometimes: the second fetch
        /// increments to 2, observes the overlap, and decrements back to 1 before the first fetch
        /// gets to look. The first then spins to its deadline and the test fails against a
        /// perfectly concurrent implementation. A counter that only rises cannot lose the
        /// evidence. (#321)
        arrivals: AtomicUsize,
        /// Wait for the other fetch to arrive before returning, up to [`WAIT`].
        rendezvous: bool,
    }

    impl FakeSource {
        fn new(profiles: Vec<Profile>, providers: Vec<Provider>) -> Self {
            Self {
                profiles,
                providers,
                profiles_fail: None,
                providers_fail: None,
                arrivals: AtomicUsize::new(0),
                rendezvous: false,
            }
        }

        fn failing(profiles_fail: Option<&str>, providers_fail: Option<&str>) -> Self {
            Self {
                profiles_fail: profiles_fail.map(str::to_string),
                providers_fail: providers_fail.map(str::to_string),
                ..Self::new(Vec::new(), Vec::new())
            }
        }

        /// Both fetches wait for each other, so a **sequential** implementation cannot satisfy
        /// them both.
        fn rendezvousing() -> Self {
            Self {
                rendezvous: true,
                ..Self::new(
                    vec![profile("planner", true)],
                    vec![provider("kiro_cli", true)],
                )
            }
        }

        /// Records arrival and, when rendezvousing, waits for the peer.
        ///
        /// Returns whether both fetches had entered a call before this one returned — which is
        /// overlap, since the caller is still inside its own call while it waits.
        ///
        /// A **deadline** rather than an unbounded wait on a `Barrier`: a sequential
        /// implementation must make this test FAIL, not hang, or the failure costs a CI timeout
        /// instead of a message. And [`Self::arrivals`] only ever rises — see its docs for the
        /// race an enter/exit counter has.
        fn arrive(&self) -> bool {
            let mine = self.arrivals.fetch_add(1, Ordering::SeqCst) + 1;
            if !self.rendezvous || mine >= 2 {
                return mine >= 2;
            }

            let deadline = Instant::now() + WAIT;
            while Instant::now() < deadline {
                if self.arrivals.load(Ordering::SeqCst) >= 2 {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            false
        }
    }

    impl PickerSource for FakeSource {
        fn profiles(&self) -> Result<Vec<Profile>, TuiError> {
            let overlapped = self.arrive();
            if let Some(message) = &self.profiles_fail {
                return Err(TuiError::Unreachable(message.clone()));
            }
            assert!(
                overlapped || !self.rendezvous,
                "the two picker fetches must overlap (BR-13)"
            );
            Ok(self.profiles.clone())
        }

        fn providers(&self) -> Result<Vec<Provider>, TuiError> {
            let overlapped = self.arrive();
            if let Some(message) = &self.providers_fail {
                return Err(TuiError::Unreachable(message.clone()));
            }
            assert!(
                overlapped || !self.rendezvous,
                "the two picker fetches must overlap (BR-13)"
            );
            Ok(self.providers.clone())
        }
    }

    /// A `cao launch` form whose agent picker has already loaded `planner` (loadable) and
    /// `__pycache__` (not).
    fn launch_form_with_pickers() -> GuidedFlow {
        let mut flow = GuidedFlow::new();
        flow.select(CommandId::Launch).expect("launch is offered");
        flow.apply_picker_update(PickerUpdate::Agents(Ok(vec![
            profile("planner", true),
            profile("__pycache__", false),
        ])));
        flow.apply_picker_update(PickerUpdate::Providers(Ok(vec![
            provider("kiro_cli", true),
            provider("mock_cli", false),
        ])));
        flow
    }

    /// A `cao launch` form with nothing loaded.
    fn launch_form() -> GuidedFlow {
        let mut flow = GuidedFlow::new();
        flow.select(CommandId::Launch).expect("launch is offered");
        flow
    }

    // ── VR-5 ─────────────────────────────────────────────────────────────────────────────

    /// **The 12 parameters, with HARD-CODED names and the 1/7/5 split** (VR-5, BR-2..BR-6).
    ///
    /// Every name, every `required`, and every kind below is a literal read off the Click tree
    /// and `cli/commands/launch.py` — **not** derived from `catalog::params`, which is the code
    /// under test. A test that built its expectation from the catalog would agree with whatever
    /// the table happened to say and would stay green through the exact drift it exists to
    /// catch: the table saying `--memory` takes a value, or `message` acquiring a `--`.
    ///
    /// Four separate assertions rather than one summed check, for the reason `catalog.rs`'s
    /// distribution test records: a total is conserved when one parameter changes kind, so
    /// `7 + 5 == 12` stays green through exactly the reclassification most likely to happen.
    ///
    /// Proven by mutation: flipping `--memory` to `ParamKind::Text` in `catalog.rs` turns this
    /// red on the kind assertion. (#321)
    #[test]
    fn the_twelve_launch_parameters_are_the_verified_cli_surface() {
        // (name, required, kind) — literals, in the CLI's declaration order.
        let expected: [(&str, bool, FieldKind); 12] = [
            ("message", false, FieldKind::Positional),
            ("--agents", true, FieldKind::Text),
            ("--session-name", false, FieldKind::Text),
            ("--headless", false, FieldKind::Flag),
            ("--provider", false, FieldKind::Text),
            ("--allowed-tools", false, FieldKind::Text),
            ("--async", false, FieldKind::Flag),
            ("--auto-approve", false, FieldKind::Flag),
            ("--yolo", false, FieldKind::Flag),
            ("--working-directory", false, FieldKind::Text),
            ("--memory", false, FieldKind::Flag),
            ("--env", false, FieldKind::Text),
        ];
        assert_eq!(
            expected.len(),
            12,
            "the literal expectation itself must list 12 parameters (BR-2)"
        );

        let fields = build_fields(CommandId::Launch);
        assert_eq!(
            fields.len(),
            12,
            "cao launch has exactly 12 parameters (BR-2); found {}",
            fields.len()
        );

        for (name, required, kind) in expected {
            let field = fields
                .iter()
                .find(|field| field.name == name)
                .unwrap_or_else(|| {
                    panic!(
                        "{name} must be a field of the launch form; found {:?}",
                        fields.iter().map(|f| f.name).collect::<Vec<_>>()
                    )
                });
            assert_eq!(
                field.required, required,
                "{name}: required must be {required} — a second required parameter refuses \
                 launches the CLI accepts (BR-3, FR-2.2)"
            );
            assert_eq!(
                field.kind, kind,
                "{name}: kind must be {kind:?}. `--memory` is the trap here — its name suggests \
                 a memory-manager VALUE and POST /sessions really does take `memory_manager`, but \
                 the CLI option is `is_flag=True` (launch.py:130-134, BR-5), and rendering it as \
                 text produces an invocation the CLI rejects"
            );
        }

        let required: Vec<&str> = fields
            .iter()
            .filter(|field| field.required)
            .map(|field| field.name)
            .collect();
        assert_eq!(
            required,
            vec!["--agents"],
            "`--agents` is the ONLY required parameter (BR-3)"
        );

        let flags = fields
            .iter()
            .filter(|field| field.kind == FieldKind::Flag)
            .count();
        let text_valued = fields
            .iter()
            .filter(|field| matches!(field.kind, FieldKind::Text | FieldKind::Positional))
            .count();
        assert_eq!(flags, 5, "5 flags (BR-2)");
        assert_eq!(
            text_valued, 7,
            "7 text-valued parameters — 6 options plus the positional `message` (BR-2)"
        );

        // BR-4: `message` is positional and must never be rendered with a `--`.
        let message = fields
            .iter()
            .find(|field| field.name == "message")
            .expect("`message` is a launch parameter");
        assert!(
            message.is_positional(),
            "`message` is a POSITIONAL argument (BR-4); rendering it as `--message` is a defect"
        );
        assert!(
            !message.name.starts_with("--"),
            "a positional argument's name must carry no `--` prefix"
        );

        // BR-6: the CLI's spelling, not the HTTP spelling. The two differ and nothing connects
        // them, so a request built from the wrong one is a runtime rejection.
        let names: BTreeSet<&str> = fields.iter().map(|field| field.name).collect();
        assert!(
            names.contains("--allowed-tools") && names.contains("--env"),
            "field names are the CLI's own spelling (BR-6); found {names:?}"
        );
        for http_spelling in ["allowed_tools", "env_vars", "agent_profile"] {
            assert!(
                !names.contains(http_spelling),
                "{http_spelling:?} is the SERVER's spelling and must not appear as a field name \
                 — `server-client` maps CLI names to HTTP names, and conflating the two produces \
                 requests the server rejects (BR-6). Found {names:?}"
            );
        }
    }

    // ── VR-3 — the sharpest trap in this unit ────────────────────────────────────────────

    /// **`can_run()` is true with ONLY `--agents` set** (VR-3, BR-3, INV-1, FR-2.2).
    ///
    /// # A test that filled every field could not detect the defect
    ///
    /// This is the assertion the design singles out. With all 12 fields populated, `can_run()`
    /// returns true whether the gate checks one required field or all twelve — so such a test
    /// would pass against the exact over-strict gating BR-3 forbids, a form that refuses launches
    /// the CLI would accept.
    ///
    /// So the form here is populated with `--agents` and **nothing else**, and the other eleven
    /// fields are asserted to still be unset — because "only agents is set" is half the
    /// hypothesis and a fixture that quietly set a second field would weaken it silently.
    ///
    /// Proven by mutation: dropping the `field.required &&` term from `missing()` turns this red.
    /// (#321)
    #[test]
    fn can_run_is_true_with_only_the_agents_field_set() {
        let mut flow = launch_form();

        assert!(
            !flow.can_run(),
            "an untouched launch form must not be runnable — `--agents` is unset"
        );

        flow.set("--agents", "planner")
            .expect("a plain profile name is valid");

        let populated: Vec<&str> = flow
            .fields()
            .iter()
            .filter(|field| field.value.is_some())
            .map(|field| field.name)
            .collect();
        assert_eq!(
            populated,
            vec!["--agents"],
            "the hypothesis is that ONLY `--agents` is set; any second populated field would \
             make this test unable to detect over-strict gating (VR-3)"
        );
        assert_eq!(
            flow.fields().len() - populated.len(),
            11,
            "the other 11 parameters must still be unset"
        );

        assert!(
            flow.can_run(),
            "`--agents` is the ONLY required parameter (BR-3), so the run must be available with \
             just it set. Blocking here refuses a launch the CLI would accept (FR-2.2)"
        );
        assert_eq!(
            flow.missing(),
            Vec::new(),
            "nothing is missing once the one required field is set"
        );
        assert_eq!(
            flow.blocked_reason(),
            None,
            "an available run states no blocking reason"
        );
    }

    /// **`can_run()` is false before a command is selected.**
    ///
    /// Not one of the six named traps, and the reason it is here is that it is the *inverse*
    /// vacuity of VR-3: an empty form has no fields, so a bare `missing().is_empty()` gate would
    /// report the run as available with nothing chosen at all. The `current.is_some()` term is
    /// what stops that, and this is the test that would notice its removal.
    #[test]
    fn can_run_is_false_before_a_command_is_selected() {
        let flow = GuidedFlow::new();

        assert_eq!(flow.fields().len(), 0, "an empty form has no fields");
        assert_eq!(
            flow.missing(),
            Vec::new(),
            "with no fields there is nothing to miss — which is exactly why `can_run` cannot be \
             `missing().is_empty()` alone"
        );
        assert!(
            !flow.can_run(),
            "the run must not be available before a command is selected"
        );
        assert_eq!(
            flow.blocked_reason().as_deref(),
            Some("blocked: no command selected"),
            "the block must state its reason even in this case (FR-6.2)"
        );
    }

    // ── VR-1 ─────────────────────────────────────────────────────────────────────────────

    /// **A blank optional is ABSENT FROM THE WIRE REQUEST**, not merely `None` in the struct
    /// (VR-1, FR-2.4, BR-9).
    ///
    /// The defect FR-2.4 guards against happens at **serialisation**, so asserting
    /// `field.value.is_none()` proves nothing: a `None` that serialised as `""` or `null` reaches
    /// the server as a supplied-but-empty value, which is not what the CLI does when a flag is
    /// omitted. So this asserts the emitted JSON — the exact body, not a key-by-key check, so a
    /// *gained* key is caught too.
    ///
    /// This is `server-client`'s VR-2 seen from the other side. **Both are needed**: that one
    /// proves the bytes leaving the process are right for a given `SessionParams`; this one
    /// proves the `SessionParams` a blank form produces is right in the first place.
    #[test]
    fn a_blank_optional_is_absent_from_the_serialised_request() {
        let mut flow = launch_form();
        flow.set("--agents", "planner").expect("valid profile");

        let params = flow.to_params().expect("the required field is set");

        assert_eq!(
            serde_json::to_string(&params).expect("SessionParams must serialise"),
            r#"{"agent_profile":"planner"}"#,
            "only the one required field may reach the wire when every optional is blank; a \
             `None` sent as \"\" or null violates FR-2.4 as surely as an empty string would"
        );
        assert_eq!(params.provider, None);
        assert_eq!(params.session_name, None);
        assert_eq!(params.working_directory, None);
        assert_eq!(params.allowed_tools, None);
        assert_eq!(params.env_vars, None);
        assert_eq!(params.initial_message, None);
    }

    /// **The typed launch message reaches `SessionParams`, under the catalog's own field name.**
    ///
    /// `to_params()` mapped 6 of 12 parameters and dropped the message, on a module-doc claim
    /// that `POST /sessions` had no parameter for it. It has one —
    /// `CreateSessionBody.initial_message` (`api/main.py:215`) — so the prompt an operator typed
    /// was collected and thrown away. (Reported by review on PR #547.)
    ///
    /// **The lookup name is what this test really pins.** `message` is a POSITIONAL argument
    /// (`launch.py:94`), so the catalog field is `"message"` with no dashes. Reading it as
    /// `"--message"` compiles, finds nothing, and silently reproduces the original defect — the
    /// failure is invisible in the type system and invisible in review. Asserting the value
    /// arrives is the only thing that catches it.
    #[test]
    fn the_typed_launch_message_reaches_the_request() {
        let mut flow = launch_form();
        flow.set("--agents", "planner").expect("valid profile");
        flow.set("message", "summarise the release notes")
            .expect("the positional message is a settable text field");

        let params = flow.to_params().expect("the required field is set");

        assert_eq!(
            params.initial_message.as_deref(),
            Some("summarise the release notes"),
            "the message the operator typed must reach the request rather than being dropped"
        );
        assert_eq!(
            serde_json::to_string(&params).expect("must serialise"),
            r#"{"agent_profile":"planner","initial_message":"summarise the release notes"}"#,
            "and it must serialise under the server's own key name"
        );

        // Blank stays absent: the server rejects `initial_message: ""` outright
        // (`api/main.py:1949-1950`), so a whitespace-only prompt must not become `Some("")`.
        let mut blank = launch_form();
        blank.set("--agents", "planner").expect("valid profile");
        blank.set("message", "   ").expect("clearing is valid");
        assert_eq!(
            blank.to_params().expect("runnable").initial_message,
            None,
            "a whitespace-only message must be ABSENT, not sent as an empty string the server \
             raises on"
        );
    }

    /// **The unwirable flags are exactly the launch parameters `to_params()` does not map.**
    ///
    /// The accounting test, and the reason a future edit cannot quietly drop a field again: all
    /// twelve `cao launch` parameters must be either **mapped** to a `SessionParams` field or
    /// **named** in [`UNWIRABLE_LAUNCH_FLAGS`] so the form can mark them. A parameter in neither
    /// set is silently discarded — which is precisely what `message` was.
    ///
    /// Derived from the catalog rather than from a hand-written list of twelve names, so adding a
    /// thirteenth parameter to `cao launch` fails here until someone decides which set it belongs
    /// in. That decision is the whole point; the test exists to force it rather than to check a
    /// number.
    #[test]
    fn the_unwirable_flags_are_exactly_the_unmapped_launch_parameters() {
        // The seven form fields `to_params()` reads, in its own order. Written out rather than
        // derived, because this list IS the claim being checked against the catalog.
        const MAPPED: [&str; 7] = [
            "--agents",
            "--provider",
            "--session-name",
            "--working-directory",
            "--allowed-tools",
            "--env",
            "message",
        ];

        let declared: Vec<&str> = catalog::params(CommandId::Launch)
            .iter()
            .map(|param| param.name)
            .collect();

        assert_eq!(
            declared.len(),
            MAPPED.len() + UNWIRABLE_LAUNCH_FLAGS.len(),
            "every `cao launch` parameter must be either mapped to the request or named as \
             unwirable — one in neither set is silently discarded, which is the defect this \
             accounting exists to prevent. Declared: {declared:?}"
        );

        for name in declared {
            let mapped = MAPPED.contains(&name);
            let unwirable = UNWIRABLE_LAUNCH_FLAGS.contains(&name);
            assert!(
                mapped != unwirable,
                "{name:?} must be in exactly one of the two sets (mapped={mapped}, \
                 unwirable={unwirable}); being in neither drops it silently and being in both \
                 means the form marks a field it does send"
            );
        }

        // `message` specifically: it moved from unwirable to mapped, and a regression would put
        // it back. Named separately so the aggregate above cannot absorb it.
        assert!(
            !UNWIRABLE_LAUNCH_FLAGS.contains(&"message"),
            "`message` IS wirable — it maps to `initial_message` in the JSON body. Listing it as \
             unwirable would re-document the defect as intended behaviour"
        );
    }

    /// The "not sent" marker is scoped to `cao launch`, because the names are not unique.
    ///
    /// `cao session send` also declares `--async` (measured: the only overlap across the 61
    /// commands), and there the claim "POST /sessions has no parameter for this" is simply false.
    /// A name-only predicate would print a confident wrong label — trading a silent drop for
    /// misinformation, which is not an improvement.
    #[test]
    fn the_not_sent_marker_applies_only_to_the_launch_form() {
        assert!(
            is_unwirable_launch_flag(Some(CommandId::Launch), "--async"),
            "`--async` on the launch form cannot reach POST /sessions and must be marked"
        );
        assert!(
            !is_unwirable_launch_flag(Some(CommandId::SessionSend), "--async"),
            "`--async` on `cao session send` is a different parameter entirely; marking it \
             \"not sent\" would be a false statement about a command this list says nothing about"
        );
        assert!(
            !is_unwirable_launch_flag(None, "--async"),
            "with no command selected there is no claim to make"
        );
        assert!(
            !is_unwirable_launch_flag(Some(CommandId::Launch), "--agents"),
            "a MAPPED launch field must never be marked as not sent"
        );
    }

    /// A field the operator fills and then clears returns to absent.
    ///
    /// The edge case `business-logic-model.md` names first, and the reason `set(name, "")` is the
    /// clear path rather than a separate method: a form that could only ever *acquire* values
    /// would make FR-2.4 unreachable the moment a key was pressed by mistake.
    #[test]
    fn clearing_a_filled_field_returns_it_to_absent_on_the_wire() {
        let mut flow = launch_form();
        flow.set("--agents", "planner").expect("valid profile");
        flow.set("--session-name", "work").expect("valid text");

        assert_eq!(
            serde_json::to_string(&flow.to_params().expect("runnable")).expect("must serialise"),
            r#"{"agent_profile":"planner","session_name":"work"}"#,
            "a filled optional reaches the wire"
        );

        flow.set("--session-name", "").expect("clearing is valid");

        assert_eq!(
            flow.field("--session-name")
                .and_then(|field| field.value.as_ref()),
            None,
            "a cleared field is unset in the form"
        );
        assert_eq!(
            serde_json::to_string(&flow.to_params().expect("runnable")).expect("must serialise"),
            r#"{"agent_profile":"planner"}"#,
            "and absent from the request — the struct being None is not the property that \
             matters (VR-1)"
        );
    }

    // ── VR-2 ─────────────────────────────────────────────────────────────────────────────

    /// **Whitespace-only input is tested SEPARATELY from empty input** (VR-2, BR-7, BR-8).
    ///
    /// A guard that trims `""` but not `"   "` passes an empty-string test while still leaking
    /// whitespace to the server, so the two cases are asserted independently and each is checked
    /// on the **wire**, not on the struct. Four whitespace shapes, because a `trim()` that was
    /// narrowed to spaces would still pass a spaces-only case: tabs, newlines and a mix are all
    /// input a paste can produce.
    #[test]
    fn whitespace_only_input_collapses_to_none_separately_from_empty_input() {
        // Case 1: the empty string.
        let mut empty = launch_form();
        empty.set("--agents", "planner").expect("valid profile");
        empty.set("--session-name", "").expect("empty is accepted");
        assert_eq!(
            serde_json::to_string(&empty.to_params().expect("runnable")).expect("serialises"),
            r#"{"agent_profile":"planner"}"#,
            "an EMPTY optional must be omitted from the request (BR-7)"
        );

        // Case 2: whitespace only — a DIFFERENT input, asserted on its own.
        for blank in ["   ", "\t", "\n", " \t \n "] {
            let mut flow = launch_form();
            flow.set("--agents", "planner").expect("valid profile");
            flow.set("--session-name", blank)
                .unwrap_or_else(|error| panic!("{blank:?} must be accepted, got {error:?}"));

            assert_eq!(
                flow.field("--session-name")
                    .and_then(|field| field.value.as_ref()),
                None,
                "{blank:?} is a blank the operator did not fill in and must collapse to None \
                 (BR-8)"
            );
            assert_eq!(
                serde_json::to_string(&flow.to_params().expect("runnable")).expect("serialises"),
                r#"{"agent_profile":"planner"}"#,
                "sending {blank:?} would be as wrong as sending \"\" — both reach the server as \
                 a supplied-but-empty value"
            );
        }

        // And a value with surrounding whitespace keeps its content, trimmed — the collapse must
        // not become "reject anything with a space in it".
        let mut padded = launch_form();
        padded.set("--agents", "  planner  ").expect("trimmed");
        assert_eq!(
            padded
                .field("--agents")
                .and_then(|field| field.value.clone()),
            Some(FieldValue::Text("planner".to_string())),
            "a padded value is trimmed, not rejected and not stored with its padding"
        );
    }

    // ── VR-4 ─────────────────────────────────────────────────────────────────────────────

    /// **`set()` returns `Err(Invalid)` for a `loadable: false` profile** (VR-4, FR-1.5, BR-15,
    /// SR-4).
    ///
    /// # Asserting that the picker lists it leaves "not selectable" unproven
    ///
    /// FR-1.5 requires an unloadable profile to be **rendered, marked, and not selectable**, and
    /// only the third of those is a code behaviour. A test that checked the list contains
    /// `__pycache__` would pass against an implementation that happily accepted it — a greyed
    /// control the keyboard can still activate is not a control. So this asserts the refusal.
    ///
    /// # This contradicts affirmed memory, and FR-1.5 governs
    ///
    /// `project.md:98` reads *"ALWAYS filter agent profiles on `loadable == true` before
    /// presenting them in any picker"*. A filter is therefore exactly what a well-intentioned
    /// implementer reading affirmed memory would add here, and this test is what stops them. The
    /// memory correction is **OQ-5**; `server.rs` carries the same note on the other half of the
    /// rule.
    ///
    /// Proven by mutation: deleting the `refuse_unloadable_profile` call turns this red. (#321)
    #[test]
    fn setting_an_unloadable_profile_is_refused_not_merely_styled() {
        let mut flow = launch_form_with_pickers();

        // Populated and marked: the profile IS in the list (FR-1.5, BR-15).
        let listed: Vec<&str> = flow
            .agent_choices()
            .choices()
            .expect("the agent picker loaded")
            .iter()
            .map(|profile| profile.name.as_str())
            .collect();
        assert!(
            listed.contains(&"__pycache__"),
            "an unloadable profile must be POPULATED, not filtered (FR-1.5, BR-15). \
             `project.md:98` says the opposite and FR-1.5 supersedes it (OQ-5) — filtering hides \
             the diagnosis, marking it explains it. Found {listed:?}"
        );

        // And NOT selectable — the assertion the listing check cannot make.
        let error = flow
            .set("--agents", "__pycache__")
            .expect_err("an unloadable profile must be refused, not accepted");

        let message = match &error {
            Error::Invalid(reason) => reason.clone(),
            other => panic!("expected Error::Invalid, got {other:?}"),
        };
        assert!(
            message.contains("__pycache__"),
            "the refusal must name the profile so the operator knows which row was refused; \
             got {message}"
        );
        assert!(
            message.contains(UNLOADABLE_MARKER),
            "the refusal must carry the same explanation the picker's marker shows, or the \
             operator gets two different accounts of one condition; got {message}"
        );
        assert!(
            !message.contains('\n'),
            "operator-facing messages are ONE line, never a traceback; got {message:?}"
        );

        // The refusal must not have written the value.
        assert_eq!(
            flow.field("--agents")
                .and_then(|field| field.value.as_ref()),
            None,
            "a refused set must leave the field unset, or the form would be runnable with a \
             profile that cannot load"
        );
        assert!(
            !flow.can_run(),
            "and the run must stay blocked (BR-3: `--agents` is required)"
        );

        // A loadable profile from the same list is accepted, so the guard is a predicate and not
        // a blanket refusal.
        flow.set("--agents", "planner")
            .expect("a loadable profile must be accepted");
        assert!(flow.can_run(), "the form is runnable with a loadable agent");
    }

    /// The refusal is checked against the **loaded** list, so a failed picker does not disable the
    /// only required field (BR-17).
    ///
    /// The alternative — refusing every name while the picker is down — would make an unreachable
    /// server a launch blocker rather than a stated condition, which is the dead-form failure
    /// BR-17 names. Asserted for both non-loaded states, because `Loading` and `Failed` reach the
    /// same branch by different routes.
    #[test]
    fn an_agent_name_is_accepted_while_the_picker_has_no_list() {
        for state in ["loading", "failed"] {
            let mut flow = launch_form();
            if state == "failed" {
                flow.apply_picker_update(PickerUpdate::Agents(Err(TuiError::Unreachable(
                    "cao-server is unreachable".to_string(),
                ))));
            }

            flow.set("--agents", "planner").unwrap_or_else(|error| {
                panic!("a name must be accepted while the picker is {state}: {error:?}")
            });
            assert!(
                flow.can_run(),
                "the form must stay runnable while the picker is {state} (BR-17)"
            );
        }
    }

    // ── VR-6 ─────────────────────────────────────────────────────────────────────────────

    /// **`missing()` names the unset required field, and drives a STATED reason** (VR-6, BR-18,
    /// BR-19, FR-6.2).
    ///
    /// The mutation target VR-6 names: inverting `is_none()` to `is_some()` in `missing()` turns
    /// this red, and so does dropping the `required &&` term. A gating guard that cannot fail is
    /// this project's dominant failure mode, which is why the predicate is asserted from both
    /// sides — unset *and* set.
    #[test]
    fn missing_names_the_unset_required_field_and_states_the_reason() {
        let mut flow = launch_form();

        let missing = flow.missing();
        assert_eq!(
            missing.iter().map(|field| field.name).collect::<Vec<_>>(),
            vec!["--agents"],
            "the one unset required field is `--agents` — and ONLY it, because the other 11 are \
             optional (BR-3)"
        );
        assert!(
            missing.iter().all(|field| field.required),
            "`missing()` reports required fields only"
        );

        // The other side of the predicate: setting it empties the list.
        flow.set("--agents", "planner").expect("valid profile");
        assert_eq!(
            flow.missing(),
            Vec::new(),
            "a satisfied required field is no longer missing — inverting the `is_none()` \
             predicate turns this assertion red (VR-6)"
        );
    }

    /// **The block states its reason rather than merely greying a control** (BR-18, FR-6.2).
    ///
    /// The exact operator-facing string is a hard-coded literal, because the requirement is about
    /// the words: "a disabled button with no reason is the failure this requirement names". A
    /// test asserting only `blocked_reason().is_some()` would pass against `Some(String::new())`.
    #[test]
    fn the_blocked_reason_names_the_field_rather_than_merely_greying_out() {
        let flow = launch_form();

        assert_eq!(
            flow.blocked_reason().as_deref(),
            Some("blocked: --agents required"),
            "FR-6.2 requires a stated reason naming the field, not a greyed control"
        );

        // And `Incomplete` carries the list so the caller need not re-derive it (BR-19).
        let error = flow
            .to_params()
            .expect_err("to_params must refuse an incomplete form");
        match error {
            Error::Incomplete { missing } => assert_eq!(
                missing.iter().map(|field| field.name).collect::<Vec<_>>(),
                vec!["--agents"],
                "Incomplete must CARRY the missing fields (BR-19)"
            ),
            other => panic!("expected Error::Incomplete, got {other:?}"),
        }
    }

    // ── INV-3 ────────────────────────────────────────────────────────────────────────────

    /// **`select()` on a hidden command is `Err(Hidden)`** (INV-3).
    ///
    /// Defensive and unreachable from the UI — `catalog::commands()` filters `Hidden` rows — so
    /// the value of the arm is that a *programmatic* caller fails loudly instead of silently
    /// building a form for a command the operator must never be offered. `cao info` and
    /// `cao memory compact` are both HIDE in the catalog; the second is there because it was
    /// classified HANDOFF during design, compiled perfectly, and was wrong.
    #[test]
    fn select_on_a_hidden_command_is_refused_and_leaves_the_form_untouched() {
        let mut flow = launch_form();
        flow.set("--agents", "planner").expect("valid profile");

        for hidden in [CommandId::Info, CommandId::MemoryCompact] {
            assert_eq!(
                flow.select(hidden),
                Err(Error::Hidden),
                "{hidden:?} is HIDE in the catalog and must not be selectable (INV-3)"
            );
        }

        assert_eq!(
            flow.current(),
            Some(CommandId::Launch),
            "a refused select must not disturb the current form"
        );
        assert!(flow.can_run(), "nor its values");
    }

    /// An unknown field name is reported **by name**.
    ///
    /// The plausible cause is a stale name after a reselect, so the message has to say which one
    /// — `UnknownField` with no payload would leave a caller diffing two field sets by hand.
    #[test]
    fn an_unknown_field_is_reported_by_name() {
        let mut flow = launch_form();

        assert_eq!(
            flow.set("--memory-manager", "curator"),
            Err(Error::UnknownField("--memory-manager".to_string())),
            "`--memory-manager` is not a `cao launch` parameter; `--memory` is a FLAG (BR-5)"
        );
        assert_eq!(
            flow.set("--scope", "team"),
            Err(Error::UnknownField("--scope".to_string())),
            "a parameter of another command is not a field of this form"
        );
    }

    // ── BR-23, SR-2, SR-3 — `--env` ──────────────────────────────────────────────────────

    /// **A malformed `--env` pair is `Err(Invalid)` NAMING the expected shape** (BR-23).
    ///
    /// An error that does not say what was expected forces the operator to guess, so every
    /// rejection below is asserted to contain the literal `KEY=VALUE`. The four causes are the
    /// four `launch.py::_parse_env_pairs` raises on, and each is exercised separately because a
    /// guard that catches a missing `=` while ignoring a blocked prefix passes a single-case
    /// test.
    #[test]
    fn a_malformed_env_pair_is_refused_and_names_the_expected_shape() {
        let mut flow = launch_form();

        // 1. No `=` at all.
        let error = flow
            .set("--env", "AWS_REGION")
            .expect_err("a pair with no `=` must be refused");
        let message = error.to_string();
        assert!(
            message.contains(ENV_PAIR_SHAPE),
            "the rejection must name the expected {ENV_PAIR_SHAPE} shape (BR-23); got {message}"
        );
        assert!(
            message.contains("AWS_REGION"),
            "and quote what was actually given; got {message}"
        );

        // 2. A key that is not a POSIX env name (`launch.py:72-79`).
        let error = flow
            .set("--env", "9LIVES=cat")
            .expect_err("a key starting with a digit must be refused");
        assert!(error.to_string().contains(ENV_PAIR_SHAPE), "got {error}");

        // 3. A blocked prefix — the deny-by-default policy, at entry (SR-3).
        let error = flow
            .set("--env", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192")
            .expect_err("a blocked prefix must be refused");
        let message = error.to_string();
        assert!(
            message.contains("CLAUDE"),
            "the rejection must name the blocked prefix so the operator can see the rule; \
             got {message}"
        );

        // 3b. ...but the six allowlisted keys survive it, which is the ordering `env_guard`
        // records as a security behaviour: every one of them starts with `CLAUDE`, so a
        // prefix-first check drops `CLAUDE_CODE_USE_BEDROCK` and breaks Bedrock auth.
        flow.set("--env", "CLAUDE_CODE_USE_BEDROCK=1")
            .expect("an allowlisted key must survive the CLAUDE prefix");

        // 4. A value at or above the byte cap. Exactly 2048 is DROPPED — `>=`, mirroring
        // `tmux.py:121` — so the fixture is exactly at the boundary, which is the one input a
        // `>` implementation gets wrong.
        let at_cap = "x".repeat(crate::env_guard::MAX_ENV_VALUE_BYTES);
        let error = flow
            .set("--env", &format!("BIG={at_cap}"))
            .expect_err("a value of exactly the cap must be refused (>=, not >)");
        let message = error.to_string();
        assert!(
            message.contains("2048"),
            "the rejection must state the cap; got {message}"
        );

        // One byte under the cap is accepted, so the boundary is a boundary and not a blanket
        // refusal.
        let under_cap = "x".repeat(crate::env_guard::MAX_ENV_VALUE_BYTES - 1);
        flow.set("--env", &format!("BIG={under_cap}"))
            .expect("one byte under the cap is accepted");
    }

    /// `--env` pairs reach the request body in deterministic order, and an empty map omits the
    /// body entirely (BR-20, BR-21, BR-22, issue **#248**).
    ///
    /// The exact-literal assertion is what gives this teeth: swapping `BTreeMap` for `HashMap`
    /// would compile and keep two equal maps equal to each other while making the emitted order
    /// differ from sorted. Values containing spaces and `=` are included because one pair per
    /// **line** is the parse rule, and a whitespace-splitting implementation would mangle both.
    #[test]
    fn env_pairs_serialise_in_sorted_order_and_an_empty_map_omits_the_body() {
        let mut flow = launch_form();
        flow.set("--agents", "planner").expect("valid profile");
        flow.set(
            "--env",
            "ZULU=last\nAWS_REGION=us-east-1\nGREETING=hello world\nEQUALS=a=b\n",
        )
        .expect("four well-formed pairs");

        let params = flow.to_params().expect("runnable");
        assert_eq!(
            serde_json::to_string(&params).expect("serialises"),
            concat!(
                r#"{"agent_profile":"planner","env_vars":{"#,
                r#""AWS_REGION":"us-east-1","EQUALS":"a=b","GREETING":"hello world","#,
                r#""ZULU":"last"}}"#
            ),
            "env_vars must serialise in sorted key order (BR-20) so a test can assert an exact \
             body; a value containing a space or an `=` must survive intact"
        );

        // BR-22: an empty entry omits the body rather than sending `{}`.
        flow.set("--env", "").expect("clearing is valid");
        assert_eq!(
            serde_json::to_string(&flow.to_params().expect("runnable")).expect("serialises"),
            r#"{"agent_profile":"planner"}"#,
            "an empty env map must omit the body ENTIRELY (BR-22), never `{{}}` and never \
             `{{\"env_vars\":{{}}}}`"
        );
        assert_eq!(flow.to_params().expect("runnable").env_vars, None);
    }

    /// A later pair overrides an earlier one on a key collision, matching `_merge_extra_env`.
    #[test]
    fn a_repeated_env_key_takes_the_later_value() {
        let mut flow = launch_form();
        flow.set("--agents", "planner").expect("valid profile");
        flow.set("--env", "AWS_REGION=us-east-1\nAWS_REGION=us-west-2")
            .expect("both pairs are well-formed");

        let params = flow.to_params().expect("runnable");
        assert_eq!(
            params
                .env_vars
                .as_ref()
                .and_then(|vars| vars.get("AWS_REGION"))
                .map(String::as_str),
            Some("us-west-2"),
            "later pairs override earlier ones, as `tmux.py:105-128`'s dict assignment does"
        );
    }

    /// The POSIX env-name predicate, at its boundaries.
    ///
    /// Mirrors `launch.py:72-79` including the `isascii()` term. Asserted directly because it is
    /// a predicate with four independent clauses and a table is the honest way to cover them.
    #[test]
    fn the_env_key_predicate_matches_the_python_shape() {
        for valid in ["A", "_", "AWS_REGION", "_private", "X9", "a_b_9"] {
            assert!(is_valid_env_key(valid), "{valid:?} must be a valid env key");
        }
        for invalid in ["", "9LIVES", "A-B", "A B", "A.B", "CAFÉ", "Ω"] {
            assert!(
                !is_valid_env_key(invalid),
                "{invalid:?} must be rejected — `launch.py:72-79` requires \
                 [A-Za-z_][A-Za-z0-9_]* and ASCII"
            );
        }
    }

    // ── Flags ────────────────────────────────────────────────────────────────────────────

    /// Flags accept booleans, reject anything else, and clear on a blank (BR-5).
    ///
    /// `--memory` is one of the five, and it is the one asserted first: its name suggests a
    /// value, `POST /sessions` really does take a `memory_manager`, and the CLI option is
    /// boolean. A form that accepted `--memory curator` would build an invocation the CLI
    /// rejects.
    #[test]
    fn flags_accept_booleans_and_reject_a_value() {
        let mut flow = launch_form();

        for flag in [
            "--memory",
            "--headless",
            "--async",
            "--auto-approve",
            "--yolo",
        ] {
            flow.set(flag, "true")
                .unwrap_or_else(|error| panic!("{flag} must accept `true`: {error:?}"));
            assert_eq!(
                flow.field(flag).and_then(|field| field.value.clone()),
                Some(FieldValue::Flag(true)),
                "{flag} stores a boolean"
            );

            let error = flow
                .set(flag, "curator")
                .expect_err("a flag must refuse a value");
            assert!(
                error.to_string().contains("flag"),
                "{flag} must refuse a value and say it is a flag; got {error}"
            );

            // The refusal leaves the previous value standing rather than half-applying.
            assert_eq!(
                flow.field(flag).and_then(|field| field.value.clone()),
                Some(FieldValue::Flag(true)),
                "{flag} keeps its prior value after a refused set"
            );

            flow.set(flag, "  ").expect("a blank clears a flag");
            assert_eq!(
                flow.field(flag).and_then(|field| field.value.as_ref()),
                None,
                "{flag}: a blank entry means `not filled in`, whatever the kind (BR-7)"
            );
        }

        // And `--memory` is a Flag in the field set, not Text — the BR-5 assertion in the place
        // an implementer would get it wrong.
        assert_eq!(
            flow.field("--memory").map(|field| field.kind),
            Some(FieldKind::Flag),
            "`--memory` is `is_flag=True` at launch.py:130-134 (BR-5)"
        );
    }

    // ── FR-2.1 / FR-2.3 / BR-10 / BR-11 ──────────────────────────────────────────────────

    /// **The guided steps come first, and all 11 optionals stay reachable** (FR-2.1, FR-2.3,
    /// BR-10, BR-11).
    ///
    /// Hidden-by-default is not the same as absent: the collapsed section holds nine of the
    /// twelve parameters and every one of them must be settable without leaving the form. The
    /// order is a hard-coded literal because FR-2.1 specifies a designed sequence — deriving it
    /// from `GUIDED_STEP_ORDER` would compare production against itself.
    #[test]
    fn the_guided_steps_come_first_and_every_optional_stays_reachable() {
        let mut flow = launch_form();

        let order: Vec<&str> = flow.fields().iter().map(|field| field.name).collect();
        assert_eq!(
            &order[..3],
            &["--agents", "--provider", "--session-name"],
            "FR-2.1's step order is agent -> provider -> session name, and it is a designed \
             sequence rather than the CLI's declaration order (BR-11). Found {order:?}"
        );
        assert_eq!(
            flow.guided_field_count(),
            3,
            "three guided steps; the other nine are the collapsed section (FR-2.3)"
        );

        // BR-10: every optional is reachable. Set all 11 through the public API — a parameter
        // that could not be addressed would be absent in practice however it is listed.
        let optionals: Vec<&str> = flow
            .fields()
            .iter()
            .filter(|field| !field.required)
            .map(|field| field.name)
            .collect();
        assert_eq!(
            optionals.len(),
            11,
            "11 of the 12 parameters are optional (BR-3); found {optionals:?}"
        );

        for name in optionals {
            let value = match flow.field(name).map(|field| field.kind) {
                Some(FieldKind::Flag) => "true",
                Some(_) if name == "--env" => "REACHED=yes",
                Some(_) => "reached",
                None => unreachable!("the name came from the field set"),
            };
            flow.set(name, value)
                .unwrap_or_else(|error| panic!("{name} must be reachable (BR-10): {error:?}"));
            assert!(
                flow.field(name)
                    .map(|field| field.value.is_some())
                    .unwrap_or(false),
                "{name} must hold the value it was given"
            );
        }
    }

    /// Reselecting a command **resets the field values** but keeps the picker choices.
    ///
    /// Carrying `--session-name` across commands would silently apply the operator's input to a
    /// command they did not type it for. The pickers survive because the agent and provider lists
    /// are properties of the server rather than of the command — re-fetching on every reselect
    /// would spend two HTTP calls to learn the same answer.
    #[test]
    fn reselecting_a_command_resets_the_values_and_keeps_the_pickers() {
        let mut flow = launch_form_with_pickers();
        flow.set("--agents", "planner").expect("valid profile");
        flow.set("--session-name", "work").expect("valid text");

        flow.select(CommandId::SessionList)
            .expect("session list is offered");
        assert_eq!(flow.current(), Some(CommandId::SessionList));
        assert!(
            flow.fields().iter().all(|field| field.value.is_none()),
            "selecting another command must not carry values across"
        );

        flow.select(CommandId::Launch).expect("launch is offered");
        assert!(
            flow.fields().iter().all(|field| field.value.is_none()),
            "and coming back must not restore them either — reset means reset"
        );
        assert!(
            flow.agent_choices().choices().is_some(),
            "the picker choices survive a reselect: they describe the server, not the command"
        );
    }

    /// `to_params()` refuses a form that is not `cao launch`.
    ///
    /// `SessionParams` is the launch request and the other 60 commands have nothing to build. A
    /// permissive implementation would assemble one from whatever fields happened to share a
    /// name, which is a request the server rejects for reasons the operator cannot see.
    #[test]
    fn to_params_refuses_a_form_that_is_not_launch() {
        let mut flow = GuidedFlow::new();

        assert!(
            matches!(flow.to_params(), Err(Error::Invalid(_))),
            "an empty form has no session request to build"
        );

        flow.select(CommandId::SessionSend)
            .expect("session send is offered");
        match flow.to_params() {
            Err(Error::Invalid(reason)) => assert!(
                reason.contains("launch"),
                "the refusal must name the one command that has a session request; got {reason}"
            ),
            other => panic!("expected Error::Invalid, got {other:?}"),
        }
    }

    // ── BR-12, BR-13, BR-16, NFR-1 — the pickers ─────────────────────────────────────────

    /// **The two picker fetches run CONCURRENTLY** (BR-13, NFR-1).
    ///
    /// # Why a rendezvous and not a stopwatch
    ///
    /// A timing assertion ("the pair finished in under N ms") is flaky on a loaded CI box and
    /// proves overlap only statistically. Instead each fetch records its arrival and waits for
    /// the other, so a **sequential** implementation cannot satisfy both: the first call would
    /// wait alone, its deadline would expire, and the assertion inside the source fails.
    ///
    /// The wait is bounded rather than a `Barrier`, deliberately: a `Barrier` would make a
    /// sequential implementation **hang**, spending a CI timeout instead of printing a message —
    /// the same reasoning the pty harness records for its per-read deadline.
    #[test]
    fn the_two_picker_fetches_run_concurrently() {
        let mut flow = launch_form();
        let source = Arc::new(FakeSource::rendezvousing());

        let feed = flow.populate_pickers(Arc::clone(&source));
        assert_eq!(feed.expected(), 2, "two independent GETs (BR-13)");
        assert!(
            flow.agent_choices().is_loading() && flow.provider_choices().is_loading(),
            "both pickers report `Loading` before either answer arrives, so the form has a \
             truthful state to render"
        );

        let applied = flow.await_pickers(&feed, WAIT);

        assert_eq!(
            applied, 2,
            "both fetches must complete; fewer means at least one waited alone for {WAIT:?}, \
             which is what a SEQUENTIAL implementation produces (BR-13)"
        );
        assert_eq!(
            flow.agent_choices()
                .choices()
                .map(<[Profile]>::len)
                .unwrap_or(0),
            1,
            "the agent picker loaded"
        );
        assert_eq!(
            flow.provider_choices()
                .choices()
                .map(<[Provider]>::len)
                .unwrap_or(0),
            1,
            "the provider picker loaded"
        );
    }

    /// Neither picker filters: unloadable profiles and uninstalled providers are both present
    /// (FR-1.5, FR-1.7, BR-15, BR-16).
    ///
    /// `installed == false` is display information and never a predicate — the drift it guards is
    /// live, since `GET /agents/providers` serves a hard-coded nine-entry map against a ten-value
    /// `ProviderType` enum. Asserting the two rules side by side pins that one forbids filtering
    /// by **loadability** and the other forbids filtering by **installation**.
    #[test]
    fn neither_picker_filters_what_the_server_returned() {
        let mut flow = launch_form();
        let source = Arc::new(FakeSource::new(
            vec![profile("planner", true), profile("__pycache__", false)],
            vec![provider("kiro_cli", true), provider("mock_cli", false)],
        ));

        let feed = flow.populate_pickers(source);
        assert_eq!(flow.await_pickers(&feed, WAIT), 2, "both pickers answered");

        let profiles = flow
            .agent_choices()
            .choices()
            .expect("the agent picker loaded");
        assert_eq!(
            profiles.len(),
            2,
            "both profiles must be present — filtering on `loadable` is forbidden (FR-1.5, \
             BR-15); found {:?}",
            profiles.iter().map(|p| &p.name).collect::<Vec<_>>()
        );
        assert!(
            profiles
                .iter()
                .any(|p| p.name == "__pycache__" && !p.loadable),
            "and the unloadable one must arrive with `loadable: false` intact — returning it as \
             loadable would defeat the marker just as thoroughly as filtering it"
        );

        let providers = flow
            .provider_choices()
            .choices()
            .expect("the provider picker loaded");
        assert_eq!(
            providers.len(),
            2,
            "providers are NEVER filtered, including on `installed == false` (FR-1.7, BR-16)"
        );
        assert!(
            providers
                .iter()
                .any(|p| p.name == "mock_cli" && !p.installed),
            "`installed` is display information, never a predicate"
        );
    }

    /// **A picker failure states cause and remedy, offers no CLI fallback, and does not disable
    /// the rest of the form** (FR-1.4, FR-6.1, BR-14, BR-17).
    ///
    /// Three distinct rules in one scenario because they are one operator experience:
    ///
    /// - The failed picker carries `server-client`'s typed error, whose `Display` already names
    ///   the address tried and the `CAO_API_HOST` remedy.
    /// - `Loaded(vec![])` is **not** how a failure is represented, so the renderer can tell "no
    ///   profiles exist" from "the server could not be read".
    /// - The **other** picker still loads and the fields are still settable. A form that went dead
    ///   because one GET failed is the failure BR-17 names.
    #[test]
    fn a_failed_picker_states_its_cause_and_leaves_the_form_usable() {
        let mut flow = launch_form();
        let source = Arc::new(FakeSource::failing(
            Some("cao-server is unreachable at 10.0.0.5:1234; check CAO_API_HOST/CAO_API_PORT"),
            None,
        ));

        let feed = flow.populate_pickers(source);
        assert_eq!(flow.await_pickers(&feed, WAIT), 2, "both fetches reported");

        let failure = flow
            .agent_choices()
            .failure()
            .expect("the agent picker failed");
        let message = failure.to_string();
        assert!(
            message.contains("10.0.0.5:1234") && message.contains("CAO_API_HOST"),
            "a failed picker must state the cause, the address, and the remedy (FR-6.1); \
             got {message}"
        );
        assert!(
            flow.agent_choices().choices().is_none(),
            "a FAILURE must not be represented as an empty list — `Loaded(vec![])` is a valid \
             answer from a machine with no profiles, and conflating the two tells the operator \
             something is broken when nothing is"
        );

        // BR-17: the rest of the form is untouched.
        assert!(
            flow.provider_choices().choices().is_some(),
            "the independent provider fetch must still have loaded (BR-17)"
        );
        flow.set("--agents", "planner")
            .expect("the required field stays settable when its picker fails (BR-17)");
        flow.set("--session-name", "work")
            .expect("and so do the others");
        assert!(
            flow.can_run(),
            "a picker failure must not make the launch unreachable — degrade visibly, not into \
             a dead form (T-6, BR-17)"
        );
    }

    /// An empty profile list is `Loaded(vec![])`, which is a valid answer and not a failure.
    #[test]
    fn an_empty_picker_result_is_loaded_and_not_failed() {
        let mut flow = launch_form();
        let feed = flow.populate_pickers(Arc::new(FakeSource::new(Vec::new(), Vec::new())));
        assert_eq!(flow.await_pickers(&feed, WAIT), 2);

        assert_eq!(
            flow.agent_choices().choices().map(<[Profile]>::len),
            Some(0),
            "an empty list is `Loaded(vec![])`, which renders an explicit empty state"
        );
        assert!(
            flow.agent_choices().failure().is_none(),
            "and is emphatically NOT a failure"
        );
    }

    /// `drain_picker_updates` applies whatever has arrived without blocking — the event-loop path
    /// (TS-2, INV-5).
    ///
    /// The mechanism behind "no locking is needed": picker state is applied on the thread that
    /// mutates fields, so `GuidedFlow` is never shared across threads at all.
    #[test]
    fn draining_applies_the_answers_without_blocking() {
        let mut flow = launch_form();
        let feed = flow.populate_pickers(Arc::new(FakeSource::new(
            vec![profile("planner", true)],
            vec![provider("kiro_cli", true)],
        )));

        // Poll as an event loop would, with a bound so a regression fails rather than hangs.
        let deadline = Instant::now() + WAIT;
        let mut applied = 0;
        while applied < feed.expected() && Instant::now() < deadline {
            applied += flow.drain_picker_updates(&feed);
            if applied < feed.expected() {
                std::thread::sleep(Duration::from_millis(2));
            }
        }

        assert_eq!(
            applied, 2,
            "both answers must be applied by non-blocking drains within {WAIT:?}"
        );
        assert_eq!(
            flow.drain_picker_updates(&feed),
            0,
            "a drained feed yields nothing further and must not block"
        );
    }

    /// `GUIDED_STEP_ORDER`'s names are real parameters of `cao launch`.
    ///
    /// A typo there would silently degrade the order to "catalog order" rather than fail: the
    /// prefix loop simply would not find the name. That is a guard that cannot fire, so it is
    /// asserted directly.
    #[test]
    fn every_guided_step_is_an_actual_launch_parameter() {
        let names: BTreeSet<&str> = build_fields(CommandId::Launch)
            .iter()
            .map(|field| field.name)
            .collect();

        for step in GUIDED_STEP_ORDER {
            assert!(
                names.contains(step),
                "guided step {step:?} is not a `cao launch` parameter, so the step order \
                 silently degrades to the CLI's declaration order. Found {names:?}"
            );
        }
    }

    /// A command with no parameters yields an empty form that is nonetheless runnable.
    ///
    /// `cao profile list` takes nothing, so `missing()` is empty and the run is available the
    /// moment it is selected. Asserted because it is the case that distinguishes "no required
    /// fields" from "no command selected": a gate that conflated them would either block a
    /// parameterless command forever or offer a run with nothing chosen.
    ///
    /// **This test named `cao session list` in its first draft and was wrong** — that command
    /// carries `--json`. The literal `0` caught it, which is the argument for the literal: an
    /// assertion of `flow.fields().len() == catalog::params(id).len()` would have passed against
    /// the mistaken premise and left the parameterless case untested. Of the 12 rows with no
    /// parameters, 8 are HIDE and thus unselectable (INV-3), so the choice is between
    /// `profile list`, `schedule list`, `profile templates`, and `skills list`. (#321)
    #[test]
    fn a_parameterless_command_is_runnable_as_soon_as_it_is_selected() {
        let mut flow = GuidedFlow::new();
        flow.select(CommandId::ProfileList)
            .expect("profile list is offered");

        assert_eq!(
            flow.fields().len(),
            0,
            "`cao profile list` takes no options"
        );
        assert!(
            flow.can_run(),
            "a command with no required fields is runnable once selected"
        );
        assert_eq!(flow.blocked_reason(), None, "and states no blocking reason");
        assert_eq!(
            flow.guided_field_count(),
            0,
            "it has none of the three guided steps"
        );
    }

    /// The `Field` values `missing()` hands back are the form's own, not fresh blanks.
    ///
    /// `Incomplete` carries them so the caller need not re-derive the list (BR-19); if they were
    /// reconstructed rather than cloned, a `required` or `kind` that had drifted would be
    /// invisible to the caller.
    #[test]
    fn the_missing_fields_are_the_forms_own_field_values() {
        let flow = launch_form();
        let missing = flow.missing();

        assert_eq!(
            missing,
            vec![Field {
                name: "--agents",
                kind: FieldKind::Text,
                required: true,
                value: None,
            }],
            "the reported field must carry its real kind and required flag, not a placeholder"
        );
    }
}
