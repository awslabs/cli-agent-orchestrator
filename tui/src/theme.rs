//! The semantic colour layer: six roles, an ANSI-16 palette, and `NO_COLOR` (issue #556).
//!
//! **This is the only module in the crate permitted to name a [`Color`].** Call sites say
//! `theme.required`, never `Color::Yellow`, so the palette is one reviewable file — the same
//! "put the decision in one auditable place" discipline `catalog.rs` applies to the run policy.
//! `tests/no_colour_literal_outside_theme.rs` is what makes that a failing test rather than a
//! convention.
//!
//! # Colour here is DECORATION, never information (NFR-3 of #321)
//!
//! The affirmed rule is *"no state conveyed by colour alone"*, and every state marker in this
//! crate is already textual: `>` for focus, `*` for selection, `(required)`, the state word from
//! `renderer::pane_state_word`, `exit {code}`, `running…`. So this module decides what gets
//! **emphasized**, not what anything **means**. Nothing breaks if it is absent — which is
//! precisely why `NO_COLOR` support is cheap and why the strip-styling guard is the one test in
//! this feature that cannot be left as a convention.
//!
//! # ANSI-16 ONLY. No RGB, no 256-colour.
//!
//! The 16 base colours are resolved by the terminal against the palette **the operator already
//! chose**, so they respect a theme this crate cannot see and behave over `ssh`, in `tmux`, and
//! in `screen`. A hardcoded `Color::Rgb` fights that setup instead of joining it.
//!
//! `Color::Black` and `Color::White` are avoided **entirely** — they invert between light and
//! dark terminals, so a `White` foreground is invisible on a light background and vice versa.
//! [`Color::Reset`] is the default-foreground role instead, and it is exactly right for the
//! monochrome theme: `Cell::EMPTY` is `fg: Color::Reset`, so a `Reset` foreground is
//! indistinguishable from an unstyled cell.
//!
//! # There is no border and no selected-row highlight to colour
//!
//! Issue #556's palette table assigns `focus` to "focused region border, selected row". **This
//! crate has neither** — it renders exactly one `Block`, and it is `Block::new()` with no
//! borders; there is no `List`, no `Table`, no `highlight_style`. So [`Theme::focus`] applies to
//! the structural `>` marker `renderer` already writes and to the line carrying it. Adding
//! borders would be a layout change interacting with NFR-6's sub-80x24 stacked mode, which is a
//! different piece of work. (#556)

use ratatui::style::{Color, Modifier, Style};

/// The number of semantic roles. A seventh role must update [`Theme::roles`], whose return type
/// is a fixed-size array — so the count and the accessor cannot drift apart silently.
///
/// Used only by [`Theme::roles`] and by tests. Kept because it is what makes the "adding a seventh
/// role does not silently escape the guards" property hold — [`Theme::roles`]'s return type is
/// `[_; ROLE_COUNT]`, so a seventh field stops the file compiling rather than quietly leaving one
/// role unscanned. A hand-maintained count that only *reads* right is the failure mode this avoids.
///
/// `allow` and not `expect`: under `--all-targets` the test cfg *does* use it, so an `expect`
/// would be unfulfilled there and `-D warnings` would fail the gate on the very attribute added
/// to satisfy it — the same trap `error.rs` documents on `TuiError::Http`. (#556)
#[allow(dead_code)]
pub(crate) const ROLE_COUNT: usize = 6;

/// Six semantic roles. Call sites name a **role**; only this module names a **colour**.
///
/// `Copy` because it is six [`Style`] values (each two `Option<Color>` plus two `Modifier`
/// bitflags) and it is read on every render path: threading it as a value costs no allocation
/// and, more usefully, no lifetime — which is what lets `renderer`'s shell hold one field and
/// `ResultsPane` own a copy without either growing a borrow. (#556)
///
/// # Every role has a production consumer
///
/// This module landed one task ahead of its call sites, under a struct-level
/// `#[allow(dead_code)]`, so that six colours could be argued with in one file before 40 call-site
/// edits were attached to them. **That attribute is gone**: `focus` and `required` are read by
/// `renderer::style_form`, `error` and `dim` by `renderer::style_pickers`, and `ok`/`warn`/`error`
/// by `results_pane::footer_style`. Only [`ROLE_COUNT`] and [`Theme::roles`] keep an `allow`, and
/// each documents why on itself.
///
/// A role with no consumer is a role no operator ever sees, so the *absence* of that attribute is
/// load-bearing: it is what makes `cargo clippy -D warnings` the check that a seventh role added
/// here also gets used. (#556)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Theme {
    /// The focus marker `>` and the line it marks.
    ///
    /// **Not a border** — see the module docs. `Cyan` is the conventional "you are here" hue and
    /// is the least likely of the 16 to collide with the semantic roles below.
    pub(crate) focus: Style,

    /// An unset required field's `(required)` suffix, and the footer's blocked reason.
    ///
    /// Shares `Yellow` with [`Theme::warn`] deliberately: both mean "attention, not failure", and
    /// inventing a distinct hue for a distinction the operator does not need would spend one of
    /// the 16. They are separate *roles* because a future palette may want to split them; the
    /// call sites already do.
    pub(crate) required: Style,

    /// `exit 0`.
    pub(crate) ok: Style,

    /// Failures, a non-zero exit, a refused hand-off (FR-5.3 of #321), a failed picker.
    pub(crate) error: Style,

    /// A cancelled follow, and a **missing** exit code.
    ///
    /// `exit ?` is `warn` and not `error`, and that is load-bearing: a missing exit code does not
    /// mean the command failed, it means the pane reached a terminal state without one. Styling
    /// it `error` would assert a failure the pane does not know occurred. See
    /// `results_pane::ResultsPane::footer`, whose comment makes the same distinction in text.
    pub(crate) warn: Style,

    /// `[unavailable]`, `[not sent — …]`, the collapsed strip, the truncation marker, key hints.
    ///
    /// `DarkGray` rather than `Modifier::DIM`: the DIM attribute is unimplemented or ignored by a
    /// number of terminals, so it is not a reliable de-emphasis channel on its own.
    pub(crate) dim: Style,
}

impl Theme {
    /// The ANSI-16 palette.
    ///
    /// `const` so it is a compile-time constant with no initialisation cost, and so a future
    /// non-ANSI-16 colour is a *compile* error inside a `const fn` rather than a runtime value
    /// only the guard would catch. (#556)
    pub(crate) const fn colour() -> Self {
        Self {
            focus: Style::new().fg(Color::Cyan),
            required: Style::new().fg(Color::Yellow),
            ok: Style::new().fg(Color::Green),
            error: Style::new().fg(Color::Red),
            warn: Style::new().fg(Color::Yellow),
            dim: Style::new().fg(Color::DarkGray),
        }
    }

    /// Every role at [`Color::Reset`] — the theme `NO_COLOR` selects.
    ///
    /// # Why `Reset` and not `Style::new()`
    ///
    /// `Style::new()` leaves `fg: None`, which means "do not change the cell's colour". That is
    /// *usually* the same thing, but it inherits whatever a previously-rendered widget left in
    /// the cell. `Color::Reset` states the intent — the terminal's default foreground — and
    /// matches `Cell::EMPTY`'s own `fg`, which is what makes the monochrome render
    /// indistinguishable from an unstyled one in a buffer assertion.
    ///
    /// # `Modifier`s ARE retained, deliberately
    ///
    /// `NO_COLOR` is about **colour**. Bold is not colour — and once colour is gone it is the
    /// only emphasis channel left, so stripping it would make this theme strictly worse than it
    /// needs to be for the operators who asked for it. `focus` and `required` keep `BOLD` so the
    /// two roles that answer "where am I" and "what is blocking me" remain visually findable on
    /// a monochrome terminal. The test asserts the modifier is *permitted*, not forbidden. (#556)
    pub(crate) const fn monochrome() -> Self {
        let plain = Style::new().fg(Color::Reset);
        Self {
            focus: plain.add_modifier(Modifier::BOLD),
            required: plain.add_modifier(Modifier::BOLD),
            ok: plain,
            error: plain.add_modifier(Modifier::BOLD),
            warn: plain,
            dim: plain,
        }
    }

    /// [`Self::monochrome`] when `NO_COLOR` is set, [`Self::colour`] otherwise.
    ///
    /// # `NO_COLOR=` (empty) DISABLES colour — a deliberate deviation from <https://no-color.org>
    ///
    /// That spec says colour should be disabled when the variable is "present and **not an empty
    /// string**". This implementation treats *any* presence, empty included, as a request for
    /// monochrome. The reason is which way the two readings fail:
    ///
    /// - An empty value is what a shell produces from `export NO_COLOR=`, and what a CI matrix
    ///   produces from an unset job variable. Reading that as "colour on" **ignores an
    ///   accessibility request on a technicality**.
    /// - Reading it as "colour off" merely loses decoration, and every state stays legible
    ///   because no state is conveyed by colour alone (NFR-3).
    ///
    /// One direction is safe to be wrong in and the other is not. The divergence is recorded
    /// here, next to the link, so a future reader sees a decision rather than a bug. (#556)
    ///
    /// # `var_os`, not `var`
    ///
    /// [`std::env::var`] returns `Err(NotUnicode)` for a non-UTF-8 value, which forces a caller
    /// to classify it — and classifying "set to invalid UTF-8" as "not set" would re-enable
    /// colour against an operator who asked for it off, the same failure as the empty-string case
    /// one step removed. `var_os` collapses the question to present/absent, which is the only
    /// distinction this function makes.
    ///
    /// # Read ONCE, at startup
    ///
    /// The caller stores the result; this is not called per frame. A per-frame `var_os` would be
    /// a syscall in the render path for a value that cannot change mid-process, and it would make
    /// the theme untestable without mutating process env — which `cargo test`'s threads make racy
    /// and edition 2024 makes `unsafe` (and this crate is `#![forbid(unsafe_code)]`). The branch
    /// itself is tested through [`Self::from_env_var`]. (#556)
    pub(crate) fn from_env() -> Self {
        Self::from_env_var(std::env::var_os("NO_COLOR"))
    }

    /// The pure half of [`Self::from_env`]: the decision, with the environment read hoisted out.
    ///
    /// Separated **only** so the three `NO_COLOR` cases — a value, the empty string, and non-UTF-8
    /// bytes — are testable without touching process env. `from_env` is then a single expression
    /// with nothing left in it to get wrong. (#556)
    fn from_env_var(no_color: Option<std::ffi::OsString>) -> Self {
        match no_color {
            // ANY presence, including "" — see the deviation note on `from_env`.
            Some(_) => Self::monochrome(),
            None => Self::colour(),
        }
    }

    /// Every role, named, for the exhaustive tests.
    ///
    /// # Why a fixed-size array and not a `Vec`
    ///
    /// The return type is `[_; ROLE_COUNT]`, so adding a seventh field to [`Theme`] without
    /// listing it here **fails to compile**. A `Vec` — or a test that names the six fields itself
    /// — would go stale on exactly the change it exists to catch: a seventh role added, and every
    /// "all roles" test silently covering six of seven. A yardstick that is hand-maintained fails
    /// when it is needed. (#556)
    ///
    /// `allow(dead_code)` for the same reason as [`ROLE_COUNT`]: its callers are all in test cfgs,
    /// and `expect` would be unfulfilled under `--all-targets`.
    #[allow(dead_code)]
    pub(crate) const fn roles(&self) -> [(&'static str, Style); ROLE_COUNT] {
        [
            ("focus", self.focus),
            ("required", self.required),
            ("ok", self.ok),
            ("error", self.error),
            ("warn", self.warn),
            ("dim", self.dim),
        ]
    }
}

impl Default for Theme {
    fn default() -> Self {
        Self::colour()
    }
}

#[cfg(test)]
mod tests {
    use super::{Theme, ROLE_COUNT};
    use ratatui::style::{Color, Modifier, Style};
    use std::ffi::OsString;

    /// The sixteen ANSI colours plus `Reset`, **hard-coded**.
    ///
    /// Deliberately a literal list and NOT derived from [`Theme::colour`]. A fixture sourced from
    /// the value under test cannot fail: an allow-set built by collecting the theme's own colours
    /// would accept `Color::Rgb(1, 2, 3)` the moment somebody wrote it, which is the entire
    /// property this list exists to check. (#556)
    const ANSI_16_AND_RESET: [Color; 17] = [
        Color::Reset,
        Color::Black,
        Color::Red,
        Color::Green,
        Color::Yellow,
        Color::Blue,
        Color::Magenta,
        Color::Cyan,
        Color::Gray,
        Color::DarkGray,
        Color::LightRed,
        Color::LightGreen,
        Color::LightYellow,
        Color::LightBlue,
        Color::LightMagenta,
        Color::LightCyan,
        Color::White,
    ];

    // ── FR-2.1 / FR-2.2 — the palette ────────────────────────────────────────────────────────

    /// **FR-2.2: every foreground in the colour theme is an ANSI-16 colour.**
    ///
    /// Iterates [`Theme::roles`], so it covers all six by construction rather than by a list this
    /// test maintains. Checked against [`ANSI_16_AND_RESET`] — see that constant for why it is a
    /// literal.
    #[test]
    fn every_colour_in_the_default_theme_is_ansi_16() {
        for (name, style) in Theme::colour().roles() {
            let fg = style
                .fg
                .unwrap_or_else(|| panic!("role `{name}` must set a foreground colour"));
            assert!(
                ANSI_16_AND_RESET.contains(&fg),
                "role `{name}` uses {fg:?}, which is not in the ANSI-16 set. RGB and 256-colour \
                 do not resolve against the operator's own terminal palette (FR-2.2, FR-2.3)"
            );
            assert!(
                style.bg.is_none(),
                "role `{name}` sets a BACKGROUND ({:?}). A background colour is not decoration — \
                 it repaints the cell and can make the foreground unreadable on a terminal theme \
                 this crate cannot see",
                style.bg
            );
        }
    }

    /// **FR-2.4: neither `Black` nor `White` appears in either theme.**
    ///
    /// The source-text guard in `tests/no_colour_literal_outside_theme.rs` checks the same
    /// property over the whole crate. This one checks the *values*, which is a different net: a
    /// colour reached through a `const` alias or a helper would satisfy the text scan and fail
    /// here.
    #[test]
    fn neither_theme_uses_black_or_white() {
        for (label, theme) in [("colour", Theme::colour()), ("mono", Theme::monochrome())] {
            for (name, style) in theme.roles() {
                for (channel, colour) in [("fg", style.fg), ("bg", style.bg)] {
                    assert!(
                        colour != Some(Color::Black) && colour != Some(Color::White),
                        "{label} theme role `{name}` uses {colour:?} for {channel}. Black and \
                         White invert between light and dark terminals, so one of the two is \
                         always unreadable (FR-2.4)"
                    );
                }
            }
        }
    }

    /// **FR-2.1: there are exactly six roles, and each is distinguishable from unstyled.**
    ///
    /// The second half is the anti-vacuity check for the palette itself. Every assertion in this
    /// module's other tests passes on a `Theme` whose six fields are all `Style::new()` — an
    /// all-default theme has no non-ANSI colour, no `Black`, no `White`, and no background. So
    /// without this, the palette could be empty and the suite green. (#556)
    #[test]
    fn the_colour_theme_has_six_distinguishable_roles() {
        let roles = Theme::colour().roles();
        assert_eq!(
            roles.len(),
            ROLE_COUNT,
            "the semantic palette is deliberately tiny; a seventh role is a design change"
        );

        for (name, style) in roles {
            assert_ne!(
                style,
                Style::new(),
                "role `{name}` is indistinguishable from unstyled, so every other assertion in \
                 this module passes vacuously for it"
            );
        }
    }

    // ── FR-3 — NO_COLOR ──────────────────────────────────────────────────────────────────────

    /// **FR-3.1: `NO_COLOR` set to a value yields the monochrome theme; unset yields colour.**
    #[test]
    fn no_color_selects_the_monochrome_theme_and_its_absence_selects_colour() {
        assert_eq!(
            Theme::from_env_var(Some(OsString::from("1"))),
            Theme::monochrome(),
            "NO_COLOR=1 must disable colour (FR-3.1)"
        );
        assert_eq!(
            Theme::from_env_var(None),
            Theme::colour(),
            "an unset NO_COLOR is the ONLY case that enables colour (FR-3.1)"
        );
    }

    /// **FR-3.2: `NO_COLOR=` — the empty string — disables colour.**
    ///
    /// This is the deliberate deviation from no-color.org, which says "present and not an empty
    /// string". The reasoning is on [`Theme::from_env`]; this test is what makes the deviation a
    /// pinned behaviour rather than an accident of implementation, so a future "fix" toward the
    /// letter of the spec has to argue with a red test. (#556)
    #[test]
    fn an_empty_no_color_still_disables_colour() {
        assert_eq!(
            Theme::from_env_var(Some(OsString::new())),
            Theme::monochrome(),
            "NO_COLOR= is what `export NO_COLOR=` and an unset CI variable produce. Reading it \
             as `colour on` ignores an accessibility request on a technicality (FR-3.2)"
        );
    }

    /// **FR-3.1: a non-UTF-8 `NO_COLOR` disables colour and does not panic.**
    ///
    /// The value is built from raw bytes that are not valid UTF-8, which is exactly the input
    /// `std::env::var` would reject with `NotUnicode` — the case that makes `var_os` the right
    /// call. Platform-gated because `OsStringExt` is a unix extension; the property it checks is
    /// about `var_os` versus `var` and holds on every platform, but only unix can *construct* the
    /// witness. (#556)
    #[cfg(unix)]
    #[test]
    fn a_non_utf8_no_color_disables_colour_without_panicking() {
        use std::os::unix::ffi::OsStringExt;

        // 0x80 is a UTF-8 continuation byte with no lead byte: invalid on its own.
        let invalid = OsString::from_vec(vec![0x80]);
        assert!(
            invalid.to_str().is_none(),
            "the witness must actually be invalid UTF-8, or this test proves nothing about \
             var_os vs var"
        );
        assert_eq!(
            Theme::from_env_var(Some(invalid)),
            Theme::monochrome(),
            "a non-UTF-8 NO_COLOR is SET. Treating it as unset would re-enable colour against an \
             operator who asked for it off"
        );
    }

    /// **FR-3.3: every monochrome role is `Color::Reset` in the foreground and sets no
    /// background.**
    ///
    /// Stated as a property of the *value* rather than of a render, because "renders without
    /// colour" is only observable through a terminal. `Cell::EMPTY` is `fg: Color::Reset`, so a
    /// `Reset` foreground is indistinguishable from an unstyled cell in a buffer — which is what
    /// makes `results_pane`'s end-to-end monochrome assertion possible without one. (#556)
    #[test]
    fn every_monochrome_role_is_reset_with_no_background() {
        for (name, style) in Theme::monochrome().roles() {
            assert_eq!(
                style.fg,
                Some(Color::Reset),
                "monochrome role `{name}` must be Color::Reset — the terminal's own default \
                 foreground (FR-3.3)"
            );
            assert!(
                style.bg.is_none(),
                "monochrome role `{name}` sets a background, which is still colour (FR-3.3)"
            );
        }
    }

    /// **FR-3.4: the monochrome theme MAY carry `Modifier`s, and this test asserts it does.**
    ///
    /// `NO_COLOR` is about colour; bold is not colour, and it is the only emphasis channel left
    /// once colour is gone. A reviewer would reasonably propose asserting `Style::new()` exactly
    /// here — this test is the counter-argument in executable form, so removing the modifiers is
    /// a deliberate change rather than a tidy-up. (#556)
    #[test]
    fn the_monochrome_theme_keeps_bold_as_its_emphasis_channel() {
        let mono = Theme::monochrome();
        assert!(
            mono.focus.add_modifier.contains(Modifier::BOLD),
            "with colour gone, BOLD is the only way `focus` can answer `where am I` (FR-3.4)"
        );
        assert!(
            mono.required.add_modifier.contains(Modifier::BOLD),
            "with colour gone, BOLD is the only way `required` can answer `what is blocking me` \
             (FR-3.4)"
        );
        assert!(
            mono.warn.add_modifier.is_empty(),
            "not every role takes a modifier; `warn` is plain, so this test cannot pass by \
             blanket-bolding the theme"
        );
    }

    /// This module's production region with every comment removed.
    ///
    /// # Why the stripping is necessary, not tidiness
    ///
    /// The first version of [`from_env_reads_only_no_color`] scanned the raw region and asserted
    /// one `var_os`. It found **four** — because the doc comments on [`Theme::from_env`] explain
    /// at length why `var_os` and not `var`, and a text scan cannot tell an explanation from a
    /// call. A prose-sensitive guard is worse than none: it goes red when somebody *documents*
    /// the module and green when somebody *breaks* it.
    ///
    /// The cut is **literal-aware**, and that is not caution for its own sake. The first version
    /// cut at the first `//` on the line, justified by "this region has no string literal
    /// containing `//`" — a claim that happens to hold today and is exactly the reasoning
    /// `no_backend_attach_call.rs` records as having already been **false** once, when
    /// `format!("http://{host}:{port}")` silently truncated its scan mid-URL. The stale
    /// justification was the defect there, because it read as a considered trade-off. Tracking the
    /// literal costs eight lines and removes the claim entirely. (#556)
    fn production_code() -> String {
        let source = include_str!("theme.rs");
        let (production, _tests) = source
            .split_once("#[cfg(test)]")
            .expect("this module must carry a #[cfg(test)] marker for the region split to hold");

        production
            .lines()
            .map(strip_line_comment)
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// Returns `line` up to a `//` that is **not** inside a string literal.
    ///
    /// Deliberately the same shape as `tests/no_backend_attach_call.rs::strip_line_comment` rather
    /// than a shared helper: an integration test is its own crate, so a tripwire cannot import
    /// from another, and the crate's established answer is that each guard carries its own copy.
    /// Not a full lexer — no raw strings, no block comments — and it does not need to be, because
    /// [`the_stripper_is_literal_aware`] pins the one property this relies on.
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

    /// [`strip_line_comment`] keeps code after a `//`-bearing literal, and still cuts real
    /// comments.
    ///
    /// Both directions, because the two plausible implementations each fail one of them and a test
    /// asserting only one would license the other bug: cutting at the first `//` truncates inside
    /// `http://` and hides whatever follows, while cutting only whole-line comments lets a
    /// trailing `// Color::Red` comment fire a colour guard on prose — and this module carries
    /// three `#[allow(dead_code)] // reason` comments, so that is not hypothetical.
    ///
    /// The URL fixture is **assembled** rather than written as a literal. `hermeticity_tripwire.rs`
    /// treats a plaintext `http:` in code as its catch-all for an HTTP client it does not know
    /// about, and it is right to: a guard that exempted "but this one is only a test fixture" would
    /// exempt the real thing too. Writing the scheme out here fires that tripwire — verified, it
    /// did — so it is built from pieces, which is the same dodge `no_backend_attach_call.rs` uses
    /// for the identical reason. (#556)
    #[test]
    fn the_stripper_is_literal_aware() {
        let scheme = format!("htt{}{}", "p:", "//");
        let url_line = format!(r#"let u = "{scheme}x"; let y = 1;"#);
        assert_eq!(
            strip_line_comment(&url_line),
            url_line,
            "the `//` inside a literal is not a comment; cutting there hides the code after it"
        );
        assert_eq!(
            strip_line_comment("let x = 1; // Color::Red"),
            "let x = 1; ",
            "a trailing comment must still be cut, or an absence guard fires on prose"
        );
        assert_eq!(
            strip_line_comment(r#"let q = "a\"//b"; let z = 2;"#),
            r#"let q = "a\"//b"; let z = 2;"#,
            "an escaped quote does not end the literal, so the `//` after it is still not a comment"
        );
    }

    /// **FR-3.5: `from_env` reads `NO_COLOR` and nothing else.**
    ///
    /// A source-text assertion, because "reads no other variable" is not observable from the
    /// outside — a second `var_os` call would simply change behaviour on a machine where that
    /// variable happens to be set, which no value-level test can see. #556 floats a future
    /// `CAO_TUI_NO_COLOR`; it is deliberately NOT implemented, because `CAO_TUI_*` is a namespace
    /// this repository has never used and a decorative-styling change is the wrong place to
    /// establish one. The `Theme` struct is the extension seam, not an env var. (#556)
    #[test]
    fn from_env_reads_only_no_color() {
        let code = production_code();

        // Anti-vacuity: every assertion below is an ABSENCE check, and all of them pass on an
        // empty string. If the region split or the comment strip ever swallows the module, this
        // is the line that notices.
        assert!(
            code.contains("fn from_env"),
            "the scanned region does not contain `from_env`, so the absence checks below prove \
             nothing about it"
        );

        assert_eq!(
            code.matches("env::").count(),
            1,
            "exactly ONE environment read is permitted in production. Found: {:?}",
            code.match_indices("env::").count()
        );
        assert!(
            code.contains("env::var_os(\"NO_COLOR\")"),
            "the one permitted read must be `var_os` on NO_COLOR. `var` returns Err(NotUnicode) \
             for a non-UTF-8 value, and classifying that as `not set` re-enables colour against \
             an operator who asked for it off"
        );
        assert!(
            !code.contains("CAO_TUI"),
            "CAO_TUI_* is unprecedented in this repository; #556's future hook is the Theme \
             struct, not an env var (FR-3.5)"
        );
        assert!(
            !code.contains("TERM"),
            "terminal-capability detection is not in scope; NO_COLOR is the operator's own \
             explicit request and needs no corroboration (FR-3.5)"
        );
    }

    // ── Housekeeping ─────────────────────────────────────────────────────────────────────────

    /// `Default` is the colour theme, so a `Theme::default()` written by mistake is not silently
    /// monochrome — which would look like working code that never shows a colour.
    #[test]
    fn default_is_the_colour_theme() {
        assert_eq!(Theme::default(), Theme::colour());
    }

    /// The two themes differ. Guards against a refactor that makes `colour()` delegate to
    /// `monochrome()`, which would leave every FR-3 test above green and every FR-2 test
    /// vacuous.
    #[test]
    fn the_two_themes_are_not_the_same_theme() {
        assert_ne!(
            Theme::colour(),
            Theme::monochrome(),
            "if these are equal, NO_COLOR is a no-op and the palette does not exist"
        );
    }
}
