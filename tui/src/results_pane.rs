//! `results-pane` (Bolt 4): the scrollable output pane — streaming render, scrollback,
//! cancel, and the completion branch (issue #321).
//!
//! Implements **FR-3.1, FR-3.3, FR-5.3, NFR-3, S-3**. This unit fixes design defect #3: the
//! superseded implementation **built a captured-output pane and never called it from
//! production code.** FR-3.2 — the pane *has* a production caller — is `renderer`'s
//! obligation, not this unit's; what this unit owes is a pane that a caller can actually
//! drive. See "The seam that makes FR-3.2 achievable" below.
//!
//! # SR-1: control sequences are stripped at the DECODE POINT, before any widget exists
//!
//! Command output is untrusted: it is whatever an arbitrary command wrote to stdout/stderr.
//! An `\x1b[2J` in it must not clear the operator's screen, and an `\x1b[3J` must not delete
//! their scrollback.
//!
//! The strip happens in [`OutputBuffer`], the `vte::Perform` sink that turns bytes into
//! retained lines. **Nothing else can add a line to the buffer** — [`ResultsPane::push_bytes`]
//! is the only mutator of the retained text and it routes every byte through the parser — so
//! there is no path by which an unstripped byte reaches a widget. Stripping at the *render*
//! site instead would leave the raw bytes sitting in a field that a later widget, a log line,
//! or a clipboard helper could read.
//!
//! ## Two things this module measured rather than assumed, one of which contradicts the design
//!
//! `security-requirements.md` SR-1 carries a correction saying an earlier draft was wrong "in
//! the dangerous direction" by claiming ratatui "contains this by construction". The
//! correction's *conclusion* — strip before the pipeline — is right and is what this module
//! does. Its *stated mechanism* is *not* true of ratatui 0.30.2, and that matters because a
//! test written from it would be **unable to fail**:
//!
//! 1. **`Span::raw(untrusted)` does NOT put an ESC byte into a `Cell` in ratatui 0.30.2.**
//!    Measured: rendering `Span::raw("before\x1b[2Jafter")` through a `Paragraph` into a
//!    `TestBackend` yields cells reading `before[2Jafter` — the ESC is **gone**, the `[2J`
//!    remains as visible text. The mechanism is
//!    `ratatui_core::text::span::Span::styled_graphemes`, which does
//!    `.filter(|g| !g.contains(char::is_control))`, and `Buffer::set_stringn`, which does the
//!    same. `char::is_control()` is true for all of U+0000..=U+001F, U+007F, and
//!    U+0080..=U+009F — 65 codepoints including ESC and the C1 CSI.
//! 2. **But the claim is not harmless to disbelieve, because `Cell::set_symbol` is public and
//!    bypasses that filter entirely.** Measured:
//!    `buf.cell_mut((0,0)).unwrap().set_symbol("\x1b[2J")` leaves `"\u{1b}[2J"` in the cell,
//!    and it survives `Backend::draw`. So the *rule* SR-1 states is real — a widget that
//!    writes cells directly can serialise an escape to the terminal — it is just not reachable
//!    via `Span`/`Paragraph` in this version.
//!
//! **The consequence for testing is the important half, and it is why this module has two ANSI
//! tests rather than one.** `frontend-components.md:155-158` requires the assertion be made on
//! the rendered cells. Taken literally — "the cells contain no ESC byte" — that assertion
//! **cannot fail**, because ratatui drops ESC whether or not this module strips it: the
//! vacuous-guard failure mode, arrived at by following the artifact exactly.
//! [`ansi_escapes_are_stripped_before_the_bytes_reach_a_cell`] therefore also asserts the
//! **payload residue** (`[2J`) is absent, which is the half that goes red when the stripper is
//! removed, and [`ansi_escapes_never_enter_the_retained_buffer`] asserts on the buffer, where
//! the strip actually happens. Both were mutation-proven.
//!
//! # PR-1: incremental, and that is why `vte` is used directly
//!
//! `strip-ansi-escapes` is what SR-1 names. Its `Writer` wraps a `std::io::LineWriter`, and a
//! newline-less chunk is therefore **withheld until a newline arrives** — measured with a spy
//! sink: after `write_all(b"Loading")` the inner writer had received `""`. That is precisely
//! the shape of a progress line, and a pane that hides `Loading 50%` until the command
//! finishes the line is the "slow command indistinguishable from a hang" failure PR-1 exists
//! to prevent. Its `strip()` helper has the opposite problem: a fresh parser per call, so an
//! escape split across two chunks leaks — `strip(b"before\x1b")` then `strip(b"[2Jafter")`
//! yields `"before"` + `"[2Jafter"`. `vte` is that crate's own parser without the
//! `LineWriter`, so this is the same supply chain minus the buffering. See `Cargo.toml`.
//!
//! [`ResultsPane::lines`] returns the retained lines **plus the pending partial line**, which
//! is what makes a byte visible the moment it arrives rather than at the next newline.
//!
//! # The seam that makes FR-3.2 achievable
//!
//! [`ResultsPane`] implements [`std::io::Write`], so `renderer` can pass `&mut pane` straight
//! into `ServerClient::run(.., sink)` — which already takes `sink: &mut W`. The pane is the
//! sink. That is deliberate: the predecessor's pane needed a bespoke wiring step that nobody
//! ever wrote, and a type that plugs into the existing signature has no such step to forget.
//!
//! **The pane never calls back into `Renderer`.** `render` is pull-based and every transition
//! is driven inward, which is what keeps the dependency graph acyclic. `policy` is *passed in*
//! (`attach`), never looked up, so this unit `depends_on: [shared-types]` and not
//! `command-catalog` — the [`Policy`] import is a type, not a dependency on the table.

use std::collections::VecDeque;
use std::io::{self, Write};

use crossterm::event::KeyCode;
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Paragraph, Widget, Wrap};
use thiserror::Error;

use crate::catalog::Policy;
use crate::theme::Theme;

/// Retained-line cap (Q2 at 3.1, SR-3, PR-2). Oldest lines are discarded past this.
///
/// 10,000 lines is ~1–2 MB. The cap exists because `components.md` records `cao workflow run`
/// and `cao schedule run` as **unbounded in duration**, which makes an unbounded buffer in the
/// front door a memory-exhaustion path. (#321)
pub const BUFFER_CAPACITY: usize = 10_000;

/// The disclosure that lines were dropped — **the load-bearing half of the ring buffer.**
///
/// Bounded memory *without* a marker is silent loss: an operator scrolling up would draw
/// conclusions from output that was discarded without saying so, which is the failure class
/// this whole intent exists to eliminate. SR-3 calls the marker "the security-relevant half".
///
/// Text, not colour or a glyph alone (NFR-3). The line count is written into the literal
/// rather than interpolated from [`BUFFER_CAPACITY`] so a test can assert the exact operator-
/// facing string; [`the_truncation_marker_states_the_actual_capacity`] keeps the two in step.
/// (#321)
pub const TRUNCATION_MARKER: &str = "⋯ earlier output dropped (buffer limit 10,000 lines)";

/// The `cancelled` footer. **This wording is binding (SR-4) and must not be softened.**
///
/// `[k]` stops *this pane* consuming the stream. It does **not** stop server-side work, and
/// there is no command-level cancel available to send: `POST /workflows/runs/{run_id}/cancel`
/// does exist (`api/main.py:2589`) but needs a `run_id` this pane does not hold — it receives
/// a byte stream, not a run handle. Routing cancel through `DELETE /terminals/{id}` was
/// considered and rejected: it destroys the terminal, far heavier than "stop this command".
///
/// So a pane reading "Cancelled" would misrepresent system state — the operator would believe
/// the command stopped when it is still running. That is the silent-mismatch class this intent
/// exists to eliminate, which is why the string says what actually happened.
/// [`the_cancelled_wording_does_not_claim_the_command_stopped`] guards it. (#321)
pub const CANCELLED_NOTICE: &str = "stopped following — the command is still running";

/// The `empty` state's viewport text — stated explicitly rather than left blank (FR-6.2).
pub const EMPTY_NOTICE: &str = "command produced no output";

/// The `collapsed` strip's label prefix. The strip is a **real rendered state**, not a hidden
/// widget: FR-3.3 requires the pane to *expand from* it, so it must be present and show a
/// count. (#321)
const COLLAPSED_PREFIX: &str = "▸ results";

/// The `running` footer. Text, never a spinner alone (NFR-3).
const RUNNING_INDICATOR: &str = "running…";

/// The `refused` footer's brief notice; the full reason goes in the viewport (Q5, FR-5.3).
const REFUSED_NOTICE: &str = "hand-off refused — run it yourself:";

/// [`ResultsPane::cancel`] was called while the pane was not `running`.
///
/// # Why a unit-local `thiserror` type and NOT an eighth [`crate::error::TuiError`] variant
///
/// The affirmed practice is one crate-root error type, and `error.rs` says later units "add
/// variants here rather than minting their own top-level error types" — the Python side's
/// `ProviderError`-defined-in-six-modules problem. This is deliberately **not** that:
/// `NotRunning` is not a top-level error type for a unit, it is one condition with one
/// variant, which is what `business-logic-model.md:194` describes ("`NotRunning` is that
/// type's single variant here").
///
/// Three reasons it does not belong in `TuiError`:
///
/// 1. **`TuiError` IS the operator-facing boundary contract** — its own docs say so, and
///    `renderer` matches on it to choose a rendered state, with each `Display` string being
///    the one styled line the operator sees. `NotRunning` is never shown to anybody:
///    `frontend-components.md` specifies that `[k]` outside `running` is **ignored**, "rather
///    than surfacing an error to the operator for a key that simply does not apply". Putting
///    it in `TuiError` would add a variant whose contract is *must never be rendered* to an
///    enum whose purpose is what to render.
/// 2. **The signature is the documentation.** `cancel() -> Result<(), NotRunning>` says the
///    only thing that can go wrong is "it was not running". `Result<(), TuiError>` would admit
///    eight variants, seven of them unreachable — an HTTP status, a decode failure — and a
///    caller could not tell from the type which ones to handle.
/// 3. **`TuiError` is not `PartialEq`** (it carries `std::io::Error`), so
///    `assert_eq!(pane.cancel(), Err(NotRunning))` would not compile against it. A single-
///    condition type can derive equality, and the test reads as the requirement.
///
/// The trade-off, stated rather than hidden: `renderer` must handle two error types where the
/// affirmed rule points at one. That cost is small and local — `cancel()` is the only fallible
/// method in the unit — and this type never crosses an integration boundary, which is the case
/// the one-type rule is about. (#321)
#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("nothing is running, so there is nothing to stop following")]
pub struct NotRunning;

/// The pane's six states.
///
/// `interaction-spec.md` defines five; **the Q1 cancel ruling adds [`PaneState::Cancelled`]**,
/// because "stopped following a still-running command" is not [`PaneState::Complete`].
///
/// A closed enum so an unhandled state is a **compile error**, following `catalog.rs`'s idiom:
/// [`ResultsPane::render`] matches exhaustively with no `_` arm, so adding a seventh state
/// fails to build rather than rendering a blank pane. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaneState {
    /// One-line strip. A real state, not a hidden widget (FR-3.3).
    Collapsed,
    /// Streaming, with a running indicator.
    Running,
    /// Finished. **Branches on [`Policy`]** — see [`ResultsPane::complete`].
    Complete,
    /// Exited with no bytes, IN-APP only. Stated explicitly, never a blank box.
    Empty,
    /// The operator pressed `[k]`. **The command is still running** — see
    /// [`CANCELLED_NOTICE`].
    Cancelled,
    /// Hand-off unavailable; carries the reason plus the exact manual argv (FR-5.3).
    Refused,
}

impl PaneState {
    /// Is this a state no further transition may leave?
    ///
    /// The guard behind two edge cases that are easy to get backwards:
    /// `complete()` called twice must leave the **first** result standing, and `cancel()`
    /// followed by a late completion must **stay** `cancelled` — a late `complete()` silently
    /// overwriting the operator's deliberate action is exactly the misrepresentation SR-4 is
    /// about. (#321)
    const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Complete | Self::Empty | Self::Cancelled | Self::Refused
        )
    }
}

/// The ring buffer, and the `vte::Perform` sink that fills it. **This is the strip point.**
///
/// Every retained character arrived through [`vte::Perform::print`], so the buffer holds
/// displayable text by construction rather than by a later filter. The parser lives in
/// [`ResultsPane`] and persists across chunks, which is what makes an escape split over a
/// chunk boundary still get recognised (see the module docs). (#321)
#[derive(Debug, Default)]
struct OutputBuffer {
    /// Retained complete lines, oldest first. Capped at [`BUFFER_CAPACITY`].
    lines: VecDeque<String>,
    /// The line being accumulated, not yet newline-terminated.
    ///
    /// Rendered as if it were a line (see [`ResultsPane::lines`]) — that is PR-1's
    /// incrementality: a command that writes `Loading 50%` with no newline is visible
    /// immediately. Holding it back until the newline is what `strip-ansi-escapes`' `Writer`
    /// does, and why this module does not use it.
    pending: String,
    /// Set the first time a line is discarded. Never cleared except by [`Self::clear`].
    truncated: bool,
    /// Lines appended since the last [`ResultsPane::take_appended`], so the scroll offset can
    /// be adjusted by exactly the right amount to hold position (PR-3).
    appended: usize,
    /// Which kind of **8-bit C1** sequence are we inside, if any?
    ///
    /// # A real SR-1 gap, measured — not a cosmetic one
    ///
    /// `vte` recognises the 7-bit forms (`\x1b[`, `\x1b]`, `\x1bP`) and dispatches them to
    /// `csi_dispatch`/`osc_dispatch`/`hook`, which this impl does not override, so those bytes
    /// vanish. It does **not** do the same for the single-byte C1 equivalents. Measured with a
    /// spy `Perform` over `b"a\x9b2Jb"`:
    ///
    /// ```text
    /// print('a'), execute(0x9b), print('2'), print('J'), print('b')
    /// ```
    ///
    /// The introducer arrives at `execute` and is dropped, but `2J` — the *payload* of a
    /// clear-screen — is then delivered as ordinary printable characters. Without this flag the
    /// buffer retained `a2Jb`, so an operator saw `2J` as garbage in their output. Compare the
    /// 7-bit form `b"a\x1b[2Jb"`, which yields `print('a'), csi(J), print('b')` — nothing to
    /// suppress.
    ///
    /// **Why this matters beyond cosmetics.** `\x9b` is a single byte, so a command wanting to
    /// smuggle a sequence past a naive `\x1b[` scanner uses exactly this form; leaving its
    /// payload in the buffer is the "stripped the marker, kept the message" half-fix. The
    /// codepoint U+009B itself is `char::is_control()`, so `print`'s filter already stopped the
    /// introducer from being retained — it was only ever the parameters that leaked.
    ///
    /// Note this suppresses *text*, never a line boundary: `execute(b'\n')` clears it before
    /// committing, so an unterminated C1 sequence cannot eat more than the rest of its own line.
    /// That bound is deliberate — the alternative, a window that stays open until a valid
    /// terminator arrives, would let one malformed byte blank every subsequent line. (#321)
    c1_payload: C1Payload,
}

/// The kind of 8-bit C1 sequence currently being suppressed, if any.
///
/// **Two variants rather than a `bool`, because the two families end differently** and treating
/// them alike leaks. A `bool` was the first implementation and it left `wned` in the buffer from
/// `\x9d0;pwned\x07` — its own test caught it. An enum makes the distinction impossible to forget
/// at the point of use: [`OutputBuffer::print`] matches it exhaustively, so a third C1 family
/// would be a compile error rather than a silent fall-through to the wrong terminator rule.
/// Follows `catalog.rs`'s closed-enum idiom for the same reason. (#321)
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
enum C1Payload {
    /// Not inside a C1 sequence; printable characters are retained.
    #[default]
    None,
    /// A CSI (`0x9B`) payload: ends at its own final byte — see
    /// [`OutputBuffer::ends_csi_payload`].
    Csi,
    /// A string-type payload — OSC, DCS, APC, PM, or SOS. Carries free-form text, so **no
    /// printable character ends it**; it closes only on a terminator `vte` delivers through
    /// `execute` (BEL `0x07` or ST `0x9c`), or at a newline.
    String,
}

impl OutputBuffer {
    /// Moves [`Self::pending`] into [`Self::lines`], evicting the oldest if at capacity.
    fn commit_line(&mut self) {
        let line = std::mem::take(&mut self.pending);
        if self.lines.len() >= BUFFER_CAPACITY {
            self.lines.pop_front();
            // SR-3/PR-2: the flag is set at the moment of loss, and the marker it drives is
            // what keeps bounded memory from being *silent* loss.
            self.truncated = true;
        }
        self.lines.push_back(line);
        self.appended = self.appended.saturating_add(1);
    }

    /// Does `c` terminate a **CSI** payload's parameter run?
    ///
    /// A CSI sequence ends at its *final byte*, `0x40..=0x7E`, so `2J` ends at `J` and `38;5;1m`
    /// ends at `m`. Parameter bytes (`0x30..=0x3F`, the digits and `;`) and intermediates
    /// (`0x20..=0x2F`) do not end it — which is why the rule is a range rather than a single
    /// character. Measured against `\x9b38;5;1mb`: the window closes at `m`, and `b` is retained.
    ///
    /// **This rule is wrong for the string-type sequences and they deliberately do not use it.**
    /// OSC/DCS/APC/PM/SOS carry free-form text, so a final byte is ordinary payload — measured
    /// against `\x9d0;pwned\x07b`, where `p` is `0x70` and would close the window five characters
    /// early, leaking `wned`. Those forms end only at a terminator `vte` reports through
    /// `execute` (BEL `0x07`, or ST `0x9c`), so they are tracked by [`C1Payload::String`] and
    /// closed there instead. Conflating the two is how the first draft of this leaked. (#321)
    fn ends_csi_payload(c: char) -> bool {
        matches!(c, '\u{40}'..='\u{7e}')
    }

    /// Resets everything, including [`Self::truncated`]. Called by `attach` for a fresh run.
    fn clear(&mut self) {
        self.lines.clear();
        self.pending.clear();
        self.truncated = false;
        self.appended = 0;
        self.c1_payload = C1Payload::None;
    }

    /// Is there nothing at all to show? Used by the IN-APP/HANDOFF completion branch.
    ///
    /// Counts [`Self::pending`] too: a command that wrote `done` with no trailing newline
    /// produced output, and calling that `empty` would tell the operator something false.
    fn is_empty(&self) -> bool {
        self.lines.is_empty() && self.pending.is_empty()
    }
}

impl vte::Perform for OutputBuffer {
    /// The only path by which a character enters the buffer.
    ///
    /// `vte` calls this for printable characters only — every 7-bit escape, CSI, OSC and DCS
    /// sequence is dispatched to a handler this impl deliberately does not override, so those
    /// bytes are consumed and discarded. Two measured leaks are closed here:
    ///
    /// 1. **DEL (U+007F)** is passed through `print` by `vte`, verified by sweeping all 256 byte
    ///    values. The `is_control` filter drops it. Without it a DEL would sit in the buffer, be
    ///    dropped *again* by ratatui's own grapheme filter at render time, and so differ between
    ///    the model and the screen for no reason.
    /// 2. **The parameter bytes of an 8-bit C1 sequence** — see [`Self::c1_payload`] for the
    ///    measurement and why this one is a real SR-1 gap rather than a cosmetic one. (#321)
    fn print(&mut self, c: char) {
        // A C1 introducer opened a sequence `vte` will not close for us, so these characters are
        // that sequence's parameters, not text the command asked to display. Exhaustive: the two
        // families end by different rules, and a `bool` here is what leaked `wned`.
        match self.c1_payload {
            C1Payload::None => {
                if !c.is_control() {
                    self.pending.push(c);
                }
            }
            C1Payload::Csi => {
                if Self::ends_csi_payload(c) {
                    self.c1_payload = C1Payload::None;
                }
            }
            // No printable character closes a string-type payload — its terminator arrives at
            // `execute`, so everything here is suppressed until then.
            C1Payload::String => {}
        }
    }

    /// C0/C1 execution. **Only `\n` is honoured**, and `\t` is folded to a space.
    ///
    /// - `\n` commits a line. This is the buffer's only line boundary.
    /// - `\t` becomes a single space rather than being dropped. Dropping it — which is what
    ///   `strip-ansi-escapes` does, measured: `col1\tcol2` → `col1col2` — runs the columns of
    ///   tabular CLI output together. A single space is display-only, keeps tokens separated,
    ///   and avoids putting a control character into the buffer. Full tab-stop expansion was
    ///   rejected: it would interact with the display-only wrapping and change scroll
    ///   positions on resize.
    /// - `\r` is **dropped, not treated as a line boundary.** Committing on `\r` would turn
    ///   every CRLF line ending — routine on Windows — into one line plus a spurious empty
    ///   one. Honouring it as "overwrite the current line" is terminal emulation and out of
    ///   scope; the consequence, stated rather than hidden, is that a `\r`-based progress bar
    ///   accumulates on one line instead of overwriting.
    /// - Everything else (BEL, backspace, VT, FF, and every C1) is discarded: these are the
    ///   sequences SR-1 exists to keep away from the terminal.
    ///
    /// The C1 introducers additionally **open a payload-suppression window**, because discarding
    /// the introducer alone is not enough — see [`Self::c1_payload`]. (#321)
    fn execute(&mut self, byte: u8) {
        match byte {
            b'\n' => {
                // A newline ends any unterminated C1 sequence: parameter bytes never span lines,
                // so a missing terminator must not swallow the rest of the output.
                self.c1_payload = C1Payload::None;
                self.commit_line();
            }
            b'\t' if self.c1_payload == C1Payload::None => self.pending.push(' '),
            // The 8-bit CSI introducer. Its payload ends at its own final byte.
            0x9b => self.c1_payload = C1Payload::Csi,
            // The string-type introducers: OSC (0x9D), DCS (0x90), APC (0x9F), PM (0x9E), and
            // SOS (0x98). Their payloads end only at BEL or ST, both handled below.
            0x9d | 0x90 | 0x9f | 0x9e | 0x98 => self.c1_payload = C1Payload::String,
            // BEL and ST — the two terminators `vte` reports here rather than through `print`.
            // Measured: `\x9d0;pwned\x07b` delivers `execute(0x07)`, and `\x9fpayload\x9cb`
            // delivers `execute(0x9c)`. Without these arms the window would stay open to the end
            // of the line and swallow the text that follows the sequence.
            0x07 | 0x9c => self.c1_payload = C1Payload::None,
            _ => {}
        }
    }
}

/// The scrollable output pane.
///
/// Drive it inward: [`Self::attach`], then [`Self::push_bytes`] (or `write!`, since it is a
/// [`Write`]) per chunk, then exactly one of [`Self::complete`] / [`Self::cancel`] /
/// [`Self::refuse`]. Render it with `frame.render_widget(&pane, area)`.
#[allow(dead_code)] // every caller is `renderer` (Bolt 5); see the note in `types.rs`. (#321)
pub struct ResultsPane {
    state: PaneState,
    /// Passed in by `Renderer`, never looked up here — this unit does not depend on
    /// `command-catalog`. `None` before the first [`Self::attach`].
    policy: Option<Policy>,
    output: OutputBuffer,
    /// Persistent across chunks. A fresh parser per chunk would leak an escape split over a
    /// chunk boundary — measured, see the module docs.
    parser: vte::Parser,
    exit_code: Option<i32>,
    /// HANDOFF's structured outcome line.
    outcome: Option<String>,
    /// `refused`: the operator-facing reason, in full.
    refusal_reason: Option<String>,
    /// `refused`: the **exact** argv to copy and run (FR-5.3).
    manual_command: Option<String>,
    /// Lines scrolled up from the newest. `0` = pinned to newest (auto-follow).
    scroll_offset: usize,
    /// The state [`Self::collapse`] left, so [`Self::expand`] can restore it.
    collapsed_from: Option<PaneState>,
    /// The semantic palette (#556). Owned rather than passed to `render`, because
    /// `Widget::render(self, area, buf)` has a fixed three-argument signature — see
    /// [`Self::set_theme`].
    ///
    /// **Presentation-only, and it is the one field here that is.** That is a real cost to the
    /// model/view separation this module otherwise keeps, and it is accepted for a specific
    /// reason: the alternative is a `ThemedPane<'a>` wrapper, which pushes a lifetime into the two
    /// `(&self.pane).render(..)` sites in `renderer.rs` to save a `Copy` field. Nothing in this
    /// module reads it outside `render`, `body`, and `footer_line`. (#556)
    theme: Theme,
}

/// Hand-written rather than derived, because **`vte::Parser` does not implement `Debug`**.
///
/// `vte::Parser` is `Parser<const OSC_RAW_BUF_SIZE: usize = 1024>` and derives nothing, so
/// `#[derive(Debug)]` on this struct fails to compile with "`Parser<1024>` doesn't implement
/// `Debug`". Every other type in this crate is `Debug`, and dropping the impl entirely would
/// make `ResultsPane` the one type that cannot appear in an `assert_eq!` failure message — so
/// the parser is reported as an opaque placeholder and every field a test would want to read is
/// printed. The parser holds no state a reader could act on anyway: it is a byte-level state
/// machine whose only observable effect is what it pushed into `output`. (#321)
impl std::fmt::Debug for ResultsPane {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ResultsPane")
            .field("state", &self.state)
            .field("policy", &self.policy)
            .field("output", &self.output)
            .field("parser", &"<vte::Parser>")
            .field("exit_code", &self.exit_code)
            .field("outcome", &self.outcome)
            .field("refusal_reason", &self.refusal_reason)
            .field("manual_command", &self.manual_command)
            .field("scroll_offset", &self.scroll_offset)
            .field("collapsed_from", &self.collapsed_from)
            .field("theme", &self.theme)
            .finish()
    }
}

impl Default for ResultsPane {
    fn default() -> Self {
        Self::new()
    }
}

#[allow(dead_code)] // every method's caller is `renderer` (Bolt 5). (#321)
impl ResultsPane {
    /// A pane in [`PaneState::Collapsed`] with an empty buffer.
    pub fn new() -> Self {
        Self {
            state: PaneState::Collapsed,
            policy: None,
            output: OutputBuffer::default(),
            parser: vte::Parser::new(),
            exit_code: None,
            outcome: None,
            refusal_reason: None,
            manual_command: None,
            scroll_offset: 0,
            collapsed_from: None,
            // `Theme::default()` is the COLOUR theme, deliberately: a pane whose theme was never
            // set renders in colour rather than silently monochrome, so a forgotten `set_theme`
            // looks like working code that has colour — not like working code that lost it, which
            // nobody would notice. `renderer` sets it from `Theme::from_env()` at startup.
            theme: Theme::default(),
        }
    }

    /// Replaces the palette (#556).
    ///
    /// # Why this is a setter and not a `render` parameter
    ///
    /// `Widget::render(self, area: Rect, buf: &mut Buffer)` is a trait method with a fixed
    /// three-argument signature; a theme parameter is not expressible without abandoning the trait
    /// or wrapping the pane in a `ThemedPane<'a>` newtype. The wrapper is defensible — it keeps
    /// presentation out of the model — but it puts a lifetime on the two `(&self.pane).render(..)`
    /// call paths in `renderer.rs`, which today have none. [`Theme`] is `Copy` and six small
    /// `Style` values, so owning one costs nothing at all.
    ///
    /// Called **once**, at construction, from `renderer`. Not per frame: `Theme::from_env` reads
    /// the environment, and a value that cannot change mid-process has no business being re-read in
    /// a render loop. (#556)
    /// `pub(crate)` and not `pub`, matching [`Theme`]'s own visibility. `pub` here is a
    /// `private_interfaces` error under `-D warnings`, which is the compiler making the same point:
    /// a method cannot be more reachable than the type in its signature.
    pub(crate) fn set_theme(&mut self, theme: Theme) {
        self.theme = theme;
    }

    /// Begins a run: `collapsed` → `running`, buffer cleared, `policy` retained.
    ///
    /// **Infallible.** A UI that raises has nowhere to raise to, so every outcome is a rendered
    /// state. Takes `policy` rather than a command id because `complete()` branches on it and
    /// nothing else here needs the catalog.
    ///
    /// The stream itself is **not** owned: the caller feeds [`Self::push_bytes`] as bytes
    /// arrive. `business-logic-model.md` describes "spawn a reader that appends to the ring
    /// buffer", and this is that shape with the ownership the same document requires — "the
    /// reader appends through the pane's own handle rather than sharing the buffer across
    /// threads". It is also what lets the pane be a `ServerClient::run` sink directly.
    ///
    /// A second `attach` while `running` replaces the first: `Renderer` serialises runs, so
    /// concurrent attachment is a caller defect, and silently interleaving two commands'
    /// output would be worse than restarting. The parser is reset too, or a half-consumed
    /// escape from the abandoned run would eat the first characters of the new one. (#321)
    pub fn attach(&mut self, policy: Policy) {
        self.output.clear();
        self.state = PaneState::Running;
        self.policy = Some(policy);

        self.parser = vte::Parser::new();
        self.exit_code = None;
        self.outcome = None;
        self.refusal_reason = None;
        self.manual_command = None;
        self.scroll_offset = 0;
        self.collapsed_from = None;
    }

    /// Feeds one chunk of raw command output. **The SR-1 strip point.**
    ///
    /// Every byte goes through the persistent `vte` parser, so control sequences are consumed
    /// here and cannot reach a widget by any route. Non-UTF-8 bytes become replacement
    /// characters and never panic (SR-2) — `vte`'s own UTF-8 handling, verified across all 256
    /// byte values.
    ///
    /// Infallible: a stream read error is the *caller's* to render as text (the design's "a
    /// stream read error is not a pane error"), and dropping bytes silently is the failure this
    /// module exists to prevent, so there is nothing here to report. (#321)
    pub fn push_bytes(&mut self, chunk: &[u8]) {
        // Disjoint field borrows: `parser` is the receiver, `output` the argument.
        self.parser.advance(&mut self.output, chunk);
        self.follow_or_hold();
    }

    /// PR-3: pinned at offset 0 follows new output; scrolled back **holds position.**
    ///
    /// Held by *incrementing* the offset by the number of lines appended, which is the
    /// non-obvious half. The offset counts lines up from the newest, so leaving it unchanged
    /// while the buffer grows would slide the window down and yank the operator's view toward
    /// the bottom — the exact thing PR-3 forbids — even though the number "did not change".
    /// (#321)
    fn follow_or_hold(&mut self) {
        let appended = std::mem::take(&mut self.output.appended);
        if self.scroll_offset == 0 {
            return; // Pinned to newest: new output scrolls with it.
        }
        self.scroll_offset = self
            .scroll_offset
            .saturating_add(appended)
            .min(self.max_scroll_offset());
    }

    /// The furthest back the operator can scroll: one screenful short of nothing.
    ///
    /// Clamped against the retained-line count rather than the viewport, because the viewport
    /// height is not known outside `render` and a clamp that depended on it would change the
    /// operator's position when the terminal is resized.
    fn max_scroll_offset(&self) -> usize {
        self.lines().len().saturating_sub(1)
    }

    /// Ends the run. **The branch the interaction spec calls non-interchangeable.**
    ///
    /// ```text
    /// (InApp,   empty)     => empty     — "command produced no output" + exit code
    /// (InApp,   non-empty) => complete  — captured output + "exit {code}" as TEXT
    /// (Handoff, _)         => complete  — the structured outcome line
    /// ```
    ///
    /// **The HANDOFF arm ignores emptiness deliberately, and that is the whole point.** A
    /// hand-off's output went to the *new window*, so an empty buffer there is **expected**;
    /// rendering the `empty` state would tell the operator "command produced no output" about a
    /// command that ran fine, which reads as a failed run. The outcome line states what was
    /// launched and where it went instead.
    ///
    /// `Option<Policy>` is `None` only if a caller completes a pane it never attached; that is
    /// treated as IN-APP, which yields the `empty`/captured branch rather than fabricating an
    /// outcome line for a hand-off that never happened.
    ///
    /// **Ignored once the pane is terminal** — a second `complete()` leaves the first result
    /// standing, and a completion arriving after `cancel()` does not overwrite the operator's
    /// action. Infallible: the ignore is the specified behaviour, not a failure. (#321)
    pub fn complete(&mut self, exit_code: i32, outcome: Option<String>) {
        if self.state.is_terminal() {
            return;
        }

        self.exit_code = Some(exit_code);

        match (self.policy, self.output.is_empty()) {
            (Some(Policy::Handoff), _) => {
                self.state = PaneState::Complete;
                self.outcome = outcome;
            }
            (_, true) => {
                self.state = PaneState::Empty;
            }
            (_, false) => {
                self.state = PaneState::Complete;
            }
        }
    }

    /// Stops following the stream. **The one fallible method** (Q1).
    ///
    /// `Err(NotRunning)` when the pane is not `running`, with **state unchanged** — the caller
    /// ignores it, per `frontend-components.md`'s rule that `[k]` outside `running` is not an
    /// operator-facing error. See [`CANCELLED_NOTICE`] for why the rendered wording is binding.
    /// (#321)
    pub fn cancel(&mut self) -> Result<(), NotRunning> {
        if self.state != PaneState::Running {
            return Err(NotRunning);
        }
        self.state = PaneState::Cancelled;
        Ok(())
    }

    /// Renders a hand-off refusal: the full reason plus the **exact** argv (FR-5.3).
    ///
    /// The argv is stored and rendered verbatim because **its entire purpose is to be copied
    /// and run** — a paraphrase is useless, and a mangled one is worse than nothing. It is
    /// `Option` to match `handoff::Refused::manual_command`, which is `Option` because two
    /// reachable refusals have no correct command to offer (an unresolvable backend, and a name
    /// that failed the allow-list); printing a confident wrong argv in exactly those cases is
    /// the failure that field's docs describe.
    ///
    /// Ignored once terminal, for the same reason as [`Self::complete`]. Infallible. (#321)
    pub fn refuse(&mut self, reason: String, manual_command: Option<String>) {
        if self.state.is_terminal() {
            return;
        }
        self.state = PaneState::Refused;
        self.refusal_reason = Some(reason);
        self.manual_command = manual_command;
    }

    /// Collapses to the one-line strip, remembering the state to return to.
    pub fn collapse(&mut self) {
        if self.state == PaneState::Collapsed {
            return;
        }
        self.collapsed_from = Some(self.state);
        self.state = PaneState::Collapsed;
    }

    /// Expands from the strip (FR-3.3), restoring the pre-collapse state.
    ///
    /// A no-op when nothing has run: expanding into a pane with no output would render a blank
    /// box, which FR-6.2 forbids. The strip stays, showing `(0)`.
    pub fn expand(&mut self) {
        if let Some(previous) = self.collapsed_from.take() {
            self.state = previous;
        }
    }

    /// The current state.
    pub const fn state(&self) -> PaneState {
        self.state
    }

    /// Whether lines have been dropped (drives [`TRUNCATION_MARKER`]).
    pub const fn truncated(&self) -> bool {
        self.output.truncated
    }

    /// Lines scrolled up from the newest; `0` is pinned.
    pub const fn scroll_offset(&self) -> usize {
        self.scroll_offset
    }

    /// The retained lines **plus the pending partial line**.
    ///
    /// Including `pending` is PR-1: a chunk with no trailing newline is visible immediately
    /// rather than at the next newline. Allocates a `Vec` of borrows, not of `String`s, so this
    /// is cheap enough to call per render.
    pub fn lines(&self) -> Vec<&str> {
        let mut lines: Vec<&str> = self.output.lines.iter().map(String::as_str).collect();
        if !self.output.pending.is_empty() {
            lines.push(&self.output.pending);
        }
        lines
    }

    /// Scrolls back `count` lines, clamped to the oldest retained line.
    pub fn scroll_up(&mut self, count: usize) {
        self.scroll_offset = self
            .scroll_offset
            .saturating_add(count)
            .min(self.max_scroll_offset());
    }

    /// Scrolls forward `count` lines. Reaching `0` re-pins to newest and resumes auto-follow.
    pub fn scroll_down(&mut self, count: usize) {
        self.scroll_offset = self.scroll_offset.saturating_sub(count);
    }

    /// `Home`: jump to the oldest retained line.
    pub fn scroll_to_oldest(&mut self) {
        self.scroll_offset = self.max_scroll_offset();
    }

    /// `End`: re-pin to newest, resuming auto-follow.
    pub fn scroll_to_newest(&mut self) {
        self.scroll_offset = 0;
    }

    /// Applies `frontend-components.md`'s key map. Returns whether the key was consumed.
    ///
    /// `page_height` is the caller's viewport height, for `PageUp`/`PageDown`. **Geometry,
    /// focus order, and which keys reach this pane at all are `renderer`'s**; what lives here
    /// is only the mapping this unit's own design documents, so the two cannot drift.
    ///
    /// `[k]` is live **only** in `running`: [`Self::cancel`]'s `Err` is swallowed rather than
    /// surfaced, because a key that does not apply is not an operator-facing error. It still
    /// reports `false` in that case, so the shell can pass the key on. (#321)
    pub fn handle_key(&mut self, key: KeyCode, page_height: usize) -> bool {
        match key {
            KeyCode::Up => self.scroll_up(1),
            KeyCode::Down => self.scroll_down(1),
            KeyCode::PageUp => self.scroll_up(page_height.max(1)),
            KeyCode::PageDown => self.scroll_down(page_height.max(1)),
            KeyCode::Home => self.scroll_to_oldest(),
            KeyCode::End => self.scroll_to_newest(),
            KeyCode::Char('k') => return self.cancel().is_ok(),
            KeyCode::Enter | KeyCode::Char(' ') => self.expand(),
            KeyCode::Esc => self.collapse(),
            _ => return false,
        }
        true
    }

    /// The footer text for the current state (NFR-3: every state is textually distinguishable).
    ///
    /// Kept separate from [`Self::footer_line`] so the **text** and the **style** are two
    /// decisions in two places. That is what lets the strip-styling guard assert the wording is
    /// self-sufficient without the guard being able to see a colour at all — which is the whole
    /// property NFR-5 claims. (#556)
    fn footer(&self) -> String {
        match self.state {
            PaneState::Collapsed => String::new(),
            PaneState::Running => RUNNING_INDICATOR.to_string(),
            // Exit code as TEXT, never colour alone (NFR-3, and the interaction spec states it
            // explicitly). `exit ?` rather than an empty footer if a caller somehow reaches a
            // terminal state without one: a missing exit code is worth showing.
            PaneState::Complete | PaneState::Empty => match self.exit_code {
                Some(code) => format!("exit {code}"),
                None => "exit ?".to_string(),
            },
            PaneState::Cancelled => CANCELLED_NOTICE.to_string(),
            PaneState::Refused => REFUSED_NOTICE.to_string(),
        }
    }

    /// The footer's semantic role. **Three-way on the exit code, not two-way** (#556).
    ///
    /// # `exit ?` is `warn`, and collapsing it into `error` would be a lie
    ///
    /// The obvious branch is "zero is good, anything else is bad", which puts a missing exit code
    /// in the same bucket as a failure. It is not one: `None` means the pane reached a terminal
    /// state *without* an exit code, which says nothing about whether the command succeeded. The
    /// comment on [`Self::footer`] already draws that distinction in text — "a missing exit code is
    /// worth showing" — and styling it `error` would assert a failure this pane has no evidence
    /// for. Three arms, and [`the_exit_footer_distinguishes_success_failure_and_absence`] holds
    /// them apart.
    ///
    /// `Cancelled` is `warn` for the same family of reason: `[k]` stopped this pane following the
    /// stream and the command is still running (see [`CANCELLED_NOTICE`]), so it is neither a
    /// success nor a failure.
    fn footer_style(&self) -> Style {
        match self.state {
            PaneState::Collapsed => self.theme.dim,
            PaneState::Running => self.theme.dim,
            PaneState::Complete | PaneState::Empty => match self.exit_code {
                Some(0) => self.theme.ok,
                Some(_) => self.theme.error,
                // NOT `error` — see the note above. A missing code is not a failure.
                None => self.theme.warn,
            },
            PaneState::Cancelled => self.theme.warn,
            PaneState::Refused => self.theme.error,
        }
    }

    /// The footer as a styled line: [`Self::footer`]'s text carrying [`Self::footer_style`]'s role.
    fn footer_line(&self) -> Line<'static> {
        Line::styled(self.footer(), self.footer_style())
    }

    /// The viewport body for the current state, as display lines.
    ///
    /// # Why this takes the viewport height (PR-3, and the defect it fixes)
    ///
    /// `Paragraph` renders the lines it is given from the **top** of its area. Handing it every
    /// line up to the scroll position therefore shows the operator the *oldest* lines and pushes
    /// the newest ones off the bottom — the opposite of a terminal's behaviour and of what
    /// "pinned to newest" means. Measured before the fix: with 20 lines in the buffer, offset 5,
    /// and an 8-row area, the pane rendered `line-0`..`line-6` while the newest visible line was
    /// supposed to be `line-14`.
    ///
    /// So the window is anchored at its **end**: it holds the last `height` lines of everything
    /// up to the scroll position. `height` is only available inside `render`, which is why it is
    /// a parameter rather than read from a field.
    ///
    /// The height is a **line** count, not a row count, so a wrapped line still occupies one
    /// slot here and the operator sees fewer lines than `height` when wrapping occurs.
    /// `Paragraph::line_count` would give the true post-wrap figure, but it is gated behind
    /// ratatui's `unstable-rendered-line-info` feature (`#[instability::unstable]`, and its own
    /// docs say the wrapping design "is not stable") — a correctness control resting on an
    /// unstable API is worse than this bounded imprecision, which costs at most a few lines of
    /// visible scrollback and never loses buffered content. (#321)
    fn body(&self, height: usize) -> Vec<Line<'_>> {
        let mut body: Vec<Line<'_>> = Vec::new();

        match self.state {
            // Never reached: `render` short-circuits the strip before calling this.
            PaneState::Collapsed => {}
            PaneState::Refused => {
                let reason = self
                    .refusal_reason
                    .as_deref()
                    .unwrap_or("the hand-off mechanism is unavailable and no reason was supplied");
                body.push(Line::styled(reason, self.theme.error));
                // The exact argv, on its own line so a terminal's line-select copies it whole.
                if let Some(command) = self.manual_command.as_deref() {
                    body.push(Line::from(""));
                    // The argv is deliberately NOT `error`, even though the refusal above is: it
                    // is the remedy, not the fault. Styling the thing the operator is meant to copy
                    // and run the same as the failure that produced it tells them to be alarmed by
                    // their own next step. Left unstyled rather than given a role — no role means
                    // "ordinary text", which is exactly what a command to run is. (#556)
                    body.push(Line::from(command));
                }
            }
            PaneState::Empty => body.push(Line::styled(EMPTY_NOTICE, self.theme.dim)),
            PaneState::Complete if self.policy == Some(Policy::Handoff) => {
                // The command's output went to the new window; an empty pane would read as a
                // failed run, so the structured outcome line stands in for it.
                body.push(Line::from(self.outcome.as_deref().unwrap_or(
                    "launched in a new window; its output is there, not here",
                )));
            }
            PaneState::Running | PaneState::Complete | PaneState::Cancelled => {
                if self.output.truncated {
                    // `dim`, not `warn`. Dropping the oldest lines is disclosure of a bounded
                    // buffer working as designed, not a problem — and SR-3 calls the marker "the
                    // security-relevant half" of the ring buffer precisely because it must be
                    // *present*, which is a text property no style can supply or remove.
                    body.push(Line::styled(TRUNCATION_MARKER, self.theme.dim));
                }
                let lines = self.lines();
                // The scroll offset counts up from the newest, so the window ENDS
                // `scroll_offset` lines before the end of the buffer.
                let end = lines.len().saturating_sub(self.scroll_offset);
                // ...and BEGINS one viewport earlier, so the newest visible line lands at the
                // bottom of the area rather than the window starting from the oldest line.
                // The marker, when present, occupies one of those rows.
                let rows = height.saturating_sub(body.len()).max(1);
                let start = end.saturating_sub(rows);
                body.extend(lines[start..end].iter().copied().map(Line::from));
            }
        }

        body
    }
}

impl Write for ResultsPane {
    /// Lets `renderer` pass `&mut pane` straight into `ServerClient::run(.., sink)`.
    ///
    /// Always reports the whole slice consumed: every byte is handed to the parser, and a
    /// short write would make the caller re-send bytes the parser has already seen. Infallible
    /// for the same reason [`ResultsPane::push_bytes`] is.
    fn write(&mut self, chunk: &[u8]) -> io::Result<usize> {
        self.push_bytes(chunk);
        Ok(chunk.len())
    }

    /// Nothing is buffered on the way out — the parser emits into the ring buffer eagerly,
    /// which is what PR-1 requires — so there is nothing to flush.
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl Widget for &ResultsPane {
    /// Pull-based: the pane renders itself and **never calls back into `Renderer`**, which is
    /// what keeps the dependency graph acyclic.
    ///
    /// The `match` on state is **exhaustive with no `_` arm**, so a seventh state is a compile
    /// error here rather than a blank pane at run time (`catalog.rs`'s idiom).
    ///
    /// Wrapping is enabled and is **display-only** (NFR-6): a line longer than the terminal
    /// wraps rather than truncating, and the ring buffer is untouched, so scroll positions do
    /// not shift when the terminal is resized.
    fn render(self, area: Rect, buf: &mut Buffer) {
        if area.is_empty() {
            return;
        }

        if self.state == PaneState::Collapsed {
            // FR-3.3: the strip is a rendered state, and it carries the count so the operator
            // can see there is something to expand into.
            let strip = format!("{COLLAPSED_PREFIX} ({})", self.lines().len());
            // `dim`: a collapsed pane is present but not where the operator is working. The `▸`
            // and the count are what say "there is something here", and both are text. (#556)
            buf.set_string(area.x, area.y, strip, self.theme.dim);
            return;
        }

        let [body_area, footer_area] =
            Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(area);

        Paragraph::new(Text::from(self.body(body_area.height as usize)))
            .wrap(Wrap { trim: false })
            .block(Block::new())
            .render(body_area, buf);

        // Rendered through the `Line`, so the style travels with the text rather than being applied
        // by position here. `set_string` with a separate `Style` argument would work identically
        // today and would put the style decision in the render site instead of next to the text it
        // describes — which is how a footer's wording and its meaning drift apart. (#556)
        self.footer_line().render(footer_area, buf);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        NotRunning, PaneState, Policy, ResultsPane, Theme, BUFFER_CAPACITY, CANCELLED_NOTICE,
        COLLAPSED_PREFIX, EMPTY_NOTICE, REFUSED_NOTICE, RUNNING_INDICATOR, TRUNCATION_MARKER,
    };
    use ratatui::backend::TestBackend;
    use ratatui::style::Color;
    use ratatui::Terminal;
    use std::io::Write;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    /// Renders `pane` into a `TestBackend` and returns every cell's symbol, concatenated.
    ///
    /// The assertions below run against **this**, not against a description of the screen: the
    /// buffer is the only place the rendered content can be inspected byte-for-byte.
    fn rendered_cells(pane: &ResultsPane, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height))
            .expect("a TestBackend terminal must construct");
        terminal
            .draw(|frame| frame.render_widget(pane, frame.area()))
            .expect("rendering into a TestBackend must not fail");
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(ratatui::buffer::Cell::symbol)
            .collect()
    }

    /// Test 9.1a — **ANSI escapes never enter the retained buffer** (SR-1).
    ///
    /// This is the assertion with teeth, and it is on the **model** because that is where the
    /// strip happens. Removing the strip makes the payload appear here immediately.
    ///
    /// **Proven by mutation.** Replacing `OutputBuffer`'s `vte`-driven `print` with a
    /// `from_utf8_lossy` + push — the exact shortcut SR-1's correction warns a developer would
    /// take — turns this red with the residue visible in the failure message.
    ///
    /// Six sequence *families* rather than one, because a hand-rolled `\x1b[` scanner passes
    /// the first and fails four of the rest, and "we strip ANSI" is usually only tested with
    /// the CSI colour case. (#321)
    #[test]
    fn ansi_escapes_never_enter_the_retained_buffer() {
        let hostile: &[(&str, &[u8], &str)] = &[
            ("CSI clear screen", b"before\x1b[2Jafter\n", "beforeafter"),
            ("CSI delete scrollback", b"a\x1b[3Jb\n", "ab"),
            ("CSI colour", b"\x1b[32mgreen\x1b[0m\n", "green"),
            ("OSC, BEL-terminated", b"a\x1b]0;pwned\x07b\n", "ab"),
            ("OSC, ST-terminated", b"a\x1b]0;pwned\x1b\\b\n", "ab"),
            ("single-character escape", b"a\x1bcb\n", "ab"),
            ("DCS", b"a\x1bPq#0;2;0;0;0\x1b\\b\n", "ab"),
            ("C1 CSI byte", b"a\x9b2Jb\n", "ab"),
            ("BEL and backspace", b"a\x07b\x08c\n", "abc"),
            ("DEL", b"a\x7fb\n", "ab"),
        ];

        for (label, bytes, expected) in hostile {
            let mut pane = ResultsPane::new();
            pane.attach(Policy::InApp);
            pane.push_bytes(bytes);

            let retained = pane.lines().join("\n");

            // The property, stated as the property: no control codepoint survives at all.
            let leaked: Vec<u32> = retained
                .chars()
                .map(|c| c as u32)
                .filter(|cp| *cp != 0x0a && char::from_u32(*cp).is_some_and(char::is_control))
                .collect();
            assert!(
                leaked.is_empty(),
                "{label}: control codepoints {leaked:x?} survived into the retained buffer. \
                 SR-1 requires control sequences be stripped BEFORE the bytes enter the render \
                 pipeline; an unstripped \\x1b[2J reaching the terminal clears the operator's \
                 screen and \\x1b[3J deletes their scrollback. Retained: {retained:?}"
            );

            // And the exact expected text, which is what catches a stripper that removes the
            // ESC but leaves the `[2J` payload behind as visible garbage.
            assert_eq!(
                retained, *expected,
                "{label}: the sequence must be consumed whole, payload included — stripping \
                 only the ESC byte leaves `[2J` on screen as text"
            );
        }
    }

    /// Test 9.1b — **the rendered CELLS contain no escape byte** (SR-1,
    /// `frontend-components.md:155-158`).
    ///
    /// # This test's literal requirement CANNOT fail, and saying so is the point
    ///
    /// The artifact requires asserting on the rendered cells. Measured in ratatui 0.30.2: the
    /// ESC byte **never reaches a `Cell` via `Paragraph`** regardless of what this module does,
    /// because `Span::styled_graphemes` and `Buffer::set_stringn` both
    /// `.filter(|g| !g.contains(char::is_control))`. So "no ESC in the cells" holds with the
    /// stripper deleted — a guard that cannot fire, reached by following the artifact exactly.
    ///
    /// Two things are therefore asserted instead of the one:
    ///
    /// 1. **No escape byte in the cells** — the artifact's requirement, kept because it is the
    ///    property SR-1 names and because `Cell::set_symbol` *is* public and bypasses that
    ///    filter (measured: it leaves `"\u{1b}[2J"` in a cell, and it survives
    ///    `Backend::draw`). If a future widget here writes cells directly, this fires.
    /// 2. **No `[2J` residue in the cells** — the half with teeth. Without stripping, ratatui
    ///    drops the ESC and the cells read `before[2Jafter`. Proven by mutation: this
    ///    assertion goes red, the escape-byte one stays green. (#321)
    #[test]
    fn ansi_escapes_are_stripped_before_the_bytes_reach_a_cell() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"before\x1b[2Jafter\n");
        pane.push_bytes(b"second\x1b[3Jline\n");

        let cells = rendered_cells(&pane, 40, 6);

        assert!(
            !cells.bytes().any(|byte| byte == 0x1b),
            "no rendered cell may contain an escape byte (0x1b): crossterm serialises cell \
             contents to the terminal and the terminal EXECUTES control sequences. Cells: \
             {cells:?}"
        );
        for residue in ["[2J", "[3J"] {
            assert!(
                !cells.contains(residue),
                "the cells still contain {residue:?}: the sequence must be consumed whole, not \
                 have its ESC byte dropped by ratatui's grapheme filter while the payload \
                 renders as visible text. Cells: {cells:?}"
            );
        }
        assert!(
            cells.contains("beforeafter") && cells.contains("secondline"),
            "the surrounding text must survive — a stripper that ate the payload AND the \
             output would pass the assertions above while showing the operator nothing. \
             Cells: {cells:?}"
        );
    }

    /// Test 9.2 — **incrementality: output is present BEFORE the stream ends** (PR-1).
    ///
    /// A test that only inspects the final buffer cannot tell a streaming pane from one that
    /// buffers to completion — both end up with the same content. So this drives a **real
    /// producer thread** that sends three chunks with a gap between them and only then signals
    /// completion, and asserts the pane has *rendered* chunk 1 while the producer is still
    /// mid-stream.
    ///
    /// Two properties, and the second is the one `strip-ansi-escapes`' `Writer` would fail:
    /// output is visible before the stream ends, **and** a chunk with no trailing newline is
    /// visible without waiting for one. A `LineWriter`-backed stripper withholds
    /// `chunk-1-no-newline` entirely until a newline arrives — measured, see the module docs.
    ///
    /// No sleep on the assertion path: the channel `recv()` blocks until the producer has
    /// actually sent, so the test is ordered by the channel rather than by a timeout, and the
    /// producer's `done` flag is checked to prove the stream had not finished. (#321)
    #[test]
    fn output_renders_incrementally_before_the_stream_ends() {
        let (chunks_tx, chunks_rx) = mpsc::channel::<Vec<u8>>();
        let (done_tx, done_rx) = mpsc::channel::<()>();

        let producer = thread::spawn(move || {
            for chunk in [
                b"chunk-1-no-newline".to_vec(),
                b"\nchunk-2\n".to_vec(),
                b"chunk-3\n".to_vec(),
            ] {
                chunks_tx.send(chunk).expect("the pane side must be alive");
                thread::sleep(Duration::from_millis(20));
            }
            done_tx.send(()).expect("the pane side must be alive");
        });

        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);

        let first = chunks_rx.recv().expect("the producer must send chunk 1");
        pane.push_bytes(&first);

        // The stream has NOT ended: the producer has two chunks left and has not signalled.
        assert_eq!(
            done_rx.try_recv(),
            Err(mpsc::TryRecvError::Empty),
            "the producer must still be mid-stream for this test to mean anything; if it has \
             already finished, the assertion below proves nothing about incrementality"
        );

        let cells = rendered_cells(&pane, 30, 5);
        assert!(
            cells.contains("chunk-1-no-newline"),
            "the first chunk must be RENDERED while the stream is still open (PR-1). A pane \
             that buffers to completion makes a slow command indistinguishable from a hang, \
             and one that waits for a newline hides a progress line entirely. Cells: {cells:?}"
        );
        assert_eq!(
            pane.state(),
            PaneState::Running,
            "the pane must still be `running`, not completed, at this point"
        );

        // Drain the rest so the producer's sends cannot block, then confirm nothing was lost.
        while let Ok(chunk) = chunks_rx.recv() {
            pane.push_bytes(&chunk);
        }
        producer.join().expect("the producer thread must not panic");
        assert_eq!(
            pane.lines(),
            vec!["chunk-1-no-newline", "chunk-2", "chunk-3"],
            "every chunk must survive the incremental path, split across chunk boundaries \
             included"
        );
    }

    /// Test 9.3 — **the truncation MARKER renders past the cap**, not merely the cap (SR-3).
    ///
    /// Bounded memory without disclosure is silent loss: an operator scrolling up would draw
    /// conclusions from output that was dropped without saying so. So this asserts the cap
    /// *and* the marker, and the marker assertion is on the rendered cells because that is
    /// where the operator would see it.
    ///
    /// **Proven by mutation.** Deleting `self.truncated = true` from `commit_line` leaves the
    /// cap working perfectly and turns this red — which is exactly the defect SR-3 names: a
    /// bounded buffer that loses lines quietly. (#321)
    #[test]
    fn truncation_marker_renders_once_lines_are_dropped() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);

        assert!(
            !pane.truncated(),
            "a fresh pane must not claim truncation, or the marker would be meaningless"
        );

        let overflow = 25;
        for line in 0..BUFFER_CAPACITY + overflow {
            pane.push_bytes(format!("line-{line}\n").as_bytes());
        }

        assert!(
            pane.truncated(),
            "dropping lines must set the truncation flag"
        );
        assert_eq!(
            pane.lines().len(),
            BUFFER_CAPACITY,
            "the buffer must hold exactly {BUFFER_CAPACITY} lines (PR-2)"
        );
        assert_eq!(
            pane.lines().first().copied(),
            Some(format!("line-{overflow}").as_str()),
            "the OLDEST lines must be the ones discarded"
        );
        assert_eq!(
            pane.lines().last().copied(),
            Some(format!("line-{}", BUFFER_CAPACITY + overflow - 1).as_str()),
            "the newest line must be retained"
        );

        // Wide enough that the marker is not wrapped mid-string by the display-only wrap.
        let cells = rendered_cells(&pane, 60, 8);
        assert!(
            cells.contains(TRUNCATION_MARKER),
            "the truncation marker must be RENDERED once lines are dropped (SR-3): bounded \
             memory without a visible marker is silent loss, which is the failure class this \
             intent exists to eliminate. Cells: {cells:?}"
        );
    }

    /// The marker's literal must state the real capacity.
    ///
    /// The marker is a hard-coded string so a test can assert the exact operator-facing text;
    /// this keeps that literal honest against [`BUFFER_CAPACITY`]. Changing the cap to 5,000
    /// and leaving the message saying 10,000 turns this red — a marker that misstates how much
    /// was kept is barely better than no marker.
    #[test]
    fn the_truncation_marker_states_the_actual_capacity() {
        // `10,000` with the thousands separator the message uses.
        let formatted = "10,000";
        assert_eq!(
            BUFFER_CAPACITY, 10_000,
            "if the capacity changes, TRUNCATION_MARKER's text must change with it"
        );
        assert!(
            TRUNCATION_MARKER.contains(formatted),
            "the marker must state the actual buffer limit: {TRUNCATION_MARKER:?}"
        );
    }

    /// Test 9.4 — **HANDOFF + empty buffer renders the OUTCOME line, not the `empty` state.**
    ///
    /// Step 5's whole point. A hand-off's output went to the new window, so an empty buffer is
    /// *expected*; rendering "command produced no output" would tell the operator a command
    /// that ran fine produced nothing, which reads as a failed run.
    ///
    /// **Proven by mutation.** Reordering `complete`'s match so the emptiness arm precedes the
    /// `Handoff` arm — the natural way to write it, and the way that looks equivalent — turns
    /// this red on both the state and the rendered text.
    ///
    /// The IN-APP half is asserted in the same test on purpose: the two arms are only
    /// meaningful against each other, and a test that checked HANDOFF alone would pass if
    /// `empty` were unreachable for everybody. (#321)
    #[test]
    fn handoff_completion_with_an_empty_buffer_renders_the_outcome_line() {
        let outcome = "launched in new window · session: my-session · exit 0";

        let mut handoff = ResultsPane::new();
        handoff.attach(Policy::Handoff);
        // No bytes at all — the hand-off's output went elsewhere.
        handoff.complete(0, Some(outcome.to_string()));

        assert_eq!(
            handoff.state(),
            PaneState::Complete,
            "a HANDOFF completion must be `complete` even with an empty buffer — the emptiness \
             is expected there, not the `empty` state"
        );
        let cells = rendered_cells(&handoff, 60, 5);
        assert!(
            cells.contains(outcome),
            "the structured outcome line must be rendered, stating what was launched and where \
             it went; an empty pane would read as a failed run. Cells: {cells:?}"
        );
        assert!(
            !cells.contains(EMPTY_NOTICE),
            "a HANDOFF pane must NEVER say {EMPTY_NOTICE:?}: the command ran fine and its \
             output is in the new window. Cells: {cells:?}"
        );
        assert!(
            cells.contains("exit 0"),
            "the exit code must render as TEXT, never colour alone (NFR-3). Cells: {cells:?}"
        );

        // The other arm, which is what makes the branch non-interchangeable.
        let mut in_app = ResultsPane::new();
        in_app.attach(Policy::InApp);
        in_app.complete(0, None);
        assert_eq!(
            in_app.state(),
            PaneState::Empty,
            "an IN-APP command that exited with no bytes IS the `empty` state — if this were \
             also `complete`, the assertion above would prove nothing about the branch"
        );
        let in_app_cells = rendered_cells(&in_app, 60, 5);
        assert!(
            in_app_cells.contains(EMPTY_NOTICE),
            "the `empty` state must state so explicitly, never render a blank box (FR-6.2). \
             Cells: {in_app_cells:?}"
        );
    }

    /// Test 9.5 — **`cancel()` when not running is `Err(NotRunning)` with state unchanged.**
    ///
    /// Every non-`running` state is exercised, not just one: `cancel` guards on `!= Running`,
    /// so a single case would pass against a guard that only special-cased `Collapsed`.
    #[test]
    fn cancel_outside_running_errors_and_leaves_the_state_alone() {
        // `collapsed` — never attached.
        let mut fresh = ResultsPane::new();
        assert_eq!(fresh.cancel(), Err(NotRunning));
        assert_eq!(fresh.state(), PaneState::Collapsed);

        // `complete`
        let mut complete = ResultsPane::new();
        complete.attach(Policy::InApp);
        complete.push_bytes(b"output\n");
        complete.complete(0, None);
        assert_eq!(complete.state(), PaneState::Complete);
        assert_eq!(complete.cancel(), Err(NotRunning));
        assert_eq!(
            complete.state(),
            PaneState::Complete,
            "a rejected cancel must not move the pane out of `complete`"
        );

        // `empty`
        let mut empty = ResultsPane::new();
        empty.attach(Policy::InApp);
        empty.complete(3, None);
        assert_eq!(empty.state(), PaneState::Empty);
        assert_eq!(empty.cancel(), Err(NotRunning));
        assert_eq!(empty.state(), PaneState::Empty);

        // `refused`
        let mut refused = ResultsPane::new();
        refused.attach(Policy::Handoff);
        refused.refuse("no tmux client".to_string(), None);
        assert_eq!(refused.state(), PaneState::Refused);
        assert_eq!(refused.cancel(), Err(NotRunning));
        assert_eq!(refused.state(), PaneState::Refused);

        // `cancelled` — a second cancel is also an error, not an idempotent success.
        let mut cancelled = ResultsPane::new();
        cancelled.attach(Policy::InApp);
        assert_eq!(cancelled.cancel(), Ok(()));
        assert_eq!(cancelled.cancel(), Err(NotRunning));
        assert_eq!(cancelled.state(), PaneState::Cancelled);
    }

    /// Test 9.6 — **`cancel()` then a late completion stays `cancelled`.**
    ///
    /// The command keeps running server-side, so a completion *can* arrive after the operator
    /// stopped following. Letting it overwrite the state would silently undo a deliberate
    /// action and replace an honest "still running" with a claim the command finished.
    ///
    /// The rendered wording is asserted here too, because the state enum being `Cancelled` is
    /// not the property that matters to the operator — the sentence is.
    #[test]
    fn a_late_completion_does_not_overwrite_a_cancel() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"partial output\n");
        assert_eq!(pane.cancel(), Ok(()));

        pane.complete(0, None);

        assert_eq!(
            pane.state(),
            PaneState::Cancelled,
            "a completion arriving after `cancel()` must NOT move the pane to `complete`: the \
             operator stopped following, and silently overwriting that misrepresents both \
             their action and the command's state"
        );
        let cells = rendered_cells(&pane, 60, 5);
        assert!(
            cells.contains(CANCELLED_NOTICE),
            "the cancel notice must survive the late completion. Cells: {cells:?}"
        );
        assert!(
            cells.contains("partial output"),
            "output received before the cancel must still be shown. Cells: {cells:?}"
        );
    }

    /// **SR-4: the `cancelled` wording must not claim the command stopped.**
    ///
    /// Its own test, because the requirement is about the *sentence*, not the state. Asserting
    /// the state is `Cancelled` would pass with a footer reading "Cancelled" — the exact
    /// wording SR-4 forbids, because the server-side work continues and the operator would
    /// believe otherwise.
    #[test]
    fn the_cancelled_wording_does_not_claim_the_command_stopped() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"still going\n");
        assert_eq!(pane.cancel(), Ok(()));

        let cells = rendered_cells(&pane, 60, 4);

        assert!(
            cells.contains("stopped following"),
            "the footer must say the PANE stopped following. Cells: {cells:?}"
        );
        assert!(
            cells.contains("the command is still running"),
            "the footer must state that the command CONTINUES; `[k]` stops consuming the \
             stream and there is no command-level cancel to send. Cells: {cells:?}"
        );
        for overstatement in ["Cancelled", "cancelled", "killed", "terminated", "aborted"] {
            assert!(
                !cells.contains(overstatement),
                "the notice must not contain {overstatement:?} (SR-4): a pane implying the \
                 command was stopped misrepresents server state, which is the silent-mismatch \
                 class this intent exists to eliminate. Cells: {cells:?}"
            );
        }
    }

    /// Test 9.7 — **non-UTF-8 bytes render lossily and never panic** (SR-2).
    ///
    /// A panic in the front door on malformed output is a denial of the operator's own tool.
    /// Includes a multi-byte character deliberately **split across two chunks**, which a
    /// per-chunk `String::from_utf8_lossy` would corrupt into two replacement characters —
    /// the shape of bug this only catches because the parser is persistent.
    #[test]
    fn non_utf8_output_renders_lossily_without_panicking() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);

        // Lone continuation bytes, an overlong-looking lead byte, and the invalid 0xFF/0xFE.
        pane.push_bytes(&[b'a', 0xff, 0xfe, b'b', 0x80, b'c', b'\n']);
        // A valid 3-byte UTF-8 character (U+2713 ✓) split across two pushes.
        pane.push_bytes(&[0xe2, 0x9c]);
        pane.push_bytes(&[0x93, b'\n']);

        let lines = pane.lines();
        assert_eq!(lines.len(), 2, "both lines must be retained: {lines:?}");
        assert!(
            lines[0].contains('a') && lines[0].contains('b') && lines[0].contains('c'),
            "the valid characters around the invalid bytes must survive: {:?}",
            lines[0]
        );
        assert!(
            lines[0].contains('\u{fffd}'),
            "invalid bytes must become replacement characters rather than being dropped \
             silently or panicking: {:?}",
            lines[0]
        );
        assert_eq!(
            lines[1], "✓",
            "a multi-byte character split across two chunks must reassemble, not become two \
             replacement characters — this is why the parser persists across chunks"
        );

        // And it renders, which is the other half of "never panics".
        let cells = rendered_cells(&pane, 20, 4);
        assert!(cells.contains('✓'), "the reassembled character must render");
    }

    /// Test 9.8 — **a scrolled-back position holds across new output** (PR-3).
    ///
    /// The non-obvious half: the offset counts up from the newest, so *holding the operator's
    /// view* requires the offset to **increase** as lines arrive. Leaving the number unchanged
    /// slides the window toward the bottom while looking like it did nothing.
    ///
    /// The assertion is therefore on the **content the operator is looking at**, not on the
    /// offset number — a test that only checked `scroll_offset` unchanged would pass against
    /// exactly the defect PR-3 forbids.
    #[test]
    fn a_scrolled_back_position_holds_when_new_output_arrives() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        for line in 0..20 {
            pane.push_bytes(format!("line-{line}\n").as_bytes());
        }

        // Scroll back so the newest visible line is `line-14`.
        pane.scroll_up(5);
        let before = rendered_cells(&pane, 20, 8);
        assert!(
            before.contains("line-14"),
            "setup: line-14 must be the newest visible line. Cells: {before:?}"
        );
        assert!(
            !before.contains("line-19"),
            "setup: the scrolled-back view must NOT already show the newest line. Cells: \
             {before:?}"
        );

        for line in 20..30 {
            pane.push_bytes(format!("line-{line}\n").as_bytes());
        }

        let after = rendered_cells(&pane, 20, 8);
        assert!(
            after.contains("line-14"),
            "the operator's view must HOLD: line-14 was the newest visible line and must stay \
             visible after 10 new lines arrive. New output yanking a reading operator to the \
             bottom is what PR-3 forbids. Cells: {after:?}"
        );
        assert!(
            !after.contains("line-29"),
            "the view must not have jumped to the newest line. Cells: {after:?}"
        );

        // And pressing End re-pins, resuming auto-follow.
        pane.scroll_to_newest();
        assert_eq!(pane.scroll_offset(), 0);
        let pinned = rendered_cells(&pane, 20, 8);
        assert!(
            pinned.contains("line-29"),
            "End must re-pin to the newest line. Cells: {pinned:?}"
        );
        pane.push_bytes(b"line-30\n");
        let followed = rendered_cells(&pane, 20, 8);
        assert!(
            followed.contains("line-30"),
            "once re-pinned, new output must scroll with the view. Cells: {followed:?}"
        );
    }

    /// **A buffer longer than the viewport shows its NEWEST lines, not its oldest.**
    ///
    /// Its own test because it is the defect this module actually shipped with, and because the
    /// scrolled-back test above only caught it by accident — through a setup assertion, which
    /// reports as a broken fixture rather than as the requirement it is.
    ///
    /// `Paragraph` renders from the top of its area, so handing it every line up to the scroll
    /// position renders the OLDEST lines and pushes the newest off the bottom. Measured with the
    /// defect present: 20 lines, pinned at offset 0, in an 8-row area rendered
    /// `line-0`..`line-6`. A pane that streams a command's output while showing only its first
    /// screenful is a pane the operator cannot use — and every assertion in this module that
    /// checks a *short* buffer stays green through it, which is why this needs a long one.
    ///
    /// Proven by mutation: reverting `body`'s window to `lines[..end]` turns this red. (#321)
    #[test]
    fn a_buffer_longer_than_the_viewport_shows_the_newest_lines() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        for line in 0..40 {
            pane.push_bytes(format!("line-{line}\n").as_bytes());
        }

        // Pinned to newest (offset 0), which is the auto-follow default.
        assert_eq!(pane.scroll_offset(), 0, "setup: the pane must be pinned");

        // 8 rows: 7 body lines plus the footer row.
        let cells = rendered_cells(&pane, 20, 8);

        assert!(
            cells.contains("line-39"),
            "a pane pinned to newest MUST render the newest line — this is what `running` means \
             to an operator watching a command. Cells: {cells:?}"
        );
        assert!(
            !cells.contains("line-0 "),
            "the oldest line must have scrolled off: rendering from the top of the buffer shows \
             the operator the first screenful of a 40-line stream forever. Cells: {cells:?}"
        );
        assert!(
            cells.contains(RUNNING_INDICATOR),
            "the footer must survive the body filling the area. Cells: {cells:?}"
        );
    }

    /// **An 8-bit C1 sequence's PAYLOAD is stripped, not just its introducer** (SR-1).
    ///
    /// Its own test because it is a real gap that the family sweep above found, and because it is
    /// the precise shape of a half-fix: the dangerous byte is removed and the message it carried
    /// is left on screen. `\x9b` is the single-byte equivalent of `\x1b[`, so it is exactly what
    /// gets used to slip past a scanner looking for the two-byte form.
    ///
    /// Measured: `vte` dispatches `\x9b` to `execute` (where it is dropped) but then reports the
    /// following `2J` through `print` as ordinary text, so the buffer retained `a2Jb`. The 7-bit
    /// form is handled entirely inside `vte` and leaks nothing — which is why testing only the
    /// `\x1b[` form would have left this defect invisible.
    ///
    /// Proven by mutation: making the C1 introducers a no-op in `execute` turns this red with
    /// `left: "a2Jb"`. (#321)
    #[test]
    fn an_eight_bit_c1_sequence_leaks_neither_its_introducer_nor_its_payload() {
        // Each C1 introducer, its payload, and the text that must survive around it.
        let cases: &[(&str, &[u8], &str)] = &[
            ("C1 CSI (0x9b) clear screen", b"a\x9b2Jb\n", "ab"),
            ("C1 CSI delete scrollback", b"a\x9b3Jb\n", "ab"),
            (
                "C1 OSC (0x9d), BEL-terminated",
                b"a\x9d0;pwned\x07b\n",
                "ab",
            ),
            ("C1 DCS (0x90)", b"a\x90q#0\x9cb\n", "ab"),
            ("C1 APC (0x9f)", b"a\x9fpayload\x9cb\n", "ab"),
        ];

        for (label, bytes, expected) in cases {
            let mut pane = ResultsPane::new();
            pane.attach(Policy::InApp);
            pane.push_bytes(bytes);
            assert_eq!(
                pane.lines().join("\n"),
                *expected,
                "{label}: the introducer AND its parameter bytes must be consumed. Dropping only \
                 the introducer leaves the payload as visible garbage in the operator's output — \
                 a half-fix that looks like stripping"
            );
        }

        // The bound on the suppression window: a C1 sequence with no terminator must not eat more
        // than the rest of its own line, or one malformed byte would silently discard every line
        // that followed it — the same silent loss the truncation marker exists to prevent.
        //
        // A string-type introducer is used because it is the family with no printable terminator,
        // so it is the one that would run away. `0;12345` is deliberately all parameter bytes: an
        // unterminated CSI cannot demonstrate this, because `0x40..=0x7E` covers the letters and
        // `\x9bu` is therefore a COMPLETE CSI sequence — measured, and the reason this assertion
        // was wrong on first writing.
        let mut unterminated = ResultsPane::new();
        unterminated.attach(Policy::InApp);
        unterminated.push_bytes(b"a\x9d0;12345\nnext-line\n");
        assert_eq!(
            unterminated.lines(),
            vec!["a", "next-line"],
            "an unterminated C1 string sequence must be bounded by the newline: the line it \
             opened on loses its remainder, but the output that follows must survive"
        );

        // And the CSI family's real bound, stated with a payload that is genuinely all parameter
        // bytes so the sequence is still open when the newline arrives.
        let mut open_csi = ResultsPane::new();
        open_csi.attach(Policy::InApp);
        open_csi.push_bytes(b"a\x9b38;5\nnext-line\n");
        assert_eq!(
            open_csi.lines(),
            vec!["a", "next-line"],
            "an unterminated CSI payload must also be closed by the newline rather than \
             suppressing the following line"
        );
    }

    /// Test 9.9 — **`complete()` called twice: the second is ignored.**
    ///
    /// Both the exit code and the state must come from the *first* call. Asserting only the
    /// state would pass against an implementation that kept the state but overwrote the exit
    /// code, which is the same defect wearing a smaller hat.
    #[test]
    fn a_second_completion_is_ignored_and_the_first_result_stands() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"output\n");

        pane.complete(0, None);
        pane.complete(1, None);

        assert_eq!(pane.state(), PaneState::Complete);
        let cells = rendered_cells(&pane, 30, 4);
        assert!(
            cells.contains("exit 0"),
            "the FIRST exit code must stand. Cells: {cells:?}"
        );
        assert!(
            !cells.contains("exit 1"),
            "the second completion must not overwrite the first result. Cells: {cells:?}"
        );

        // And a refusal after completion is ignored too — same guard, other caller.
        pane.refuse("too late".to_string(), Some("tmux ls".to_string()));
        assert_eq!(
            pane.state(),
            PaneState::Complete,
            "a refusal arriving after completion must not replace the result"
        );
    }

    /// Test 9.10 — **the refusal argv is reproduced byte-for-byte** (FR-5.3).
    ///
    /// Its whole purpose is to be **copied and run**, so a paraphrase is useless and a mangled
    /// one is worse than nothing. The expectation is a **hard-coded literal**: deriving it from
    /// the value passed in would make the test agree with whatever the pane happened to store,
    /// so it would stay green through the exact mangling it exists to catch.
    ///
    /// The reason is asserted in full as well, because `frontend-components.md` specifies the
    /// viewport carries the *full* refusal reason with only a brief notice in the footer.
    #[test]
    fn the_refusal_argv_is_reproduced_exactly() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::Handoff);
        pane.refuse(
            "$TMUX is unset, so there is no tmux client to move".to_string(),
            Some("tmux attach-session -t work:planner-1".to_string()),
        );

        assert_eq!(pane.state(), PaneState::Refused);

        // 70 columns so neither string is wrapped by the display-only wrap.
        let cells = rendered_cells(&pane, 70, 6);

        assert!(
            cells.contains("tmux attach-session -t work:planner-1"),
            "the manual command must appear EXACTLY as given — its purpose is to be copied and \
             run, and a paraphrase or a re-spaced argv is useless (FR-5.3). Cells: {cells:?}"
        );
        assert!(
            cells.contains("$TMUX is unset, so there is no tmux client to move"),
            "the FULL refusal reason belongs in the viewport, not a truncated summary. Cells: \
             {cells:?}"
        );

        // A refusal with no correct command to offer must not fabricate one.
        let mut no_command = ResultsPane::new();
        no_command.attach(Policy::Handoff);
        no_command.refuse(
            "the terminal backend could not be resolved".to_string(),
            None,
        );
        let bare = rendered_cells(&no_command, 70, 6);
        assert!(
            bare.contains("the terminal backend could not be resolved"),
            "the reason must render even when no command can be offered. Cells: {bare:?}"
        );
        assert!(
            !bare.contains("tmux"),
            "with no manual command available the pane must NOT invent one: a confident wrong \
             argv is worse than none. Cells: {bare:?}"
        );
    }

    /// `collapsed` is a real rendered state carrying a count (FR-3.3).
    ///
    /// Not a hidden widget: FR-3.3 requires the pane to *expand from* the strip, so the strip
    /// must be present on screen. Also covers the collapse/expand round trip, which is the
    /// mechanism by which "expands from the strip" is observable.
    #[test]
    fn the_collapsed_strip_is_rendered_and_expands_back_to_its_state() {
        let mut pane = ResultsPane::new();
        assert_eq!(pane.state(), PaneState::Collapsed);

        let strip = rendered_cells(&pane, 20, 1);
        assert!(
            strip.contains("▸ results (0)"),
            "the collapsed strip must render with its count. Cells: {strip:?}"
        );

        pane.attach(Policy::InApp);
        pane.push_bytes(b"a\nb\nc\n");
        assert_eq!(pane.state(), PaneState::Running);

        pane.collapse();
        assert_eq!(pane.state(), PaneState::Collapsed);
        let collapsed = rendered_cells(&pane, 20, 1);
        assert!(
            collapsed.contains("▸ results (3)"),
            "the strip must report the retained line count so the operator can see there is \
             something to expand into. Cells: {collapsed:?}"
        );

        pane.expand();
        assert_eq!(
            pane.state(),
            PaneState::Running,
            "expanding must restore the state the pane was collapsed FROM, not guess one"
        );
        let expanded = rendered_cells(&pane, 20, 5);
        assert!(
            expanded.contains('a') && expanded.contains('c'),
            "the retained output must survive a collapse/expand round trip. Cells: {expanded:?}"
        );

        // Expanding a never-run pane is a no-op: an expanded blank box violates FR-6.2.
        let mut never_run = ResultsPane::new();
        never_run.expand();
        assert_eq!(never_run.state(), PaneState::Collapsed);
    }

    /// The pane is a `ServerClient::run` sink, which is the seam FR-3.2 needs.
    ///
    /// `ServerClient::run` takes `sink: &mut W where W: Write`. This asserts a `ResultsPane`
    /// satisfies that bound and that bytes written through the `Write` impl land in the buffer
    /// stripped — so wiring the pane to production is a one-line change rather than the
    /// bespoke glue the predecessor's pane needed and never got.
    ///
    /// The generic function is what makes this more than a call to `write!`: it is the same
    /// bound `run` declares, so this stops compiling if the impl is removed.
    #[test]
    fn the_pane_is_usable_as_a_write_sink_for_streamed_output() {
        fn stream_into<W: Write>(sink: &mut W, chunks: &[&[u8]]) -> std::io::Result<()> {
            for chunk in chunks {
                sink.write_all(chunk)?;
            }
            sink.flush()
        }

        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        stream_into(&mut pane, &[b"first\n", b"\x1b[2Jsecond\n"])
            .expect("writing to a ResultsPane is infallible");

        assert_eq!(
            pane.lines(),
            vec!["first", "second"],
            "bytes written through the Write impl must be retained AND stripped — the sink \
             path must not bypass SR-1"
        );
    }

    /// The documented key map, and `[k]` being live only in `running`.
    ///
    /// `frontend-components.md` § Interaction flows specifies these bindings for this
    /// component; geometry and focus order are `renderer`'s. `[k]` outside `running` must be
    /// *ignored*, not surfaced as an error.
    #[test]
    fn the_key_map_scrolls_and_cancels_only_while_running() {
        use crossterm::event::KeyCode;

        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        for line in 0..50 {
            pane.push_bytes(format!("line-{line}\n").as_bytes());
        }

        assert!(pane.handle_key(KeyCode::Up, 10));
        assert_eq!(pane.scroll_offset(), 1, "Up scrolls one line");
        assert!(pane.handle_key(KeyCode::PageUp, 10));
        assert_eq!(pane.scroll_offset(), 11, "PageUp scrolls one viewport");
        assert!(pane.handle_key(KeyCode::PageDown, 10));
        assert_eq!(
            pane.scroll_offset(),
            1,
            "PageDown scrolls back one viewport"
        );
        assert!(pane.handle_key(KeyCode::Down, 10));
        assert_eq!(pane.scroll_offset(), 0, "Down re-pins at zero");
        assert!(pane.handle_key(KeyCode::Home, 10));
        assert_eq!(pane.scroll_offset(), 49, "Home jumps to the oldest line");
        assert!(pane.handle_key(KeyCode::End, 10));
        assert_eq!(pane.scroll_offset(), 0, "End re-pins to newest");

        // `[k]` while running cancels.
        assert!(
            pane.handle_key(KeyCode::Char('k'), 10),
            "[k] must be consumed while running"
        );
        assert_eq!(pane.state(), PaneState::Cancelled);

        // `[k]` again is ignored, and reported as not consumed rather than raised.
        assert!(
            !pane.handle_key(KeyCode::Char('k'), 10),
            "[k] outside `running` must be IGNORED, not surfaced as an operator-facing error"
        );
        assert_eq!(pane.state(), PaneState::Cancelled);

        // An unmapped key is not consumed, so the shell can act on it.
        assert!(!pane.handle_key(KeyCode::Char('z'), 10));
    }

    /// Every state renders something the operator can read, with no colour dependency (NFR-3).
    ///
    /// The point is the absence of a blank pane in *any* state — FR-6.2 forbids rendering an
    /// empty box, and a state added later without a body would slip past every test above.
    #[test]
    fn every_state_renders_distinguishable_text() {
        let mut cases: Vec<(PaneState, ResultsPane)> = Vec::new();

        cases.push((PaneState::Collapsed, ResultsPane::new()));

        let mut running = ResultsPane::new();
        running.attach(Policy::InApp);
        running.push_bytes(b"working\n");
        cases.push((PaneState::Running, running));

        let mut complete = ResultsPane::new();
        complete.attach(Policy::InApp);
        complete.push_bytes(b"rows\n");
        complete.complete(0, None);
        cases.push((PaneState::Complete, complete));

        let mut empty = ResultsPane::new();
        empty.attach(Policy::InApp);
        empty.complete(2, None);
        cases.push((PaneState::Empty, empty));

        let mut cancelled = ResultsPane::new();
        cancelled.attach(Policy::InApp);
        cancelled.push_bytes(b"partial\n");
        cancelled.cancel().expect("running panes may be cancelled");
        cases.push((PaneState::Cancelled, cancelled));

        let mut refused = ResultsPane::new();
        refused.attach(Policy::Handoff);
        refused.refuse("no client".to_string(), Some("tmux ls".to_string()));
        cases.push((PaneState::Refused, refused));

        assert_eq!(
            cases.len(),
            6,
            "all SIX states must be covered — the interaction spec's five plus `cancelled` \
             from the Q1 ruling"
        );

        for (expected, pane) in &cases {
            assert_eq!(pane.state(), *expected, "fixture must be in {expected:?}");
            let cells = rendered_cells(pane, 60, 6);
            assert!(
                cells.trim().chars().any(|c| !c.is_whitespace()),
                "{expected:?} rendered a blank pane; every state must be textually \
                 distinguishable (NFR-3) and never an empty box (FR-6.2)"
            );
            assert!(
                !cells.bytes().any(|byte| byte == 0x1b),
                "{expected:?} put an escape byte into a rendered cell"
            );
        }
    }

    /// **F-1 (§12a review) — the cross-chunk property that justified choosing `vte`.**
    ///
    /// A 7-bit escape SPLIT ACROSS TWO `push_bytes` CALLS must not leak. This is the primary
    /// stated reason `strip-ansi-escapes` was rejected, and until this test nothing defended it:
    /// a future change allocating a fresh `vte::Parser::new()` per call — exactly the antipattern
    /// the module doc warns about — would have left all 20 tests green while the advantage
    /// vanished silently.
    ///
    /// A stateless stripper yields `"before"` + `"[2Jafter"`, leaking the payload as visible
    /// garbage. The persistent parser carries the half-open state across the boundary. (#321)
    #[test]
    fn an_escape_split_across_two_pushes_does_not_leak_its_payload() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);

        // The ESC arrives at the very end of one chunk; its introducer and payload in the next.
        pane.push_bytes(b"before\x1b");
        pane.push_bytes(b"[2Jafter\n");

        let lines = pane.lines();
        assert_eq!(
            lines,
            vec!["beforeafter"],
            "an escape split across two reads must be consumed whole. A stateless stripper \
             yields \"before\" + \"[2Jafter\", leaking `[2J` as visible garbage — the failure \
             this crate chose `vte` to avoid. Got: {lines:?}"
        );
        let joined = lines.join("");
        assert!(
            !joined.contains("2J"),
            "the payload `2J` must not survive the chunk boundary. Got: {joined:?}"
        );
    }

    // ── #556: the semantic colour layer ──────────────────────────────────────────────────────

    /// One pane per [`PaneState`], each in its real state via the public API.
    ///
    /// Shared by the styling tests below and built by driving the pane rather than by setting
    /// fields: a fixture that assigned `state` directly could be in a state the API cannot
    /// actually produce, and then the tests would be about a pane that does not exist.
    fn one_pane_per_state() -> Vec<(PaneState, ResultsPane)> {
        let mut cases: Vec<(PaneState, ResultsPane)> = Vec::new();

        cases.push((PaneState::Collapsed, ResultsPane::new()));

        let mut running = ResultsPane::new();
        running.attach(Policy::InApp);
        running.push_bytes(b"working\n");
        cases.push((PaneState::Running, running));

        let mut complete = ResultsPane::new();
        complete.attach(Policy::InApp);
        complete.push_bytes(b"rows\n");
        complete.complete(0, None);
        cases.push((PaneState::Complete, complete));

        let mut empty = ResultsPane::new();
        empty.attach(Policy::InApp);
        empty.complete(2, None);
        cases.push((PaneState::Empty, empty));

        let mut cancelled = ResultsPane::new();
        cancelled.attach(Policy::InApp);
        cancelled.push_bytes(b"partial\n");
        cancelled.cancel().expect("running panes may be cancelled");
        cases.push((PaneState::Cancelled, cancelled));

        let mut refused = ResultsPane::new();
        refused.attach(Policy::Handoff);
        refused.refuse("no client".to_string(), Some("tmux ls".to_string()));
        cases.push((PaneState::Refused, refused));

        assert_eq!(
            cases.len(),
            6,
            "all SIX states must be covered — the interaction spec's five plus `cancelled` from \
             the Q1 ruling. A seventh state added without a fixture here would leave the styling \
             guards silently covering six of seven"
        );
        for (expected, pane) in &cases {
            assert_eq!(pane.state(), *expected, "fixture must be in {expected:?}");
        }
        cases
    }

    /// Renders `pane` and returns every cell as `(symbol, fg, bg)`.
    ///
    /// The style half is read from the same `Cell` the symbol comes from, so a test cannot assert
    /// a style the terminal would not actually produce.
    fn rendered_style_cells(
        pane: &ResultsPane,
        width: u16,
        height: u16,
    ) -> Vec<(String, Color, Color)> {
        let mut terminal = Terminal::new(TestBackend::new(width, height))
            .expect("a TestBackend terminal must construct");
        terminal
            .draw(|frame| frame.render_widget(pane, frame.area()))
            .expect("rendering into a TestBackend must not fail");
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| (cell.symbol().to_string(), cell.fg, cell.bg))
            .collect()
    }

    /// **FR-5.1 / FR-5.4 / NFR-5 — THE STRIP-STYLING GUARD. Read this before changing wording.**
    ///
    /// Every state stays identifiable when **all styling is discarded**. This is the executable
    /// form of NFR-3's "no state conveyed by colour alone", and it is the one test in this feature
    /// that must not be allowed to become a formality — because the property it defends is not
    /// about colour at all. It is about whether the *text* is sufficient.
    ///
    /// # Why the expectations come from the constants
    ///
    /// [`CANCELLED_NOTICE`], [`EMPTY_NOTICE`], [`RUNNING_INDICATOR`], [`TRUNCATION_MARKER`] and
    /// [`COLLAPSED_PREFIX`] are read from the module, not retyped here. That is the *opposite* of
    /// the usual "never source a fixture from the value under test" rule, and the distinction is
    /// which value is under test: this test asserts the **styling** is unnecessary, so sourcing
    /// the **wording** from its constant cannot make it vacuous. It also stops the test reddening
    /// on a copy edit, which is what would eventually get it deleted. The wording itself is pinned
    /// by [`the_cancelled_wording_does_not_claim_the_command_stopped`] and by SR-4.
    ///
    /// `Complete` is the exception and is checked against a literal `exit 0`, because there is no
    /// constant to read — the string is built by `format!` — and a derived expectation there would
    /// agree with the formatter through any typo in it.
    ///
    /// # What "styling discarded" means here
    ///
    /// The cells' symbols are read and their `fg`/`bg` thrown away, which is what a screen reader,
    /// a pipe, a `TERM=dumb` terminal, and a monochrome operator all see. The test does not render
    /// under `monochrome()` to achieve this — that would prove something weaker, since
    /// `monochrome()` keeps `Modifier::BOLD`. Discarding the style entirely is the stronger claim.
    ///
    /// **Mutation-proven (FR-5.4).** Dropping [`CANCELLED_NOTICE`] from the footer turns this red.
    #[test]
    fn every_state_is_identifiable_with_all_styling_discarded() {
        for (state, pane) in one_pane_per_state() {
            let text: String = rendered_style_cells(&pane, 70, 6)
                .into_iter()
                .map(|(symbol, _fg, _bg)| symbol)
                .collect();

            // The marker each state must be identifiable BY, in text alone.
            let expected: &str = match state {
                PaneState::Collapsed => COLLAPSED_PREFIX,
                PaneState::Running => RUNNING_INDICATOR,
                // No constant: built by `format!`, so a derived expectation would agree with the
                // formatter through any typo in it.
                PaneState::Complete => "exit 0",
                PaneState::Empty => EMPTY_NOTICE,
                PaneState::Cancelled => CANCELLED_NOTICE,
                PaneState::Refused => REFUSED_NOTICE,
            };

            assert!(
                text.contains(expected),
                "{state:?} is NOT identifiable from text alone.\n\
                 \n\
                 Expected the rendered cells to contain {expected:?}, but got:\n  {text:?}\n\
                 \n\
                 This is FR-5.1 and NFR-3: no state may be conveyed by colour alone. An operator \
                 on a monochrome terminal, a screen-reader user, and anyone piping this output all \
                 see exactly the text above and no styling whatsoever. If you reached this by \
                 moving a distinction into a colour, move it back into the words."
            );
        }
    }

    /// The foreground the given text is actually drawn in, located by scanning rendered rows.
    ///
    /// Reads the style from the same cells the text occupies rather than from a helper on the pane,
    /// so a test using this cannot pass by agreeing with a mapping function that is itself wrong.
    /// Panics if the text is absent, which keeps "the style is right" from silently degrading into
    /// "the text was not there, so nothing was checked".
    fn foreground_of(pane: &ResultsPane, width: u16, height: u16, needle: &str) -> Color {
        let cells = rendered_style_cells(pane, width, height);
        let rows: Vec<&[(String, Color, Color)]> = cells.chunks(width as usize).collect();

        for row in &rows {
            let text: String = row.iter().map(|(symbol, _, _)| symbol.as_str()).collect();
            if let Some(start) = text.find(needle) {
                // `find` returns a BYTE offset; the cells are graphemes. Every needle these tests
                // pass is ASCII, so the two coincide — asserted rather than assumed, because a
                // future non-ASCII needle would silently read the style of the wrong cell.
                assert!(
                    needle.is_ascii() && text.is_char_boundary(start),
                    "foreground_of indexes cells by byte offset and so requires an ASCII needle; \
                     got {needle:?}"
                );
                let foregrounds: Vec<Color> = row[start..start + needle.len()]
                    .iter()
                    .map(|(_, fg, _)| *fg)
                    .collect();
                let first = foregrounds[0];
                assert!(
                    foregrounds.iter().all(|fg| *fg == first),
                    "{needle:?} is drawn in more than one colour ({foregrounds:?}), so there is no \
                     single role to check"
                );
                return first;
            }
        }

        let all: String = cells.iter().map(|(symbol, _, _)| symbol.as_str()).collect();
        panic!("{needle:?} was not rendered at all, so its style could not be read. Got: {all:?}");
    }

    /// **FR-4.1 — `exit 0` carries the `ok` role.**
    ///
    /// Asserted on the rendered cells via [`foreground_of`], not on [`ResultsPane::footer_style`]:
    /// a test that called the mapping helper and compared it to the theme would agree with the
    /// helper through any error in it.
    #[test]
    fn a_zero_exit_footer_carries_the_ok_role() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"output\n");
        pane.complete(0, None);
        pane.set_theme(Theme::colour());

        let expected = Theme::colour().ok.fg.expect("`ok` sets a foreground");
        assert_eq!(
            foreground_of(&pane, 40, 3, "exit 0"),
            expected,
            "a successful exit must be drawn in the `ok` role"
        );
    }

    /// **FR-4.1 — a non-zero `exit` carries the `error` role.**
    #[test]
    fn a_non_zero_exit_footer_carries_the_error_role() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"output\n");
        pane.complete(1, None);
        pane.set_theme(Theme::colour());

        let expected = Theme::colour().error.fg.expect("`error` sets a foreground");
        assert_eq!(
            foreground_of(&pane, 40, 3, "exit 1"),
            expected,
            "a failing exit must be drawn in the `error` role"
        );
    }

    /// **FR-4.1 / A-1 — a MISSING exit code carries `warn`, not `error`.**
    ///
    /// Three arms, not two, and this is the third. A missing exit code does not mean the command
    /// failed; it means the pane reached a terminal state without one. Colouring that `error` would
    /// have the pane assert a failure it has no evidence for — and it is the likeliest defect in
    /// this feature, because the two-way `Some(0)` / `Some(_)` branch is the obvious one to write.
    ///
    /// # Why this one sets the field directly
    ///
    /// [`ResultsPane::complete`] takes `exit_code: i32` and always stores `Some`, so `exit ?` is
    /// **unreachable through the public API today**. It is a defensive arm. The two honest options
    /// were to delete the arm or to test it below the API; deleting it would put a `match` on
    /// `Option` with no `None` case, i.e. a compile error, so the arm has to exist and therefore
    /// has to be right. This test lives inside the module, drives the pane through the real API
    /// first, then clears the one field the API cannot clear — and says so here so nobody reads it
    /// as evidence that `exit ?` is reachable.
    #[test]
    fn a_missing_exit_code_footer_carries_warn_not_error() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        pane.push_bytes(b"output\n");
        pane.complete(0, None);
        pane.set_theme(Theme::colour());

        assert_eq!(
            pane.exit_code,
            Some(0),
            "precondition: `complete` stores the code, which is why this state needs forcing"
        );
        pane.exit_code = None;
        assert_eq!(
            pane.state(),
            PaneState::Complete,
            "clearing the code must not disturb the state — the footer arm under test is the \
             `Complete`/`Empty` one"
        );

        let theme = Theme::colour();
        let warn = theme.warn.fg.expect("`warn` sets a foreground");
        let error = theme.error.fg.expect("`error` sets a foreground");
        assert_ne!(
            warn, error,
            "precondition: if `warn` and `error` were the same colour this test could not fail"
        );

        assert_eq!(
            foreground_of(&pane, 40, 3, "exit ?"),
            warn,
            "a missing exit code must be `warn`, NOT `error`: it does not mean the command failed, \
             only that no code arrived (A-1)"
        );
    }

    /// The three exit roles are **actually different styles**.
    ///
    /// Without this, [`the_exit_footer_distinguishes_success_failure_and_absence`] would pass on a
    /// theme whose `ok`, `error` and `warn` were all the same value — the three-way distinction
    /// asserted against a palette that does not make it. This is the anti-vacuity half.
    ///
    /// Note `required` and `warn` ARE the same style by design (both `Yellow`), so this checks only
    /// the three roles the exit footer uses. A blanket "all six roles differ" would be a stronger
    /// claim than the palette makes and would fail on a deliberate decision.
    #[test]
    fn the_three_exit_roles_are_mutually_distinguishable() {
        let theme = Theme::colour();
        assert_ne!(theme.ok, theme.error, "`exit 0` and `exit 1` must differ");
        assert_ne!(
            theme.error, theme.warn,
            "`exit 1` and `exit ?` must differ, or the three-way branch is decoration"
        );
        assert_ne!(theme.ok, theme.warn, "`exit 0` and `exit ?` must differ");
    }

    /// **FR-3.3 end-to-end — under `NO_COLOR`, every rendered cell is `Color::Reset`.**
    ///
    /// The value-level version of this lives in `theme.rs`; this is the render-level one, and it is
    /// the half that catches a style applied *outside* the theme. A `Color::Green` written inline
    /// at a call site would satisfy every assertion in `theme.rs` and fail here.
    ///
    /// `bg` is checked as well as `fg`: `Cell::EMPTY` is `fg: Reset, bg: Reset`, so a monochrome
    /// render is required to be indistinguishable from an unstyled one in both channels.
    #[test]
    fn under_no_color_every_rendered_cell_is_reset() {
        for (state, mut pane) in one_pane_per_state() {
            pane.set_theme(Theme::monochrome());

            for (symbol, fg, bg) in rendered_style_cells(&pane, 70, 6) {
                assert_eq!(
                    fg,
                    Color::Reset,
                    "{state:?} rendered cell {symbol:?} with foreground {fg:?} under \
                     NO_COLOR. Every role in the monochrome theme is Color::Reset, so a coloured \
                     cell here means a style was applied OUTSIDE the theme — an inline colour at a \
                     call site that `from_env` cannot switch off (FR-3.3, FR-1.4)"
                );
                assert_eq!(
                    bg,
                    Color::Reset,
                    "{state:?} rendered cell {symbol:?} with background {bg:?} under NO_COLOR. A \
                     background colour is still colour"
                );
            }
        }
    }

    /// The colour theme **does** reach the cells — the anti-vacuity floor for the render path.
    ///
    /// Every styling assertion above is satisfied by a pane that applies no style at all:
    /// [`under_no_color_every_rendered_cell_is_reset`] passes trivially if nothing is ever styled,
    /// and the strip guard passes *by design* when styling is absent. So without this test, wave 1
    /// of this feature could be a no-op with a green suite.
    ///
    /// Deliberately weak about *which* cells: it asserts only that at least one rendered cell
    /// carries a non-`Reset` foreground drawn from the palette. Pinning positions would make it a
    /// layout test that reddens on a wording change.
    #[test]
    fn the_colour_theme_actually_reaches_the_rendered_cells() {
        let theme = Theme::colour();
        let palette: Vec<Color> = theme
            .roles()
            .iter()
            .filter_map(|(_, style)| style.fg)
            .collect();

        let mut states_with_colour = 0;
        for (state, mut pane) in one_pane_per_state() {
            pane.set_theme(theme);
            let coloured: Vec<_> = rendered_style_cells(&pane, 70, 6)
                .into_iter()
                .filter(|(_, fg, _)| *fg != Color::Reset)
                .collect();

            if !coloured.is_empty() {
                states_with_colour += 1;
                for (symbol, fg, _) in coloured {
                    assert!(
                        palette.contains(&fg),
                        "{state:?} rendered {symbol:?} with {fg:?}, which is not in the theme's \
                         palette ({palette:?}). A colour that is not a role is a colour \
                         `NO_COLOR` cannot switch off (FR-1.4)"
                    );
                }
            }
        }

        assert!(
            states_with_colour >= 4,
            "only {states_with_colour} of 6 states rendered any colour at all. This is the \
             anti-vacuity floor: every other styling assertion in this module is satisfied by a \
             pane that styles nothing, so if the palette stopped reaching the cells this is the \
             only test that would notice"
        );
    }

    /// A forgotten [`ResultsPane::set_theme`] leaves the pane in **colour**, not monochrome.
    ///
    /// The failure this guards is asymmetric. A default of `monochrome()` would make a missing
    /// `set_theme` call look like working code that simply has no colour — invisible in review and
    /// invisible at run time, since nothing is broken. Defaulting to colour makes the same mistake
    /// visible as "colour is on when `NO_COLOR` is set", which somebody reports.
    #[test]
    fn a_pane_with_no_theme_set_renders_in_colour() {
        let mut fresh = ResultsPane::new();
        fresh.attach(Policy::InApp);
        fresh.complete(1, None);

        let explicit = {
            let mut pane = ResultsPane::new();
            pane.attach(Policy::InApp);
            pane.complete(1, None);
            pane.set_theme(Theme::colour());
            rendered_style_cells(&pane, 40, 3)
        };

        assert_eq!(
            rendered_style_cells(&fresh, 40, 3),
            explicit,
            "a pane whose theme was never set must render exactly as one set to Theme::colour(). \
             Defaulting to monochrome would make a forgotten set_theme call indistinguishable from \
             working code"
        );
    }

    /// Adding styling did **not** open the SR-1 escape path.
    ///
    /// `results_pane.rs`'s module docs record two measured facts: `Span::raw` does not put an ESC
    /// byte into a `Cell` in ratatui 0.30.2 (the grapheme filter drops control characters), but
    /// `Cell::set_symbol` is public and bypasses that filter entirely. So "we now attach styles"
    /// is safe only as long as the styling goes through `Line`/`Span`, which is a property of the
    /// implementation rather than of the API.
    ///
    /// This asserts it from the outside for every state, styled: no rendered cell contains an ESC
    /// byte and no cell contains the `[2J` payload residue. The residue half is the half that can
    /// fail — see the module docs on why the ESC half alone is vacuous.
    #[test]
    fn styling_did_not_open_a_path_for_an_escape_to_reach_a_cell() {
        let mut pane = ResultsPane::new();
        pane.set_theme(Theme::colour());
        pane.attach(Policy::InApp);
        pane.push_bytes(b"before\x1b[2Jafter\n");
        pane.complete(0, None);

        let cells = rendered_style_cells(&pane, 60, 4);
        let text: String = cells.iter().map(|(symbol, _, _)| symbol.as_str()).collect();

        assert!(
            !text.bytes().any(|byte| byte == 0x1b),
            "an ESC byte reached a rendered cell. Note this half of the assertion cannot fail via \
             Span/Paragraph in ratatui 0.30.2 — it is here to catch a switch to Cell::set_symbol, \
             which bypasses the grapheme filter (SR-1)"
        );
        assert!(
            !text.contains("2J"),
            "the payload residue `[2J` reached a rendered cell, so the strip was bypassed. This is \
             the half that CAN fail: got {text:?}"
        );
    }

    /// **F-3 (§12a review) — HANDOFF with a NON-EMPTY buffer.**
    ///
    /// Every other HANDOFF test uses an empty buffer, so the `(Handoff, _)` arm's indifference to
    /// emptiness was never actually exercised: narrowing it to
    /// `if policy == Handoff && output.is_empty()` would have kept those tests green while
    /// rendering buffer contents instead of the outcome line for any hand-off that wrote bytes
    /// before completing. (#321)
    #[test]
    fn handoff_completion_ignores_a_non_empty_buffer_and_still_renders_the_outcome_line() {
        let outcome = "launched in new window · session: my-session · exit 0";

        let mut pane = ResultsPane::new();
        pane.attach(Policy::Handoff);
        // A hand-off CAN emit bytes before the new window takes over — a warning on stderr, a
        // progress line. The outcome line still governs.
        pane.push_bytes(b"warning: reusing an existing tmux server\n");
        pane.complete(0, Some(outcome.to_string()));

        assert_eq!(
            pane.state(),
            PaneState::Complete,
            "a HANDOFF completion is `complete` regardless of what the buffer holds"
        );
        let cells = rendered_cells(&pane, 60, 5);
        assert!(
            cells.contains(outcome),
            "the outcome line must render even when the buffer is NON-empty — the `(Handoff, _)` \
             arm ignores emptiness by design. Cells: {cells:?}"
        );
        assert!(
            !cells.contains(EMPTY_NOTICE),
            "a HANDOFF pane must never render {EMPTY_NOTICE:?}. Cells: {cells:?}"
        );
    }

    /// A fresh `attach` resets the previous run rather than interleaving with it.
    ///
    /// Covers the parser reset specifically: a half-consumed escape left over from an
    /// abandoned run would silently eat the opening characters of the next one.
    #[test]
    fn attach_resets_the_previous_run_including_the_parser() {
        let mut pane = ResultsPane::new();
        pane.attach(Policy::InApp);
        for line in 0..BUFFER_CAPACITY + 5 {
            pane.push_bytes(format!("old-{line}\n").as_bytes());
        }
        assert!(pane.truncated(), "setup: the first run must have truncated");
        pane.scroll_up(3);
        // An escape sequence deliberately left half-consumed.
        pane.push_bytes(b"\x1b[");

        pane.attach(Policy::Handoff);

        assert_eq!(pane.state(), PaneState::Running);
        assert!(
            pane.lines().is_empty(),
            "a new run must start with an empty buffer: {:?}",
            pane.lines()
        );
        assert!(
            !pane.truncated(),
            "the truncation flag belongs to the run that dropped lines, not to the pane"
        );
        assert_eq!(pane.scroll_offset(), 0, "a new run starts pinned to newest");

        pane.push_bytes(b"2Jfresh\n");
        assert_eq!(
            pane.lines(),
            vec!["2Jfresh"],
            "the parser must be reset: a half-consumed escape from the abandoned run would \
             otherwise swallow the start of this line"
        );
    }
}
