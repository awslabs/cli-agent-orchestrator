//! The wire vocabulary every other `cao-tui` unit is written in (issue #321).
//!
//! This module is `spec`-kind: type definitions and their serialisation, and nothing else.
//! No HTTP, no subprocess, no file I/O (INV-4). The types here mirror what `cao-server`
//! actually puts on the wire — deliberately the *projection*, not the server's internal
//! model, because a Rust struct that mirrors an internal model will silently deserialise
//! the fields the projection drops to `None`.
//!
//! Six units consume this vocabulary. Two rules therefore matter more than they look:
//!
//! - `Profile` carries no `provider` field, and that is a prohibition (BR-1), not an
//!   oversight — see the type's own docs.
//! - `SessionParams::agents` serialises as `agent_profile`, and getting that wrong is a
//!   runtime 422 rather than a compile error — see the field's own docs.
//!
//! Nothing in this module performs the six-to-three `TerminalStatus` → `Readiness`
//! collapse. That decision belongs to `skeleton-handoff-proof`'s `await_ready`, where it
//! is explicit and reviewable (BR-7).

// ## Why NONE of the seven types below carries `#[allow(dead_code)]` any more
//
// This module is a vocabulary, and in a **binary** crate `pub` does not exempt an item from
// `dead_code` — there is no downstream crate that could use it — so a type warns until an
// in-crate caller exists. When this module was written all seven warned (measured: 7 in the bin
// cfg with the allows removed). `skeleton-handoff-proof` then consumed four of them (`Terminal`,
// `TerminalStatus`, `Readiness`, `Health`) and those four allows were removed, leaving three on
// `Profile`, `Provider`, and `SessionParams` with a standing instruction: *"the unit that first
// needs `Profile`, `Provider`, or `SessionParams` should either [add lib.rs] or delete that
// type's allow."*
//
// **`server-client` (Bolt 3) is that unit, and the last three allows are now gone.** `server.rs`
// returns `Vec<Profile>` from `profiles()`, `Vec<Provider>` from `providers()`, and takes
// `&SessionParams` in `create_session`, so all three have real production consumers.
//
// Re-measured rather than assumed, in both cfgs: with every allow stripped,
// `cargo build --locked` and `cargo clippy --locked --all-targets` each report **0** `dead_code`
// warnings for this module. The attributes were removed because they had become the thing they
// were installed to prevent — a suppression that outlives its reason hides the next genuinely
// orphaned type, and this module now has no suppression at all.
//
// Two notes retained because they still govern anything added here later:
//
// 1. **Per-item, never a module-level `#![allow(dead_code)]`.** A module-wide allow would cover
//    every type added here in future, silently and permanently. If a new type genuinely needs a
//    suppression, it gets its own, and it names the unit that will consume it.
// 2. **`allow`, not `expect`.** `#[expect(dead_code)]` self-retires by warning once the item is
//    used, which is better in principle, but it does not survive `--all-targets` here. Measured
//    when all seven were present, by swapping them: `cargo clippy -- -D warnings` reported 1
//    unfulfilled expectation (`TerminalStatus`, already used by `Terminal::status`) and
//    `cargo clippy --all-targets -- -D warnings` reported 6, because the `cfg(test)` build
//    constructs every type except `Readiness`. `-D warnings` promotes
//    `unfulfilled_lint_expectations` to an error, so `expect` fails the gate outright.
//
// `lib.rs` + `main.rs` remains the structural fix that would make these types a genuine public
// API rather than in-crate items, and it is no longer needed for this reason — every type here
// now has a caller. It is still what `tests/endpoint_contract.rs` would need in order to import
// `Profile`, and that file's docs explain why it must keep its literals regardless. (#321)

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// An agent profile exactly as `GET /agents/profiles` projects it.
///
/// **Eight fields, and deliberately no ninth.** `AgentProfile.provider` exists in the
/// server's model (`models/agent_profile.py:38`), but the listing projection drops it:
/// `_discovery_fields()` (`utils/agent_profiles.py:60-89`) whitelists only
/// `description`/`capabilities`/`tags`/`role`, and the assembly sites
/// (`:171-173`, `:274`) add only `name`, `source`, `loadable`, and `duplicated_in`.
///
/// **The absence of `provider` is a prohibition, not an omission (BR-1).** A `Profile`
/// carrying an always-`None` `provider` would invite the per-profile N+1 fetch that ADR-02
/// declined — a caller who sees the field would reasonably fetch each profile individually
/// to populate it. Omitting the field makes that pattern *unavailable* rather than merely
/// discouraged. Provider choices come from `GET /agents/providers` and land in [`Provider`].
/// Do not add the field back. (#321)
///
/// Absent metadata arrives as `""` or `[]`, never `null`: `_discovery_fields` coerces a
/// non-string description and a non-list `tags`/`capabilities` to empty. The `Option` and
/// `default` tolerances below therefore cost nothing today and keep the front door alive if
/// the coercion ever changes. Detecting live-shape *drift* is `skeleton-endpoint-verify`'s
/// job (NFR-7), not this type's.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile {
    /// Profile identifier.
    pub name: String,
    /// Which agent directory the profile was discovered in.
    pub source: String,
    /// Whether `load_agent_profile()` would accept this entry.
    ///
    /// Load-bearing for FR-1.5: an unloadable profile is **rendered with a textual marker
    /// and made unselectable**, never filtered out (BR-8). The operator learns the profile
    /// exists and why it is unavailable. Filtering it would hide the diagnosis. (#321)
    pub loadable: bool,
    /// Display text. `Some("")` in practice — the server coerces a missing description.
    #[serde(default)]
    pub description: Option<String>,
    /// Declared capabilities.
    #[serde(default)]
    pub capabilities: Vec<String>,
    /// Classification tags.
    #[serde(default)]
    pub tags: Vec<String>,
    /// Declared role. `Some("")` in practice — the server coerces a missing role.
    #[serde(default)]
    pub role: Option<String>,
    /// Other enabled sources declaring the same profile name; the first scanned wins.
    #[serde(default)]
    pub duplicated_in: Vec<String>,
}

/// A provider as `GET /agents/providers` returns it (`api/main.py:1547-1551`).
///
/// `installed == false` is **not** grounds for hiding the provider (BR-9). FR-1.7 forbids
/// silently dropping a provider that is known elsewhere in the system, and the drift is
/// real — the endpoint's hardcoded binary map has nine entries against a ten-value
/// `ProviderType` enum. FR-1.7 guards the mechanism, so a future divergence is covered
/// too. `installed` is therefore display information, never a filter. (#321)
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Provider {
    /// Provider identifier, e.g. `kiro_cli`.
    pub name: String,
    /// Executable name looked up on `PATH`.
    pub binary: String,
    /// Whether the binary was found.
    pub installed: bool,
}

/// A created session's terminal, as `POST /sessions` returns it and readiness polling
/// re-reads it.
///
/// This is a four-field **projection** of the server's ten-field `Terminal`
/// (`models/terminal.py:24-44`); `serde` ignores the keys we do not name.
///
/// **The window name arrives under the key `name`. There is no `window_name` key on the
/// wire.** An earlier draft of the design invented one; review caught it. The distinction
/// is not cosmetic — a struct declaring `window_name` would compile, deserialise to
/// `None`/empty, and fail only at hand-off time with nothing upstream to catch it. The
/// server's ten fields are `id`, `name`, `provider`, `session_name`, `agent_profile`,
/// `caller_id`, `allowed_tools`, `shell_command`, `status`, `last_active` — `window_name`
/// appears nowhere among them. (#321)
///
/// Note also that the server's own hand-off path does not read the tmux window from this
/// projection at all: `api/main.py:3024` derives it from `metadata["tmux_window"]`. A
/// consumer building a navigate target uses `session_name` plus `name`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Terminal {
    /// Terminal identifier; the key readiness polling uses.
    pub id: String,
    /// The terminal/window name. The wire key is `name` — see the type docs. (#321)
    pub name: String,
    /// The owning session.
    pub session_name: String,
    /// The live status, when the server can report one.
    ///
    /// `Option` because the server declares the field optional and live-only
    /// (`models/terminal.py:41-42`). **An absent status is not an error** — it means "keep
    /// polling", exactly as [`TerminalStatus::Unknown`] does (BR-4). `default` so an
    /// omitted key is tolerated as well as an explicit `null`. (#321)
    #[serde(default)]
    pub status: Option<TerminalStatus>,
}

/// The server's six terminal states, mirrored verbatim from `models/terminal.py:13-21`.
///
/// All six are mirrored rather than collapsed at the wire boundary (BR-7): collapsing
/// during deserialisation would discard information before anyone decided what it means.
/// The collapse to [`Readiness`] happens in `skeleton-handoff-proof`'s `await_ready`.
///
/// **An unrecognised wire value deserialises to [`TerminalStatus::Unknown`] rather than
/// erroring (BR-6)** — a status added server-side must not crash the front door, and
/// `Unknown` degrades safely to "keep polling".
///
/// That tolerance is deliberate work, not a freebie: `serde`'s default for an enum is to
/// **fail** on an unrecognised value. `#[serde(other)]` is the usual catch-all, but it is
/// only accepted on internally- or adjacently-tagged enums — this enum arrives as a bare
/// JSON string, so the attribute does not apply. The equivalent used instead is
/// `#[serde(from = "String")]`, which routes deserialisation through the
/// [`From<String>`][TerminalStatus::from] impl below and its `_ => Unknown` arm.
///
/// One consequence worth naming: `from = "String"` means `rename_all` governs
/// *serialisation* while the `From` impl governs *deserialisation*, so the wire strings are
/// written twice. Test 4 round-trips all six variants in both directions, which is what
/// keeps the two halves honest. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", from = "String")]
pub enum TerminalStatus {
    /// The server cannot say yet. Maps to "keep polling".
    Unknown,
    /// The canonical ready state.
    Idle,
    /// Still working. Maps to "keep polling".
    Processing,
    /// Launched and finished.
    Completed,
    /// Launched fine and is blocked on a question for the operator.
    WaitingUserAnswer,
    /// The one genuine failure state.
    Error,
}

impl From<String> for TerminalStatus {
    /// Maps a wire value to a variant, and **anything unrecognised to
    /// [`TerminalStatus::Unknown`]** (BR-6).
    ///
    /// This is the catch-all `#[serde(other)]` cannot express for a bare-string enum; see
    /// the type docs. The `_` arm is the whole point of the impl — deleting it, or the
    /// `#[serde(from = "String")]` attribute that routes deserialisation here, makes an
    /// unknown status a hard deserialisation error and takes the front door down with it.
    /// (#321)
    fn from(wire: String) -> Self {
        match wire.as_str() {
            "unknown" => Self::Unknown,
            "idle" => Self::Idle,
            "processing" => Self::Processing,
            "completed" => Self::Completed,
            "waiting_user_answer" => Self::WaitingUserAnswer,
            "error" => Self::Error,
            _ => Self::Unknown,
        }
    }
}

/// The three-state readiness verdict (ADR-04).
///
/// **Deliberately not a `Result`** (BR-5). A two-state return forces [`Readiness::Unknown`]
/// into the error arm, which is exactly the conflation this type exists to prevent:
/// *unknown is not broken*. A readiness poll that times out earns a **warning** and the
/// flow continues to hand-off; a hard error is reserved for genuine breakage — a 5xx or
/// [`TerminalStatus::Error`]. (#321)
///
/// This unit only **declares** the type. The six-to-three mapping from [`TerminalStatus`]
/// lives in `skeleton-handoff-proof`'s `await_ready` and nowhere else (BR-7), so it is
/// deliberately absent here.
///
/// No `Serialize`/`Deserialize`: this is an internal verdict computed from a response, not
/// a wire shape. Deriving serde on it would imply the server speaks it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Readiness {
    /// The agent launched successfully; hand off.
    Ready,
    /// No verdict within the poll cap. **Not a failure.**
    Unknown,
    /// Genuine failure, carrying the status that produced it.
    Failed(TerminalStatus),
}

/// The `POST /sessions` launch request.
///
/// Every optional field is `Option<T>` rather than a `String` defaulting to `""`, and
/// serialisation skips `None` (BR-2). Together those two make FR-2.4 — a blank optional is
/// **omitted**, never sent as an empty string — a property of the type instead of something
/// each caller has to remember. A `None` that serialised as `null` or `""` would violate
/// FR-2.4 just as surely as an empty string. (#321)
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionParams {
    /// The agent profile to launch. **The only required field.**
    ///
    /// **Serialises as `agent_profile`, not `agents`.** The server's parameter is
    /// `agent_profile` (`api/main.py:1690`); the Rust field is named `agents` to match the
    /// operator-facing `--agents` flag. Both names are right on their own side and nothing
    /// connects them, so dropping the rename produces a **runtime 422 and never a compile
    /// error** — the single most expensive mistake available in this struct. (#321)
    #[serde(rename = "agent_profile")]
    pub agents: String,
    /// Provider override.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    /// Session name override.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_name: Option<String>,
    /// Working directory for the launched agent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub working_directory: Option<String>,
    /// Comma-separated allowed CAO tools, as the server's parameter expects.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allowed_tools: Option<String>,
    /// Operator-forwarded environment variables.
    ///
    /// **These travel in the JSON body and never in the query string.** Values may contain
    /// secrets and the query string lands in cao-server's HTTP access log — see issue
    /// **#248**, which is why `CreateSessionBody.env_vars` exists at all.
    ///
    /// `BTreeMap`, not `HashMap`, so serialisation order is deterministic (BR-11) and a
    /// test can assert an exact request body rather than parsing and re-comparing. (#321)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub env_vars: Option<BTreeMap<String, String>>,
    /// The launch message — `cao launch`'s trailing POSITIONAL argument.
    ///
    /// **This field exists because the form collected the message and threw it away.** The
    /// module docs of `guided_flow` asserted that `POST /sessions` "has no parameter for
    /// `message`" and that it was therefore carried for an argv-building hand-off path — and
    /// both halves were wrong. `CreateSessionBody.initial_message` has been on the endpoint all
    /// along (`api/main.py:215`, inherited from `CreateTerminalBody`), and no code anywhere
    /// builds a `cao launch` argv. So an operator who typed a first prompt watched it vanish.
    /// (Reported by review on PR #547.)
    ///
    /// **In the body, never the query string**, for the same reason as `env_vars` and stated in
    /// the server's own words at `api/main.py:206-212`: prompt content "can be large
    /// (URL-length 414 risk) and sensitive (query strings are routinely captured in HTTP access
    /// logs and traces)". A message in a query param would be the #248 defect again with a
    /// different payload.
    ///
    /// `Option`, and an empty message must arrive as `None` rather than `Some("")`: the server
    /// raises `ValueError("initial_message must not be empty")` on the empty string
    /// (`api/main.py:1949-1950`, and again in `session_service.py:69-70`). `GuidedFlow::set`
    /// already collapses blank text to `None` at the point of entry, so that holds by
    /// construction here rather than by a check at this boundary. (#321)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initial_message: Option<String>,
}

/// The `GET /health` projection (`api/main.py:824-827`).
///
/// A two-field projection of a larger response; `serde` ignores `service` and `components`.
/// `terminal_backend` is why the TUI needs no backend configuration of its own: ADR-01 keys
/// the hand-off strategy on the value the server reports rather than on a guess. (#321)
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Health {
    /// Server status, e.g. `"ok"`.
    pub status: String,
    /// `"tmux"` or `"herdr"`.
    pub terminal_backend: String,
}

#[cfg(test)]
mod tests {
    use super::{Health, Profile, Provider, Readiness, SessionParams, Terminal, TerminalStatus};
    use std::collections::{BTreeMap, BTreeSet};

    /// A fully-populated `Profile`. Every field is `Some`/non-empty on purpose: the key-set
    /// test below must observe the type's shape, not a fixture's sparseness.
    fn sample_profile() -> Profile {
        Profile {
            name: "planner".to_string(),
            source: "~/.claude/agents".to_string(),
            loadable: true,
            description: Some("Plans work".to_string()),
            capabilities: vec!["planning".to_string()],
            tags: vec!["core".to_string()],
            role: Some("planner".to_string()),
            duplicated_in: vec!["builtin".to_string()],
        }
    }

    fn json_keys(value: &serde_json::Value) -> BTreeSet<String> {
        value
            .as_object()
            .expect("a struct must serialise to a JSON object")
            .keys()
            .cloned()
            .collect()
    }

    /// Test 1 — `Profile` has exactly the eight projected keys.
    ///
    /// The expected names are **hard-coded literals** (VR-2). Deriving them from `Profile`
    /// — or from a constant `Profile` also used — would make the test agree with whatever
    /// the type happens to say, so it would stay green through the exact change it exists
    /// to catch. These eight literals were read off `utils/agent_profiles.py` (`:85-88`,
    /// `:171-173`, `:274`), not off the struct.
    ///
    /// Set equality catches a field lost *and* a field gained, `provider` included. Test 2
    /// then names `provider` explicitly, because a returning `provider` deserves a failure
    /// message that says so. Proven by mutation: deleting `duplicated_in` turns this red.
    /// (#321)
    #[test]
    fn profile_has_exactly_the_eight_projected_keys() {
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
        .map(|s| s.to_string())
        .collect();
        assert_eq!(
            expected.len(),
            8,
            "the literal expectation itself must list 8 names"
        );

        let actual =
            json_keys(&serde_json::to_value(sample_profile()).expect("Profile must serialise"));

        assert_eq!(
            actual, expected,
            "Profile's serialised key set must match the endpoint's projection exactly; \
             a gained key (especially `provider`) or a lost key is a defect (INV-1)"
        );
    }

    /// Test 2 — `provider` is absent from `Profile`.
    ///
    /// Its own explicit negative assertion (BR-1). `serde` ignores unknown keys when
    /// deserialising, so a `provider` field reintroduced on the Rust side would never fail
    /// a parse test — the return has to be asserted against directly. (#321)
    #[test]
    fn profile_has_no_provider_field() {
        let keys =
            json_keys(&serde_json::to_value(sample_profile()).expect("Profile must serialise"));

        assert!(
            !keys.contains("provider"),
            "Profile must NOT carry `provider` (BR-1): the endpoint's projection drops it, \
             and an always-None field invites the per-profile N+1 fetch ADR-02 declined. \
             Provider choices come from GET /agents/providers into `Provider`. Found: {keys:?}"
        );
    }

    /// Test 3 — an unrecognised wire value deserialises to `Unknown` instead of erroring.
    ///
    /// This is the tolerance BR-6 requires, and `serde`'s default is the opposite: an
    /// unrecognised enum value is an error. Proven by mutation: removing
    /// `#[serde(from = "String")]` — which routes deserialisation through the `From` impl's
    /// `_ => Unknown` arm — turns this red with an "unknown variant" error. (#321)
    #[test]
    fn unknown_terminal_status_wire_value_deserialises_to_unknown() {
        let parsed: Result<TerminalStatus, _> = serde_json::from_str("\"warp_drive_engaged\"");

        let status = parsed.expect(
            "an unrecognised TerminalStatus must deserialise, not error (BR-6): a status \
             added server-side must not crash the front door",
        );
        assert_eq!(
            status,
            TerminalStatus::Unknown,
            "an unrecognised status must land on Unknown, which means `keep polling`"
        );
    }

    /// Test 4 — all six known statuses round-trip through their exact wire strings.
    ///
    /// The pairs are hard-coded from `models/terminal.py:13-21`. This is also what keeps
    /// the two halves of the serialisation honest: `rename_all` drives serialisation while
    /// the `From<String>` impl drives deserialisation, so a typo in either direction — or a
    /// variant that falls through to `Unknown` because its arm was dropped — turns this
    /// red. (#321)
    #[test]
    fn all_six_terminal_statuses_round_trip() {
        let cases = [
            (TerminalStatus::Unknown, "unknown"),
            (TerminalStatus::Idle, "idle"),
            (TerminalStatus::Processing, "processing"),
            (TerminalStatus::Completed, "completed"),
            (TerminalStatus::WaitingUserAnswer, "waiting_user_answer"),
            (TerminalStatus::Error, "error"),
        ];
        assert_eq!(
            cases.len(),
            6,
            "all six server states must be covered (INV-2)"
        );

        for (variant, wire) in cases {
            let serialised =
                serde_json::to_string(&variant).expect("TerminalStatus must serialise");
            assert_eq!(
                serialised,
                format!("\"{wire}\""),
                "{variant:?} must serialise to the exact wire value {wire:?}"
            );

            let parsed: TerminalStatus =
                serde_json::from_str(&serialised).expect("a known wire value must deserialise");
            assert_eq!(
                parsed, variant,
                "{wire:?} must deserialise back to {variant:?}, not fall through to Unknown"
            );
        }
    }

    /// Test 5 — a `None` optional is omitted entirely, not emitted as `null` or `""`.
    ///
    /// FR-2.4/BR-2. The assertion is on *absence of the key*, because `null` and `""` both
    /// reach the server as a supplied-but-empty value and are exactly what this must
    /// prevent. Proven by mutation: dropping a `skip_serializing_if` turns this red. (#321)
    #[test]
    fn none_optionals_are_omitted_from_serialised_output() {
        let params = SessionParams {
            agents: "planner".to_string(),
            provider: None,
            session_name: None,
            working_directory: None,
            allowed_tools: None,
            env_vars: None,
            initial_message: None,
        };

        let value = serde_json::to_value(&params).expect("SessionParams must serialise");
        let keys = json_keys(&value);

        for absent in [
            "provider",
            "session_name",
            "working_directory",
            "allowed_tools",
            "env_vars",
            // `initial_message` matters more than the others here: the server raises
            // `ValueError("initial_message must not be empty")` on an empty string
            // (`api/main.py:1949-1950`), so emitting `""` for an unfilled message would turn a
            // launch with no prompt into a 400. Omission is the only correct encoding.
            "initial_message",
        ] {
            assert!(
                !keys.contains(absent),
                "a None optional must be omitted, not sent as null or \"\": found {absent:?} \
                 in {keys:?}"
            );
        }
        assert_eq!(
            serde_json::to_string(&params).expect("SessionParams must serialise"),
            r#"{"agent_profile":"planner"}"#,
            "only the one required field may appear when every optional is None"
        );
    }

    /// Test 6 — `agents` serialises as `agent_profile`.
    ///
    /// The runtime-422 trap: the rename is invisible to the compiler, so only an assertion
    /// on the emitted key can hold it. Both halves matter — `agent_profile` present *and*
    /// `agents` absent — because a partial fix would otherwise pass. Proven by mutation:
    /// removing `#[serde(rename = "agent_profile")]` turns this red. (#321)
    #[test]
    fn agents_serialises_as_agent_profile() {
        let params = SessionParams {
            agents: "planner".to_string(),
            provider: Some("kiro_cli".to_string()),
            session_name: None,
            working_directory: None,
            allowed_tools: None,
            env_vars: None,
            initial_message: None,
        };

        let value = serde_json::to_value(&params).expect("SessionParams must serialise");
        let keys = json_keys(&value);

        assert!(
            keys.contains("agent_profile"),
            "`agents` must serialise as the server's `agent_profile` parameter \
             (api/main.py:1690); anything else is a 422 at runtime. Found: {keys:?}"
        );
        assert!(
            !keys.contains("agents"),
            "the Rust field name `agents` must not reach the wire: the server has no \
             `agents` parameter. Found: {keys:?}"
        );
        assert_eq!(
            value.get("agent_profile").and_then(|v| v.as_str()),
            Some("planner"),
            "the renamed key must carry the profile value, not an empty placeholder"
        );
    }

    /// Test 7 — `env_vars` serialisation order is deterministic.
    ///
    /// Two maps built with the keys inserted in opposite orders must serialise identically
    /// (BR-11). The equality assertion is the requirement as stated; the **exact-literal**
    /// assertion is what gives the test teeth, since swapping `BTreeMap` for `HashMap`
    /// would compile and keep the two constructions equal to each other while making the
    /// emitted order differ from sorted. Six keys are used so a `HashMap`'s randomised
    /// order effectively cannot coincide with sorted order. (#321)
    #[test]
    fn env_vars_serialisation_order_is_deterministic() {
        let names = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT"];

        let mut ascending = BTreeMap::new();
        for (index, name) in names.iter().enumerate() {
            ascending.insert((*name).to_string(), index.to_string());
        }
        let mut descending = BTreeMap::new();
        for (index, name) in names.iter().enumerate().rev() {
            descending.insert((*name).to_string(), index.to_string());
        }

        let params = |env_vars: BTreeMap<String, String>| SessionParams {
            agents: "planner".to_string(),
            provider: None,
            session_name: None,
            working_directory: None,
            allowed_tools: None,
            env_vars: Some(env_vars),
            initial_message: None,
        };

        let first = serde_json::to_string(&params(ascending)).expect("must serialise");
        let second = serde_json::to_string(&params(descending)).expect("must serialise");

        assert_eq!(
            first, second,
            "insertion order must not affect the serialised body (BR-11)"
        );
        assert_eq!(
            first,
            concat!(
                r#"{"agent_profile":"planner","env_vars":{"#,
                r#""ALPHA":"0","BRAVO":"1","CHARLIE":"2","DELTA":"3","ECHO":"4","FOXTROT":"5"}}"#
            ),
            "env_vars must serialise in sorted key order so a test can assert an exact body"
        );
    }

    /// Deserialisation cover for the remaining wire types, so the projections are exercised
    /// against realistic payloads rather than only constructed in Rust.
    ///
    /// Not one of the seven required tests: it guards that the projections tolerate the
    /// server's *extra* keys (`serde` ignores them) and that `Terminal` reads the window
    /// name from `name`. `window_name` is fed here alongside `name` precisely because a
    /// struct declaring `window_name` would compile and silently produce an empty window.
    #[test]
    fn projections_deserialise_from_realistic_payloads() {
        let terminal: Terminal = serde_json::from_str(
            r#"{"id":"a1b2c3d4","name":"planner-1","provider":"kiro_cli",
                "session_name":"work","agent_profile":"planner","caller_id":null,
                "window_name":"IGNORED","status":"idle","last_active":null}"#,
        )
        .expect("Terminal must deserialise from the server's ten-field model");
        assert_eq!(
            terminal.name, "planner-1",
            "the window name comes from `name`"
        );
        assert_eq!(terminal.status, Some(TerminalStatus::Idle));

        let no_status: Terminal =
            serde_json::from_str(r#"{"id":"a1b2c3d4","name":"n","session_name":"work"}"#)
                .expect("an absent status is not an error (BR-4)");
        assert_eq!(no_status.status, None, "absent status means `keep polling`");

        let provider: Provider =
            serde_json::from_str(r#"{"name":"kiro_cli","binary":"kiro-cli","installed":false}"#)
                .expect("Provider must deserialise");
        assert!(
            !provider.installed,
            "`installed` is display data, never a filter (BR-9)"
        );

        let health: Health = serde_json::from_str(
            r#"{"status":"ok","service":"cli-agent-orchestrator","terminal_backend":"tmux",
                "components":{"cao":"ok"}}"#,
        )
        .expect("Health must deserialise, ignoring the keys it does not project");
        assert_eq!(health.terminal_backend, "tmux");

        // `Readiness` is declared here and produced by `skeleton-handoff-proof`; this only
        // pins that `Failed` carries the status, so `Unknown` can never be mistaken for a
        // failure (BR-5).
        assert_ne!(
            Readiness::Unknown,
            Readiness::Failed(TerminalStatus::Error),
            "Unknown is not a failure (ADR-04)"
        );
    }
}
