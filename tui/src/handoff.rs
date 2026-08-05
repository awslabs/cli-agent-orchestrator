//! The hand-off mechanism: resolve the backend, wait for readiness, move the operator's view
//! **without ending the TUI's own process** (issue #321).
//!
//! This is `skeleton-handoff-proof`, the walking skeleton's largest unit, and it retires
//! constraint T-4.
//!
//! # Why this unit exists, and why it had nothing to copy
//!
//! CAO's two terminal backends each expose a method that attaches the caller to a session, and
//! **neither is usable for a hand-off** — for *asymmetric* reasons, both re-read at source
//! rather than taken from a docstring:
//!
//! - `backends/tmux_backend.py:131-135` is a `subprocess.run([...])`, so it **blocks until the
//!   operator detaches and then returns**. Its docstring claims it "replaces current process";
//!   the docstring is **wrong**, and quoting it as fact was a real error earlier in this
//!   intent. Calling it would freeze the TUI's event loop.
//! - `backends/herdr_backend.py:631` is an `os.execvp`, so it **genuinely replaces the process
//!   image**. Calling it would destroy the TUI outright.
//!
//! Navigation capability is asymmetric too: herdr's `prepare_web_attach` calls
//! `herdr tab focus` (`herdr_backend.py:637`) and really does move the view, while tmux's
//! (`tmux_backend.py:137-139`) only *returns an argv list* for the browser PTY WebSocket and
//! moves nobody's view. And there is **no navigation call anywhere in
//! `src/cli_agent_orchestrator/` to copy** — verified zero hits for `select-window`,
//! `switch-client`, and `switch-pane`. Everything below is new work, which is what makes this
//! unit complexity L. (#321)
//!
//! # What is a mechanism here and what is not
//!
//! This module provides [`HandoffDriver::await_ready`] and [`HandoffDriver::handoff`] and
//! **does not sequence them** (BR-16). `Renderer::launch()` owns the order
//! `to_params -> create_session -> await_ready -> handoff`, and it renders every result. The
//! driver renders nothing.
//!
//! # Two type-level guarantees worth naming
//!
//! 1. **[`Host::run`] takes `&[&str]`, so a shell string is not expressible.** T-10 and SR-1
//!    require an argv vector because session and window names arrive from *server responses*;
//!    an interpolated command line would make them injection vectors. Here the compiler
//!    enforces it rather than a reviewer.
//! 2. **[`HandoffDriver::await_ready`] returns [`Readiness`], not a `Result`** (BR-13). A
//!    two-state return would force `Readiness::Unknown` into the error arm, which is exactly
//!    the conflation ADR-04 exists to prevent: *unknown is not broken*.

use std::cell::OnceCell;
use std::process::Command;
use std::time::{Duration, Instant};

use crate::error::TuiError;
use crate::types::{Health, Readiness, Terminal, TerminalStatus};

/// The readiness poll's hard ceiling (ADR-04, BR-8).
///
/// Reaching it yields [`Readiness::Unknown`], a **warning** the flow continues past — never an
/// error. The operator's ruling was explicit: warn, do not block, let them retry. (#321)
const READINESS_CAP: Duration = Duration::from_secs(30);

/// Gap between readiness polls (BR-8, functional-design Q5: 1s, fixed).
const POLL_INTERVAL: Duration = Duration::from_secs(1);

/// Longest session or window name accepted, mirroring the server's own allow-list.
///
/// `utils/terminal.py:20` is `^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$` — one leading character plus
/// up to 63 more. See [`is_valid_target_name`]. (#321)
const MAX_TARGET_NAME_LEN: usize = 64;

/// Reads the server. The seam that keeps this unit testable and free of an HTTP client.
///
/// **This unit deliberately picks no HTTP client.** `server-client` (Bolt 3) owns that choice
/// and will implement this trait; taking the two reads through a trait means the hand-off logic
/// is exercised with no I/O at all and Bolt 3's decision is not pre-empted by a dependency
/// added here for a test. (#321)
pub trait ServerRead {
    /// `GET /health`, whose `terminal_backend` field decides the hand-off strategy (BR-14).
    fn health(&self) -> Result<Health, TuiError>;

    /// `GET /terminals/{id}`, re-read once per poll.
    fn terminal(&self, terminal_id: &str) -> Result<Terminal, TuiError>;
}

/// The local machine: the `$TMUX` witness, a monotonic clock, and process spawning.
///
/// Separate from [`ServerRead`] because it is a different boundary — one trait per boundary.
/// Injecting it buys three things that matter:
///
/// - **`$TMUX` is read through [`Host::tmux_env`] instead of `std::env::var`.** Rust runs tests
///   as threads in one process, so mutating a process-global environment variable to exercise
///   the other branch would race every other test in the binary.
/// - **The clock is fakeable**, so the 30-second cap is provable in microseconds instead of
///   thirty seconds per assertion.
/// - **Spawning is observable**, so a test can assert that the refusal path spawned *nothing* —
///   which is the property that matters, and is invisible if the real `tmux` is invoked. (#321)
pub trait Host {
    /// The value of `$TMUX`, or `None` when it is unset.
    ///
    /// This is the **observable condition** BR-3 keys the tmux branch on. tmux sets `$TMUX` for
    /// every process it spawns, so its presence is what says "there is a client whose view can
    /// be moved" — not a heuristic, and not intuition.
    fn tmux_env(&self) -> Option<String>;

    /// Monotonic time since an arbitrary fixed epoch.
    ///
    /// Deliberately not `Instant`: an `Instant` cannot be constructed at a chosen value, so a
    /// cap test would have to sleep in real time.
    fn now(&self) -> Duration;

    /// Blocks for `duration`.
    fn sleep(&self, duration: Duration);

    /// Runs `argv` to completion, reporting a human-readable message on failure.
    ///
    /// `&[&str]` and not a command string, so SR-1/T-10 hold by construction.
    fn run(&self, argv: &[&str]) -> Result<(), String>;
}

/// The production [`Host`]: the real environment, the real clock, real subprocesses.
#[allow(dead_code)] // no in-crate consumer until `renderer` (Bolt 5); see `types.rs`. (#321)
#[derive(Debug)]
pub struct RealHost {
    /// Captured once so [`Host::now`] can be monotonic and still fakeable elsewhere.
    epoch: Instant,
}

#[allow(dead_code)] // constructed by `renderer` (Bolt 5). (#321)
impl RealHost {
    /// Starts the monotonic clock.
    pub fn new() -> Self {
        Self {
            epoch: Instant::now(),
        }
    }
}

impl Default for RealHost {
    fn default() -> Self {
        Self::new()
    }
}

impl Host for RealHost {
    fn tmux_env(&self) -> Option<String> {
        std::env::var("TMUX").ok()
    }

    fn now(&self) -> Duration {
        self.epoch.elapsed()
    }

    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }

    fn run(&self, argv: &[&str]) -> Result<(), String> {
        let (program, args) = argv
            .split_first()
            .ok_or_else(|| "refusing to spawn an empty argv".to_string())?;

        // `Command::new(program).args(args)` execs the program directly: no shell, no word
        // splitting, no globbing. There is deliberately no `sh -c` anywhere in this crate.
        let status = Command::new(program)
            .args(args)
            .status()
            .map_err(|error| format!("could not spawn `{program}`: {error}"))?;

        if status.success() {
            Ok(())
        } else {
            Err(format!(
                "`{program}` exited with {code:?}",
                code = status.code()
            ))
        }
    }
}

/// Which multiplexer the **server** is driving (BR-14).
///
/// A closed two-variant enum. An unrecognised `terminal_backend` is an [`TuiError::Unreachable`]
/// rather than a third variant or a default (BR-15): a wrong guess produces a hand-off that
/// silently does nothing, which is the worst of the available failures because it looks like
/// success. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    /// tmux. No navigation capability of its own — see the module docs.
    Tmux,
    /// herdr. Already focuses the tab it creates (`herdr_backend.py:637`).
    Herdr,
}

impl Backend {
    /// Maps the server's reported `terminal_backend` string, refusing to guess.
    ///
    /// The two accepted values are the two `api/main.py:820` can produce. Anything else is an
    /// error that **names the offending value**, so the operator learns what the server said
    /// rather than watching a hand-off quietly do nothing. (#321)
    fn parse(reported: &str) -> Result<Self, TuiError> {
        match reported {
            "tmux" => Ok(Self::Tmux),
            "herdr" => Ok(Self::Herdr),
            other => Err(TuiError::Unreachable(format!(
                "cao-server reports terminal_backend {other:?}, which this build does not know \
                 how to hand off to; guessing tmux or herdr would produce a hand-off that \
                 silently does nothing"
            ))),
        }
    }
}

/// A hand-off that moved the operator's view.
///
/// Rendered by `results-pane` as the structured outcome line — the command's own output went to
/// the new window, so an empty pane would otherwise read as a failed run. (#321)
#[allow(dead_code)] // read by `results-pane` (Bolt 5). (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    /// The session the view now shows.
    pub session: String,
    /// The window/tab the view now shows.
    pub window: String,
}

/// A hand-off that could not move the view — **a legitimate outcome, not an error** (BR-7).
///
/// Modelled as its own type, and returned in the error arm of
/// [`Result`][HandoffDriver::handoff] so the type system says "this arm is a designed path".
/// The `$TMUX`-unset case is not a malfunction: there is no client to move, and FR-5.3's whole
/// answer is to hand the operator the command that does the job.
#[allow(dead_code)] // read by `results-pane` (Bolt 5). (#321)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    /// Why the view could not be moved, in one operator-facing sentence.
    pub reason: String,

    /// The exact argv to run by hand, rendered ready to paste — or `None` when no correct
    /// command can be offered.
    ///
    /// **`Option`, and that is a deliberate deviation from `domain-entities.md`'s `String`.**
    /// Two reachable refusals have no correct command to give: the backend being unresolvable
    /// (BR-15 forbids guessing tmux, and herdr's own attach target is backend-internal), and a
    /// name that fails the allow-list (printing it would hand back an argv that is either
    /// unrunnable or hostile). A `String` would force a wrong-but-confident answer in exactly
    /// the cases where the operator can least afford one, so absence is modelled instead.
    ///
    /// Every `Some` is safe to paste **because** [`is_valid_target_name`] ran first: the
    /// allow-list excludes whitespace, quotes, and every shell metacharacter, so joining the
    /// argv with spaces for display cannot change how a shell would parse it. Remove the
    /// validation and this field stops being paste-safe. (#321)
    pub manual_command: Option<String>,
}

/// The conclusion drawn from one readiness poll.
///
/// Extracted so the six-to-three collapse is a **pure function of the status alone** and lives
/// in exactly one place (BR-9, INV-4), directly testable without a clock, a server, or a loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Verdict {
    /// A conclusive verdict; stop polling.
    Settled(Readiness),
    /// No verdict yet; sleep and poll again.
    KeepPolling,
}

/// The six-to-three status collapse (BR-9). **The only place it happens.**
///
/// | [`TerminalStatus`] | Verdict |
/// |---|---|
/// | `Idle`, `Completed`, `WaitingUserAnswer` | [`Readiness::Ready`] |
/// | `Processing`, `Unknown`, absent (`None`) | keep polling |
/// | `Error` | [`Readiness::Failed`] |
///
/// Three arms are decisions rather than mechanics:
///
/// - **`WaitingUserAnswer` -> `Ready` is the consequential one** (BR-10). An agent blocked on a
///   question launched *perfectly* and needs the operator **now**; treating it as not-ready
///   would burn the entire 30-second cap on a healthy agent and then report `Unknown`.
/// - **`Completed` -> `Ready`.** It launched and finished; hand off so the result is seen.
/// - **absent -> keep polling** (BR-4 of `shared-types`). The server declares `status` optional
///   and live-only, so its absence means "no answer yet", exactly like `Unknown`. Treating a
///   missing field as a failure would fail every poll that races terminal registration.
///
/// `Error` is the sole status that settles as a failure, which is what makes `Unknown` mean
/// *unknown*. (#321)
fn classify(status: Option<TerminalStatus>) -> Verdict {
    match status {
        Some(
            TerminalStatus::Idle | TerminalStatus::Completed | TerminalStatus::WaitingUserAnswer,
        ) => Verdict::Settled(Readiness::Ready),
        Some(TerminalStatus::Error) => Verdict::Settled(Readiness::Failed(TerminalStatus::Error)),
        Some(TerminalStatus::Processing | TerminalStatus::Unknown) | None => Verdict::KeepPolling,
    }
}

/// Whether a name is safe to place in a multiplexer target, mirroring the server's allow-list.
///
/// A hand-rolled equivalent of `utils/terminal.py:20`'s
/// `^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$` — no `regex` dependency for one fixed pattern.
/// Byte-wise rather than char-wise is exact here, not an approximation: every accepted byte is
/// ASCII, so any multi-byte character is rejected by the per-byte check before the length cap
/// could disagree with the Python regex's character count.
///
/// **SR-2: this unit validates rather than assuming upstream sanitisation held.** The server
/// re-validates at its own tmux sink for precisely this reason (`api/main.py:3023-3024`,
/// "Defence-in-depth"), and this crate is a second, independent consumer of the same names. It
/// also rules out the three characters that would otherwise change a target's *meaning*: `:`
/// and `.` are tmux target delimiters, and a leading `-` parses as an option. (#321)
fn is_valid_target_name(name: &str) -> bool {
    let bytes = name.as_bytes();

    match bytes.split_first() {
        None => false,
        Some((first, rest)) => {
            bytes.len() <= MAX_TARGET_NAME_LEN
                && (first.is_ascii_alphanumeric() || *first == b'_')
                && rest
                    .iter()
                    .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'_' || *byte == b'-')
        }
    }
}

/// Moves the operator's view to a freshly launched agent, leaving the TUI running.
///
/// One instance per TUI process. `backend` is cached because `terminal_backend` is server
/// configuration that does not change during a session, and re-reading it would add a network
/// round trip to every launch. It is a [`OnceCell`] rather than a constructor argument so a
/// server that is **down at startup does not stop the TUI from opening** — FR-6.1 requires a
/// rendered server-down state with cause, remedy, and retry, not a startup crash. A failed
/// resolution is deliberately **not** cached, so the retry FR-6.1 promises actually re-reads.
#[allow(dead_code)] // constructed by `renderer` (Bolt 5). (#321)
pub struct HandoffDriver<'a, S: ServerRead, H: Host> {
    server: &'a S,
    host: &'a H,
    backend: OnceCell<Backend>,
}

#[allow(dead_code)] // every method's caller is `renderer` (Bolt 5). (#321)
impl<'a, S: ServerRead, H: Host> HandoffDriver<'a, S, H> {
    /// Borrows the two seams; resolves nothing yet.
    pub fn new(server: &'a S, host: &'a H) -> Self {
        Self {
            server,
            host,
            backend: OnceCell::new(),
        }
    }

    /// The server's terminal backend, read once and cached (BR-14, BR-15).
    ///
    /// The TUI holds **no backend configuration of its own**, so by construction it cannot
    /// disagree with the server about which multiplexer is in use (ADR-01). (#321)
    pub fn backend(&self) -> Result<Backend, TuiError> {
        if let Some(cached) = self.backend.get() {
            return Ok(*cached);
        }

        let resolved = Backend::parse(&self.server.health()?.terminal_backend)?;

        // Cannot fail: nothing else can have set the cell between the `get` above and here —
        // `OnceCell` is single-threaded and `&self` is not `Sync`-shared.
        let _ = self.backend.set(resolved);
        Ok(resolved)
    }

    /// Polls until the terminal is ready, capped at 30 seconds (FR-5.4, ADR-04).
    ///
    /// Returns [`Readiness`] and **never a `Result`** (BR-13) — see the module docs.
    ///
    /// # What settles the loop, and what deliberately does not
    ///
    /// - A status settles it via [`classify`].
    /// - **An explicit 5xx settles it as a failure. Nothing else does** (BR-12). Any other read
    ///   failure — connection refused, a timeout, a 404 for a row that has not appeared yet —
    ///   keeps polling, because the server may simply be restarting and the cap already bounds
    ///   the wait. Failing on the first transient error is how a recoverable blip becomes a
    ///   launch failure, and it is the single behaviour here most likely to be "simplified"
    ///   into a bug.
    /// - **The cap settles it as `Unknown`, never a failure** (BR-11).
    ///
    /// The `Failed` payload for a 5xx is [`TerminalStatus::Error`]: `Readiness::Failed` carries
    /// the *class* of failure, and a 5xx means the server never reported a status at all. (#321)
    pub fn await_ready(&self, terminal_id: &str) -> Readiness {
        let deadline = self.host.now() + READINESS_CAP;

        loop {
            if self.host.now() >= deadline {
                return Readiness::Unknown;
            }

            match self.server.terminal(terminal_id) {
                Ok(terminal) => match classify(terminal.status) {
                    Verdict::Settled(readiness) => return readiness,
                    Verdict::KeepPolling => {}
                },
                // The one conclusive read failure (BR-12).
                Err(TuiError::Http(code)) if code >= 500 => {
                    return Readiness::Failed(TerminalStatus::Error)
                }
                Err(_) => {}
            }

            self.host.sleep(POLL_INTERVAL);
        }
    }

    /// Moves the operator's view to `terminal`, or refuses with something they can run.
    ///
    /// **Create and navigate are two distinct steps** (BR-2). `POST /sessions` already created
    /// the session *and* its window, so the only job here is moving the **view**. Conflating
    /// the two is what makes people reach for a backend attach method.
    ///
    /// The branch, keyed on observable conditions only (BR-3):
    ///
    /// | Condition | Action |
    /// |---|---|
    /// | herdr | Rely on the backend's existing `herdr tab focus` (`herdr_backend.py:637`) |
    /// | tmux **and** `$TMUX` set | `tmux switch-client -t <session>:<window>` |
    /// | tmux **and** `$TMUX` unset | Refuse, with the exact attach argv (FR-5.3) |
    ///
    /// # BR-4 said `switch-client` was forbidden. BR-4's premise was wrong, and this is the
    /// # correction
    ///
    /// The rule read: *"`switch-client` retargets the whole client to another session — the wrong
    /// verb for same-session navigation"*, and prescribed `select-window` instead. **The hand-off
    /// is not same-session navigation.** `POST /sessions` reaches `create_terminal` with
    /// `new_session=True` (`services/session_service.py:84`), so the terminal being handed off to
    /// lives in a session the operator's client is *not* attached to.
    ///
    /// `select-window` cannot move a client across sessions, and it does not fail when asked to —
    /// live-probed on tmux 3.6a with a real client attached to session A:
    /// `select-window -t B:planner` exits **0** with the client still on A. So `handoff()` returned
    /// `Ok`, the pane rendered "launched in new window · …", and the operator's screen never
    /// changed — the confident hand-off that silently does nothing, which the module docs above
    /// name as the worst available failure. It was the *rule* that was wrong, not the code
    /// implementing it. (Reported by review on PR #547.)
    ///
    /// `switch-client -t <session>:<window>` is one call that covers every case, each verified on
    /// the same probe: cross-session (client moves, target window becomes active), same-session
    /// (window changes, still correct if the topology ever changes), a bad session (**exit 1**,
    /// `can't find session`), and a bad window (**exit 1**, `can't find window`, active window
    /// untouched). Failing loudly is what lets the `Err` arm below hand back a usable
    /// `attach-session` argv instead of reporting a move that did not happen.
    ///
    /// A nested attach remains forbidden, and that half of BR-4 was right: tmux refuses
    /// `attach-session` from inside a client by default, so it would fail in exactly the case
    /// that matters. Neither backend's attach helper is called from this crate at all
    /// (BR-1/INV-2/SR-3); `tests/no_backend_attach_call.rs` is the tripwire. (#321)
    pub fn handoff(&self, terminal: &Terminal) -> Result<Outcome, Refused> {
        let session = terminal.session_name.as_str();
        let window = terminal.name.as_str();

        // SR-2, before any branch: a name that fails the allow-list cannot be placed in a
        // target, and must not be echoed back as a command either. `{:?}` so control
        // characters in a hostile name are escaped rather than smuggled into the pane — the
        // same reason `utils/terminal.py:47` uses `repr()`.
        if !is_valid_target_name(session) {
            return Err(Refused {
                reason: format!(
                    "the session name {session:?} is not a valid multiplexer target, so the \
                     view cannot be moved to it"
                ),
                manual_command: None,
            });
        }

        let backend = match self.backend() {
            Ok(backend) => backend,
            // BR-15 in its consequential form: with no known backend there is no correct
            // command to offer, and offering the tmux one anyway would be the guess the rule
            // forbids.
            Err(error) => {
                return Err(Refused {
                    reason: error.to_string(),
                    manual_command: None,
                })
            }
        };

        // The window is validated after the session so the session-level fallback below has a
        // validated session to name. An invalid window still leaves a *correct* command: attach
        // to the session and let the operator pick the window.
        if !is_valid_target_name(window) {
            return Err(Refused {
                reason: format!(
                    "the window name {window:?} is not a valid multiplexer target, so the view \
                     cannot be moved to that window"
                ),
                manual_command: match backend {
                    Backend::Tmux => Some(render_argv(&attach_argv(session))),
                    Backend::Herdr => None,
                },
            });
        }

        let target = format!("{session}:{window}");
        let outcome = || Outcome {
            session: session.to_string(),
            window: window.to_string(),
        };

        match backend {
            // herdr already navigates: `prepare_web_attach` focuses the tab
            // (`herdr_backend.py:637`), which is a `_run_herdr` subprocess call and not a
            // process replacement. Nothing to spawn from here — and note this is the branch
            // this machine's server actually reports.
            Backend::Herdr => Ok(outcome()),

            Backend::Tmux => match self.host.tmux_env() {
                Some(_) => match self.host.run(&switch_client_argv(&target)) {
                    Ok(()) => Ok(outcome()),
                    // The session or window existed a moment ago and does not now — closed
                    // between the poll and the navigate. tmux exits non-zero for both cases
                    // (probed), so this arm is reachable rather than theoretical, and the attach
                    // argv still gets the operator there.
                    Err(failure) => Err(Refused {
                        reason: format!("could not move the tmux view to {target}: {failure}"),
                        manual_command: Some(render_argv(&attach_argv(&target))),
                    }),
                },

                // FR-5.3. The designed path, not a failure: outside tmux there is no client
                // whose view could be moved, so the honest answer is the command that does it.
                None => Err(Refused {
                    reason: "the TUI is not running inside tmux ($TMUX is unset), so there is \
                             no tmux client whose view can be moved"
                        .to_string(),
                    manual_command: Some(render_argv(&attach_argv(&target))),
                }),
            },
        }
    }
}

/// The navigate argv: move this client to `session:window`, wherever that session is (TS-1).
///
/// `switch-client` and not `select-window`, because the target session is a **different** session
/// from the one the operator's client is attached to — `POST /sessions` always creates a new one.
/// `select-window` exits 0 without moving a client across sessions, which made the whole hand-off
/// a silent no-op. A single `session:window` target moves the client *and* selects the window; see
/// [`HandoffDriver::handoff`] for the probe results behind each claim.
/// (Reported by review on PR #547.)
fn switch_client_argv(target: &str) -> [&str; 4] {
    ["tmux", "switch-client", "-t", target]
}

/// The argv the operator runs by hand when nothing here can move their view (FR-5.3).
///
/// `target` is either `session:window` or a bare `session`; both are valid tmux targets.
fn attach_argv(target: &str) -> [&str; 4] {
    ["tmux", "attach-session", "-t", target]
}

/// Renders an argv for display, ready to paste.
///
/// Space-joined with no quoting, which is correct **only** because every element is either a
/// fixed literal or a name that passed [`is_valid_target_name`] — the allow-list contains no
/// whitespace, quote, or metacharacter, so there is nothing a shell could re-parse. This
/// function is display-only; execution always goes through [`Host::run`]'s argv slice. (#321)
fn render_argv(argv: &[&str]) -> String {
    argv.join(" ")
}

#[cfg(test)]
mod tests {
    use super::{
        classify, is_valid_target_name, Backend, HandoffDriver, Host, Readiness, ServerRead,
        Terminal, TerminalStatus, Verdict, POLL_INTERVAL, READINESS_CAP,
    };
    use crate::error::TuiError;
    use crate::types::Health;
    use std::cell::{Cell, RefCell};
    use std::collections::VecDeque;
    use std::time::Duration;

    /// One scripted `terminal()` answer. Modelled as data rather than as
    /// `Result<Terminal, TuiError>` because `TuiError` is not `Clone`, and a script that can be
    /// replayed is what lets the fake keep answering after it runs dry.
    enum Reply {
        /// A successful read carrying this status (`None` = the field was absent).
        Status(Option<TerminalStatus>),
        /// An HTTP error status.
        Http(u16),
        /// The server could not be reached at all.
        Unreachable,
    }

    struct FakeServer {
        /// What `GET /health` reports as `terminal_backend`.
        backend: String,
        /// Consumed front-to-back. Once empty the server answers `Processing` forever, which
        /// is what lets a cap test keep polling to the deadline.
        script: RefCell<VecDeque<Reply>>,
        health_calls: Cell<usize>,
        terminal_calls: Cell<usize>,
    }

    impl FakeServer {
        fn new(backend: &str, script: Vec<Reply>) -> Self {
            Self {
                backend: backend.to_string(),
                script: RefCell::new(script.into()),
                health_calls: Cell::new(0),
                terminal_calls: Cell::new(0),
            }
        }

        fn with_backend(backend: &str) -> Self {
            Self::new(backend, Vec::new())
        }
    }

    impl ServerRead for FakeServer {
        fn health(&self) -> Result<Health, TuiError> {
            self.health_calls.set(self.health_calls.get() + 1);
            Ok(Health {
                status: "ok".to_string(),
                terminal_backend: self.backend.clone(),
            })
        }

        fn terminal(&self, terminal_id: &str) -> Result<Terminal, TuiError> {
            self.terminal_calls.set(self.terminal_calls.get() + 1);

            let reply = self
                .script
                .borrow_mut()
                .pop_front()
                .unwrap_or(Reply::Status(Some(TerminalStatus::Processing)));

            match reply {
                Reply::Status(status) => Ok(Terminal {
                    id: terminal_id.to_string(),
                    name: "planner-1".to_string(),
                    session_name: "work".to_string(),
                    status,
                }),
                Reply::Http(code) => Err(TuiError::Http(code)),
                Reply::Unreachable => Err(TuiError::Unreachable("connection refused".to_string())),
            }
        }
    }

    struct FakeHost {
        tmux: Option<String>,
        /// Advanced only by `sleep`, so the 30s cap is reached in microseconds.
        now: Cell<Duration>,
        sleeps: Cell<u32>,
        run_result: Result<(), String>,
        /// Every argv actually spawned. A test asserting this is **empty** is how the refusal
        /// path is proven to spawn nothing.
        spawned: RefCell<Vec<Vec<String>>>,
    }

    impl FakeHost {
        fn inside_tmux() -> Self {
            Self::new(
                Some("/private/tmp/tmux-504/default,12345,0".to_string()),
                Ok(()),
            )
        }

        /// Mirrors this machine's verified state: `$TMUX` unset.
        fn outside_tmux() -> Self {
            Self::new(None, Ok(()))
        }

        fn new(tmux: Option<String>, run_result: Result<(), String>) -> Self {
            Self {
                tmux,
                now: Cell::new(Duration::ZERO),
                sleeps: Cell::new(0),
                run_result,
                spawned: RefCell::new(Vec::new()),
            }
        }
    }

    impl Host for FakeHost {
        fn tmux_env(&self) -> Option<String> {
            self.tmux.clone()
        }

        fn now(&self) -> Duration {
            self.now.get()
        }

        fn sleep(&self, duration: Duration) {
            self.sleeps.set(self.sleeps.get() + 1);
            self.now.set(self.now.get() + duration);
        }

        fn run(&self, argv: &[&str]) -> Result<(), String> {
            self.spawned
                .borrow_mut()
                .push(argv.iter().map(|part| (*part).to_string()).collect());
            self.run_result.clone()
        }
    }

    /// A terminal whose names are the ones every hard-coded expectation below is written
    /// against: session `work`, window `planner-1`.
    fn sample_terminal() -> Terminal {
        Terminal {
            id: "a1b2c3d4".to_string(),
            name: "planner-1".to_string(),
            session_name: "work".to_string(),
            status: Some(TerminalStatus::Idle),
        }
    }

    /// Test 1 — the six-to-three map, **all eight cases** (VR-2, BR-9).
    ///
    /// Eight and not two: a test covering only `Idle` and `Error` leaves `WaitingUserAnswer` —
    /// the consequential mapping (BR-10) — completely unproven, and that arm is the one whose
    /// mistake costs a healthy agent the full 30-second cap.
    ///
    /// Seven cases are pure and asserted against [`classify`]; the eighth (cap elapsed) needs
    /// the loop and is asserted through `await_ready` with a fake clock. The expected verdicts
    /// are written out as literals read off BR-9's table, **not** derived from `classify` —
    /// deriving them would make the test agree with whatever the code says.
    ///
    /// Proven by mutation: changing the `WaitingUserAnswer` arm to keep-polling turns this red.
    /// (#321)
    #[test]
    fn the_six_to_three_map_covers_all_eight_cases() {
        let cases = [
            (
                Some(TerminalStatus::Idle),
                Verdict::Settled(Readiness::Ready),
            ),
            (
                Some(TerminalStatus::Completed),
                Verdict::Settled(Readiness::Ready),
            ),
            (
                Some(TerminalStatus::WaitingUserAnswer),
                Verdict::Settled(Readiness::Ready),
            ),
            (Some(TerminalStatus::Processing), Verdict::KeepPolling),
            (Some(TerminalStatus::Unknown), Verdict::KeepPolling),
            (None, Verdict::KeepPolling),
            (
                Some(TerminalStatus::Error),
                Verdict::Settled(Readiness::Failed(TerminalStatus::Error)),
            ),
        ];
        assert_eq!(
            cases.len(),
            7,
            "the literal table itself must carry all six statuses plus the absent case; the \
             eighth case (cap elapsed) is asserted below"
        );

        for (status, expected) in cases {
            assert_eq!(
                classify(status),
                expected,
                "BR-9's table maps {status:?} to {expected:?}"
            );
        }

        // Case 8: the cap. The server never settles, so only the deadline can end the loop.
        let server = FakeServer::new(
            "tmux",
            vec![Reply::Status(Some(TerminalStatus::Processing))],
        );
        let host = FakeHost::outside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        assert_eq!(
            driver.await_ready("a1b2c3d4"),
            Readiness::Unknown,
            "a poll that never settles must yield Unknown — a WARNING, never a failure \
             (BR-11): `unknown is not broken`"
        );
        assert_eq!(
            host.sleeps.get(),
            30,
            "30 sleeps of {POLL_INTERVAL:?} must be what reaches the {READINESS_CAP:?} cap; a \
             different count means the interval or the cap moved"
        );
        assert_ne!(
            Readiness::Unknown,
            Readiness::Failed(TerminalStatus::Error),
            "Unknown must never be equal to a failure (ADR-04)"
        );
    }

    /// Test 2 — the refusal path yields the **exact** argv (FR-5.3, VR-3, VR-5).
    ///
    /// `$TMUX` unset is this machine's verified state, so this is the branch that actually
    /// executes here. The expected command is a **hard-coded literal** (VR-5): deriving it from
    /// `attach_argv`/`render_argv` would make the test agree with the builder and stay green
    /// through the exact typo it exists to catch.
    ///
    /// The second assertion is the one with real teeth beyond the string: **nothing was
    /// spawned**. A refusal that quietly ran a command anyway would satisfy the string
    /// assertion perfectly.
    ///
    /// Proven by mutation: altering one character of the expected argv turns this red. (#321)
    #[test]
    fn tmux_outside_tmux_refuses_with_the_exact_attach_argv() {
        let server = FakeServer::with_backend("tmux");
        let host = FakeHost::outside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        let refused = driver
            .handoff(&sample_terminal())
            .expect_err("outside tmux there is no client to move, so this must refuse (FR-5.3)");

        assert_eq!(
            refused.manual_command.as_deref(),
            Some("tmux attach-session -t work:planner-1"),
            "the refusal must carry the exact runnable argv, ready to paste with no editing \
             (SR-4); an argv the operator has to fix invites them to improvise"
        );
        assert!(
            refused.reason.contains("$TMUX"),
            "the reason must name the observable condition the branch keyed on, not a \
             paraphrase; got {reason:?}",
            reason = refused.reason
        );
        assert!(
            host.spawned.borrow().is_empty(),
            "the refusal path must spawn NOTHING; it ran {spawned:?}",
            spawned = host.spawned.borrow()
        );
    }

    /// Test 4 — an unrecognised `terminal_backend` is an error, never a default (BR-15).
    ///
    /// Both halves matter. The first is that `backend()` errors and **names the offending
    /// value**. The second is the consequence: `handoff` refuses *without inventing a command*
    /// and without spawning anything — because the failure BR-15 guards against is a confident
    /// hand-off that silently does nothing, and a guessed argv is exactly that failure moved
    /// into the operator's clipboard. (#321)
    #[test]
    fn an_unrecognised_terminal_backend_is_an_error_not_a_default() {
        let server = FakeServer::with_backend("screen");
        let host = FakeHost::inside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        let error = driver
            .backend()
            .expect_err("an unknown terminal_backend must not resolve to a variant (BR-15)");

        assert!(
            matches!(error, TuiError::Unreachable(_)),
            "an unknown backend is the driver's one genuine failure; got {error:?}"
        );
        assert!(
            error.to_string().contains("screen"),
            "the error must name what the server actually reported so the operator can act on \
             it; got {error}"
        );

        let refused = driver
            .handoff(&sample_terminal())
            .expect_err("with no known backend there is no view this crate can move");
        assert_eq!(
            refused.manual_command, None,
            "no backend means no correct command; offering the tmux one would be the guess \
             BR-15 forbids"
        );
        assert!(
            host.spawned.borrow().is_empty(),
            "an unknown backend must spawn nothing at all; it ran {spawned:?}",
            spawned = host.spawned.borrow()
        );
    }

    /// Test 5 — a transient read failure keeps polling; **only a 5xx is conclusive** (BR-12).
    ///
    /// Both directions are asserted in one test on purpose: "keeps polling" alone would also be
    /// satisfied by a loop that can never fail at all, so the 5xx contrast is what gives the
    /// first half meaning. Getting this wrong turns a server restart — a recoverable blip —
    /// into a launch failure. (#321)
    #[test]
    fn a_transient_read_failure_continues_but_a_5xx_is_conclusive() {
        let server = FakeServer::new(
            "tmux",
            vec![
                Reply::Unreachable,
                Reply::Unreachable,
                // Not a 5xx, so not conclusive: the row may not be visible yet.
                Reply::Http(404),
                Reply::Status(Some(TerminalStatus::Idle)),
            ],
        );
        let host = FakeHost::outside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        assert_eq!(
            driver.await_ready("a1b2c3d4"),
            Readiness::Ready,
            "two unreachable polls and a 404 must not end the wait: the server may be \
             restarting and the 30s cap already bounds it (BR-12)"
        );
        assert_eq!(
            server.terminal_calls.get(),
            4,
            "the poll must have survived the first three answers and read a fourth time"
        );
        assert_eq!(
            host.sleeps.get(),
            3,
            "one sleep per inconclusive answer, so the retry is paced rather than a hot loop"
        );

        let failing = FakeServer::new("tmux", vec![Reply::Http(503)]);
        let host_for_5xx = FakeHost::outside_tmux();
        let driver = HandoffDriver::new(&failing, &host_for_5xx);

        assert_eq!(
            driver.await_ready("a1b2c3d4"),
            Readiness::Failed(TerminalStatus::Error),
            "an explicit 5xx is the one conclusive read failure (BR-12)"
        );
        assert_eq!(
            failing.terminal_calls.get(),
            1,
            "a 5xx must stop the poll immediately rather than waiting out the cap"
        );
    }

    /// Test 6 — the tmux **navigate** branch builds `switch-client`, and nothing else.
    ///
    /// # What this proves, and what it explicitly does NOT
    ///
    /// It proves the branch selection keyed on `$TMUX` and the **exact argv constructed** —
    /// including that the verb is `switch-client` and not `select-window` (which cannot move a
    /// client across sessions) and not an attach (which tmux refuses from inside a client).
    ///
    /// **This assertion was inverted, and the inversion was the defect.** It used to require
    /// `select-window` on BR-4's stated premise that a hand-off is "same-session navigation". It
    /// is not: `POST /sessions` always creates a NEW session
    /// (`services/session_service.py:84`, `new_session=True`), so the target is a session the
    /// operator's client is not attached to. `select-window` exits **0** there without moving
    /// anything — live-probed on tmux 3.6a — so this test passed while the feature did nothing.
    /// A test can be green *because* of the bug; this was that. (Reported by review on PR #547.)
    ///
    /// It still does **not** prove that a real operator's view moves, nor that the TUI survives
    /// it — that is one process spawning `tmux` against a live server, which this unit test does
    /// not do. What backs the verb choice is a manual probe with a real client attached over a
    /// pty, recorded in [`HandoffDriver::handoff`]'s docs: cross-session move, same-session move,
    /// bad session (exit 1) and bad window (exit 1) were each observed. `$TMUX` here remains an
    /// injected value. The branch this machine's server actually reports is herdr. (#321)
    #[test]
    fn tmux_inside_tmux_builds_the_switch_client_argv_and_nothing_else() {
        let server = FakeServer::with_backend("tmux");
        let host = FakeHost::inside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        let outcome = driver
            .handoff(&sample_terminal())
            .expect("inside tmux, navigating to an existing window must succeed");

        assert_eq!(outcome.session, "work");
        assert_eq!(outcome.window, "planner-1");
        assert_eq!(
            *host.spawned.borrow(),
            vec![vec![
                "tmux".to_string(),
                "switch-client".to_string(),
                "-t".to_string(),
                "work:planner-1".to_string(),
            ]],
            "exactly one argv, hard-coded here rather than taken from the builder: \
             `switch-client` at the `session:window` target, which moves the client AND selects \
             the window in one call. `select-window` cannot cross sessions and exits 0 anyway; a \
             nested attach fails outright"
        );

        // The negative half, asserted separately so a regression to the old verb cannot hide
        // behind a passing shape comparison. `select-window` is the specific wrong answer here.
        let flattened = host.spawned.borrow().concat().join(" ");
        assert!(
            !flattened.contains("select-window"),
            "`select-window` is the verb that made this hand-off a silent no-op; it must not \
             reappear on the navigate path. Got: {flattened:?}"
        );
    }

    /// Test 7 — herdr relies on the backend's own tab focus and spawns nothing from here.
    ///
    /// This is the branch this machine's live `GET /health` actually reports, so it is the one
    /// that executes in practice. The assertion that matters is the negative: this crate spawns
    /// **no** process on the herdr path, because `prepare_web_attach` already focused the tab
    /// server-side (`herdr_backend.py:637`, a `_run_herdr` subprocess call — not the
    /// `os.execvp` at `:631` that would have replaced the caller). (#321)
    #[test]
    fn herdr_relies_on_the_backends_existing_focus() {
        let server = FakeServer::with_backend("herdr");
        let host = FakeHost::outside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        let outcome = driver
            .handoff(&sample_terminal())
            .expect("herdr already focuses the tab it created, so hand-off succeeds");

        assert_eq!(outcome.window, "planner-1");
        assert!(
            host.spawned.borrow().is_empty(),
            "the herdr path must spawn nothing from this crate; it ran {spawned:?}",
            spawned = host.spawned.borrow()
        );
        assert_eq!(
            server.health_calls.get(),
            1,
            "the backend is read once and cached; re-reading it would add a network round trip \
             to every launch"
        );

        // The cache is what that assertion is really about: a second hand-off must not re-read.
        let _ = driver.handoff(&sample_terminal());
        assert_eq!(
            server.health_calls.get(),
            1,
            "a second hand-off must use the cached backend (TS-3)"
        );
        assert_eq!(driver.backend().expect("cached"), Backend::Herdr);
    }

    /// Test 8 — names are validated before they reach a target (SR-2).
    ///
    /// The allow-list is mirrored from `utils/terminal.py:20`, and the cases below are the ones
    /// that would change a target's *meaning* rather than merely look odd: `:` and `.` are tmux
    /// target delimiters, a leading `-` parses as an option, and `;`/`$()`/whitespace are what
    /// would matter if the display string were ever pasted. The refusal for a bad window still
    /// hands back a **correct** command — attach to the session — rather than nothing. (#321)
    #[test]
    fn invalid_target_names_are_refused_before_they_reach_a_command() {
        for good in ["work", "planner-1", "_a", "a", &"a".repeat(64)] {
            assert!(
                is_valid_target_name(good),
                "{good:?} matches the server's own allow-list and must be accepted"
            );
        }
        for bad in [
            "",
            "-work",
            "work:evil",
            "work.evil",
            "work;rm -rf /",
            "work $(id)",
            "work evil",
            "work\n",
            "wörk",
            &"a".repeat(65),
        ] {
            assert!(
                !is_valid_target_name(bad),
                "{bad:?} must be rejected: it either changes the tmux target's meaning or \
                 would not be safe to print as a runnable command"
            );
        }

        let server = FakeServer::with_backend("tmux");
        let host = FakeHost::inside_tmux();
        let driver = HandoffDriver::new(&server, &host);

        let bad_window = Terminal {
            name: "planner:1".to_string(),
            ..sample_terminal()
        };
        let refused = driver
            .handoff(&bad_window)
            .expect_err("a window name carrying a tmux delimiter must not reach a target");
        assert_eq!(
            refused.manual_command.as_deref(),
            Some("tmux attach-session -t work"),
            "an unusable window still leaves a correct session-level command"
        );
        assert!(
            host.spawned.borrow().is_empty(),
            "an invalid name must be caught before anything is spawned"
        );

        let bad_session = Terminal {
            session_name: "-work".to_string(),
            ..sample_terminal()
        };
        let refused = driver
            .handoff(&bad_session)
            .expect_err("a session name that tmux would parse as an option must be refused");
        assert_eq!(
            refused.manual_command, None,
            "with no usable session there is no correct command to offer"
        );
    }
}
