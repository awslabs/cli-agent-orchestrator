//! Guard 2 of the `safety-guards` unit: the hermeticity tripwire (issue #321).
//!
//! Tests must not reach real HTTP or spawn a real `cao` binary (BR-8, SR-5). A test that
//! silently talks to a live server passes or fails on the developer's machine state — it is
//! green on a laptop with `cao-server` running and red in CI for reasons that have nothing to do
//! with the change under review. The tripwire converts that into a loud, legible failure.
//!
//! # THE ONE EXEMPTION IS EXPLICIT AND NAMED (BR-9, INV-3)
//!
//! [`EXEMPTIONS`] has exactly one member: `tests/endpoint_contract.rs`, the
//! `skeleton-endpoint-verify` unit, which makes **real** HTTP calls to a live `cao-server` by
//! design. That is `team.md`'s affirmed walking-skeleton item 4 and NFR-7, and a stubbed
//! response there would assert the stub's shape and prove nothing about the endpoint.
//!
//! It is a **named, countable set** rather than a per-test marker or an `#[allow]`-style
//! escape hatch, for the reason BR-9 gives: *an exemption that arises because the tripwire
//! happens not to cover a path is indistinguishable from a hole in the tripwire.* A set can be
//! counted, so [`the_exemption_set_has_exactly_one_member`] asserts `len() == 1` and a second
//! exemption becomes a reviewable design change instead of a local decision. A marker cannot be
//! counted from outside, which is exactly why it was rejected at 3.2's review.
//!
//! # AND ONE PRODUCTION MODULE OWNS THE TRANSPORT — a DIFFERENT mechanism (BR-1)
//!
//! Bolt 3 landed `server-client`, so the crate now performs production HTTP for the first time.
//! [`HTTP_OWNER`] names the one module permitted to, and it is deliberately **not** a second
//! `EXEMPTIONS` member: that set answers "which *test* may reach a live server?", and
//! `src/server.rs` is not a test and reaches nothing during `cargo test` — its own tests run
//! against a stub bound on port 0. Conflating the two would make `EXEMPTIONS.len() == 2` assert
//! something false and spend the friction BR-9 built.
//!
//! Two things make it a narrowing rather than a hole, and both are asserted:
//! the **`cao`-spawn needles still apply to it in full** (ADR-02/BR-2 forbid a subprocess there
//! above all — it is where a CLI fallback would be written), and
//! [`only_the_http_owner_names_an_http_client`] fails if a **second** production module names a
//! client. That converse is a *stronger* check than what existed before: until a production
//! module did HTTP at all, "no production module does HTTP" held trivially.
//!
//! The exemption is also checked for being **load-bearing** rather than decorative: the exempt
//! file must actually contain the HTTP the tripwire would otherwise reject (VR-6). An exemption
//! for a file that stopped making live calls is stale, and stale exemptions are how the set
//! quietly grows.
//!
//! # HOW IT WORKS, AND THEREFORE WHAT IT CANNOT DO
//!
//! This is a **static scan of source text**, not a runtime sandbox. It embeds every Rust source
//! in the crate at compile time and rejects the vocabulary of real network access and real `cao`
//! spawns. The honest limits are enumerated in
//! [`what_this_tripwire_cannot_detect`] — read them before trusting a green run. Overstating a
//! guard is worse than shipping a narrow one, because the overstatement is what stops anyone
//! adding the guard that would have caught the thing.
//!
//! A runtime alternative was considered and rejected: `#![forbid(unsafe_code)]` and this
//! crate's dependency-free posture mean there is no seccomp/`LD_PRELOAD` hook available without
//! adding exactly the supply-chain surface TS-1 forbids, and a test binary cannot revoke its own
//! socket access portably. A static scan catches the realistic failure — somebody writes a test
//! that calls the server — at the cost of not catching a determined author.
//!
//! # Not shipped
//!
//! TS-2: the tripwire is a test target, so it is absent from the release build. A shipped
//! tripwire would add runtime cost and could interfere with the production HTTP this crate
//! legitimately performs now that `server-client` (Bolt 3) has landed — see [`HTTP_OWNER`].

use std::collections::BTreeMap;

/// Every Rust source in the crate, embedded at compile time.
///
/// `include_str!` rather than a directory walk, following the precedent set by
/// `no_backend_attach_call.rs`: a walk depends on the runner's working directory and — worse — a
/// walk that silently found no files would **pass**. Embedding makes a missing file a *compile*
/// error, so the scan cannot degrade into a vacuous pass. The cost is that a new file must be
/// listed here, and [`the_scan_set_covers_every_source_in_the_crate`]'s count assertion turns
/// that from a silent gap into a failing test.
///
/// **This file is in its own scan set.** Excluding it would make the tripwire the one place in
/// the crate where real HTTP could be introduced unchallenged, so the needles below are
/// assembled from fragments to keep this file from tripping over its own vocabulary. (#321)
const SOURCES: &[(&str, &str)] = &[
    // Production sources.
    ("src/main.rs", include_str!("../src/main.rs")),
    ("src/error.rs", include_str!("../src/error.rs")),
    ("src/handoff.rs", include_str!("../src/handoff.rs")),
    ("src/types.rs", include_str!("../src/types.rs")),
    ("src/env_guard.rs", include_str!("../src/env_guard.rs")),
    // Added by `command-catalog` (Bolt 3). This tripwire has no `mod`-declaration cross-check of
    // the kind `no_backend_attach_call.rs` grew, so leaving the module unlisted here would have
    // been a genuinely silent hole rather than a failing test — the count assertion below only
    // fires once a file is ADDED. The module is the natural place for a future "fetch the policy
    // table over HTTP" change, which is exactly what SR-1 says must stay visible. (#321)
    ("src/catalog.rs", include_str!("../src/catalog.rs")),
    // Added by `results-pane` (Bolt 4), the first unit to bring the TUI rendering stack in.
    // Listing it matters for a reason specific to this module: it is the crate's **untrusted-
    // input sink** — it renders whatever an arbitrary command wrote to stdout/stderr — so it is
    // the natural place somebody would later add "fetch the output over HTTP instead of taking
    // it as a stream", which is exactly what BR-1 says must stay in `server.rs` alone.
    // [`only_the_http_owner_names_an_http_client`] is what enforces that, and it only sees
    // modules that are in this list. (#321)
    (
        "src/results_pane.rs",
        include_str!("../src/results_pane.rs"),
    ),
    // Added by `server-client` (Bolt 3). **This is the crate's HTTP owner** — see
    // [`HTTP_OWNER`], which is a different mechanism from [`EXEMPTIONS`] and deliberately so.
    // It is in the scan set like everything else, so the `cao`-spawn half of the guard applies
    // to it in full. (#321)
    ("src/server.rs", include_str!("../src/server.rs")),
    // Added by `guided-flow` (Bolt 4), and this is the module the HTTP-ownership half of the
    // guard exists for. `guided-flow` genuinely PERFORMS I/O — it populates the agent and provider
    // pickers — so it is the one non-owner production module with a real reason to reach for a
    // client, and BR-1 of `server-client` says it must go through `ServerClient` instead. That is
    // why it takes its reads through a `PickerSource` trait: naming a client (or a socket type in
    // a test stub) here fails [`only_the_http_owner_names_an_http_client`], correctly. It is also
    // where FR-1.4's forbidden CLI fallback would be written — "the picker failed, so shell out to
    // `cao profile list`" — which the `cao`-spawn needles catch. (#321)
    ("src/guided_flow.rs", include_str!("../src/guided_flow.rs")),
    // Added by `renderer` (Bolt 5), and load-bearing for the HTTP-ownership half specifically.
    // `renderer` is the crate's orchestrator: it calls `create_session`, `terminal` and `run`, so
    // it is the module with the most reasons to want a client of its own — and BR-1 says every one
    // of those calls goes through `ServerClient`. It takes them through a `ServerApi` trait for
    // exactly that reason, which is also why its own tests can name no socket type. It is
    // additionally where FR-1.4's forbidden CLI fallback would be written ("the picker failed, so
    // shell out to `cao profile list`") — the `cao`-spawn needles catch that. (#321)
    ("src/renderer.rs", include_str!("../src/renderer.rs")),
    // Added by the semantic colour layer (#556). This entry was **not** forced by a failing test:
    // when `mod theme;` landed, `no_backend_attach_call.rs` went red immediately and this tripwire
    // stayed green, because its coverage check is a hand-maintained count and a count cannot
    // notice an omission. The `catalog.rs` comment above named that hole exactly, and it stayed
    // open — so `theme.rs` sat unscanned by the HTTP-ownership guard until somebody thought to
    // look. It is closed now: [`the_scan_set_covers_every_source_in_the_crate`] cross-checks
    // `src/main.rs`'s `mod` declarations, so the next module is forced in rather than remembered
    // in. Nothing in a palette wants HTTP, which is the point — the file judged least interesting
    // is the one an omission hides in. (#556)
    ("src/theme.rs", include_str!("../src/theme.rs")),
    // Test targets and shared test infrastructure.
    (
        "tests/binary_exits_zero.rs",
        include_str!("binary_exits_zero.rs"),
    ),
    (
        "tests/endpoint_contract.rs",
        include_str!("endpoint_contract.rs"),
    ),
    (
        "tests/no_backend_attach_call.rs",
        include_str!("no_backend_attach_call.rs"),
    ),
    ("tests/pty.rs", include_str!("pty.rs")),
    (
        "tests/pty_harness/mod.rs",
        include_str!("pty_harness/mod.rs"),
    ),
    (
        "tests/hermeticity_tripwire.rs",
        include_str!("hermeticity_tripwire.rs"),
    ),
    // Added by the semantic colour layer (#556). A source-text guard needs no I/O at all, which is
    // exactly why it is listed: the plausible regression is somebody deciding the scan would be
    // tidier reading files from disk than embedding them with `include_str!`, and `fs` is one step
    // from `minreq`. The `mod` cross-check above cannot force this entry — it derives from
    // `src/main.rs`, and a test target is not a module — so a new file under `tests/` still has to
    // be remembered. That gap is named in [`what_this_tripwire_cannot_detect`]. (#556)
    (
        "tests/no_colour_literal_outside_theme.rs",
        include_str!("no_colour_literal_outside_theme.rs"),
    ),
];

/// The exemption set. **Exactly one member** (BR-9, INV-3, SR-5).
///
/// Each entry is `(path, unit-name, why)`. The unit name is mandatory: BR-9 requires the
/// exemption to *name* the unit, so that reviewing the set means reviewing a decision somebody
/// made rather than inferring one from a coverage hole.
///
/// Adding a second member is a design change requiring review. It is not enough to append a row
/// here — [`the_exemption_set_has_exactly_one_member`] hard-codes `1` and will go red, which is
/// the intended friction. (#321)
const EXEMPTIONS: &[(&str, &str, &str)] = &[(
    "tests/endpoint_contract.rs",
    "skeleton-endpoint-verify",
    "walking-skeleton item 4 / NFR-7: it must call the LIVE cao-server API and assert the \
     profiles projection shape. A stub would assert the stub's shape and prove nothing about \
     the endpoint, so there is nothing to mock here",
)];

/// The **one** production module permitted to name an HTTP client: `server-client` (BR-1, SR-1).
///
/// # Why this is a separate mechanism from [`EXEMPTIONS`] and not a second member of it
///
/// The two answer different questions, and merging them would destroy what each is for.
///
/// [`EXEMPTIONS`] answers *"which TEST may reach a real server?"* — the hazard BR-8 names, whose
/// answer must stay exactly one, countable, and reviewable. `src/server.rs` is not a test and
/// reaches no server during `cargo test`; adding it to that set would make `EXEMPTIONS.len()`
/// read as "two tests may call the live server", which is false, and would spend the very
/// friction BR-9 built.
///
/// This constant answers a question the tripwire could not previously ask, because until Bolt 3
/// the crate performed **no** production HTTP at all: *"which production module owns the
/// transport?"* The module docs already anticipated the arrival — "the production HTTP this crate
/// legitimately performs once `server-client` (Bolt 3) lands" — and BR-1 gives the answer as a
/// requirement: **one** unit owns all HTTP and no other unit opens a connection.
///
/// So this is not a hole; it is the guard gaining the distinction it lacked. What it now enforces
/// that it could not before is the *converse*, and that is stronger than what was there:
/// [`only_the_http_owner_names_an_http_client`] fails if a **second** production module names a
/// client, which is exactly BR-1 in executable form. Before Bolt 3, "no production module does
/// HTTP" was enforced trivially by there being none.
///
/// Three properties keep it narrow, each asserted rather than asserted-about:
///
/// - **One member**, hard-coded, same as `EXEMPTIONS`. A second is a reviewable design change.
/// - **The `cao`-spawn needles still apply to it in full.** Only the network vocabulary is
///   relaxed, and only for this path — a `Command::new("cao")` in `src/server.rs` still fires.
///   That matters because BR-2/ADR-02 forbid subprocess execution *especially* here: this is the
///   module where a CLI fallback would be written.
/// - **It is load-bearing**, checked the same way the exemption is: the file must actually
///   contain the HTTP vocabulary, or the entry is stale.
const HTTP_OWNER: (&str, &str, &str) = (
    "src/server.rs",
    "server-client",
    "BR-1/BR-2: this unit owns ALL of the crate's HTTP and is the only I/O component in it. Its \
     production HTTP is the point of the unit, not a hermeticity failure — it performs no I/O \
     during `cargo test`, where its own tests run against a stub bound on port 0. The `cao`-spawn \
     needles still apply to it in full, because ADR-02 forbids subprocess execution here above \
     all: this is the module where a CLI fallback would be written",
);

/// Returns `line` up to a `//` that is **not inside a string literal**.
///
/// Applied per line rather than to the whole file because a violation must report its **line
/// number** (BR-10).
///
/// # This used to cut at the first `//` anywhere, and that weakened the scan
///
/// The claim justifying the simpler form was that it "can only ever *weaken* this scan by hiding
/// real code", which is true and was treated as acceptable. It is not: hiding real code is exactly
/// how a forbidden call goes unnoticed. `no_backend_attach_call.rs` fixed its copy of this function
/// for that reason, and this one was left behind still carrying a comment claiming the two were the
/// same rule — so the doc asserted parity that no longer existed.
///
/// **Measured across the crate before this change: 21 lines where the naive form hides code the
/// string-aware form keeps.** They are not hypothetical: `src/server.rs:871` builds
/// `format!("http://{{host}}:{{port}}")`, so everything after the scheme's `//` on that line — the
/// live URL construction in the module that owns all HTTP — was invisible to this scan.
///
/// The obvious alternative repair, stripping only lines whose trimmed form *starts* with `//`,
/// fails in the worse direction: a trailing `let x = 1; // never call TcpStream` would survive into
/// the scanned text and fire the tripwire on prose, and this crate has many such trailing comments.
/// So the scan tracks whether it is inside a string literal, which is enough for this crate's
/// syntax and no more.
///
/// The URL scheme needles below stay spelt without their slashes. That is now belt-and-braces
/// rather than a requirement, and [`every_needle_is_actually_findable_in_stripped_code`] is what
/// keeps it honest either way. (#321, and review on PR #547)
fn strip_comment(line: &str) -> &str {
    let bytes = line.as_bytes();
    let mut in_string = false;
    let mut index = 0;

    while index < bytes.len() {
        match bytes[index] {
            // An escape inside a string consumes the next byte, so `\"` does not end the literal.
            b'\\' if in_string => index += 1,
            b'"' => in_string = !in_string,
            b'/' if !in_string && bytes.get(index + 1) == Some(&b'/') => return &line[..index],
            _ => {}
        }
        index += 1;
    }
    line
}

/// The stripper keeps code after a URL literal, and still strips trailing comments.
///
/// Both directions, because the two plausible implementations each fail one of them, and a test
/// asserting only one would license the other bug. The first case is taken from real code —
/// `src/server.rs` builds `http://{host}:{port}` — which is what made the previous
/// implementation's stated assumption false here just as it was in the sibling file.
/// (Review on PR #547.)
#[test]
fn the_stripper_keeps_code_after_a_url_literal_and_still_strips_trailing_comments() {
    let scheme = format!("http{}", "://");
    // Assembled from fragments, like every needle in this file, so THIS TEST's own body does not
    // contain the forbidden vocabulary contiguously. Written out verbatim first, and the run
    // failed: the stricter stripper — which is the whole point of the change — no longer hid these
    // strings, so the tripwire fired on itself at three lines. That is the same self-exemption trap
    // `forbidden_needles`' docs record about its `reason` strings, met from the other side.
    let stream = format!("Tcp{}", "Stream");
    let socket = format!("Udp{}", "Socket");

    // A needle after a URL literal on the same line must survive stripping.
    let line = format!("let url = \"{scheme}host\"; let leak = {stream}::connect(addr);");
    let stripped = strip_comment(&line);
    assert!(
        stripped.contains(&stream),
        "code after a URL literal must survive: cutting at the `//` inside the scheme hides it, \
         and a hidden forbidden call is a scan that reports success. Got: {stripped:?}"
    );

    // A genuine trailing comment must still be removed, or prose trips the guard.
    let commented = format!("let x = 1; // never call {stream}::connect");
    assert_eq!(
        strip_comment(&commented).trim(),
        "let x = 1;",
        "a trailing comment must still be stripped, or documenting this guard becomes a hazard"
    );

    // A whole-line comment strips to nothing.
    let prose = format!("    // prose about {socket}");
    assert_eq!(strip_comment(&prose).trim(), "");
}

/// The forbidden network vocabulary, each mapped to why it is forbidden.
///
/// Assembled from fragments so this file's own comment-stripped body does **not** contain them
/// contiguously. Without that, the tripwire fires on itself — and a guard that always fails is
/// as useless as one that never can, because the first thing anyone does is delete it.
///
/// **The `reason` strings are held to the same rule, and that was found by the guard rather than
/// by reasoning.** The first run of this file failed on three of its own reason strings: the
/// `Udp` reason said "same reasoning as TcpStream", the `wg`+`et` reason said "same as curl", and
/// the `https` reason said "as http:". They are code, not comments, so they were scanned and
/// they matched. The reasons below therefore describe the mechanism instead of naming its
/// sibling. Self-exempting this file was the alternative and was rejected: it would make the
/// tripwire the one place real HTTP could enter unchallenged, which is the hole BR-9 is about.
///
/// The set covers the crate's actual reach, not every networking API in existence: the HTTP
/// clients that could plausibly be added here (`minreq` is already a dev-dependency; the others
/// are what a developer reaches for by habit), raw sockets, the two shell fetchers a test might
/// shell out to, and the URL schemes themselves as a catch-all for a client this list does not
/// name. See [`what_this_tripwire_cannot_detect`] for what that still misses. (#321)
fn http_needles() -> BTreeMap<String, &'static str> {
    let mut needles = BTreeMap::new();

    needles.insert(
        format!("min{}", "req"),
        "the crate's own dev-dependency HTTP client; a real request in a test makes it pass or \
         fail on whether cao-server happens to be running",
    );
    needles.insert(
        format!("req{}", "west"),
        "an async HTTP client: real network access, and ~60 transitive crates TS-1 forbids",
    );
    needles.insert(
        format!("u{}", "req"),
        "a blocking HTTP client: real network access",
    );
    needles.insert(
        format!("hyper{}", "::"),
        "the HTTP implementation underneath most clients: real network access",
    );
    needles.insert(
        format!("Tcp{}", "Stream"),
        "a raw stream socket reaches the network without any HTTP client at all",
    );
    needles.insert(
        format!("Udp{}", "Socket"),
        "a raw datagram socket reaches the network without any HTTP client at all",
    );
    needles.insert(
        format!("cu{}", "rl"),
        "shelling out to a fetcher is real HTTP wearing a subprocess costume",
    );
    needles.insert(
        format!("wg{}", "et"),
        "a second shell fetcher: a subprocess that performs real HTTP",
    );
    // Spelt WITHOUT the slashes, deliberately. `strip_comment` truncates at `//`, so a needle
    // containing them could never match stripped code. See `strip_comment`'s doc comment.
    needles.insert(
        format!("http{}", ":"),
        "a plaintext URL scheme in code is the catch-all for an HTTP client this list does not \
         name",
    );
    needles.insert(
        format!("https{}", ":"),
        "a TLS URL scheme in code is the catch-all for a client this list does not name",
    );

    needles
}

/// The `cao` binaries whose spawn is forbidden, as they would appear in a program position.
///
/// Restricted to the **program position** (`new("cao"`, `&["cao"`) rather than the bare string
/// `"cao"`, because the bare form has a legitimate use that would make this guard cry wolf:
/// `src/types.rs:617` carries `{"cao":"ok"}` inside a `GET /health` JSON fixture. A needle that
/// fires on test data would be deleted within a week.
///
/// **`CARGO_BIN_EXE_cao-tui` is deliberately NOT caught, and that is correct.**
/// `binary_exits_zero.rs` spawns the crate's own freshly-built binary through
/// `env!("CARGO_BIN_EXE_cao-tui")` — a hermetic artifact of this very `cargo test` invocation,
/// not the operator's installed Python CLI. The needles below match a quoted `cao` program name,
/// which that form does not contain. Spawning the thing cargo just built is the opposite of a
/// hermeticity problem. (#321)
fn cao_spawn_needles() -> BTreeMap<String, &'static str> {
    let mut needles = BTreeMap::new();
    let cao = format!("c{}o", "a");

    for binary in [
        cao.clone(),
        format!("{cao}-server"),
        format!("{cao}-mcp-server"),
    ] {
        needles.insert(
            format!("new(\"{binary}"),
            "spawning the installed CAO CLI reaches the operator's real machine state: its \
             config, its database, and its running sessions",
        );
        needles.insert(
            format!("[\"{binary}\""),
            "an argv slice whose program is a real CAO binary is the same spawn in a different \
             shape",
        );
    }
    needles
}

/// Is `path` exempt?
fn is_exempt(path: &str) -> bool {
    EXEMPTIONS.iter().any(|(exempt, _, _)| *exempt == path)
}

/// Is `path` the one production module that owns the transport? See [`HTTP_OWNER`].
///
/// Distinct from [`is_exempt`]: this relaxes **only** the network vocabulary, and only for one
/// named path. The `cao`-spawn needles apply to it unchanged.
fn is_http_owner(path: &str) -> bool {
    path == HTTP_OWNER.0
}

/// One violation found by the scan: what was attempted, where, and why it is forbidden.
///
/// BR-10 requires a tripwire failure to **name what was attempted**. A bare "hermeticity
/// violation" leaves the developer hunting through a test file for something they cannot see, so
/// the needle, the path, the line number and the reason all travel together.
#[derive(Debug, PartialEq, Eq)]
struct Violation {
    path: String,
    line_number: usize,
    needle: String,
    reason: &'static str,
}

impl Violation {
    /// The operator-facing message, naming the attempt (BR-10).
    fn message(&self) -> String {
        format!(
            "HERMETICITY VIOLATION: {path}:{line} uses {needle:?} — {reason}.\n  \
             Tests must not reach real HTTP or spawn a real `cao` binary (BR-8, SR-5): a test \
             that does passes or fails on machine state rather than on the change under review.\n  \
             There is exactly ONE exemption, `{exempt_path}` ({exempt_unit}), and adding a \
             second is a reviewable design change, not a local decision (BR-9, INV-3).",
            path = self.path,
            line = self.line_number,
            needle = self.needle,
            reason = self.reason,
            exempt_path = EXEMPTIONS[0].0,
            exempt_unit = EXEMPTIONS[0].1,
        )
    }
}

/// The tripwire predicate: scan one source for forbidden vocabulary.
///
/// Factored out from the tests so it can be pointed at a **synthetic hostile fixture** and
/// observed to fire (VR-5). An untested tripwire is an assumption, and the only way to test one
/// without committing a genuinely non-hermetic test is to run the detector over source text that
/// is not a real test target. See [`the_tripwire_actually_fires_on_a_hostile_fixture`].
///
/// Applies **every** needle, with no notion of an exemption — that is the callers' job, and
/// keeping it out of here is what lets [`the_one_exemption_is_load_bearing_and_would_otherwise_fire`]
/// and [`the_http_owner_entry_is_load_bearing`] point this at their own supposedly-exempt files
/// and observe that it *would* have fired. A `scan` that consulted the exemption set could not be
/// used to prove an exemption is needed. (#321)
fn scan(path: &str, source: &str) -> Vec<Violation> {
    let mut all_needles = http_needles();
    all_needles.extend(cao_spawn_needles());

    scan_with(path, source, &all_needles)
}

/// Scans `source` for `needles` only.
///
/// The seam that makes [`HTTP_OWNER`] a *narrow* relaxation rather than a blanket one: the owner
/// is scanned with [`cao_spawn_needles`] alone, so a `Command::new("cao")` there still fires
/// while `minreq::get` does not. A single boolean "skip this file" — the shape [`is_exempt`] has
/// — would have relaxed both halves at once, and the half that matters most in that module is the
/// subprocess half (ADR-02, BR-2). (#321)
fn scan_with(path: &str, source: &str, needles: &BTreeMap<String, &'static str>) -> Vec<Violation> {
    let mut violations = Vec::new();
    let all_needles = needles;

    for (index, raw_line) in source.lines().enumerate() {
        let line = strip_comment(raw_line);

        for (needle, reason) in all_needles {
            if line.contains(needle) {
                violations.push(Violation {
                    path: path.to_string(),
                    line_number: index + 1,
                    needle: needle.clone(),
                    reason,
                });
            }
        }
    }

    violations
}

/// Test 1 — **no non-exempt source reaches real HTTP or spawns a real `cao`** (BR-8, SR-5).
///
/// The tripwire proper. Eleven of the thirteen sources are scanned with every needle and no
/// relaxation at all. The two that are not are each accounted for by its own test below rather
/// than waved through:
///
/// - `tests/endpoint_contract.rs` — the one [`EXEMPTIONS`] member, skipped entirely because it
///   makes genuinely live calls by design.
/// - `src/server.rs` — the [`HTTP_OWNER`], scanned with the **`cao`-spawn needles only**. It is
///   not skipped: a subprocess spawn there still fires, which is the half that matters most in
///   the crate's one I/O module (ADR-02, BR-2).
#[test]
fn no_non_exempt_source_reaches_real_http_or_spawns_cao() {
    let mut violations = Vec::new();

    for (path, source) in SOURCES {
        if is_exempt(path) {
            continue;
        }
        if is_http_owner(path) {
            // Narrowed, not skipped. See `HTTP_OWNER`.
            violations.extend(scan_with(path, source, &cao_spawn_needles()));
            continue;
        }
        violations.extend(scan(path, source));
    }

    assert!(
        violations.is_empty(),
        "{}",
        violations
            .iter()
            .map(Violation::message)
            .collect::<Vec<_>>()
            .join("\n\n")
    );
}

/// Test 1b — **only `server-client` names an HTTP client, and that is BR-1 made executable.**
///
/// The converse of [`HTTP_OWNER`], and the assertion the tripwire could not make before Bolt 3:
/// BR-1 says one unit owns all HTTP and **no other unit opens a connection**. Until a production
/// module did HTTP at all, that held trivially. Now it is checked.
///
/// So the relaxation added for `src/server.rs` is paid for twice over: test 1 still scans every
/// other production module with the full needle set, and this test asserts the owner is *the
/// only* one — a `minreq::get` appearing in `renderer` or `guided-flow` fails here even though
/// each of those would also fail test 1. The redundancy is deliberate: the failure message from
/// this test names the requirement, which is what a developer needs to know.
#[test]
fn only_the_http_owner_names_an_http_client() {
    let (owner_path, owner_unit, owner_why) = HTTP_OWNER;

    assert!(
        owner_why.len() > 40,
        "the HTTP owner must carry a reason a reviewer can evaluate, not a bare path"
    );
    assert_eq!(
        owner_unit, "server-client",
        "the HTTP owner must NAME its unit, for the reason BR-9 gives about the exemption set: an \
         unnamed relaxation is indistinguishable from a gap"
    );

    // Named `http_naming_modules` and not `production_with_http`: this file scans ITSELF, and the
    // `http:` needle fires on a colon immediately after the scheme — so a variable named
    // `production_with_http:` in a type annotation or a `{..:?}` interpolation trips the guard.
    // Found by running it, not by review. The needle's own doc comment explains why it is spelt
    // without slashes; this is the other edge of the same bluntness, and renaming is the right fix
    // because the alternative would be loosening a catch-all that exists to cover clients the
    // list does not name. (#321)
    let http_naming_modules: Vec<&str> = SOURCES
        .iter()
        .filter(|(path, _)| path.starts_with("src/"))
        .filter(|(_, source)| !scan_with("probe", source, &http_needles()).is_empty())
        .map(|(path, _)| *path)
        .collect();

    assert_eq!(
        http_naming_modules,
        vec![owner_path],
        "EXACTLY ONE production module may name an HTTP client — `{owner_path}` \
         ({owner_unit}) — because BR-1 states this unit owns ALL of the crate's HTTP and no \
         other unit opens a connection. `guided-flow` populates pickers THROUGH it; \
         `results-pane` receives a stream HANDED to it. Found: {http_naming_modules:?}"
    );
}

/// Test 2 — **the exemption set has exactly one member** (BR-9, INV-3, SR-5).
///
/// The countability requirement, made executable. `1` is a hard-coded literal, not
/// `EXEMPTIONS.len()` compared with itself, so appending a second row turns this red. That is
/// the point: BR-9 makes a second exemption a design change requiring review, and the only way
/// to enforce "requiring review" from inside a test suite is to make the change fail loudly.
#[test]
fn the_exemption_set_has_exactly_one_member() {
    assert_eq!(
        EXEMPTIONS.len(),
        1,
        "the tripwire has exactly ONE exemption (BR-9, INV-3). Found {}: {:?}. A second \
         exemption is a design change requiring review — if a new unit genuinely needs live \
         HTTP, that is a conversation, not an appended row",
        EXEMPTIONS.len(),
        EXEMPTIONS
            .iter()
            .map(|(p, u, _)| (p, u))
            .collect::<Vec<_>>()
    );

    let (path, unit, why) = EXEMPTIONS[0];
    assert_eq!(
        path, "tests/endpoint_contract.rs",
        "the one exemption is the endpoint contract test"
    );
    assert_eq!(
        unit, "skeleton-endpoint-verify",
        "the exemption must NAME the unit it belongs to (BR-9); an unnamed exemption is \
         indistinguishable from a gap in the tripwire"
    );
    assert!(
        why.len() > 40,
        "the exemption must carry a reason a reviewer can evaluate, not a bare path"
    );

    assert!(
        SOURCES.iter().any(|(scanned, _)| *scanned == path),
        "the exempt file must still be in the scan set, so that removing its exemption is all \
         it takes to start enforcing on it. An exemption for an unscanned file is a hole \
         wearing an exemption's clothes"
    );
}

/// Test 3 — **the exemption is load-bearing, not decorative** (VR-6).
///
/// If `endpoint_contract.rs` stopped making live HTTP calls, the exemption would be stale — and
/// a stale exemption is how the set grows without anybody noticing, because nothing distinguishes
/// it from a needed one. So this asserts the exempt file **does** contain the HTTP the tripwire
/// would otherwise reject, and that removing the exemption would therefore change the outcome.
///
/// This is also the structural half of "verify the exemption works": the behavioural half is
/// that `endpoint_contract.rs`'s four tests still pass under the same `cargo test` run as this
/// file. A broken exemption blocks an affirmed walking-skeleton deliverable, so it fails visibly
/// in the same command rather than needing a separate ritual.
#[test]
fn the_one_exemption_is_load_bearing_and_would_otherwise_fire() {
    let (path, unit, _) = EXEMPTIONS[0];

    let source = SOURCES
        .iter()
        .find(|(scanned, _)| *scanned == path)
        .map(|(_, source)| *source)
        .unwrap_or_else(|| panic!("{path} must be in SOURCES"));

    let violations = scan(path, source);

    assert!(
        !violations.is_empty(),
        "{path} ({unit}) is exempt because it makes REAL HTTP calls, but the scan finds none. \
         Either it stopped calling the live server — in which case NFR-7 is no longer covered \
         and the exemption must be REMOVED — or the needles stopped matching it, in which case \
         the tripwire has quietly stopped detecting HTTP everywhere else too"
    );

    let needles: Vec<&str> = violations.iter().map(|v| v.needle.as_str()).collect();
    assert!(
        needles.contains(&format!("min{}", "req").as_str()),
        "the exempt file must still use the crate's HTTP client; found {needles:?}"
    );

    // BR-10: the message names the attempt. Asserted on a real violation rather than a
    // hand-built one, so the formatting cannot drift away from the data.
    let message = violations[0].message();
    assert!(
        message.contains(path) && message.contains(&violations[0].line_number.to_string()),
        "a violation message must name the file AND the line so the developer is not left \
         hunting (BR-10): {message}"
    );
    assert!(
        message.contains(unit),
        "the message must name the one exempt unit, so a developer who hits the tripwire learns \
         that an exemption exists and is not tempted to invent a second one silently: {message}"
    );
}

/// Test 3b — **the [`HTTP_OWNER`] entry is load-bearing, and its narrowing really is narrow.**
///
/// Held to the same two standards as the exemption above, because a relaxation nobody re-checks
/// is how a guard rots:
///
/// 1. **Load-bearing.** `src/server.rs` must actually contain the HTTP vocabulary the full scan
///    would reject. If it stopped — the unit was deleted, or the client swapped for something the
///    needles do not name — this entry is stale and must be removed, *or* the needle set has
///    quietly stopped detecting HTTP everywhere else too.
/// 2. **Narrow.** The `cao`-spawn needles must still fire on it. Proven against a synthetic
///    hostile fixture scanned exactly as test 1 scans the owner — `scan_with(.., cao_spawn_needles())`
///    — so this asserts the real code path rather than a re-expression of it. A relaxation that
///    silenced both halves would leave the crate's one I/O module as the single place a CLI
///    fallback could be added unchallenged, which is precisely what ADR-02 and BR-2 forbid and
///    what FR-1.4 calls the defect being removed.
#[test]
fn the_http_owner_entry_is_load_bearing() {
    let (owner_path, owner_unit, _) = HTTP_OWNER;

    let source = SOURCES
        .iter()
        .find(|(scanned, _)| *scanned == owner_path)
        .map(|(_, source)| *source)
        .unwrap_or_else(|| panic!("{owner_path} must be in SOURCES"));

    // 1. Load-bearing: the full scan WOULD fire on it.
    let would_fire = scan(owner_path, source);
    assert!(
        !would_fire.is_empty(),
        "{owner_path} ({owner_unit}) is the HTTP owner because it genuinely performs production \
         HTTP, but the full scan finds none. Either the unit no longer does HTTP — in which case \
         this entry must be REMOVED — or the needles stopped matching, in which case the tripwire \
         has quietly stopped detecting HTTP everywhere else too"
    );
    let needles: Vec<&str> = would_fire.iter().map(|v| v.needle.as_str()).collect();
    assert!(
        needles.contains(&format!("min{}", "req").as_str()),
        "the owner must still name the crate's HTTP client; found {needles:?}"
    );

    // 2. Narrow: the cao-spawn half still fires there, via the exact call test 1 makes.
    let cao = format!("c{}o", "a");
    let hostile_fallback = format!(
        "fn fallback() {{\n    Command::new(\"{cao}\").args([\"profile\", \"list\"]).output();\n}}"
    );
    let spawn_violations = scan_with(owner_path, &hostile_fallback, &cao_spawn_needles());
    assert!(
        !spawn_violations.is_empty(),
        "the HTTP owner's relaxation must cover the NETWORK needles only: a `{cao}` spawn in \
         {owner_path} must still fire, because ADR-02/BR-2 forbid subprocess execution there \
         above all — that module is where a CLI fallback would be written, and FR-1.4 calls a \
         fallback the defect being removed rather than a resilience feature"
    );
    assert_eq!(
        spawn_violations[0].line_number, 2,
        "the violation must point at the offending line (BR-10)"
    );

    // And the owner's own source is clean under that same narrowed scan — which is what test 1
    // relies on. Asserted here too so the reason for a test-1 failure is legible.
    assert!(
        scan_with(owner_path, source, &cao_spawn_needles()).is_empty(),
        "{owner_path} must spawn no `{cao}` binary: {:?}",
        scan_with(owner_path, source, &cao_spawn_needles())
    );
}

/// Test 4 — **the tripwire actually fires** (VR-5).
///
/// An untested tripwire is an assumption. The detector is run over synthetic hostile source —
/// not a real test target, so nothing non-hermetic is committed to prove this — and must report
/// a violation for each of the two things it exists to block.
///
/// The fixtures are assembled from fragments for the same reason the needles are: written out
/// longhand they would sit in this file's own scanned body and trip
/// [`no_non_exempt_source_reaches_real_http_or_spawns_cao`].
#[test]
fn the_tripwire_actually_fires_on_a_hostile_fixture() {
    let client = format!("min{}", "req");
    let cao = format!("c{}o", "a");

    let hostile_http = format!(
        "fn sneaky() {{\n    let _ = {client}::get(\"http{}//127.0.0.1:9889/health\").send();\n}}",
        ":"
    );
    let hostile_spawn =
        format!("fn sneaky() {{\n    Command::new(\"{cao}\").arg(\"launch\").output();\n}}");

    for (label, fixture) in [("http", &hostile_http), ("cao spawn", &hostile_spawn)] {
        let violations = scan("tests/fixture_not_a_real_target.rs", fixture);
        assert!(
            !violations.is_empty(),
            "the tripwire must FIRE on a deliberate {label} attempt, otherwise it is an \
             assumption rather than a guard (VR-5). Fixture:\n{fixture}"
        );
        assert_eq!(
            violations[0].line_number, 2,
            "the violation must point at the offending line, not the top of the file (BR-10)"
        );
    }

    // And it must NOT fire on hermetic source, or it would be deleted for crying wolf. The
    // CARGO_BIN_EXE form is the specific case that must stay clean: it spawns the binary cargo
    // just built, which is an artifact of this test run, not the operator's installed CLI.
    let benign = format!(
        "fn fine() {{\n    Command::new(env!(\"CARGO_BIN_EXE_{cao}-tui\")).output();\n    \
         let json = r#\"{{\"components\":{{\"{cao}\":\"ok\"}}}}\"#;\n}}"
    );
    let benign_violations = scan("tests/fixture_not_a_real_target.rs", &benign);
    assert!(
        benign_violations.is_empty(),
        "the tripwire must not fire on the crate's own CARGO_BIN_EXE binary or on `{cao}` \
         appearing as JSON test data (src/types.rs:617 has exactly that). A guard that cries \
         wolf gets deleted: {benign_violations:?}"
    );
}

/// Test 5 — **every needle is findable in stripped code** (the anti-vacuous check).
///
/// A needle that cannot match is a guard that cannot fire, and it is invisible to review: the
/// list looks thorough, every test is green, and nothing is being checked. The concrete trap here
/// is real — [`strip_comment`] truncates at the first `//`, so a needle written as the full `http://`
/// would match nothing, ever. This test plants each needle in a synthetic code line and asserts
/// the scan finds it.
#[test]
fn every_needle_is_actually_findable_in_stripped_code() {
    let mut all = http_needles();
    all.extend(cao_spawn_needles());

    assert!(
        all.len() >= 16,
        "expected at least 16 needles (10 network + 2 shapes x 3 cao binaries); found {}. A \
         shrinking needle set is how coverage is lost quietly",
        all.len()
    );

    for (needle, reason) in &all {
        assert!(
            !needle.contains("//"),
            "needle {needle:?} contains `//`, which `strip_comment` truncates at — it can never \
             match stripped code, so it is a guard that cannot fire"
        );
        assert!(
            !reason.is_empty(),
            "needle {needle:?} must carry the reason it is forbidden (BR-10)"
        );

        let planted = format!("let x = {needle};");
        let found = scan("tests/fixture_not_a_real_target.rs", &planted);
        assert!(
            found.iter().any(|v| &v.needle == needle),
            "needle {needle:?} was not found in a line that plainly contains it — the scan \
             cannot detect what it claims to detect"
        );

        // And it must be invisible inside a comment, or prose about hermeticity would trip the
        // guard and every doc comment in the crate would become a hazard.
        let commented = format!("// prose mentioning {needle} harmlessly");
        assert!(
            scan("tests/fixture_not_a_real_target.rs", &commented).is_empty(),
            "needle {needle:?} fired inside a COMMENT; prose naming the forbidden vocabulary \
             must be safe or nobody can document this guard"
        );
    }
}

/// Test 6 — **the scan set covers every source in the crate.**
///
/// The cost of `include_str!` over a directory walk is that a new file must be listed. This
/// count assertion is what turns forgetting into a failing test rather than a silent blind spot:
/// an unlisted file is not scanned, and an unscanned file is where the next real HTTP call
/// lands.
///
/// The number is a literal for the usual reason — `SOURCES.len()` compared against itself proves
/// nothing.
///
/// # The count was never enough, and #556 demonstrated it
///
/// A count reddens when a file is **added** to this list and stays green when one is **omitted**,
/// which is the wrong direction: the unlisted file is the unscanned one. That asymmetry was
/// documented on the `catalog.rs` entry in [`SOURCES`] and left open here, while
/// `no_backend_attach_call.rs` closed it by cross-checking `src/main.rs`'s `mod` declarations. The
/// consequence arrived on schedule: `mod theme;` landed, that tripwire went red and this one did
/// not, so `src/theme.rs` was outside the HTTP-ownership scan while this test reported full
/// coverage. The cross-check below is that repair — a `mod` declaration with no [`SOURCES`] entry
/// is now a failing test, so the next module cannot be forgotten the same way. (#556)
#[test]
fn the_scan_set_covers_every_source_in_the_crate() {
    assert_eq!(
        SOURCES.len(),
        18,
        "expected 18 Rust sources: 11 under src/ (main, error, handoff, types, env_guard, catalog, \
         results_pane, server, guided_flow, renderer, theme) and 7 under tests/ \
         (binary_exits_zero, endpoint_contract, no_backend_attach_call, pty, pty_harness/mod, \
         hermeticity_tripwire, no_colour_literal_outside_theme). A new file must be added to \
         SOURCES or the tripwire silently stops covering it"
    );

    let production = SOURCES
        .iter()
        .filter(|(path, _)| path.starts_with("src/"))
        .count();
    assert_eq!(production, 11, "11 production sources");

    let test_sources = SOURCES
        .iter()
        .filter(|(path, _)| path.starts_with("tests/"))
        .count();
    assert_eq!(test_sources, 7, "7 test sources");

    // Every `mod` declared by the crate root must be listed. This is the half the count cannot
    // do — see the note above on why `theme.rs` went unscanned. Derived from the declarations
    // rather than from a second literal, so it closes the omission direction instead of
    // restating the addition one.
    let (_, crate_root) = SOURCES
        .iter()
        .find(|(path, _)| *path == "src/main.rs")
        .expect("src/main.rs must be listed in SOURCES for the mod cross-check to run");

    let mut declared = 0;
    for line in crate_root.lines() {
        let trimmed = line.trim();
        // `pub mod`/`pub(crate) mod` are handled so this does not quietly stop matching if a
        // module's visibility changes. An inline `mod x { .. }` is not a separate file and is
        // skipped; today every declaration in the root is a file.
        let Some(rest) = trimmed
            .strip_prefix("mod ")
            .or_else(|| trimmed.strip_prefix("pub mod "))
            .or_else(|| trimmed.strip_prefix("pub(crate) mod "))
        else {
            continue;
        };
        let Some(module) = rest.strip_suffix(';') else {
            continue;
        };

        declared += 1;
        let expected = format!("src/{module}.rs");
        assert!(
            SOURCES.iter().any(|(path, _)| *path == expected),
            "`src/main.rs` declares `mod {module};` but {expected} is not in SOURCES, so this \
             tripwire does not scan it. An unlisted module is a silent hole: the count assertion \
             above only fires when a file is ADDED to the list, never when one is omitted"
        );
    }

    assert_eq!(
        declared, 10,
        "expected 10 `mod` declarations in src/main.rs (every production source except main.rs \
         itself). If this is 0 the loop above matched nothing and its assertion is vacuous"
    );

    // No duplicate paths: a duplicated entry would inflate the count above and let a real file
    // go unlisted while the assertion still passed.
    let mut seen = std::collections::BTreeSet::new();
    for (path, source) in SOURCES {
        assert!(seen.insert(*path), "{path} is listed twice in SOURCES");
        assert!(
            !source.is_empty(),
            "{path} embedded as empty — an empty source scans clean and proves nothing"
        );
    }

    assert!(
        seen.contains("tests/hermeticity_tripwire.rs"),
        "the tripwire must scan ITSELF, or it becomes the one file in the crate where real HTTP \
         could be introduced unchallenged"
    );
}

/// Test 7 — **what this tripwire cannot detect**, asserted rather than merely documented.
///
/// An overstated guard is worse than a narrow one: it stops anyone from adding the guard that
/// would actually have caught the thing. So the known gaps are written as executable
/// demonstrations — each fixture below is genuinely non-hermetic and the scan genuinely misses
/// it. If a later change closes one of these gaps, this test goes red and the limitation is
/// removed from the docs deliberately rather than the docs drifting into a lie.
///
/// The gaps, all of them consequences of this being a **static text scan**:
///
/// 1. **A runtime-assembled string.** Splitting a client name across a `format!` defeats the
///    needles — the same technique this file uses on itself.
/// 2. **A transitive dependency's network access.** The scan sees this crate's source, not what
///    a crate it calls does. `cargo-deny`'s `bans` list in `rust-ci` is the control there.
/// 3. **An indirect subprocess.** `sh -c 'cao launch'`, or any spawn whose program comes from a
///    variable, has no `cao` literal in a program position. `pty.rs` legitimately spawns
///    `/bin/sh` seven times, so forbidding shells outright is not available to this guard.
/// 4. **Filesystem and clock non-hermeticity.** Reading `~/.cao/`, or depending on wall-clock
///    time, are hermeticity failures this guard says nothing about. It is scoped to BR-8's two
///    named hazards: real HTTP and real `cao` spawns.
#[test]
fn what_this_tripwire_cannot_detect() {
    let cao = format!("c{}o", "a");

    // Gap 1: a runtime-assembled client name.
    let assembled = "let name = format!(\"{}{}\", \"min\", \"req\"); dynamic_call(&name);";
    assert!(
        scan("tests/fixture_not_a_real_target.rs", assembled).is_empty(),
        "gap 1 has CLOSED: the scan now catches runtime-assembled names. Update this test and \
         the module docs — a guard documented as narrower than it is invites redundant work"
    );

    // Gap 3: an indirect subprocess. The program is a shell; the CAO invocation is an argument.
    let indirect = format!("Command::new(\"/bin/sh\").args([\"-c\", \"{cao} launch\"]).output();");
    assert!(
        scan("tests/fixture_not_a_real_target.rs", &indirect).is_empty(),
        "gap 3 has CLOSED: the scan now catches shell-indirected CAO spawns. Update this test \
         and the module docs"
    );

    // Gap 4: filesystem non-hermeticity, entirely outside this guard's scope.
    let filesystem = "let config = std::fs::read_to_string(\"/Users/someone/.cao/config.yaml\");";
    assert!(
        scan("tests/fixture_not_a_real_target.rs", filesystem).is_empty(),
        "gap 4 has CLOSED: the scan now catches filesystem reads. Update this test and the docs"
    );

    // Gap 2 cannot be demonstrated with a fixture — it is about what OTHER crates do, which is
    // outside the source text by definition. Asserted structurally instead: nothing in the scan
    // consults the dependency graph, so `Cargo.toml` is deliberately not in SOURCES. The
    // dependency-graph control lives in `rust-ci` (cargo-deny bans, and the no-FFI check).
    assert!(
        !SOURCES.iter().any(|(path, _)| path.ends_with(".toml")),
        "gap 2: this guard scans Rust source, not the dependency graph. If Cargo.toml enters \
         SOURCES the claim in the docs must change"
    );
}

/// **The crate root declares `forbid(unsafe_code)` unindented — and `deny` FAILS this.**
///
/// This test lives HERE, not in `src/main.rs`, and the placement is the entire point. The original
/// guard sat inside `main.rs` and compared against a needle **literal in the same file** that
/// `include_str!` embedded, so a `forbid` -> `deny` edit changed the attribute **and the needle in
/// lock-step** and the test still passed. Measured: replacing all 3 occurrences in `main.rs` left
/// `crate_root_forbids_unsafe_code ... ok`. The conductor had recorded that mutation as FAILING; it
/// does not reproduce. Found by the §12a reviewer for `skeleton-crate`.
///
/// From a separate file the needle cannot co-mutate: an edit to `main.rs` moves the haystack while
/// this literal stays put. The second assertion is the one the old test lacked — it rejects `deny`
/// by name, because `deny` is locally overridable by an inner `#[allow(unsafe_code)]`, which is the
/// silent creep the affirmed rule exists to prevent. (#321)
#[test]
fn the_crate_root_forbids_unsafe_code_and_deny_is_not_accepted() {
    let crate_root = SOURCES
        .iter()
        .find(|(path, _)| *path == "src/main.rs")
        .map(|(_, source)| *source)
        .expect("src/main.rs must be listed in SOURCES");

    assert!(
        crate_root
            .lines()
            .any(|line| line == "#![forbid(unsafe_code)]"),
        "expected an unindented `#![forbid(unsafe_code)]` at the crate root of src/main.rs"
    );

    assert!(
        !crate_root
            .lines()
            .any(|line| line == "#![deny(unsafe_code)]"),
        "the crate root declares `#![deny(unsafe_code)]` instead of `forbid`. `deny` can be \
         locally overridden by `#[allow(unsafe_code)]`, so it does not satisfy the affirmed rule"
    );
}
