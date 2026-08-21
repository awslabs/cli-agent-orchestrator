//! **`src/theme.rs` is the only module permitted to name a `Color`** (#556).
//!
//! # What this guard is for
//!
//! A semantic colour layer whose rule is a convention is a colour layer that decays. The first
//! call site that writes `Color::Yellow` inline because it is quicker than adding a role does not
//! break anything, so nothing objects; the tenth one has made the palette unfindable and
//! `NO_COLOR` a lie, because `from_env` cannot switch a literal. This test is what makes the rule
//! fail a build.
//!
//! It also enforces the palette's two shape rules, which are not about ownership at all:
//!
//! - **ANSI-16 only** (FR-2.3). `Color::Rgb` and `Color::Indexed` are scanned for over the
//!   **whole** crate, test regions included, because a 24-bit colour hardcodes a value the
//!   operator's own terminal palette cannot override — and a test asserting one is a fixture
//!   pinning a behaviour the production palette must never have.
//! - **No `Black`, no `White`** (FR-2.4). They invert between light and dark terminals, so
//!   whichever one is chosen is unreadable on half the terminals in use.
//!
//! # The guard is worthless without its anti-vacuity floor, and that is not a figure of speech
//!
//! Every ownership and shape check in this file is an **absence** check, and every one of them
//! passes on a crate with no colour anywhere — which is what this crate was before #556. Run this
//! file against the previous commit and it is green. That makes it indistinguishable, on its own,
//! from a test that does nothing.
//!
//! [`the_theme_module_actually_defines_a_palette`] is the repair: `src/theme.rs`'s production
//! region must name at least [`MINIMUM_THEME_COLOURS`] colours. With it, emptying `theme.rs`,
//! deleting the palette, or reducing `colour()` to `monochrome()` is a red test rather than a
//! green one.
//!
//! # What this cannot detect
//!
//! A static text scan, with the same limits the crate's other two tripwires document:
//!
//! - A colour reached through an alias — `use ratatui::style::Color as C;` then `C::Red` — is
//!   invisible here. `theme.rs`'s own value-level tests cover the palette from the other side, and
//!   between them the plausible routes are covered; neither alone is sufficient.
//! - A `Style` built from a colour computed at runtime.
//! - A colour written by a dependency. `ratatui`'s own widget defaults are outside this scan by
//!   construction, which is one reason `results_pane.rs` renders a borderless `Block::new()`
//!   rather than a themed one.
//!
//! These are stated rather than papered over: an overstated guard is worse than a narrow one,
//! because it discourages the guard that would actually have caught the thing.

/// Every Rust source in the crate, embedded at compile time.
///
/// `include_str!` rather than a directory walk, following the precedent set by
/// `no_backend_attach_call.rs` and `hermeticity_tripwire.rs`: a walk depends on the runner's
/// working directory and — worse — a walk that silently found no files would **pass**. Embedding
/// makes a missing file a *compile* error, so the scan cannot degrade into a vacuous pass.
///
/// The cost is that a new file must be listed. That cost is paid by
/// [`the_scan_set_covers_every_module_the_crate_root_declares`], which derives the expected set
/// from `src/main.rs`'s `mod` declarations rather than from a hand-maintained count — because a
/// count only reddens when a file is **added** and stays green when one is **omitted**, and the
/// omitted file is the unscanned one. That exact asymmetry left `src/theme.rs` outside
/// `hermeticity_tripwire.rs`'s scan for the length of this feature's first task. (#556)
const SOURCES: &[(&str, &str)] = &[
    ("src/main.rs", include_str!("../src/main.rs")),
    ("src/error.rs", include_str!("../src/error.rs")),
    ("src/handoff.rs", include_str!("../src/handoff.rs")),
    ("src/types.rs", include_str!("../src/types.rs")),
    ("src/env_guard.rs", include_str!("../src/env_guard.rs")),
    ("src/catalog.rs", include_str!("../src/catalog.rs")),
    (
        "src/results_pane.rs",
        include_str!("../src/results_pane.rs"),
    ),
    ("src/server.rs", include_str!("../src/server.rs")),
    ("src/guided_flow.rs", include_str!("../src/guided_flow.rs")),
    ("src/renderer.rs", include_str!("../src/renderer.rs")),
    ("src/theme.rs", include_str!("../src/theme.rs")),
];

/// The one module allowed to name a colour.
const THEME_OWNER: &str = "src/theme.rs";

/// The lower bound on colours named in [`THEME_OWNER`]'s production region.
///
/// Six, because the palette has six roles and each names one. A floor rather than an equality:
/// pinning the exact count would redden on a legitimate refactor — a `const` shared between the
/// two themes, say — which trains people to edit the number instead of reading the test. The
/// property that matters is "the palette is not empty", and a floor states exactly that.
///
/// Deliberately **not** derived from `theme::ROLE_COUNT`. An integration test is its own crate and
/// cannot see a private item, but the more important reason is that a fixture sourced from the
/// value under test cannot fail: if `theme.rs` were emptied, a derived floor would fall to zero
/// alongside it and this test would stay green through the exact deletion it exists to catch.
const MINIMUM_THEME_COLOURS: usize = 6;

/// How much of a file a forbidden-variant needle applies to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scope {
    /// Production **and** test regions. For a variant with no legitimate use anywhere.
    WholeFile,
    /// Production regions only. For a variant a test may legitimately *name* in order to forbid
    /// it, or to enumerate a set it belongs to.
    ProductionOnly,
}

/// The forbidden `Color` variants and how far each needle reaches.
///
/// # Why the scope is per-needle, and why it is not all `WholeFile`
///
/// The design for this guard specified a whole-crate scan for all four, reasoning that "a
/// `Color::Rgb` in a test is a fixture asserting a behaviour the production palette must never
/// have". That reasoning is sound for `Rgb` and `Indexed` and **wrong** for `Black` and `White`,
/// which was found by writing the guard and watching it fail: it fired on three lines in
/// `src/theme.rs`'s own test module, and all three were correct code.
///
/// - `theme.rs`'s ANSI-16 allow-set is a hard-coded list of all sixteen variants, and `Black` and
///   `White` *are* ANSI-16 colours. Omitting them to satisfy this scan would make the allow-set
///   wrong — it would then reject a legitimately-ANSI colour — and the list must stay a literal,
///   since deriving it from the theme would make it unable to fail.
/// - `theme.rs`'s FR-2.4 test asserts no role uses `Black` or `White`. It cannot assert that
///   without naming them. A guard that fires on the test enforcing the same rule one layer down is
///   a guard that gets deleted, and the deletion would be the reviewer's correct call.
///
/// `Rgb` and `Indexed` have no such legitimate mention: nothing needs to enumerate them and
/// nothing needs to forbid them by name, so they stay `WholeFile`, where a test fixture pinning a
/// 24-bit colour is caught.
///
/// The generalisation — "scan test regions unless a test has a reason to name the needle" — is the
/// one this crate's other tripwires already reached from the other direction, with per-region
/// budgets rather than blanket bans. (#556)
const FORBIDDEN_VARIANTS: &[(&str, Scope, &str)] = &[
    (
        "Color::Rgb",
        Scope::WholeFile,
        "a 24-bit colour hardcodes a value the operator's terminal palette cannot override, and \
         it degrades unpredictably over ssh, tmux and screen (FR-2.3)",
    ),
    (
        "Color::Indexed",
        Scope::WholeFile,
        "a 256-colour index is resolved against a palette this crate cannot see; indices 16-255 \
         are not the operator's chosen theme (FR-2.3)",
    ),
    (
        "Color::Black",
        Scope::ProductionOnly,
        "Black is invisible on a dark terminal. Color::Reset is the default-foreground role \
         (FR-2.4)",
    ),
    (
        "Color::White",
        Scope::ProductionOnly,
        "White is invisible on a light terminal. Color::Reset is the default-foreground role \
         (FR-2.4)",
    ),
];

// ── The comment stripper ──────────────────────────────────────────────────────────────────────

/// `source` with every line comment removed. `//` covers `///` and `//!` too.
///
/// # Why this is not `line.find("//")`
///
/// Because that was tried, in this crate, and it was wrong. `no_backend_attach_call.rs` records
/// the history: the justification "no string literal in this crate contains `//`" was **false**,
/// since `src/server.rs` builds a URL with `format!`, so cutting at the first `//` truncated real
/// code mid-literal and hid anything after it from the scan. The stale reasoning was the defect,
/// because it read as a considered trade-off.
///
/// The obvious repair — strip only lines whose trimmed form *starts* with `//` — fails in the
/// other and worse direction: a trailing `let x = 1; // never write Color::Red` would survive into
/// the scanned text and fire this guard **on prose about itself**. This crate carries dozens of
/// `#[allow(..)] // reason` comments, and this feature's own modules discuss `Color::` at length,
/// so that is not hypothetical — it is guaranteed.
///
/// So the scan tracks whether it is inside a string literal and treats `//` as a comment only
/// when it is not. Deliberately not a full lexer: raw strings and block comments are out of
/// scope, and [`the_stripper_survives_a_literal_containing_a_comment_marker`] pins the assumption
/// that makes that acceptable instead of merely asserting it in prose.
///
/// Copied rather than shared, because an integration test is its own crate and cannot import from
/// a sibling test target. That is the crate's established answer — `hermeticity_tripwire.rs`
/// carries its own copy for the same reason.
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
            // An escape inside a literal consumes the next byte, so `\"` does not end it.
            b'\\' if in_string => index += 1,
            b'"' => in_string = !in_string,
            b'/' if !in_string && bytes.get(index + 1) == Some(&b'/') => return &line[..index],
            _ => {}
        }
        index += 1;
    }
    line
}

/// Splits a source into its production and test regions at the `#[cfg(test)]` marker.
///
/// # Why a missing marker PANICS rather than falling back to the whole file
///
/// A whole-file fallback would look harmless and be the opposite. This module's ownership check
/// deliberately scans production only, because a test asserting `Color::Cyan` is legitimate and a
/// production line writing it is not. If the marker were missing and the split silently returned
/// the whole file, the scan would start reading test code as production and fire on a correct
/// test — and the fix a hurried reader would reach for is to delete the assertion.
///
/// The mirror failure is worse: were the fallback the other way round (no marker → scan nothing),
/// a module with no test module would be scanned not at all. That is precisely where an unchecked
/// colour would sit, since a module nobody tested is a module nobody read.
///
/// So neither fallback. `no_backend_attach_call.rs` panics at the same point for the same reason.
///
/// # `split_once`, not `rsplit_once`
///
/// `src/renderer.rs` contains five occurrences of the marker string: one real attribute and four
/// inside doc comments and string literals discussing the split. Cutting at the **first** is
/// correct — it is the real attribute at `src/renderer.rs:2348`, and everything after it is test
/// code by definition. Cutting at the last would put four test modules' worth of code into the
/// production region and fire this guard on their fixtures. (#556)
fn production_region<'a>(path: &str, code: &'a str) -> &'a str {
    let marker = "#[cfg(test)]";
    let (production, _tests) = code.split_once(marker).unwrap_or_else(|| {
        panic!(
            "{path} contains no `{marker}` marker, so this guard cannot tell its production code \
             from its tests.\n\
             \n\
             This is a hard failure on purpose. Falling back to scanning the whole file would \
             fire on legitimate test fixtures, and falling back to scanning nothing would leave \
             the module with no tests — the likeliest home for an unreviewed colour — entirely \
             uncovered. Add a `#[cfg(test)] mod tests` to {path}, or add {path} to an explicit \
             exemption with a reason."
        )
    });
    production
}

// ── The tests ─────────────────────────────────────────────────────────────────────────────────

/// **FR-1.4: no production module except `src/theme.rs` names a `Color`.**
///
/// Steps 1-4 of design A-2. Two needles rather than one: `rustfmt` cannot emit `Color :: Red`, but
/// a hand-edit between two `cargo fmt` runs can, and the second needle costs one line.
#[test]
fn no_production_module_outside_the_theme_names_a_colour() {
    let mut offenders = Vec::new();

    for (path, source) in SOURCES {
        // The owner is the point of the exercise, not an exception to it.
        if *path == THEME_OWNER {
            continue;
        }

        let code = code_only(source);
        let production = production_region(path, &code);

        for (number, line) in production.lines().enumerate() {
            for needle in ["Color::", "Color ::"] {
                if line.contains(needle) {
                    offenders.push(format!("{path}:{} — {}", number + 1, line.trim()));
                }
            }
        }
    }

    assert!(
        offenders.is_empty(),
        "these production lines name a colour outside `{THEME_OWNER}`:\n  {}\n\
         \n\
         Colour belongs to the theme so that the palette is one reviewable file and `NO_COLOR` can \
         switch it. A literal at a call site cannot be switched, which makes it an accessibility \
         regression and not merely an untidy one. Add or reuse a semantic role — \
         `theme.focus`, `theme.required`, `theme.ok`, `theme.error`, `theme.warn`, `theme.dim` — \
         and write `theme.error` here instead (FR-1.4).",
        offenders.join("\n  ")
    );
}

/// **FR-2.3 / FR-2.4: no file names `Rgb`, `Indexed`, `Black` or `White`.**
///
/// Steps 5-8 of design A-2, with the per-needle scope [`FORBIDDEN_VARIANTS`] explains. `theme.rs`
/// is **not** exempt from this check — unlike the ownership check, this one is about which colours
/// may exist rather than about where they may be written, and the owner is the module most able to
/// introduce a bad one.
///
/// Comments are stripped throughout: this file, `theme.rs`, and the spec all discuss these
/// variants by name, and a guard that fires on its own documentation is a guard people delete.
#[test]
fn no_file_uses_a_non_ansi_or_inverting_colour() {
    let mut offenders = Vec::new();

    for (path, source) in SOURCES {
        let code = code_only(source);
        let production = production_region(path, &code);

        for (needle, scope, reason) in FORBIDDEN_VARIANTS {
            let scanned = match scope {
                Scope::WholeFile => code.as_str(),
                Scope::ProductionOnly => production,
            };

            for (number, line) in scanned.lines().enumerate() {
                if line.contains(needle) {
                    offenders.push(format!(
                        "{path}:{} uses {needle} — {reason}\n      {}",
                        number + 1,
                        line.trim()
                    ));
                }
            }
        }
    }

    assert!(
        offenders.is_empty(),
        "forbidden colour variants found:\n  {}",
        offenders.join("\n  ")
    );
}

/// The `ProductionOnly` scope is **load-bearing**, not a convenience.
///
/// An exemption nobody checks is an exemption that outlives its reason. This pins both halves:
/// `Black` and `White` genuinely do appear in `theme.rs`'s test region — so narrowing their scope
/// was necessary and is not dead configuration — and they genuinely do **not** appear in any
/// production region, so the narrowing costs no coverage where it matters.
///
/// If a refactor removes those test mentions, this test reddens and the scope should be widened
/// back to `WholeFile` deliberately. That is the opposite of how a stale exemption normally dies:
/// silently, still granted, protecting nothing. (#556)
#[test]
fn the_production_only_scope_is_load_bearing() {
    let narrowed: Vec<&str> = FORBIDDEN_VARIANTS
        .iter()
        .filter(|(_, scope, _)| *scope == Scope::ProductionOnly)
        .map(|(needle, _, _)| *needle)
        .collect();

    assert_eq!(
        narrowed.len(),
        2,
        "expected exactly 2 production-only needles (Black, White). Adding a third is a reviewable \
         widening of what tests may name, not a local decision. Found: {narrowed:?}"
    );

    let (_, theme_source) = SOURCES
        .iter()
        .find(|(path, _)| *path == THEME_OWNER)
        .unwrap_or_else(|| panic!("{THEME_OWNER} must be listed in SOURCES"));
    let code = code_only(theme_source);
    let production = production_region(THEME_OWNER, &code);
    let tests = &code[production.len()..];

    for needle in &narrowed {
        assert!(
            tests.contains(needle),
            "{needle} is scoped ProductionOnly but no longer appears in {THEME_OWNER}'s test \
             region, so the narrowing protects nothing. Widen it back to Scope::WholeFile"
        );
        assert!(
            !production.contains(needle),
            "{needle} appears in {THEME_OWNER}'s PRODUCTION region. The narrowed scope exists so \
             tests may NAME these variants in order to forbid them — not so production may use one"
        );
    }
}

/// **The anti-vacuity floor. Read the module docs before touching this.**
///
/// Every other assertion in this file passes on a crate containing no colour at all, so without
/// this one the guard is green before the feature exists and stays green if the feature is
/// deleted. This is the assertion that makes the file mean something.
///
/// # It counts DISTINCT variants, and the first version did not
///
/// This originally counted `Color::` *occurrences*. A T-3 mutation — rewriting every colour in
/// `theme.rs` to `Color::Reset` — left **12** occurrences in the production region, cleared a floor
/// of 6, and this test stayed **green** through the exact deletion the docstring above claims it
/// catches: `colour()` reduced to `monochrome()`. Seven tests elsewhere caught it, so the property
/// was never unguarded, but the file's own stated floor was not holding it.
///
/// Counting distinct variants fixes that: six roles collapsed onto one hue is one variant, not six.
/// A monochrome `colour()` therefore reads as `1 < 6` and reddens here, where a reader looking for
/// the floor will find it.
#[test]
fn the_theme_module_actually_defines_a_palette() {
    let (_, source) = SOURCES
        .iter()
        .find(|(path, _)| *path == THEME_OWNER)
        .unwrap_or_else(|| panic!("{THEME_OWNER} must be listed in SOURCES"));

    let code = code_only(source);
    let production = production_region(THEME_OWNER, &code);

    // The variant name is everything up to the first non-identifier character after `Color::`, so
    // `Color::Cyan)` and `Color::Cyan,` are one variant and not two spellings of it.
    let mut variants: Vec<&str> = production
        .match_indices("Color::")
        .map(|(at, needle)| {
            let rest = &production[at + needle.len()..];
            let end = rest
                .find(|c: char| !c.is_alphanumeric() && c != '_')
                .unwrap_or(rest.len());
            &rest[..end]
        })
        .collect();
    variants.sort_unstable();
    variants.dedup();
    let colours = variants.len();

    assert!(
        colours >= MINIMUM_THEME_COLOURS,
        "{THEME_OWNER}'s production region names {colours} DISTINCT colours ({variants:?}); at \
         least {MINIMUM_THEME_COLOURS} are required, one per semantic role.\n\
         \n\
         This is the anti-vacuity floor. Every other assertion in this file is an ABSENCE check, \
         and all of them pass on a crate with no colour anywhere — so if the palette were emptied, \
         or `colour()` were reduced to `monochrome()`, this is the assertion that notices. It \
         counts DISTINCT variants for that reason: six roles rewritten to a single hue leaves \
         plenty of `Color::` occurrences and would clear an occurrence-based floor. If you are \
         here because a refactor legitimately shrank the palette, lower the floor deliberately and \
         say why; do not delete the check."
    );
}

/// **Every `mod` the crate root declares is in [`SOURCES`].**
///
/// A count assertion cannot do this job. It reddens when a file is **added** to the list and stays
/// green when one is **omitted**, and it is the omitted file that goes unscanned. That is not a
/// hypothetical: `hermeticity_tripwire.rs` shipped with a count and no cross-check, and when
/// `mod theme;` was added its coverage test stayed green while `src/theme.rs` sat outside its scan
/// entirely. This guard is born with the cross-check rather than acquiring one later. (#556)
#[test]
fn the_scan_set_covers_every_module_the_crate_root_declares() {
    let (_, crate_root) = SOURCES
        .iter()
        .find(|(path, _)| *path == "src/main.rs")
        .expect("src/main.rs must be listed in SOURCES for the cross-check to run");

    let mut declared = 0;
    for line in crate_root.lines() {
        let trimmed = line.trim();
        // `pub mod` / `pub(crate) mod` are matched too, so a visibility change does not quietly
        // stop this loop from seeing a module.
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
             guard does not scan it — and an unscanned module is exactly where an inline \
             `Color::` would survive review"
        );
    }

    assert_eq!(
        declared, 10,
        "expected 10 `mod` declarations in src/main.rs. If this is 0 the loop above matched \
         nothing and its assertion never ran"
    );

    // A duplicated entry would let a real file go unlisted while every count still agreed.
    let mut seen = std::collections::BTreeSet::new();
    for (path, source) in SOURCES {
        assert!(seen.insert(*path), "{path} is listed twice in SOURCES");
        assert!(
            !source.is_empty(),
            "{path} embedded as empty — an empty source scans clean and proves nothing"
        );
    }
}

/// The scan finds a colour it is supposed to find, and ignores one it is supposed to ignore.
///
/// Without this, every assertion above could be passing because the needles never match anything.
/// Both directions, because each plausible stripper bug breaks exactly one of them.
#[test]
fn the_scan_detects_a_planted_colour_and_ignores_one_in_a_comment() {
    // Assembled so this test file does not itself contain the needle it plants — otherwise this
    // file could never be added to SOURCES, and the guard that scans every source but itself is
    // the guard with a hole in the obvious place.
    let needle = format!("Colo{}", "r::Cyan");

    let planted = format!("let s = Style::new().fg({needle});");
    assert!(
        code_only(&planted).contains("Color::"),
        "the scan cannot see a colour on a plain code line, so every absence assertion above is \
         vacuous"
    );

    let commented = format!("// prefer theme.focus over {needle} at a call site");
    assert!(
        !code_only(&commented).contains("Color::"),
        "the scan fired on a COMMENT. Prose naming a colour must be safe, or this feature's own \
         documentation — including this file — becomes unwritable"
    );

    let spaced = format!("let s = Style::new().fg(Colo{});", "r ::Cyan");
    assert!(
        code_only(&spaced).contains("Color ::"),
        "the hand-edited spacing variant is not detectable, so the second needle is decorative"
    );
}

/// [`strip_line_comment`] keeps code that follows a literal containing `//`, and still cuts real
/// trailing comments.
///
/// Both directions, because the two plausible implementations each fail one and a test asserting
/// only one would license the other bug. The URL scheme is assembled from pieces rather than
/// written out: `hermeticity_tripwire.rs` treats a plaintext `http:` in code as its catch-all for
/// an unnamed HTTP client, and it is right to — a guard that exempted "but this one is only a
/// fixture" would exempt the real thing too.
#[test]
fn the_stripper_survives_a_literal_containing_a_comment_marker() {
    let scheme = format!("htt{}{}", "p:", "//");
    let url_line = format!(r#"    let u = format!("{scheme}{{host}}"); let y = 1;"#);
    assert_eq!(
        strip_line_comment(&url_line),
        url_line,
        "the `//` inside a literal is not a comment; cutting there hides the code after it, which \
         is a real defect this crate already shipped once"
    );

    let needle = format!("Colo{}", "r::Red");
    let trailing = format!("    let x = 1; // never write {needle} here");
    assert_eq!(
        strip_line_comment(&trailing),
        "    let x = 1; ",
        "a trailing comment must still be cut, or this guard fires on prose about itself"
    );

    let escaped = r#"    let q = "a\"//b"; let z = 2;"#;
    assert_eq!(
        strip_line_comment(escaped),
        escaped,
        "an escaped quote does not end the literal, so the `//` after it is still not a comment"
    );
}
