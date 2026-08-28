#![forbid(unsafe_code)]
//! `cao-tui` — the Rust terminal UI front door for CLI Agent Orchestrator (issue #321).
//!
//! This is the walking skeleton's first unit: it establishes the crate and its crate-wide
//! safety posture, and nothing else. No TUI, no HTTP client, no pty — those are separate
//! units with their own definitions of done.
//!
//! `forbid` rather than `deny` on `unsafe_code` is deliberate: `deny` can be turned off
//! again by an `#[allow(unsafe_code)]` further down the tree, so a single module could
//! quietly reintroduce `unsafe`. `forbid` cannot be overridden, which makes any genuine
//! need for `unsafe` a visible, reviewable change to this line. (#321)

use std::io::{self, IsTerminal, Write};
use std::sync::Arc;

/// The static run-policy table (`command-catalog`): 61 leaf commands, each classified in-app,
/// hand-off, or hidden. An unclassified command **fails to compile** — see the module docs.
/// (#321)
mod catalog;
/// Guard 1 of `safety-guards` (S-5): the operator-supplied env-var mirror. Blocked prefixes,
/// a six-entry allowlist, and a 2048-byte cap, mirroring `clients/tmux.py` — and a WARNING on
/// every drop, which is the control itself rather than a nicety. (#321)
mod env_guard;
mod error;
/// `guided-flow` (Bolt 4): the guided launch form — field state, required-field gating on
/// **`--agents` alone**, and the two pickers, which is where this unit performs I/O. All of that
/// I/O goes through `server-client`; the parameter surface is the re-verified 12/1/7/5, with
/// `message` positional and `--memory` a flag. (#321)
mod guided_flow;
/// The hand-off mechanism (walking-skeleton item 5): resolve the backend, wait for readiness,
/// move the operator's view **without ending this process**. Retires constraint T-4. (#321)
mod handoff;
/// `renderer` (Bolt 5): the DAG root and **the only orchestrator** — shell geometry, focus order,
/// the key map, sub-80x24 stacked collapse, and the four-step `launch()` sequence. This is the unit
/// that discharges **FR-3.2**: the results pane gets a production caller here, at two sites, which
/// is the defect the predecessor shipped (built the pane, never invoked it). (#321)
mod renderer;
/// `results-pane` (Bolt 4): the scrollable output pane — six states, a 10,000-line ring buffer
/// with a visible truncation marker, and **the SR-1 strip point**. Control sequences in command
/// output are consumed by a `vte` parser at the decode point, so no unstripped byte can reach a
/// widget by any path. The first unit to bring the TUI rendering stack into the crate. (#321)
mod results_pane;
/// `server-client` (Bolt 3): **all** of the crate's HTTP, and the only I/O component in it.
/// Six methods, six error variants, the 21-route table, and no subprocess anywhere (ADR-02).
/// (#321)
mod server;
/// The semantic colour layer (#556): six roles, an ANSI-16 palette, and `NO_COLOR`. **The only
/// module permitted to name a `Color`** — every other module says `theme.error`, and
/// `tests/no_colour_literal_outside_theme.rs` makes that a failing test rather than a convention.
/// Colour is decoration only: every state this crate shows is already textual, which is NFR-3 of
/// #321 and the reason `NO_COLOR` costs nothing to honour. (#556)
mod theme;
/// The wire vocabulary six later units share. Declared here so every consumer imports the
/// types from one place rather than redeclaring the server's shapes locally. (#321)
mod types;

use error::TuiError;

/// The operator-facing help text, printed for `--help`/`-h` and on an unrecognised argument.
///
/// `cao tui` forwards its argv to this binary verbatim (`cli/commands/tui.py:87-109`, with
/// `ignore_unknown_options` and `allow_extra_args` set precisely so the TUI owns its own argument
/// surface). **The binary read no argv at all**, so `cao tui --help` opened the TUI and
/// `cao tui --nonsense` did too — silently, exit 0. Both verified against the built binary before
/// this was written. Forwarding to a program that ignores what it is given is not a contract, it is
/// a coincidence. (Reported by review on PR #547.)
const HELP: &str = "\
cao-tui — the terminal UI front door for CLI Agent Orchestrator

USAGE:
    cao tui [OPTIONS]

OPTIONS:
    -h, --help       Print this help and exit
    -V, --version    Print the version and exit

The TUI takes no other arguments: every command, parameter, and picker is chosen inside it.
Set CAO_API_HOST and CAO_API_PORT to reach a cao-server elsewhere than the default
127.0.0.1:9889.";

/// What the argv asked for.
///
/// A three-variant enum rather than a pair of bools, so `main` cannot accidentally handle
/// "help and also start the TUI". Parsing is deliberately hand-rolled and tiny — adding `clap` for
/// two flags would pull a dependency tree through `cargo-deny` for no gain.
enum Invocation {
    /// Start the TUI.
    Run,
    /// Print [`HELP`] and exit 0.
    Help,
    /// Print the version and exit 0.
    Version,
    /// An argument this binary does not accept. Exits **non-zero** naming it.
    Unknown(String),
}

/// Classifies the argv, ignoring `argv[0]`. **Every argument is examined, not just the first.**
///
/// An unrecognised argument is an **error**, not something to ignore: `cao tui --agents planner`
/// looks like it should work (the launch form has an `--agents` field), and silently opening an
/// empty TUI teaches the operator that the flag was accepted. Naming the rejected argument is the
/// same posture the rest of this crate takes toward stated limits.
///
/// The first version of this function returned from inside the loop body on every arm, so it only ever read
/// `args[0]` — `cao tui --help --nonsense` would print help and ignore the rest. Clippy caught it
/// as `never_loop`, which is why `--all-targets -- -D warnings` is a gate and not a suggestion.
///
/// Precedence is deliberate: an **unknown argument wins over help**. `cao tui --help --nonsense`
/// must not print help and exit 0, because that reports success for a command line it did not
/// honour — the same silent-acceptance failure this function exists to remove.
fn parse_args<I: Iterator<Item = String>>(args: I) -> Invocation {
    let mut invocation = Invocation::Run;
    for arg in args {
        match arg.as_str() {
            "-h" | "--help" => {
                if matches!(invocation, Invocation::Run) {
                    invocation = Invocation::Help;
                }
            }
            "-V" | "--version" => {
                if matches!(invocation, Invocation::Run) {
                    invocation = Invocation::Version;
                }
            }
            // Returns immediately: an argument this binary cannot honour is the answer, whatever
            // else was asked for alongside it.
            other => return Invocation::Unknown(other.to_string()),
        }
    }
    invocation
}

/// Exits 0 on success. Returning `Err` exits non-zero and prints one line, which is the
/// operator-facing boundary contract inherited from the Python CLI. (#321)
///
/// # This is `renderer`'s production entry point, and that is load-bearing
///
/// FR-3.2 is an anti-requirement about a component that was *built and never invoked*. Wiring the
/// shell here — rather than leaving it reachable only from tests behind a blanket
/// `#[allow(dead_code)]` — is what makes the whole unit reachable from the binary rather than only
/// from its own test module. The pane's production callers live inside `Renderer::launch` and
/// `Renderer::run_in_app`; this is the path that reaches them.
///
/// # Why the size falls back instead of failing
///
/// `crossterm::terminal::size()` fails when stdout is not a tty — which is exactly how
/// `tests/binary_exits_zero.rs` runs it, and how any pipeline would. Falling back to NFR-6's 80x24
/// floor keeps that an ordinary run rather than a startup failure, and it is consistent with
/// FR-6.1's posture: the TUI opens, and conditions are rendered rather than raised. `Fatal` is
/// reserved for a zero-area terminal, where rendering is not available as an answer at all.
///
/// # `main` is a thin wrapper so the exit path can be styled
///
/// `fn main() -> Result<_, E>` prints a failure through **`Debug`**, so a `TuiError::Unreachable`
/// reached the operator as `Error: Unreachable("cao-server is not reachable at …")` — neither
/// styled nor accurately named, and the exact traceback-shaped output the Python CLI's error
/// contract exists to avoid. `run_app` returns the error and this function renders it.
/// (Reported by review on PR #547.)
fn main() {
    match parse_args(std::env::args().skip(1)) {
        Invocation::Help => {
            println!("{HELP}");
            return;
        }
        Invocation::Version => {
            println!("cao-tui {}", env!("CARGO_PKG_VERSION"));
            return;
        }
        Invocation::Unknown(argument) => {
            // One line, to stderr, non-zero — the Python CLI's boundary contract.
            eprintln!("cao-tui: unrecognised argument {argument:?}");
            eprintln!("Run `cao tui --help` to see what it accepts.");
            std::process::exit(2);
        }
        Invocation::Run => {}
    }

    install_panic_hook();

    if let Err(error) = run_app() {
        // ONE styled line naming the failure, never a `Debug` rendering. `TuiError`'s own
        // `Display` is the operator-facing sentence; the variant name is not part of it.
        eprintln!("cao-tui: {error}");
        std::process::exit(1);
    }
}

/// Makes a panic message survive the alternate screen.
///
/// `TerminalRestore`'s `Drop` runs `LeaveAlternateScreen` while unwinding, and **that erases
/// whatever the default panic hook just printed** — so a panicking TUI restored the terminal
/// cleanly and ate its own diagnosis, leaving the operator with a silent exit. Writing the message
/// to stderr *after* the screen is restored is the whole fix.
///
/// The payload is extracted rather than reformatted with `{info}`: `PanicHookInfo`'s `Display`
/// includes the location, and printing it twice reads as noise. `&str` and `String` are the two
/// payload types `panic!` produces; anything else falls back to a stated unknown rather than being
/// dropped. (Reported by review on PR #547.)
fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        // Leave the alternate screen and raw mode FIRST, so what follows is visible on the
        // operator's real terminal. Both are idempotent, so `TerminalRestore`'s own `Drop` doing
        // it again during unwind is harmless.
        let _ = crossterm::terminal::disable_raw_mode();
        let _ = crossterm::execute!(io::stdout(), crossterm::terminal::LeaveAlternateScreen);

        let location = info
            .location()
            .map(|location| format!("{}:{}", location.file(), location.line()));
        eprint!("{}", panic_report(&payload_text(info.payload()), location));

        // The default hook still runs, so `RUST_BACKTRACE=1` keeps working for whoever is
        // debugging. It prints after the restore, which is the point.
        default_hook(info);
    }));
}

/// The panic payload as text, or a stated fallback.
///
/// `&str` and `String` are what `panic!` produces; `Box<dyn Any>` admits anything, and a payload
/// of some third type must still yield a message rather than nothing. Split from the hook because
/// a hook cannot be invoked from a test without actually panicking the test process.
fn payload_text(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<&str>()
        .map(|text| (*text).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "a panic with a non-string payload".to_string())
}

/// The operator-facing panic text, ending in a newline.
///
/// A pure function so the **content** is testable: the hook itself can only be exercised by
/// panicking the process, and what mattered about the reported defect was that the message existed
/// and reached stderr at all — the alternate screen was erasing it.
fn panic_report(payload: &str, location: Option<String>) -> String {
    let mut report = format!("cao-tui panicked: {payload}\n");
    if let Some(location) = location {
        report.push_str(&format!("  at {location}\n"));
    }
    // The issue tracker is named WITHOUT a URL scheme, deliberately. `tests/hermeticity_tripwire.rs`
    // treats a literal `https:` in production code as a client this crate must not contain, and it
    // is right to: the needle is a catch-all that cannot distinguish a URL it should worry about
    // from one it should not. Exempting `main.rs` to keep a clickable link would put a hole in the
    // one guard that keeps HTTP inside `server.rs`. The repo path is enough to find the tracker.
    report.push_str(
        "This is a bug in cao-tui. Please report it on the \
         awslabs/cli-agent-orchestrator issue tracker.\n",
    );
    report
}

/// The real work, split out of [`main`] so its error can be rendered rather than `Debug`-printed.
fn run_app() -> Result<(), TuiError> {
    let (cols, rows) = crossterm::terminal::size().unwrap_or((80, 24));
    let interactive = io::stdout().is_terminal();

    let server = Arc::new(server::ServerClient::from_env());
    let host = handoff::RealHost::new();
    let mut shell = renderer::Renderer::new(server.as_ref(), &host, cols, rows)
        .with_concurrent_pickers(Arc::clone(&server));

    // `NO_COLOR` is read exactly ONCE, here, and the resolved palette is threaded down (#556).
    // Re-reading it per frame would let a mid-session change produce a half-coloured screen, and
    // reading it deeper in the call tree would make every unit test's output depend on the ambient
    // environment. This is the only `from_env` call in the crate.
    shell.set_theme(theme::Theme::from_env());

    // A `Fatal` here exits non-zero with one styled line — never a traceback (SR-1). Mapped into
    // `TuiError` because this function's signature is the boundary contract, and `Fatal`'s own
    // `Display` already carries the whole operator-facing sentence.
    shell
        .run()
        .map_err(|fatal| TuiError::Unreachable(fatal.to_string()))?;

    // Pipes cannot host an interactive TUI. `Renderer::run` deliberately returns after one tick
    // in that case, and this textual frame keeps `cao-tui | ...` populated rather than hanging or
    // emitting alternate-screen control sequences. (#321)
    if !interactive {
        let frame = shell.render();
        let mut out = io::stdout().lock();
        // `{line}` on a `Line` writes its spans' content and **no SGR codes** — checked in
        // ratatui-core 0.1.2, `Span`'s `Display` is a plain `write!` of `content`. That is what
        // keeps a pipe free of escapes now that these are styled values (SR-1); it is relied upon
        // here rather than merely true, so it is written down. Guarded by
        // `renderer::tests::a_styled_line_displays_without_escape_codes` — nothing asserted this
        // from a real piped process, and a dependency on an upstream `Display` impl with no test
        // behind it is what silently breaks on a minor-version bump.
        //
        // Still header+footer only, deliberately: this is the pipe frame from #321, and widening it
        // to `plain_lines()` would change what `cao-tui | ...` prints under cover of a colour
        // change. (#556)
        for line in frame.header.iter().chain(&frame.footer) {
            writeln!(out, "{line}")?;
        }
        out.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    // The `#![forbid(unsafe_code)]` assertion USED to live here and was VACUOUS: it compared
    // `include_str!("main.rs")` against a needle literal in this same file, so a `forbid` -> `deny`
    // edit changed both in lock-step and the test still passed. Measured — the mutation left it
    // `ok`, while the conductor's record claimed it FAILED.
    //
    // It now lives in `tests/hermeticity_tripwire.rs`
    // (`the_crate_root_forbids_unsafe_code_and_deny_is_not_accepted`), which embeds `main.rs` as
    // one of its SOURCES. From there the needle cannot co-mutate with the haystack, and a second
    // assertion rejects `deny` by name. Found by the §12a reviewer for `skeleton-crate`. (#321)

    use super::{panic_report, parse_args, payload_text, Invocation, HELP};

    fn classify(args: &[&str]) -> Invocation {
        parse_args(args.iter().map(|arg| (*arg).to_string()))
    }

    /// **Every accepted argv form is classified, and anything else is refused.**
    ///
    /// The binary read no argv at all, so `cao tui --help` opened the TUI and `cao tui --nonsense`
    /// did too, both exiting 0 — while `tui.py` forwards argv verbatim on the stated premise that
    /// "the TUI owns its own argument surface".
    ///
    /// The `Unknown` case is the one that matters most, and `--agents` is the adversarial input:
    /// the launch form has a field with that name, so it is the flag an operator is likeliest to
    /// try. `tests/binary_exits_zero.rs` covers the exit codes; this covers the classification.
    /// (Reported by review on PR #547.)
    #[test]
    fn argv_is_classified_and_an_unknown_argument_is_refused_by_name() {
        assert!(matches!(classify(&[]), Invocation::Run));
        assert!(matches!(classify(&["--help"]), Invocation::Help));
        assert!(matches!(classify(&["-h"]), Invocation::Help));
        assert!(matches!(classify(&["--version"]), Invocation::Version));
        assert!(matches!(classify(&["-V"]), Invocation::Version));

        match classify(&["--agents", "planner"]) {
            Invocation::Unknown(argument) => assert_eq!(
                argument, "--agents",
                "the refusal must carry the REJECTED argument so the message can name it; \
                 `--agents` is a launch-form field name, which is exactly the flag an operator \
                 would expect to work"
            ),
            other => panic!(
                "an unrecognised argument must be refused, not silently accepted — accepting it \
                 is what made the forwarding contract a coincidence. Got: {}",
                match other {
                    Invocation::Run => "Run",
                    Invocation::Help => "Help",
                    Invocation::Version => "Version",
                    Invocation::Unknown(_) => unreachable!(),
                }
            ),
        }

        // A positional is refused too: the TUI takes none, and swallowing one would be the same
        // silent acceptance in a form that does not even look like a flag.
        assert!(matches!(classify(&["planner"]), Invocation::Unknown(_)));
    }

    /// The help text names the two things an operator can actually change from outside.
    #[test]
    fn the_help_text_states_the_usage_and_the_two_environment_variables() {
        assert!(HELP.contains("USAGE"), "help must have a usage section");
        for needle in ["--help", "--version", "CAO_API_HOST", "CAO_API_PORT"] {
            assert!(
                HELP.contains(needle),
                "help must mention {needle:?} — those flags and those two variables are the whole \
                 external surface. Got: {HELP:?}"
            );
        }
    }

    /// **A panic produces a message naming the payload, the location, and where to report it.**
    ///
    /// The hook exists because `TerminalRestore`'s `Drop` runs `LeaveAlternateScreen` during
    /// unwind, **erasing whatever the default hook just printed** — a panicking TUI restored the
    /// terminal and ate its own diagnosis. The content is asserted here; that it reaches stderr
    /// after the restore is a property of the hook's ordering, which cannot be unit-tested without
    /// panicking the test process.
    #[test]
    fn the_panic_report_names_the_payload_the_location_and_the_issue_tracker() {
        let report = panic_report(
            "something went wrong",
            Some("src/renderer.rs:42".to_string()),
        );

        assert!(
            report.contains("something went wrong"),
            "the payload is the diagnosis; losing it is the defect. Got: {report:?}"
        );
        assert!(
            report.contains("src/renderer.rs:42"),
            "the location must survive, or the report cannot be acted on. Got: {report:?}"
        );
        // The message below deliberately does NOT spell the TLS scheme out. `hermeticity_tripwire.rs`
        // scans this whole file, tests included, and its scheme needles are assembled from
        // fragments precisely so the guard does not fire on itself — an assertion string quoting
        // one verbatim trips it just as a real client would. That happened here, which is the same
        // self-tripping trap the tripwire's own docs record about its `reason` strings.
        assert!(
            report.contains("issue tracker") && report.contains("cli-agent-orchestrator"),
            "a panic is a bug, so the report must say where to file it. The repo is named without \
             a URL scheme on purpose: the hermeticity tripwire treats a literal URL scheme in \
             production code as a forbidden client, and exempting `main.rs` to keep a clickable \
             link would hole the guard that keeps HTTP inside `server.rs`. Got: {report:?}"
        );
        assert!(
            report.ends_with('\n'),
            "the report is written with `eprint!`, so it must supply its own trailing newline"
        );

        // A panic with no location still reports the payload rather than nothing.
        let no_location = panic_report("bare", None);
        assert!(no_location.contains("bare"));
        assert!(
            !no_location.contains("  at "),
            "with no location there must be no empty `at` line. Got: {no_location:?}"
        );
    }

    /// Both payload types `panic!` produces are extracted, and a third yields a stated fallback.
    #[test]
    fn a_panic_payload_is_extracted_from_either_string_type() {
        assert_eq!(payload_text(&"a &str payload"), "a &str payload");
        assert_eq!(
            payload_text(&"a String payload".to_string()),
            "a String payload"
        );
        assert_eq!(
            payload_text(&42u32),
            "a panic with a non-string payload",
            "a payload of some other type must still yield a message — `Box<dyn Any>` admits \
             anything, and silence is the one unacceptable answer"
        );
    }
}
