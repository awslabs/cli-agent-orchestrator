//! The live-server contract test of the `skeleton-endpoint-verify` unit (issue #321).
//!
//! Walking-skeleton item 4: "a test that calls the live `cao-server` API and asserts the
//! profiles projection shape (absent `provider` field)". It implements **NFR-7**.
//!
//! # The failure this exists to catch is SILENT
//!
//! `serde` ignores unknown response keys by default. If `provider` returns to the projection,
//! or `loadable` disappears, `server-client` keeps deserialising happily while both pickers go
//! quietly wrong — no error, no log line, no failing test. That silence is the vulnerability,
//! and it is why the assertions below compare the **exact key set** rather than merely
//! checking that a parse succeeded (BR-3).
//!
//! # Three properties of this file are load-bearing, and each is easy to break by accident
//!
//! 1. **It talks to a REAL server (BR-6).** A stubbed response would assert the stub's shape
//!    and prove nothing about the endpoint. Detecting drift in the real service is the entire
//!    point, so there is nothing to mock here.
//!
//! 2. **It FAILS when the server is absent. It never skips (BR-7).** No `#[ignore]`, no
//!    feature gate, no `if unreachable { return }` early-out, and no environment variable that
//!    turns it into a no-op. A skipped contract test reports green while verifying nothing,
//!    which is worse than no test at all: it suppresses the very question it was written to
//!    ask. When the server is down every request helper below panics with a message telling
//!    the operator to start `cao-server`.
//!
//!    The same trap in a different costume is the Python suite's pty-adjacent tests: they live
//!    under `test/e2e/` or carry `@pytest.mark.e2e`, and every pytest invocation in all 8 CI
//!    workflows applies both `--ignore=test/e2e` and `-m "not e2e"`, so they never execute.
//!    Not skipping is necessary but not sufficient — the test also has to be *reachable* by
//!    the command CI actually runs. This one runs under a plain `cargo test`.
//!
//! 3. **It asserts SHAPE, never CONTENT (INV-2, VR-3).** Which profiles exist depends on the
//!    machine (25 on the author's, from 6 packaged built-ins plus local and provider
//!    directories); the per-entry key set does not. A test naming a particular profile, or
//!    pinning the count, would be environment-dependent and would get deleted rather than
//!    fixed. The one non-shape assertion is *non-emptiness*, and its justification is in
//!    [`get_json_array`] — it is not a count assertion.
//!
//! # Why the expectations are hard-coded here rather than taken from `Profile`
//!
//! BR-4. The eight key names are written out longhand as string literals, read off
//! `utils/agent_profiles.py` (`:85-88`, `:171-173`, `:274`) and re-confirmed against the live
//! endpoint. They are deliberately **not** derived from `crate::types::Profile`, nor from any
//! constant `Profile` also uses: a test that sources its expectation from the type under test
//! stays green through exactly the change it exists to catch. That vacuous-guard shape is the
//! dominant failure mode observed on this project, so the duplication is the feature.
//!
//! It happens that `Profile` could not be imported here anyway — `src/types.rs` is a private
//! module of a **binary** crate with no `lib.rs`, and an integration test links against the
//! crate's library target, which does not exist. That is a convenience, not the reason. If a
//! later unit adds `lib.rs`, these literals must stay literals.
//!
//! # What this test cannot see
//!
//! A key that keeps its name and type but changes *meaning* passes here. So does `loadable`
//! becoming always-`true` (`server-client`'s FR-1.5 tests cover that), and a provider missing
//! from the route but present in `ProviderType` — the shape is correct while the contents are
//! incomplete, which is real today (a hardcoded 9-entry binary map against a ten-value enum)
//! and is FR-1.7's problem, not this file's. Stated so nobody over-trusts it.
//!
//! # Hermeticity
//!
//! `safety-guards` (Bolt 2) will block real HTTP in tests (S-5). **This file is the ONE
//! deliberate exemption and must be named explicitly by that tripwire** (BR-8). An exemption
//! that arises because the tripwire happens not to cover this path is indistinguishable from a
//! hole in the tripwire. Bolt 2 does not exist yet, so there is nothing to exempt from today;
//! recorded here so the exemption set stays countable at exactly one.
//!
//! # Failure of this test is a STOP, not a warning
//!
//! INV-4. It means the design's foundation moved: `server-client`, `guided-flow`, and both
//! pickers all rest on these two shapes.

use std::collections::BTreeSet;
use std::env;

use serde_json::Value;

/// The documented default host, used only when `CAO_API_HOST` is unset.
///
/// Mirrors `constants.py:337`. The literal lives here as a *fallback*, which is what the
/// affirmed rule permits — what it forbids is reaching the server at a hard-coded address
/// regardless of the environment. See [`base_url`]. (#321)
const DEFAULT_API_HOST: &str = "127.0.0.1";

/// The documented default port, used only when `CAO_API_PORT` is unset. Mirrors
/// `constants.py:338`. (#321)
const DEFAULT_API_PORT: &str = "9889";

/// Per-request bound, in whole seconds (`minreq`'s unit).
///
/// A live-server test that hangs forever is the same failure class as the pty deadlock this
/// skeleton's item 2 exists to prevent, and it fails the same way: the job burns its runner
/// minutes and nobody learns anything. The bound converts "the server never answered" into a
/// prompt, legible failure. It is the in-test half of the pairing `team.md` requires; the
/// other half is an explicit `timeout-minutes` on the CI job, which `ci-pipeline` owns. (#321)
const REQUEST_TIMEOUT_SECS: u64 = 10;

/// The API root, taken from the environment exactly as the Python client does.
///
/// `CAO_API_HOST` / `CAO_API_PORT` with the documented defaults, composed the same way as
/// `constants.py:342`'s `API_BASE_URL`. Never a hard-coded `127.0.0.1:9889`: an affirmed
/// `project.md` rule, and also what makes VR-2 testable — pointing the two variables at a
/// closed port is how the no-skip behaviour gets proven without stopping the operator's
/// server. (#321)
fn base_url() -> String {
    let host = env::var("CAO_API_HOST").unwrap_or_else(|_| DEFAULT_API_HOST.to_string());
    let port_raw = env::var("CAO_API_PORT").unwrap_or_else(|_| DEFAULT_API_PORT.to_string());

    // Parsed rather than interpolated blindly, mirroring the `int()` on the Python side. A
    // typo'd port would otherwise surface as a baffling connection error instead of naming the
    // bad value. (#321)
    let port: u16 = port_raw.parse().unwrap_or_else(|error| {
        panic!("CAO_API_PORT must be a TCP port number; got {port_raw:?} ({error})")
    });

    format!("http://{host}:{port}")
}

/// GETs `path` and returns its entries, **panicking on anything that is not a healthy 200 with
/// a non-empty JSON array**.
///
/// Every failure mode below is a test failure, deliberately (BR-7). There is no branch that
/// returns early, and no branch that reports success on a response this test could not read.
///
/// # Why an EMPTY array fails
///
/// Because an empty array would make every assertion in this file **vacuous**: with zero
/// entries there are zero key sets to compare, the per-entry loops never execute, and the test
/// reports green while verifying nothing — precisely the outcome BR-7 forbids, arriving
/// through the front door instead of via `#[ignore]`.
///
/// It is also not a state a healthy server can reach. `/agents/providers` is built from a
/// hardcoded nine-entry map (`api/main.py:1535-1545`), so it is empty only if that route was
/// rewritten. `/agents/profiles` scans the packaged built-in store unconditionally
/// (`utils/agent_profiles.py:249-267`, six `.md` files shipped in
/// `cli_agent_orchestrator/agent_store/`), and that scan sits inside a
/// `try/except Exception: logger.debug(...)` — so an empty list is the *visible symptom of a
/// silently swallowed error*, which is exactly the class of failure this intent exists to
/// eliminate. Passing on it would launder that silence into a green build.
///
/// This is **not** a count assertion and does not conflict with VR-3: `>= 1` is the threshold
/// at which the shape assertions have something to bite on, whereas `== 25` would encode the
/// author's machine. (#321)
fn get_json_array(path: &str) -> Vec<Value> {
    let url = format!("{}{path}", base_url());

    let response = minreq::get(&url)
        .with_timeout(REQUEST_TIMEOUT_SECS)
        .send()
        .unwrap_or_else(|error| {
            panic!(
                "could not reach cao-server at {url} -- {error}\n\
                 \n\
                 This test FAILS rather than skips, on purpose (BR-7): a contract test that \n\
                 skipped when the server was down would report green while verifying nothing, \n\
                 which is how the drift it guards against would reach production.\n\
                 \n\
                 Start the server (`cao-server`) or point CAO_API_HOST / CAO_API_PORT at a \n\
                 running instance, then re-run. Current values: CAO_API_HOST={host:?}, \n\
                 CAO_API_PORT={port:?} (defaults {DEFAULT_API_HOST}:{DEFAULT_API_PORT}).",
                host = env::var("CAO_API_HOST").ok(),
                port = env::var("CAO_API_PORT").ok(),
            )
        });

    // BR-9 / Step 5: the status is part of the contract and must not be assumed. Both routes
    // return 200; a 401 or 403 here would mean the endpoint started requiring auth, which
    // would otherwise slip past a shape-only assertion. (#321)
    let status = response.status_code;
    let body = response.as_str().unwrap_or_else(|error| {
        panic!("GET {url} returned status {status} with a non-UTF-8 body -- {error}")
    });
    assert_eq!(
        status, 200,
        "GET {url} must return 200; got {status}. Body: {body}"
    );

    let parsed: Value = serde_json::from_str(body)
        .unwrap_or_else(|error| panic!("GET {url} must return JSON -- {error}. Body: {body}"));
    let entries = parsed
        .as_array()
        .unwrap_or_else(|| {
            panic!(
                "GET {url} must return a JSON array; got {kind}. Body: {body}",
                kind = wire_type(&parsed)
            )
        })
        .clone();

    assert!(
        !entries.is_empty(),
        "GET {url} returned an empty array. That is a FAILURE, not a pass: with no entries \
         the key-set assertions below would have nothing to check and would report green \
         while verifying nothing. A healthy server cannot return empty here -- \
         /agents/providers is a hardcoded 9-entry map, and /agents/profiles always scans the \
         6 packaged built-in profiles unless that scan raised and was swallowed by its \
         `except Exception: logger.debug(...)`."
    );

    entries
}

/// The wire type of `value`, as one of the tags the expectation tables below use.
///
/// An empty array reports `string[]`, which is the right tolerance: `[]` is the documented
/// value for an absent `capabilities` / `tags` / `duplicated_in`, and `_discovery_fields`
/// coerces to empty rather than `null`. A heterogeneous array reports the weaker `array` so a
/// type change inside a list is still visible. (#321)
fn wire_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(items) if items.iter().all(Value::is_string) => "string[]",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// The key set of one array entry, failing if the entry is not a JSON object.
fn key_set(entry: &Value, url_path: &str, index: usize) -> BTreeSet<String> {
    entry
        .as_object()
        .unwrap_or_else(|| {
            panic!(
                "entry {index} of GET {url_path} must be a JSON object; got {kind}",
                kind = wire_type(entry)
            )
        })
        .keys()
        .cloned()
        .collect()
}

/// Names an entry for a failure message without asserting anything about it.
///
/// Reporting *which* profile broke the contract is diagnostics; asserting that a particular
/// profile exists would be the content-dependence INV-2 forbids. The distinction matters. (#321)
fn entry_label(entry: &Value) -> String {
    entry
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("<no name key>")
        .to_string()
}

/// Test 1 — every `GET /agents/profiles` entry carries exactly the eight projected keys, with
/// the documented wire type for each.
///
/// The primary assertion of this unit (Steps 2, 5, 7; NFR-7, BR-1, BR-3, BR-4, BR-9). Two
/// independent hard-coded tables, cross-checked against each other so a typo in either is
/// caught: the eight names written out longhand, and the eight name/type pairs. Neither is
/// derived from `Profile` — see the module docs for why that would make this test vacuous.
///
/// Set equality catches a key lost *and* a key gained, `provider` included; test 2 then names
/// `provider` on its own so a regression gets a message that says so.
///
/// This asserts against the **list** route, never `GET /agents/profiles/{name}` (INV-1). The
/// singular route applies `model_dump(exclude_none=True)` (`api/main.py:1498`) while the list
/// route returns `list_agent_profiles()` directly (`:1485`), so they project differently and an
/// assertion against the singular route would neither prove nor disprove the list shape. (#321)
#[test]
fn profiles_endpoint_returns_exactly_the_eight_projected_keys() {
    // Read off utils/agent_profiles.py (:85-88, :171-173, :274) and re-confirmed live against
    // the running server. NOT read off `Profile` (BR-4). (#321)
    let expected_keys: BTreeSet<String> = [
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
        expected_keys.len(),
        8,
        "the hard-coded expectation must itself list 8 distinct names"
    );

    // The second, independent table: the documented wire type per key. `description` and
    // `role` are `string` and never `null` -- `_discovery_fields` coerces a missing value to
    // `""` -- so an assertion expecting `null` would fail against a healthy server.
    let expected_types = [
        ("name", "string"),
        ("source", "string"),
        ("loadable", "bool"),
        ("description", "string"),
        ("capabilities", "string[]"),
        ("tags", "string[]"),
        ("role", "string"),
        ("duplicated_in", "string[]"),
    ];
    let typed_keys: BTreeSet<String> = expected_types
        .iter()
        .map(|(key, _)| (*key).to_string())
        .collect();
    assert_eq!(
        typed_keys, expected_keys,
        "the two hard-coded tables in this test must agree; a name in one and not the other \
         is a typo in the expectation, not a finding about the server"
    );

    let entries = get_json_array("/agents/profiles");

    // Every entry, not merely the first: a projection that diverges for one profile is still
    // a broken contract, and the loop costs nothing. (#321)
    for (index, entry) in entries.iter().enumerate() {
        let actual = key_set(entry, "/agents/profiles", index);
        assert_eq!(
            actual,
            expected_keys,
            "GET /agents/profiles entry {index} ({label:?}) must carry exactly the 8 projected \
             keys. A GAINED key -- `provider` above all -- or a LOST key is a defect that \
             breaks both pickers silently, because serde ignores unknown keys. Got: {actual:?}",
            label = entry_label(entry)
        );

        for (key, expected_type) in expected_types {
            let value = entry.get(key).unwrap_or_else(|| {
                panic!("entry {index} lost key {key:?} after the key-set check")
            });
            assert_eq!(
                wire_type(value),
                expected_type,
                "GET /agents/profiles entry {index} ({label:?}) key {key:?} must be \
                 {expected_type} on the wire; got {actual_type} ({value}). A key that keeps \
                 its name and changes type deserialises into a different Rust shape.",
                label = entry_label(entry),
                actual_type = wire_type(value)
            );
        }
    }
}

/// Test 2 — `provider` is ABSENT from every `GET /agents/profiles` entry.
///
/// Step 3 / BR-5. Test 1's set equality already covers this implicitly; this explicit negative
/// is what **documents assumption A-2's resolution** and fails with a message that names the
/// field if it ever returns.
///
/// `AgentProfile.provider` does exist in the server's model (`models/agent_profile.py:38`) --
/// the listing projection simply drops it, because `_discovery_fields()`
/// (`utils/agent_profiles.py:60-89`) whitelists only description/capabilities/tags/role. So
/// this is a field one refactor away from reappearing, and its reappearance would be benign to
/// `serde` and expensive to the design: ADR-02 declined the per-profile N+1 fetch that a
/// visible-but-always-`None` `provider` invites. Provider choices come from
/// `GET /agents/providers`. (#321)
#[test]
fn profiles_endpoint_does_not_return_a_provider_field() {
    let entries = get_json_array("/agents/profiles");

    for (index, entry) in entries.iter().enumerate() {
        let keys = key_set(entry, "/agents/profiles", index);
        assert!(
            !keys.contains("provider"),
            "GET /agents/profiles entry {index} ({label:?}) must NOT carry `provider` (A-2, \
             BR-5). The listing projection drops it deliberately; its return means \
             `_discovery_fields()` changed, and ADR-02's rejected per-profile N+1 fetch \
             becomes available to callers again. Keys: {keys:?}",
            label = entry_label(entry)
        );
    }
}

/// Test 3 — every `GET /agents/providers` entry carries exactly `{name, binary, installed}`.
///
/// Step 4 / BR-2 / BR-9. Route B of A-2's resolution, which the provider picker depends on
/// (FR-1.2), verified at `api/main.py:1531-1550`. The three names are hard-coded literals for
/// the same reason the eight are.
///
/// Note what this does *not* assert: that the list is complete. The route's hardcoded map has
/// nine entries against a ten-value `ProviderType` enum (`MOCK_CLI` absent), so the *shape* is
/// correct while the *contents* are incomplete. That is real today and FR-1.7 addresses it in
/// `server-client` by forbidding the client from hiding providers — a different mechanism.
/// `installed == false` is display data here, never a filter (BR-9 of `shared-types`). (#321)
#[test]
fn providers_endpoint_returns_exactly_name_binary_and_installed() {
    let expected_keys: BTreeSet<String> = ["name", "binary", "installed"]
        .iter()
        .map(|key| (*key).to_string())
        .collect();
    assert_eq!(
        expected_keys.len(),
        3,
        "the hard-coded expectation must itself list 3 distinct names"
    );
    let expected_types = [
        ("name", "string"),
        ("binary", "string"),
        ("installed", "bool"),
    ];

    let entries = get_json_array("/agents/providers");

    for (index, entry) in entries.iter().enumerate() {
        let actual = key_set(entry, "/agents/providers", index);
        assert_eq!(
            actual, expected_keys,
            "GET /agents/providers entry {index} must carry exactly {{name, binary, \
             installed}}; the provider picker (FR-1.2) reads all three. Got: {actual:?}"
        );

        for (key, expected_type) in expected_types {
            let value = entry.get(key).unwrap_or_else(|| {
                panic!("entry {index} lost key {key:?} after the key-set check")
            });
            assert_eq!(
                wire_type(value),
                expected_type,
                "GET /agents/providers entry {index} key {key:?} must be {expected_type} on \
                 the wire; got {actual_type} ({value})",
                actual_type = wire_type(value)
            );
        }
    }
}

/// Test 4 — this file contains no mechanism that could turn the three tests above into no-ops.
///
/// BR-7 is the rule this unit's whole value rests on, and running the suite against a closed
/// port (VR-2) proves it **once, for the code as it stands today**. This test is what makes it
/// hold *tomorrow*: a future edit that adds `#[ignore]` or a feature gate to quiet a red build
/// turns this red instead, in CI, rather than silently converting a contract guard into
/// decoration. It is the same self-inspection the crate already uses to hold
/// `#![forbid(unsafe_code)]` at the crate root.
///
/// **What it honestly cannot catch:** an `if unreachable { return; }` early-out, which has no
/// reliable textual signature. Stated rather than papered over with a check that would look
/// stronger than it is — that overclaiming is its own failure mode. The runtime VR-2 proof
/// covers that shape today, and review covers it thereafter.
///
/// # Two things about how it searches, both learned the hard way
///
/// **The needles are assembled from fragments at runtime**, so this file never literally
/// contains the string it searches for. Without that, the search term sitting in this test body
/// would match itself and the guard could never fail — the vacuous-guard trap, arriving inside
/// the very test written to prevent one.
///
/// **The search runs over CODE LINES ONLY, with `//`-comment lines stripped.** This was not a
/// precaution; the first version of this test failed on its own prose, because the module docs
/// above legitimately spell out the attribute they forbid. Two wrong fixes were available and
/// both would have gutted the guard: deleting the explanation (the docs are how a reader learns
/// *why*), or matching only at column 0 (an attribute indented inside a nested `mod` would then
/// walk straight past). Stripping comments keeps the prose free to name the thing and still
/// catches an attribute at any indentation. A line whose code half is empty after the strip
/// contributes nothing, so a `//`-comment can never satisfy or defeat the check. (#321)
#[test]
fn this_file_contains_no_skip_mechanism() {
    const THIS_FILE: &str = include_str!("endpoint_contract.rs");

    let ignore_attribute = format!("#[{}", "ignore");
    let feature_gate = format!("#[cfg({}", "feature");
    let test_attribute = format!("#[{}]", "test");

    // `//` covers `///` and `//!` too, so doc comments are stripped along with plain ones.
    // Deliberately not a full lexer: no string literal in this file contains `//`, and a
    // comment-stripper that over-strips would only ever weaken this guard, never fake a pass.
    let code: String = THIS_FILE
        .lines()
        .map(|line| match line.find("//") {
            Some(comment_start) => &line[..comment_start],
            None => line,
        })
        .collect::<Vec<_>>()
        .join("\n");

    assert!(
        !code.contains(&ignore_attribute),
        "this file's CODE must contain no {ignore_attribute}] attribute (BR-7): an ignored \
         contract test reports green while verifying nothing, which is strictly worse than no \
         test because it suppresses the question. If the server is unavailable the test must \
         FAIL."
    );
    assert!(
        !code.contains(&feature_gate),
        "this file's CODE must carry no {feature_gate}..)] gate (BR-7): a test that only \
         compiles under a feature CI does not enable is skipped in every way that matters, \
         exactly as the Python pty tests excluded by `-m \"not e2e\"` are."
    );

    // Counted in the stripped code, so the attributes named in the docs above are not tallied.
    let test_count = code.matches(&test_attribute).count();
    assert_eq!(
        test_count, 4,
        "expected exactly 4 {test_attribute} functions (3 contract assertions + this guard); \
         found {test_count}. Deleting a contract test is the other way to make this file stop \
         verifying the endpoints; adding one without updating this number is a prompt to \
         confirm the addition is a contract assertion and not an escape hatch."
    );
}
