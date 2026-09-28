//! Guard 1 of the `safety-guards` unit: the operator-supplied env-var mirror (issue #321).
//!
//! This is a **re-earned** control, not new work. `cao launch --env` already applies it on the
//! Python side (`clients/tmux.py`), and the predecessor TUI lost it. S-5 exists so it is not
//! lost again.
//!
//! # The purpose is the WARNING, not the drop
//!
//! `code-quality-assessment.md` § Security Posture and devsecops finding 8 both state the
//! requirement the same way: **so silently-dropped vars keep failing loudly.** A variable that
//! vanishes without a message is the original defect — the operator forwards
//! `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, nothing happens, and there is no way to find out why. So
//! [`EnvDecision::warning`] names every dropped variable, and [`EnvDecision`] models the reason as
//! a **value** rather than returning `bool`, which is what stops the message from being quietly
//! dropped in a later refactor (BR-5, SR-4).
//!
//! # What this module deliberately no longer contains
//!
//! It used to also export `merge` (a `BTreeMap` merge mirroring `_merge_extra_env`) and
//! `WriterWarnings`/`WarnSink` (a stderr sink). **All three were unreachable and their doc comments
//! claimed production callers that could never exist**: `merge` mirrors the stage that assembles a
//! tmux process environment, and this TUI never does that — it sends `env_vars` in the
//! `POST /sessions` body and cao-server performs that merge itself. Built-and-never-invoked is the
//! exact defect class (FR-3.2) this crate's own documentation lectures about, so they were deleted
//! rather than kept behind an `#[allow(dead_code)]` promising a caller.
//!
//! What survives is what the front door actually uses: [`decide`], [`is_blocked`], the policy
//! constants, and [`EnvDecision::warning`] — now called by `guided_flow::parse_env_pairs`. The
//! policy tests were kept and repointed at those, since the byte-cap boundary and the
//! allowlist-before-prefix ordering are properties of `decide`, not of the deleted plumbing.
//! (Reported by review on PR #547.)
//!
//! # Three details a re-implementation working from prose gets wrong
//!
//! Every constant and both behaviours below were read from `clients/tmux.py` directly, at
//! implementation time, not carried from a design summary:
//!
//! 1. **The allowlist is tested BEFORE the prefix loop** (`tmux.py:101-103`).
//!    `CLAUDE_CODE_USE_BEDROCK` starts with `CLAUDE`, so a prefix-first implementation drops it
//!    and **breaks Bedrock authentication**. See [`is_blocked`].
//! 2. **The cap comparison is `>=`, not `>`** (`tmux.py:121`), so a value of **exactly 2048
//!    bytes is DROPPED**. `>` differs from Python at exactly one input, which no test using
//!    100-byte and 5000-byte values can see. See [`decide`].
//! 3. **The cap counts BYTES, not characters** — Python's `len(value.encode("utf-8"))`. A
//!    multi-byte value sails through a `chars().count()` check while failing the byte check.
//!
//! # Which Python path this mirrors, because there are two and they disagree on the operator
//!
//! **`_merge_extra_env` (`tmux.py:105-128`) is authoritative here** — the operator-supplied
//! `--env` path. The inherited-env comprehension (`tmux.py:157-169`) applies the same policy but
//! writes the cap as `len(...) < _MAX_ENV_VALUE_BYTES` (`:166`) where `_merge_extra_env` writes
//! `>=` (`:121`). Those two boundaries are **logically identical** — both exclude exactly 2048 —
//! so there is no behavioural difference to reproduce and detail 2 above is correct either way.
//! Recorded because anyone diffing this file against the Python meets both operators and needs
//! to know which one this unit is a mirror of. The inherited slice additionally requires a
//! `CAO_`/`KIRO_`/`MISE_`/`AWS_` prefix, which is **not** in S-5's scope (BR-6).
//!
//! # Why compile-time constants rather than configuration
//!
//! These are a **security policy**, and a policy an operator can override at run time is not a
//! guard. Deny-by-default (T-10) means the blocked list cannot be widened and the allowlist
//! cannot be extended without a reviewable source change.
//!
//! # Scope
//!
//! This module decides and warns. It **never errors** (INV-1): a blocked or oversized variable
//! is dropped with a warning and the launch proceeds, because failing the launch would be a
//! behaviour change from the Python path. Where the `--env` pairs come from is `guided-flow`'s
//! problem (Bolt 4); building the argv **vector** they feed — never an interpolated shell
//! string (T-10, BR-7) — is the caller's, and `Host::run` in `handoff.rs` already makes a shell
//! string inexpressible at the type level.

/// Provider env prefixes that cause "nested session" errors when CAO runs inside a provider.
///
/// Verbatim from `tmux.py:83`. Note `CLAUDE` has **no** trailing underscore, so a key that is
/// exactly `CLAUDE` is blocked too — `starts_with` matches the whole string. (#321)
pub const BLOCKED_PREFIXES: [&str; 3] = ["CLAUDE", "CODEX_", "__MISE_"];

/// The six keys that survive a blocked prefix, because provider **authentication** needs them.
///
/// Verbatim from `tmux.py:84-93`. Membership is **exact-match, not prefix-match** (INV-2):
/// `CLAUDE_CODE_USE_SOMETHING_ELSE` is not one of these six and is dropped. (#321)
pub const BLOCKED_PREFIX_ALLOWLIST: [&str; 6] = [
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
];

/// Per-variable value cap in **bytes**, from `tmux.py:96`.
///
/// The cap exists to keep the full `tmux new-session -e` / `new-window -e` argv under the kernel
/// argv limit on busy hosts — attributed to **PR #246** at `tmux.py:94-95`. (#321)
pub const MAX_ENV_VALUE_BYTES: usize = 2048;

/// What the mirror decided about one variable.
///
/// Modelled as an enum carrying the key — rather than a `bool` filter — specifically so the
/// warning **cannot be silently omitted**. A `bool` is how the original defect happened: the
/// caller learns that something was dropped but not what, so there is nothing to put in a
/// message and the message gets left out. Making the reason a value forces it into the log line
/// (BR-5, SR-4).
///
/// `byte_len` rides on [`EnvDecision::DropOversized`] so the warning can state the **actual**
/// size rather than only the limit; "your value was too big" is much less useful than "your
/// value was 4096 bytes". (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnvDecision {
    /// Passed both checks; the variable reaches the child environment.
    Keep,
    /// Matched a blocked prefix and is not one of the six allowlisted keys.
    DropBlocked {
        /// The rejected key. Present because the warning must name it (BR-5).
        key: String,
    },
    /// The value is at or above [`MAX_ENV_VALUE_BYTES`].
    DropOversized {
        /// The rejected key. Present because the warning must name it (BR-5).
        key: String,
        /// The value's actual size in bytes, so the warning can quote it.
        byte_len: usize,
    },
}

impl EnvDecision {
    /// The operator-facing warning for this decision, or `None` for a keep.
    ///
    /// Deriving the text from the decision rather than writing it at each `continue` site is
    /// what makes the two impossible to get out of step: a new drop variant has no warning
    /// until someone adds one here, and the compiler's exhaustiveness check asks for it.
    ///
    /// The oversized wording deliberately does **not** copy Python's "value exceeds 2048 bytes"
    /// (`tmux.py:123`). That phrasing is literally false for a value of exactly 2048 bytes,
    /// which this guard drops — and an operator-facing message that misstates the boundary is
    /// how someone comes to "correct" the `>=` into a `>`. (#321)
    pub fn warning(&self) -> Option<String> {
        match self {
            EnvDecision::Keep => None,
            EnvDecision::DropBlocked { key } => Some(format!(
                "Dropping forwarded env var with blocked prefix: {key}"
            )),
            EnvDecision::DropOversized { key, byte_len } => Some(format!(
                "Dropping forwarded env var {key} — value is {byte_len} bytes, \
                 at or above the {MAX_ENV_VALUE_BYTES}-byte cap"
            )),
        }
    }
}

/// Is `key` blocked?
///
/// # THE ORDERING BELOW IS A SECURITY BEHAVIOUR, NOT A STYLE CHOICE (#321)
///
/// The allowlist **must** be tested before the prefix loop, exactly as `tmux.py:101-103` does.
/// Every one of the six allowlisted keys begins with `CLAUDE`, so a prefix-first implementation
/// drops all six — and dropping `CLAUDE_CODE_USE_BEDROCK` **breaks Bedrock authentication** for
/// every session the TUI launches. The failure is silent from the guard's point of view: it
/// logs a tidy warning and moves on, while the provider then fails to authenticate somewhere
/// else entirely.
///
/// **Do not "simplify" this into a single `any(...)` expression, and do not reorder it.** The
/// two statements look redundant and are not. Proven by mutation: moving the allowlist check
/// after the prefix loop turns
/// `tests::all_six_allowlisted_keys_survive_the_claude_prefix` red (BR-2, SR-2).
pub fn is_blocked(key: &str) -> bool {
    if BLOCKED_PREFIX_ALLOWLIST.contains(&key) {
        return false;
    }
    BLOCKED_PREFIXES
        .iter()
        .any(|prefix| key.starts_with(prefix))
}

/// Decide one variable, without touching any environment.
///
/// A standalone function rather than logic inlined at the call site, so the boundary is directly
/// addressable by a test and by a mutation: the two lines that matter most in this file are the
/// `is_blocked` call and the `>=` below, and both are easier to trust when nothing else is
/// happening around them. (It was originally split out of a `merge` that has since been deleted
/// for having no production caller; the reason for the split outlived it.)
///
/// # The comparison is `>=` and the length is in BYTES
///
/// `>=` mirrors `tmux.py:121`, so **exactly [`MAX_ENV_VALUE_BYTES`] is DROPPED**. `>` would
/// differ from the Python at exactly one input value (BR-3, SR-3).
///
/// `value.as_bytes().len()` is spelt out rather than `value.len()` — the two are identical in
/// Rust, and writing it longhand is the point: it says BYTES at the site of the decision so
/// nobody reaches for `chars().count()` to "handle unicode properly". Python counts
/// `len(value.encode("utf-8"))`; 700 three-byte characters are 2100 bytes and 700 chars, so a
/// char count keeps a value the Python path drops (BR-4). Both mutations are logged.
///
/// Clippy's `needless_as_bytes` asks for `value.len()` here and the suppression below is
/// deliberate, per the affirmed escape hatch (`#[allow]` plus an issue number). The lint is
/// correct that the calls are equivalent; it is the *legibility* that is load-bearing. `len()` on
/// a `&str` is a byte count that reads like a length, and this is precisely the line where
/// somebody "fixing unicode handling" would reach for `chars().count()` and silently start
/// keeping values the Python path drops. Saying BYTES at the decision site is cheaper than the
/// mutation that catches it. (#321)
pub fn decide(key: &str, value: &str) -> EnvDecision {
    if is_blocked(key) {
        return EnvDecision::DropBlocked {
            key: key.to_string(),
        };
    }

    #[allow(clippy::needless_as_bytes)] // BYTES is the behaviour, not the spelling. (#321)
    let byte_len = value.as_bytes().len();
    if byte_len >= MAX_ENV_VALUE_BYTES {
        return EnvDecision::DropOversized {
            key: key.to_string(),
            byte_len,
        };
    }

    EnvDecision::Keep
}

#[cfg(test)]
mod tests {
    use super::{
        decide, is_blocked, EnvDecision, BLOCKED_PREFIXES, BLOCKED_PREFIX_ALLOWLIST,
        MAX_ENV_VALUE_BYTES,
    };
    use std::collections::BTreeMap;

    // ## Every expectation below is a LITERAL, re-read from `clients/tmux.py` (VR-4)
    //
    // Not one of them is sourced from the module under test. A test that wrote
    // `MAX_ENV_VALUE_BYTES` where `2048` belongs would keep passing after someone changed the
    // constant to 4096 — it would assert only that the guard agrees with itself. That vacuous
    // shape is the dominant failure mode on this project, so the duplication is the feature and
    // `the_policy_constants_match_the_python_source_exactly` is what keeps the copies honest.
    // (#321)

    /// `tmux.py:83`, verbatim.
    const TMUX_PY_BLOCKED_PREFIXES: [&str; 3] = ["CLAUDE", "CODEX_", "__MISE_"];

    /// `tmux.py:84-93`, verbatim, all six.
    const TMUX_PY_ALLOWLIST: [&str; 6] = [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    ];

    /// `tmux.py:96`, verbatim.
    const TMUX_PY_CAP_BYTES: usize = 2048;

    /// Applies the guard to `extra_env` exactly as a caller must: decide, warn on every drop, keep
    /// only what survives. Returns the resulting map, the per-pair decisions, and the warnings.
    ///
    /// # This used to call a production `merge`, which had no production caller
    ///
    /// `env_guard::merge` and `WriterWarnings` were written for a `guided-flow` caller that the
    /// `#[allow(dead_code)]` notes promised — and it never arrived, because it never could:
    /// `merge` mirrors `clients/tmux.py::_merge_extra_env`, which is the stage that assembles a
    /// **tmux process environment**, and the TUI does not do that. It sends `env_vars` in the
    /// `POST /sessions` body and cao-server performs that merge itself. So the code was
    /// unreachable by design while its doc comments claimed production callers — the FR-3.2 defect
    /// class this crate's own documentation lectures about, in the crate's own source.
    ///
    /// The functions were deleted. **The tests were not**: they assert the byte cap boundary, the
    /// allowlist-before-prefix ordering, and that every drop emits a warning — all real properties
    /// of `decide`/`warning`, which `guided_flow::parse_env_pairs` genuinely calls. Deleting the
    /// tests along with the dead code would have thrown away the coverage that matters, so this
    /// helper does the loop locally instead, over the same two functions the front door uses.
    /// (Reported by review on PR #547.)
    fn apply_guard(extra_env: &[(String, String)]) -> (BTreeMap<String, String>, Vec<String>) {
        let mut environment = BTreeMap::new();
        let warnings = apply_guard_into(&mut environment, extra_env);
        (environment, warnings)
    }

    /// The same loop, into an environment that already holds inherited values.
    ///
    /// Separate from [`apply_guard`] because the override property — an explicit
    /// `--env AWS_REGION=us-west-2` beating an inherited `eu-west-1` (issue #248,
    /// `tmux.py:170-174`) — is only observable when the map is non-empty to begin with. Folding
    /// the two would have quietly dropped that assertion when the dead `merge` was deleted.
    fn apply_guard_into(
        environment: &mut BTreeMap<String, String>,
        extra_env: &[(String, String)],
    ) -> Vec<String> {
        let mut warnings: Vec<String> = Vec::new();

        for (key, value) in extra_env {
            let decision = decide(key, value);
            // The warning is the control (BR-5, SR-4), so it is emitted here for the same reason
            // the deleted `merge` emitted it: a test asserting only that the key is absent stays
            // green while the operator is never told.
            if let Some(message) = decision.warning() {
                warnings.push(message);
            }
            if matches!(decision, EnvDecision::Keep) {
                environment.insert(key.clone(), value.clone());
            }
        }

        warnings
    }

    /// One pair through the guard: the environment, its single decision, and the warnings — so no
    /// test can assert on the drop while forgetting the warning.
    fn merge_one(key: &str, value: &str) -> (BTreeMap<String, String>, EnvDecision, Vec<String>) {
        let pairs = vec![(key.to_string(), value.to_string())];
        let (environment, warnings) = apply_guard(&pairs);
        (environment, decide(key, value), warnings)
    }

    /// Test 1 — the byte cap boundary: 2047 keeps, **2048 drops**, 2049 drops (VR-1, BR-3).
    ///
    /// The 2048 row is the whole reason this test exists. It is the only input at which `>=`
    /// and `>` disagree, so a suite testing 100 bytes and 5000 bytes proves the cap exists
    /// while proving nothing about where it is. Mutation-logged.
    #[test]
    #[allow(clippy::needless_as_bytes)] // the fixture's assertion is about BYTES. (#321)
    fn the_byte_cap_boundary_is_2047_keep_2048_drop_2049_drop() {
        // Fixture sizes asserted against literals rather than trusted: if `repeat` were
        // mis-called the test would otherwise still pass, having measured the wrong boundary.
        for (size, expect_keep) in [(2047_usize, true), (2048, false), (2049, false)] {
            let value = "a".repeat(size);
            assert_eq!(
                value.as_bytes().len(),
                size,
                "fixture must be exactly {size} bytes for this boundary to mean anything"
            );

            let (environment, decision, warnings) = merge_one("AWS_REGION", &value);

            if expect_keep {
                assert_eq!(
                    decision,
                    EnvDecision::Keep,
                    "{size} bytes is BELOW the 2048-byte cap and must be kept"
                );
                assert_eq!(environment.get("AWS_REGION"), Some(&value));
                assert!(warnings.is_empty(), "a kept var must not warn");
            } else {
                assert_eq!(
                    decision,
                    EnvDecision::DropOversized {
                        key: "AWS_REGION".to_string(),
                        byte_len: size,
                    },
                    "{size} bytes is AT OR ABOVE the 2048-byte cap and must be dropped — the \
                     comparison is `>=` (tmux.py:121), so exactly 2048 is dropped, not kept"
                );
                assert!(
                    !environment.contains_key("AWS_REGION"),
                    "a dropped var must not reach the environment"
                );
                assert_eq!(warnings.len(), 1, "every drop must warn exactly once");
            }
        }
    }

    /// Test 2 — all six allowlisted keys **survive** the `CLAUDE` prefix (VR-2, BR-2, SR-2).
    ///
    /// This is the allowlist-ordering proof, and it is the test the whole ordering comment in
    /// [`is_blocked`] points at. A suite that only checked "blocked vars are dropped" would
    /// pass with the two statements swapped, while Bedrock, Vertex and Foundry authentication
    /// were all silently broken. Mutation-logged.
    #[test]
    fn all_six_allowlisted_keys_survive_the_claude_prefix() {
        assert_eq!(
            TMUX_PY_ALLOWLIST.len(),
            6,
            "the allowlist is a SIX-entry set (tmux.py:84-93); a five-entry expectation would \
             let one key silently lose its exemption"
        );

        for key in TMUX_PY_ALLOWLIST {
            assert!(
                key.starts_with("CLAUDE"),
                "{key} must start with a BLOCKED prefix, otherwise this test proves nothing \
                 about ordering — the whole point is that the allowlist beats the prefix"
            );

            assert!(
                !is_blocked(key),
                "{key} must NOT be blocked: it is allowlisted, and the allowlist is checked \
                 BEFORE the prefix loop (tmux.py:101-103). Blocking it breaks provider \
                 authentication"
            );

            let (environment, decision, warnings) = merge_one(key, "1");
            assert_eq!(decision, EnvDecision::Keep, "{key} must be kept");
            assert_eq!(environment.get(key), Some(&"1".to_string()));
            assert!(warnings.is_empty(), "{key} is kept, so nothing may warn");
        }
    }

    /// Test 3 — allowlist membership is **exact-match**, not prefix-match (INV-2).
    ///
    /// `CLAUDE_CODE_USE_SOMETHING_ELSE` shares a long prefix with a real allowlist entry and
    /// must still be dropped; a `starts_with` allowlist would wave through every
    /// `CLAUDE_CODE_USE_*` variable in existence. The bare `CLAUDE` row is here because
    /// `BLOCKED_PREFIXES` has no trailing underscore, so `starts_with` matches the whole string.
    #[test]
    fn the_allowlist_is_exact_match_not_prefix_match() {
        for key in [
            "CLAUDE_CODE_USE_SOMETHING_ELSE",
            "CLAUDE_CODE_USE_BEDROCK_EXTRA",
            "CLAUDE_CODE_SKIP_BEDROCK",
            "CLAUDE",
        ] {
            assert!(
                is_blocked(key),
                "{key} is not one of the six allowlisted keys and must be dropped — allowlist \
                 membership is exact-match, not prefix-match (INV-2)"
            );

            let (environment, decision, warnings) = merge_one(key, "1");
            assert_eq!(
                decision,
                EnvDecision::DropBlocked {
                    key: key.to_string()
                }
            );
            assert!(
                environment.is_empty(),
                "{key} must not reach the environment"
            );
            assert_eq!(warnings.len(), 1, "{key} was dropped, so it must warn");
        }
    }

    /// Test 4 — all **three** blocked prefixes drop (BR-1).
    ///
    /// Each prefix is exercised through a realistic key rather than the bare prefix, because a
    /// deleted prefix is the mutation this catches and it is the `*_ANYTHING` form that a real
    /// `--env` would carry. Mutation-logged (`__MISE_`).
    #[test]
    fn all_three_blocked_prefixes_drop() {
        assert_eq!(
            TMUX_PY_BLOCKED_PREFIXES.len(),
            3,
            "there are exactly THREE blocked prefixes (tmux.py:83)"
        );

        for (key, prefix) in [
            ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE"),
            ("CODEX_HOME", "CODEX_"),
            ("__MISE_DIFF", "__MISE_"),
        ] {
            assert!(
                TMUX_PY_BLOCKED_PREFIXES.contains(&prefix),
                "{prefix} must be one of the three literals read from tmux.py:83"
            );
            assert!(
                key.starts_with(prefix),
                "{key} must exercise the {prefix} prefix"
            );

            let (environment, decision, warnings) = merge_one(key, "1");
            assert_eq!(
                decision,
                EnvDecision::DropBlocked {
                    key: key.to_string()
                },
                "{key} carries the blocked prefix {prefix} and must be dropped: forwarding it \
                 reintroduces the nested-session failure the prefix list exists to prevent \
                 (tmux.py:78-82)"
            );
            assert!(environment.is_empty());
            assert_eq!(warnings.len(), 1);
        }
    }

    /// Test 5 — an oversized value is judged by **bytes**, never by characters (VR-7, BR-4).
    ///
    /// Both fixtures are genuinely multi-byte and both would be **kept** by a
    /// `chars().count()` check while the Python path drops them. The second lands on the exact
    /// 2048-byte boundary as well, so it holds under both this mutation and the `>=` one.
    /// Mutation-logged.
    #[test]
    #[allow(clippy::needless_as_bytes)] // BYTES versus chars is the property under test. (#321)
    fn an_oversized_value_is_judged_by_bytes_not_characters() {
        // 700 x U+304C, three bytes each: 2100 bytes but only 700 characters.
        let three_byte = "が".repeat(700);
        // 1024 x U+00E9, two bytes each: exactly 2048 bytes but only 1024 characters.
        let two_byte = "é".repeat(1024);

        for (value, bytes, chars) in [
            (&three_byte, 2100_usize, 700_usize),
            (&two_byte, 2048, 1024),
        ] {
            // Both counts asserted against literals. Without this the fixture could quietly
            // become single-byte and the test would keep passing, proving nothing about
            // encoding — the guard-must-not-trust-its-own-fixture failure mode.
            assert_eq!(
                value.as_bytes().len(),
                bytes,
                "fixture must be exactly {bytes} BYTES"
            );
            assert_eq!(
                value.chars().count(),
                chars,
                "fixture must be exactly {chars} CHARACTERS, fewer than its byte count, or the \
                 byte-versus-char distinction is untested"
            );
            assert!(
                chars < TMUX_PY_CAP_BYTES,
                "the character count must fall BELOW 2048 — that is what makes a chars() \
                 implementation keep this value while the byte check drops it"
            );

            let (environment, decision, warnings) = merge_one("AWS_PROFILE", value);

            assert_eq!(
                decision,
                EnvDecision::DropOversized {
                    key: "AWS_PROFILE".to_string(),
                    byte_len: bytes,
                },
                "a {bytes}-byte / {chars}-character value must be dropped on its BYTE length; \
                 Python counts len(value.encode(\"utf-8\")) (tmux.py:121)"
            );
            assert!(environment.is_empty());
            assert_eq!(warnings.len(), 1);
        }
    }

    /// Test 6 — **every drop emits a warning naming the variable** (VR-3, BR-5, BR-14, SR-4).
    ///
    /// This is the guard's actual purpose and the assertion the rest of the suite cannot stand
    /// in for: asserting that a key is absent from the environment passes perfectly well when
    /// the operator is never told, which is the original defect. So this test asserts on the
    /// **warning text**, for both drop reasons, and checks the variable's name is in it.
    ///
    /// It is also the test BR-14's mandatory mutation targets: removing the `warn` call while
    /// leaving the drop in place turns this red and nothing else.
    #[test]
    fn every_drop_emits_a_warning_naming_the_variable() {
        let oversized = "z".repeat(4096);
        let pairs = vec![
            ("AWS_REGION".to_string(), "us-east-1".to_string()),
            ("CODEX_HOME".to_string(), "/tmp/codex".to_string()),
            ("HUGE_PAYLOAD".to_string(), oversized.clone()),
            ("CLAUDE_CODE_USE_BEDROCK".to_string(), "1".to_string()),
        ];

        let (environment, warnings) = apply_guard(&pairs);

        // BOTH halves, and the pairing is the point: the two drops are absent from the environment
        // AND named in a warning. Asserting only the absence is the original defect — the variable
        // vanishes and the operator is never told — while asserting only the warning would pass a
        // guard that warns and then forwards the variable anyway.
        assert_eq!(
            environment.keys().collect::<Vec<_>>(),
            vec!["AWS_REGION", "CLAUDE_CODE_USE_BEDROCK"],
            "exactly the two keeps reach the environment: `CLAUDE_CODE_USE_BEDROCK` survives its \
             blocked prefix because the allowlist is tested FIRST (`tmux.py:101-103`), and losing \
             it breaks Bedrock authentication. Got: {environment:?}"
        );
        assert_eq!(
            warnings.len(),
            2,
            "exactly the two dropped variables must warn — got {warnings:?}"
        );

        let blocked_warning = warnings
            .iter()
            .find(|line| line.contains("CODEX_HOME"))
            .unwrap_or_else(|| {
                panic!(
                    "no warning named the blocked variable CODEX_HOME. Dropping it is not the \
                     requirement; TELLING the operator is (BR-5). Warnings: {warnings:?}"
                )
            });
        assert!(
            blocked_warning.contains("blocked prefix"),
            "the blocked warning must say WHY, not just that something happened: {blocked_warning}"
        );

        let oversized_warning = warnings
            .iter()
            .find(|line| line.contains("HUGE_PAYLOAD"))
            .unwrap_or_else(|| {
                panic!(
                    "no warning named the oversized variable HUGE_PAYLOAD (BR-5). \
                     Warnings: {warnings:?}"
                )
            });
        assert!(
            oversized_warning.contains("4096"),
            "the oversized warning must state the value's ACTUAL size so the operator can see \
             how far over the cap they are: {oversized_warning}"
        );
        assert!(
            oversized_warning.contains("2048"),
            "the oversized warning must state the cap: {oversized_warning}"
        );

        for kept in ["AWS_REGION", "CLAUDE_CODE_USE_BEDROCK"] {
            assert!(
                warnings.iter().all(|line| !line.contains(kept)),
                "{kept} was KEPT and must not be warned about — a warning about a variable that \
                 was actually forwarded trains the operator to ignore the channel"
            );
        }

        // What used to follow here replayed the same warnings through `WriterWarnings`, the
        // stderr sink — which was deleted along with `merge` for having no production caller. The
        // assertions it made were about that sink's own plumbing (one `writeln!` per warning, a
        // failure counter for a closed pipe), not about this guard's policy, so nothing this
        // module is responsible for lost coverage. The warning TEXT is asserted above, which is
        // the part `guided_flow::parse_env_pairs` actually surfaces to the operator.
        // (Reported by review on PR #547.)
        assert_eq!(
            warnings.len(),
            2,
            "re-asserted after the sink block was removed: exactly the two drops warn, and the \
             count is what a future edit would break silently"
        );
    }

    /// Test 7 — keeps reach the environment, later pairs win, and the mirror never errors
    /// (INV-1).
    ///
    /// The override row is `tmux.py:170-174` / issue #248: an explicit
    /// `--env AWS_REGION=us-west-2` must beat the inherited value. The empty-input row is the
    /// no-op edge case. Neither path returns a `Result`, which is the invariant: a blocked
    /// variable is a warning, never a failed launch.
    #[test]
    fn kept_vars_reach_the_environment_and_later_pairs_win() {
        let mut environment = BTreeMap::new();
        environment.insert("AWS_REGION".to_string(), "eu-west-1".to_string());

        // Empty input is a no-op: nothing inserted, nothing warned.
        let none = apply_guard_into(&mut environment, &[]);
        assert!(none.is_empty(), "no pairs means no warnings");
        assert_eq!(
            environment.len(),
            1,
            "an empty merge must not disturb the map"
        );

        let pairs = vec![
            ("AWS_REGION".to_string(), "us-west-2".to_string()),
            ("DO_NOT_TRACK".to_string(), "1".to_string()),
        ];
        let warnings = apply_guard_into(&mut environment, &pairs);

        assert_eq!(
            pairs.iter().map(|(k, v)| decide(k, v)).collect::<Vec<_>>(),
            vec![EnvDecision::Keep, EnvDecision::Keep]
        );
        assert!(
            warnings.is_empty(),
            "nothing was dropped, so nothing may warn"
        );
        assert_eq!(
            environment.get("AWS_REGION"),
            Some(&"us-west-2".to_string()),
            "an operator-supplied --env must OVERRIDE the inherited value (issue #248)"
        );
        assert_eq!(environment.get("DO_NOT_TRACK"), Some(&"1".to_string()));

        // Shell metacharacters are data, not syntax: the value survives byte-for-byte because
        // the caller builds an argv VECTOR, never an interpolated shell string (T-10, BR-7).
        let hostile = "; rm -rf / #$(whoami)`id`";
        let (env, decision, warns) = merge_one("AWS_PROFILE", hostile);
        assert_eq!(
            decision,
            EnvDecision::Keep,
            "the mirror filters keys and sizes, not content"
        );
        assert_eq!(env.get("AWS_PROFILE"), Some(&hostile.to_string()));
        assert!(warns.is_empty());
    }

    /// Test 8 — the guard's constants equal the literals read from `clients/tmux.py`.
    ///
    /// The one place the two copies are compared, which is what licenses every other test in
    /// this file to hard-code its expectations (VR-4). Non-vacuous because one side of each
    /// comparison is a literal: changing `MAX_ENV_VALUE_BYTES` to 4096 turns this red instead
    /// of quietly redefining what the rest of the suite means.
    #[test]
    fn the_policy_constants_match_the_python_source_exactly() {
        assert_eq!(
            BLOCKED_PREFIXES, TMUX_PY_BLOCKED_PREFIXES,
            "the blocked prefixes must match tmux.py:83 exactly, in the same three entries"
        );
        assert_eq!(
            BLOCKED_PREFIX_ALLOWLIST, TMUX_PY_ALLOWLIST,
            "the allowlist must match tmux.py:84-93 exactly — all six entries"
        );
        assert_eq!(
            MAX_ENV_VALUE_BYTES, TMUX_PY_CAP_BYTES,
            "the cap must be 2048 bytes (tmux.py:96, PR #246)"
        );

        // A drift check the equality above cannot make: no allowlist entry may be redundant.
        // If an entry stopped matching a blocked prefix it would no longer need an exemption,
        // which would mean the prefix list had changed underneath it.
        for key in BLOCKED_PREFIX_ALLOWLIST {
            assert!(
                TMUX_PY_BLOCKED_PREFIXES
                    .iter()
                    .any(|prefix| key.starts_with(prefix)),
                "{key} is allowlisted but matches no blocked prefix, so the exemption is dead \
                 code — the prefix list must have changed"
            );
        }

        assert_eq!(
            decide("AWS_REGION", "us-east-1"),
            EnvDecision::Keep,
            "an ordinary variable with a short value must survive both checks"
        );
    }
}
