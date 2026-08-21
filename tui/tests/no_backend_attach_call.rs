//! Test 3 of `skeleton-handoff-proof`: **neither backend's `attach_session` is called from this
//! crate, ever** (BR-1, INV-2, SR-3, and the structural half of FR-5.1). Issue #321.
//!
//! # This is a safety property, not a style rule
//!
//! CAO's two backends each expose an `attach_session`, and both are unusable for a hand-off —
//! for *asymmetric* reasons, each re-read at source rather than taken from its docstring:
//!
//! | Backend | Body | Effect on a TUI that called it |
//! |---|---|---|
//! | tmux (`backends/tmux_backend.py:131-135`) | `subprocess.run([...])` | **Blocks until the operator detaches**, freezing the event loop |
//! | herdr (`backends/herdr_backend.py:631`) | `os.execvp` | **Replaces the process image** — the TUI is gone |
//!
//! The tmux docstring claims the method "replaces current process". **It does not** — the body
//! is `subprocess.run`. Quoting that docstring as fact was a real error earlier in this intent,
//! corrected across six artifacts. The lesson is in this file's own method: it asserts against
//! **source text**, never against a comment about it.
//!
//! # Why a source-text assertion rather than a type
//!
//! FR-5.1 — "the TUI process is still alive after hand-off" — **is not expressible in the type
//! system**. `os.execvp` type-checks perfectly, and so does any Rust equivalent. Nothing about a
//! signature distinguishes a call that returns from one that never does, so the only available
//! guard is a check on what the source contains. That is precisely why the predecessor's defect
//! was possible.
//!
//! # What this test does NOT prove
//!
//! It proves this crate contains no call to either attach helper, and no direct spawn of the
//! attach verb. It does **not** prove end-to-end that a live TUI process survives a real
//! hand-off — that needs `server-client` (Bolt 3) and `renderer` (Bolt 5) to exist so there is
//! a real process to observe. Stated here so nobody reads a green run as the full FR-5.1
//! discharge (VR-1 remains outstanding).

use std::collections::BTreeMap;

/// Every Rust source file in this crate, embedded at compile time.
///
/// `include_str!` rather than a `walkdir` over `src/`: a filesystem walk depends on the working
/// directory a runner happens to use, and — worse — **a walk that silently finds no files would
/// pass**. Embedding makes a missing file a *compile* error, so the guard cannot degrade into a
/// vacuous pass. The cost is that a newly added module must be listed here; the count assertion
/// below is what turns that from a silent gap into a failing test. (#321)
const SOURCES: &[(&str, &str)] = &[
    ("src/main.rs", include_str!("../src/main.rs")),
    ("src/error.rs", include_str!("../src/error.rs")),
    ("src/handoff.rs", include_str!("../src/handoff.rs")),
    ("src/types.rs", include_str!("../src/types.rs")),
    // Added by `safety-guards` (Bolt 2) along with the module itself. Listing it here is not
    // optional bookkeeping: the count assertion below reddens when a module is ADDED to this
    // list, but nothing reddens when a new module is left OUT of it — so an unlisted file is a
    // silent hole, which is precisely what this happened to be until it was caught. (#321)
    ("src/env_guard.rs", include_str!("../src/env_guard.rs")),
    // Added by `command-catalog` (Bolt 3) along with the module itself, because the
    // `mod`-declaration cross-check below made omitting it a FAILING test rather than a silent
    // hole — which is the asymmetry the comment above was written about, now closed in the
    // direction it named. (#321)
    ("src/catalog.rs", include_str!("../src/catalog.rs")),
    // Added by `results-pane` (Bolt 4). Listing it is load-bearing for this guard specifically,
    // not just for the cross-check: the pane is what renders a hand-off's *refusal*, so it is the
    // module where somebody trying to be helpful would most plausibly "just run the
    // attach-session command for them" instead of printing the argv for the operator to copy.
    // That is exactly the process-replacing call `project.md`'s TUI run-policy forbids, and this
    // file is the guard that would catch it. (#321)
    (
        "src/results_pane.rs",
        include_str!("../src/results_pane.rs"),
    ),
    // Added by `server-client` (Bolt 3). This is the module that most needed listing: it is the
    // crate's only I/O component, so it is the one place a `CommandExt::exec` or an
    // `attach-session` spawn could plausibly be written by somebody wiring a hand-off through
    // HTTP. The `mod`-declaration cross-check below is what made omitting it fail rather than
    // pass silently. (#321)
    ("src/server.rs", include_str!("../src/server.rs")),
    // Added by `guided-flow` (Bolt 4). Load-bearing for this guard in its own right: this is the
    // module that decides whether a launch may proceed, so it is where somebody would most
    // plausibly follow `to_params()` straight through to "and then attach the operator to the
    // session" — the process-replacing call `project.md`'s TUI run-policy forbids. The
    // `mod`-declaration cross-check below is what made omitting it fail rather than pass. (#321)
    ("src/guided_flow.rs", include_str!("../src/guided_flow.rs")),
    // Added by `renderer` (Bolt 5). **This is the module this whole tripwire was written for.**
    // Every other listing above is defence in depth; this one is the primary target: `renderer`
    // is the unit that performs the hand-off, so it is where the forbidden call would actually be
    // written. `project.md`'s TUI run-policy mandates a NEW window and both Python backends
    // violate it by construction (`tmux_backend.attach_session` blocks, `herdr_backend`'s
    // `os.execvp`s), so the correct path here is `HandoffDriver::handoff` — and the incorrect one
    // is one `Command::new` away. Budgeted at ZERO occurrences of every needle, including the
    // verb: `renderer` receives the refusal argv as an opaque `Option<String>` from `Refused` and
    // hands it straight to `ResultsPane::refuse`, so it never names, builds, or spawns it. (#321)
    ("src/renderer.rs", include_str!("../src/renderer.rs")),
    // Added by the semantic colour layer (#556). Listed for coverage, not because a palette is a
    // plausible place to spawn tmux — it is the least likely module in the crate. That is the
    // reason to list it rather than an argument against: the `mod`-declaration cross-check below
    // makes every `src/` file mandatory precisely so nobody has to be right about which files are
    // "interesting", and the one judged uninteresting is where an unscanned hole would sit. Adding
    // this entry was in fact FORCED by that cross-check, which reddened the moment `main.rs`
    // declared `mod theme;` — the asymmetry the `env_guard` comment above describes, working. (#556)
    ("src/theme.rs", include_str!("../src/theme.rs")),
];

/// Strips `//`-comments so the needles named in prose are not counted as code.
///
/// `//` covers `///` and `//!` too.
///
/// # Why this is not `line.find("//")`
///
/// It used to be, and the claim justifying it — "no string literal in this crate contains `//`" —
/// **was false**: `src/server.rs:421` builds `format!("http://{host}:{port}")`. Cutting at the
/// first `//` anywhere in a line therefore truncated real code at the `//` inside a URL, so any
/// forbidden needle sitting after a URL literal on the same line would have been hidden from the
/// scan. The stale reasoning was the actual defect: it read as a considered trade-off, so the
/// weakening was invisible. (Reported by review on PR #547.)
///
/// The obvious repair — strip only lines whose trimmed form *starts* with `//` — fails in the
/// other direction, and that direction is worse. A trailing comment such as
/// `let x = 1; // never call attach_session` would then survive into the scanned text and fire
/// the tripwire on prose. This crate has 22 such trailing `#[allow(..)] // reason` comments, so
/// that is not hypothetical.
///
/// So the scan tracks whether it is inside a string literal and only treats `//` as a comment
/// when it is not. That is enough for this crate's syntax and no more: raw strings (`r"…"`,
/// `r#"…"#`) are handled by the same quote tracking because none of them span a line containing a
/// `//` sequence, and block comments (`/* … */`, 2 occurrences, both whole-line) never carry a
/// needle. It remains deliberately not a full lexer — but the assumption it rests on is now
/// asserted by [`the_stripper_keeps_code_after_a_url_literal`] rather than merely stated.
fn code_only(source: &str) -> String {
    source
        .lines()
        .map(strip_line_comment)
        .collect::<Vec<_>>()
        .join("\n")
}

/// Returns `line` up to a `//` that is not inside a string literal.
fn strip_line_comment(line: &str) -> &str {
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

/// The stripper keeps code that follows a URL literal, and still strips trailing comments.
///
/// Both directions, because the two plausible implementations each fail one of them: cutting at
/// the first `//` truncates at the `//` in `http://` and hides code behind it, while stripping
/// only whole-line comments lets a trailing `// … attach_session` comment fire the tripwire on
/// prose. A test asserting only one direction would license the other bug.
///
/// The first case is taken from real code — `src/server.rs` builds `http://{host}:{port}` — which
/// is what made the previous implementation's stated assumption false. (#321)
#[test]
fn the_stripper_keeps_code_after_a_url_literal() {
    let verb = attach_verb();
    // The URL scheme is ASSEMBLED, never written contiguously. `hermeticity_tripwire.rs` forbids
    // a plaintext `http:` in any non-exempt test source as its catch-all for an unnamed HTTP
    // client, and it caught the first draft of this test doing exactly that (3 violations). The
    // guard is right and the fixture was wrong: a literal here would be indistinguishable from a
    // test that really does reach the network. Same technique the needles above use on themselves.
    let scheme = format!("http{}//", ':');

    // 1. Code after a URL literal SURVIVES. Under the old `find("//")` this returned
    //    `    let u = format!("http:` and the trailing call vanished with it.
    let with_url = format!(r#"    let u = format!("{scheme}{{host}}"); host.run(&{verb});"#);
    assert!(
        strip_line_comment(&with_url).contains(&verb),
        "a needle after a URL literal must remain visible to the scan, or the tripwire can be \
         evaded by putting the forbidden call on the same line as a URL. Got: {:?}",
        strip_line_comment(&with_url)
    );

    // 2. A trailing comment is still STRIPPED, so prose cannot fire the guard.
    let commented = format!("    let x = 1; // never call {verb}");
    assert!(
        !strip_line_comment(&commented).contains(&verb),
        "a needle inside a trailing comment must be stripped, or the tripwire fires on prose. \
         Got: {:?}",
        strip_line_comment(&commented)
    );

    // 3. Doc comments and whole-line comments, the bulk of this crate's prose.
    assert_eq!(strip_line_comment(&format!("/// {verb} is forbidden")), "");
    assert_eq!(strip_line_comment(&format!("    // {verb}")), "    ");

    // 4. An escaped quote must not be mistaken for the end of a literal — otherwise the parser
    //    would think it had left the string and strip the rest of a line as a comment.
    let escaped = r#"    let s = "a \" b"; let t = 1;"#;
    assert_eq!(strip_line_comment(escaped), escaped);

    // 5. A `//` inside a string is not a comment; a real comment after that string still is.
    let both = format!(r#"    let u = "{scheme}x"; let y = 2; // {scheme}z"#);
    assert_eq!(
        strip_line_comment(&both),
        format!(r#"    let u = "{scheme}x"; let y = 2; "#)
    );

    // 6. A line with no comment at all is returned untouched.
    let plain = "    let x = 1;";
    assert_eq!(strip_line_comment(plain), plain);
}

/// The forbidden needles, each with the reason it is forbidden.
///
/// Assembled from **three** spellings rather than one, because a single needle is easy to slip
/// past by accident:
///
/// - `attach_session` — the Python method name, as it would appear in a comment, a subprocess
///   argument, or a Rust helper named after it.
/// - `attach-session` — the tmux **verb**. Spawning it directly is the same defect wearing a
///   different costume: from inside tmux it fails outright (tmux refuses a nested attach), and
///   from outside it blocks the TUI until the operator detaches. BR-4 forbids it as navigation.
/// - `execvp` / `exec_replace` — the mechanism itself. `std::os::unix::process::CommandExt::exec`
///   replaces the process image exactly as herdr's `os.execvp` does, and would be the natural
///   Rust translation of that line by someone porting it faithfully.
///
/// Each is split at construction so **this array does not contain its own needles** as
/// contiguous text. Without that, the comment-stripped body of this file would contain them and
/// the guard would fire on itself — a guard that always fails is as useless as one that never
/// can. (#321)
fn forbidden_needles() -> BTreeMap<String, &'static str> {
    let mut needles = BTreeMap::new();
    needles.insert(
        format!("attach{}session", "_"),
        "tmux's blocks the TUI's event loop until detach (subprocess.run, tmux_backend.py:131); \
         herdr's replaces the TUI process image (os.execvp, herdr_backend.py:631)",
    );
    needles.insert(
        format!("attach{}session", "-"),
        "the tmux attach verb must never be spawned as navigation (BR-4): nested attach fails \
         outright, and from outside tmux it blocks until the operator detaches. It may only \
         appear as a STRING the operator is handed to run themselves",
    );
    needles.insert(
        format!("exec{}", "vp"),
        "replacing the process image is the exact defect herdr's attach_session has; the TUI \
         must outlive every hand-off (FR-5.1)",
    );
    needles.insert(
        format!("CommandExt{}", "::exec"),
        "std::os::unix::process::CommandExt::exec is Rust's os.execvp — it replaces the process \
         image and never returns (FR-5.1)",
    );
    // **The idiomatic spelling, which the needle above does NOT catch.** Nobody writes
    // `CommandExt::exec(&mut cmd)`; they write `use std::os::unix::process::CommandExt;` and then
    // `cmd.exec()`. Probed against this needle set before the fix: that two-line form produced
    // ZERO hits, so the one mechanism this file exists to forbid was reachable by writing it the
    // way a Rust programmer actually would.
    //
    // The needle is the TRAIT NAME, not `.exec(`. Two reasons, both from trying the alternatives:
    // `.exec(` is too broad (any method named `exec` on any type trips it), while the trait name
    // is precise — importing `CommandExt` has exactly one purpose in this crate's context, and
    // `exec` is the only reason to reach for it. It fires on the `use` line, which is the right
    // place: the import is the reviewable decision.
    // (Reported by review on PR #547.)
    needles.insert(
        format!("Command{}", "Ext"),
        "importing std::os::unix::process::CommandExt brings `.exec()` into scope, and `.exec()` \
         replaces the process image exactly as herdr's os.execvp does. The TUI must outlive every \
         hand-off (FR-5.1), so the trait has no legitimate use here — this catches the idiomatic \
         `use CommandExt; cmd.exec()` spelling that the qualified-path needle misses",
    );
    // **tmux accepts abbreviated commands**, so `attach` alone is the same call as
    // `attach-session` — verified: `tmux attach -t X` is the documented short form. The
    // `attach-session` needle above therefore covers only the long spelling, and spawning the
    // short one is the identical defect (nested attach fails; from outside tmux it blocks until
    // the operator detaches).
    //
    // Spelt as the ARGV FRAGMENT `"attach"` rather than the bare word, so it fires on a spawn
    // rather than on prose. `attach_argv`'s legitimate `attach-session` string does not match this
    // needle, and the word "attach" in a doc comment is stripped before the scan.
    // (Reported by review on PR #547.)
    needles.insert(
        format!("\"att{}\"", "ach"),
        "tmux accepts abbreviations, so `attach` is `attach-session` — spawning it is the same \
         defect BR-4 forbids, wearing a shorter name",
    );
    needles
}

/// The one file permitted to name the tmux attach **verb** at all: the module that builds the
/// FR-5.3 refusal string.
///
/// The exemption is narrow by construction — it covers one file and one needle, and
/// [`the_attach_verb_is_only_ever_a_printed_string`] then accounts for every occurrence inside
/// it. An exemption that arises because the tripwire happens not to cover a path is
/// indistinguishable from a hole in the tripwire, which is why it is enumerated rather than
/// implied. (#321)
const REFUSAL_ARGV_FILE: &str = "src/handoff.rs";

/// Every file permitted to name the attach verb, with the **exact** occurrences allowed in each
/// region: `(path, production, tests, why)`.
///
/// A budget rather than a bare allow-list, because "this file may mention the verb" is not the
/// property worth enforcing — *how many times, and on which side of `#[cfg(test)]`* is. A file
/// budgeted at zero production occurrences is still fully guarded: adding one fires
/// [`the_attach_verb_is_only_ever_a_printed_string`] even though the file is exempt from the
/// blanket scan.
///
/// `results_pane.rs` was added by `results-pane` (Bolt 4) and is budgeted at **0 production**.
/// It renders FR-5.3's refusal argv but never builds one — the string is passed in by `renderer`
/// as an opaque `Option<String>` — so its only occurrences are the two hard-coded test
/// expectations that assert the argv is reproduced byte-for-byte. Those must stay literals:
/// deriving them from the value handed to `refuse()` would make the test agree with whatever the
/// pane stored and stay green through the exact mangling it exists to catch. (#321)
const VERB_BUDGET: &[(&str, usize, usize, &str)] = &[
    (
        REFUSAL_ARGV_FILE,
        1,
        2,
        "the display-only `attach_argv` builder, plus VR-5's two hard-coded expectations",
    ),
    (
        "src/results_pane.rs",
        0,
        2,
        "renders the refusal argv it is GIVEN and never builds one, so production must never \
         name the verb; the two test occurrences are FR-5.3's byte-for-byte expectations",
    ),
];

/// The tmux attach verb, assembled so this file's own code does not contain it contiguously.
fn attach_verb() -> String {
    format!("attach{}session", "-")
}

/// The `attach_session` methods, and process replacement, appear in **no** Rust source here.
///
/// Three of the four needles have **zero** legitimate uses anywhere in this crate, so they are
/// checked with no exemption at all. The fourth — the tmux verb — is exempted only in
/// [`REFUSAL_ARGV_FILE`], and is then fully accounted for by the next test rather than waved
/// through. (#321)
#[test]
fn no_rust_source_calls_either_backend_attach_session() {
    // Cross-checked against the module declarations in `src/main.rs` rather than only against a
    // literal. A bare count cannot notice a module that was never listed — it only reddens once
    // somebody adds one — and that asymmetry let `env_guard` sit unscanned after Bolt 2 added it.
    // Deriving the expected set from `mod` declarations closes the direction the count misses.
    // (#321)
    assert_eq!(
        SOURCES.len(),
        11,
        "expected exactly 11 Rust sources under src/ (main, error, handoff, types, env_guard, \
         catalog, results_pane, server, guided_flow, renderer, theme); a new module must be added \
         to SOURCES or this tripwire silently stops covering it"
    );

    let crate_root = SOURCES
        .iter()
        .find(|(path, _)| *path == "src/main.rs")
        .map(|(_, source)| code_only(source))
        .expect("src/main.rs must be listed in SOURCES");

    for line in crate_root.lines() {
        let declaration = line.trim();
        // `pub mod` and `pub(crate) mod` are matched too. `strip_prefix("mod ")` alone missed
        // them, so a module declared `pub mod foo;` was invisible to this cross-check and could
        // be omitted from SOURCES with nothing to say so — the exact silent hole the assertion
        // exists to close, reachable by a one-word edit. Every module in this crate happens to be
        // private today, which is why the gap was never observed. (Reported by review on PR #547.)
        let declaration = declaration
            .strip_prefix("pub(crate) ")
            .or_else(|| declaration.strip_prefix("pub "))
            .unwrap_or(declaration);
        let Some(module) = declaration
            .strip_prefix("mod ")
            .and_then(|rest| rest.strip_suffix(';'))
        else {
            continue;
        };
        // A `mod foo { .. }` inline module has no file, so it is skipped rather than demanded.
        // Reached only if someone writes one in `main.rs`; today none exists.
        if module.contains('{') || module.contains(' ') {
            continue;
        }
        let expected = format!("src/{module}.rs");
        assert!(
            SOURCES.iter().any(|(path, _)| *path == expected),
            "`src/main.rs` declares `mod {module};` but {expected} is not in SOURCES, so this \
             guard does not scan it. An unlisted module is a silent hole: the count assertion \
             above only fires when a file is ADDED to the list, never when one is omitted"
        );
    }

    let verb = attach_verb();

    for (path, source) in SOURCES {
        let code = code_only(source);

        for (needle, reason) in forbidden_needles() {
            if needle == verb {
                // Accounted for per-occurrence and per-region by the next test, for the budgeted
                // files only. Every other needle, and every other file, is checked with no
                // exemption at all.
                if VERB_BUDGET.iter().any(|(budgeted, ..)| budgeted == path) {
                    continue;
                }
            }

            assert!(
                !code.contains(&needle),
                "{path} must not contain {needle:?}: {reason}"
            );
        }
    }
}

/// Every occurrence of the attach verb is a **printed string**, never something this crate runs.
///
/// This is what keeps the exemption above honest: without it, the exemption would license
/// `handoff.rs` to spawn `tmux attach-session` — the very call BR-1 forbids — while the previous
/// test stayed green.
///
/// # The accounting is per-region, and that is the point
///
/// The file is split at its `#[cfg(test)]` marker and each side gets its own exact count, rather
/// than one loose total. A single total is what a stray production use would hide behind: adding
/// one in production while deleting one from the tests would keep any total-only assertion
/// green.
///
/// - **Production: exactly 1.** The display-only builder `attach_argv`. Zero would mean FR-5.3's
///   command was dropped and this exemption is stale.
/// - **Tests: exactly 2.** The two hard-coded expectations VR-5 *requires* — `work:planner-1`
///   for the `$TMUX`-unset refusal and the bare `work` session-level fallback. They must stay
///   literals: deriving them from `attach_argv` would make those tests agree with the builder
///   and stay green through the exact typo they exist to catch.
///
/// Every budgeted file is checked this way, not just `handoff.rs` — see [`VERB_BUDGET`].
/// `results_pane.rs` is budgeted at **0 production**, which is what makes its exemption from the
/// blanket scan safe: the pane renders an argv it is handed and must never name one itself, so a
/// production occurrence appearing there fires here.
///
/// # The assertion that actually holds BR-1, and why the obvious one did not
///
/// The obvious check is "no line naming the verb also spawns". **It is not sufficient, and that
/// was found by mutation rather than by reasoning.** Injecting the real defect —
/// `self.host.run(&attach_argv(&target))` on the refusal path — left this test **green**, because
/// the spawn site names `attach_argv`, not the verb. The verb sits one function away.
///
/// So the check is inverted to follow the *builder* instead of the string: every call to
/// `attach_argv` must be wrapped in `render_argv(&attach_argv(..))`, the display-only path. A call
/// reaching `Host::run` or a `Command` cannot satisfy that shape. The line-level check is kept as
/// a cheap second net, but the builder-wrapping assertion is the one with teeth. (#321)
#[test]
fn the_attach_verb_is_only_ever_a_printed_string() {
    let verb = attach_verb();
    let test_marker = format!("#[cfg({})]", "test");

    // Every budgeted file, region by region. This is the loop that makes a zero-production budget
    // meaningful: an exemption from the blanket scan is only safe because the exact count is
    // asserted here instead.
    for (path, allowed_production, allowed_tests, why) in VERB_BUDGET {
        let source = SOURCES
            .iter()
            .find(|(scanned, _)| scanned == path)
            .map(|(_, source)| code_only(source))
            .unwrap_or_else(|| panic!("{path} must be listed in SOURCES to be budgeted"));

        let (production, test_code) = source.split_once(&test_marker).unwrap_or_else(|| {
            panic!("{path} must carry a #[cfg(test)] module for the region split to mean anything")
        });

        assert_eq!(
            production.matches(&verb).count(),
            *allowed_production,
            "{path} may name the attach verb exactly {allowed_production} time(s) in PRODUCTION \
             code ({why}). A production occurrence beyond the budget is how the verb reaches a \
             process instead of the operator's clipboard (BR-1, BR-4)"
        );
        assert_eq!(
            test_code.matches(&verb).count(),
            *allowed_tests,
            "{path} may name the attach verb exactly {allowed_tests} time(s) in TEST code ({why})"
        );
    }

    let handoff = SOURCES
        .iter()
        .find(|(path, _)| *path == REFUSAL_ARGV_FILE)
        .map(|(_, source)| code_only(source))
        .expect("src/handoff.rs must be listed in SOURCES");

    let (production, test_code) = handoff.split_once(&test_marker).expect(
        "src/handoff.rs must carry a #[cfg(test)] module for the split below to mean anything",
    );

    assert_eq!(
        production.matches(&verb).count(),
        1,
        "the attach verb may appear exactly ONCE in {REFUSAL_ARGV_FILE}'s production code — \
         inside the display-only argv builder for the FR-5.3 refusal. Zero means the refusal no \
         longer offers a command; more than one means a second use is hiding behind the exemption"
    );
    assert_eq!(
        test_code.matches(&verb).count(),
        2,
        "exactly TWO hard-coded expectations may name the attach verb (VR-5): the \
         `work:planner-1` refusal and the session-level `work` fallback. They must stay literals \
         — deriving them from `attach_argv` would make the tests agree with the builder"
    );

    let builder = "fn attach_argv";
    assert!(
        production.contains(builder),
        "the refusal argv must be built by `{builder}`, a named display-only helper, so its one \
         permitted use is reviewable in a single place"
    );

    // The load-bearing assertion: follow the BUILDER, not the string. Every `attach_argv(` call
    // outside its own definition must be wrapped by `render_argv(&attach_argv(`, which is the
    // display-only path — a call that reached `Host::run` or a `Command` could not match this
    // shape. Found necessary by mutation: a `self.host.run(&attach_argv(&target))` injected on
    // the refusal path left the line-level check below green, because the spawn site names the
    // builder rather than the verb.
    let builder_call = "attach_argv(";
    let display_wrapped = format!("render_argv(&{builder_call}");

    let total_calls = production.matches(builder_call).count();
    let definition_occurrences = production.matches(builder).count();
    let wrapped_calls = production.matches(&display_wrapped).count();

    assert!(
        total_calls > definition_occurrences,
        "`attach_argv` must be CALLED somewhere in production, otherwise FR-5.3's refusal no \
         longer offers a command at all"
    );
    assert_eq!(
        wrapped_calls,
        total_calls - definition_occurrences,
        "every call to `attach_argv` must be wrapped as `{display_wrapped}..)` — the \
         display-only path. Found {total_calls} occurrences, {definition_occurrences} of which \
         is the definition, but only {wrapped_calls} wrapped for display. An unwrapped call is \
         how the attach argv reaches a process instead of the operator's clipboard, which is \
         exactly BR-1: tmux's attach blocks the TUI until detach, herdr's replaces its process \
         image, and a nested attach fails outright (BR-4)"
    );

    // A cheap second net: naming the verb on the same line as a spawner. Weaker than the check
    // above (a spawn one function away slips past it), kept because it costs nothing.
    for (index, line) in handoff.lines().enumerate() {
        if !line.contains(&verb) {
            continue;
        }
        for spawner in [".run(", "Command::new", "spawn(", "status()", "output()"] {
            assert!(
                !line.contains(spawner),
                "line {number} names the attach verb AND {spawner:?}: the verb may only ever be \
                 built into a string for the operator to run themselves (BR-1/BR-4). Line: \
                 {line:?}",
                number = index + 1
            );
        }
    }

    assert!(
        !handoff.contains("sh -c") && !handoff.contains("bash -c"),
        "no shell may be invoked anywhere in the hand-off path: session and window names come \
         from server responses, so a shell string would make them injection vectors (T-10, SR-1)"
    );
}

/// **Every needle is findable in stripped code, and invisible inside a comment.**
///
/// The anti-vacuous check this file lacked. `hermeticity_tripwire.rs` has had one all along
/// (`every_needle_is_actually_findable_in_stripped_code`) for a reason its own docs give: a needle
/// that cannot match is a guard that cannot fire, and it is invisible to review — the list looks
/// thorough, every test is green, and nothing is being checked.
///
/// Its absence here was not theoretical. Two mechanisms this file exists to forbid were reachable,
/// and both were verified by probing the needle set directly before the fix:
///
/// 1. **`use CommandExt; cmd.exec()`** — the *idiomatic* spelling, and the one anybody porting
///    herdr's `os.execvp` would write. The `CommandExt::exec` needle matches only the
///    fully-qualified call nobody writes. Zero hits.
/// 2. **`tmux attach -t X`** — tmux accepts abbreviations, so this is `attach-session`. The
///    `attach-session` needle matches only the long spelling. Zero hits.
///
/// So this test does both halves: each needle must fire on a line that plainly contains it, and
/// must NOT fire inside a comment — otherwise documenting this guard would trip it, which is how a
/// tripwire gets deleted. (Reported by review on PR #547.)
#[test]
fn every_needle_is_findable_in_stripped_code_and_inert_in_a_comment() {
    let needles = forbidden_needles();

    assert!(
        needles.len() >= 6,
        "expected at least 6 needles: two attach spellings (long and abbreviated), two exec \
         spellings (qualified path and trait import), and the two argv shapes. Found {}. A \
         shrinking needle set is how coverage is lost quietly",
        needles.len()
    );

    for (needle, reason) in &needles {
        assert!(
            !needle.contains("//"),
            "needle {needle:?} contains `//`, which `strip_line_comment` cuts at outside a string \
             literal — it could never match, so it is a guard that cannot fire"
        );
        assert!(
            reason.len() > 30,
            "needle {needle:?} must carry a reason a reviewer can evaluate, not a label"
        );

        let planted = format!("let x = {needle};");
        assert!(
            code_only(&planted).contains(needle),
            "needle {needle:?} was not found in a line that plainly contains it — the scan cannot \
             detect what it claims to detect"
        );

        let commented = format!("// prose mentioning {needle} harmlessly");
        assert!(
            !code_only(&commented).contains(needle),
            "needle {needle:?} survived comment stripping; prose naming the forbidden vocabulary \
             must be safe or nobody can document this guard"
        );
    }
}

/// **The two evasions found by review are caught**, asserted on the real needle set.
///
/// A regression test for the coverage gap itself rather than for a source file: these are the exact
/// code shapes that produced zero hits before the needles were extended. Written as synthetic
/// snippets because the whole point is that no file in the crate contains them — so there is
/// nothing to scan, and the property has to be checked against the needle set directly.
///
/// The `attach_argv` control at the end is what keeps this from being a one-way ratchet: the
/// legitimate display-only builder must NOT match the new abbreviation needle, or the tripwire
/// would fire on the very string FR-5.3 requires it to produce.
#[test]
fn the_idiomatic_exec_and_the_abbreviated_attach_verb_are_both_caught() {
    let needles = forbidden_needles();
    let caught = |snippet: &str| {
        let code = code_only(snippet);
        needles.keys().any(|needle| code.contains(needle))
    };

    assert!(
        caught("use std::os::unix::process::CommandExt;"),
        "the CommandExt IMPORT must be caught: `use CommandExt; cmd.exec()` is how process \
         replacement is actually written, and the qualified-path needle misses it entirely"
    );
    assert!(
        caught("Command::new(\"tmux\").args([\"attach\", \"-t\", target]).status();"),
        "the ABBREVIATED tmux attach verb must be caught: tmux accepts abbreviations, so `attach` \
         is `attach-session` and spawning it is the identical defect"
    );

    // Controls. Both must be caught for the same reason they always were.
    assert!(
        caught("Command::new(\"tmux\").args([\"attach-session\", \"-t\", t]).status();"),
        "the long spelling must still be caught"
    );
    assert!(
        caught("std::os::unix::process::CommandExt::exec(&mut cmd);"),
        "the qualified path must still be caught"
    );

    // And the NEGATIVE control: the legitimate refusal-string builder must not match the new
    // abbreviation needle. A guard that fires on FR-5.3's own output would be deleted within a day.
    assert!(
        !needles
            .keys()
            .any(|needle| needle == &format!("\"att{}\"", "ach")
                && code_only("[\"tmux\", \"attach-session\", \"-t\", target]").contains(needle)),
        "the abbreviation needle must not match the long-form `attach-session` string that \
         `attach_argv` legitimately builds — that string is budgeted and accounted for elsewhere"
    );
}
