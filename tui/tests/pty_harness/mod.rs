//! The real-terminal test harness for `cao-tui` (issue #321). Walking-skeleton item 2.
//!
//! # Why this module exists
//!
//! The predecessor Python TUI passed CI with **304 tests, 39 mutation proofs, and 90.66%
//! coverage — and it only partially worked.** Across its 273 test files the only `openpty`
//! references are three *mock* sites in `test/api/test_terminals.py`: `:719` and `:761`
//! patch it with `return_value=(100, 101)` — fabricated file descriptors — and `:793`
//! asserts it is never called. Production genuinely calls `pty.openpty()` at
//! `api/main.py:3039`. The pty path was not merely untested; it was **deliberately stubbed,
//! and the suite asserted against fake fds**.
//!
//! So the value here is not "we have a harness". It is that **`isatty` becomes assertable**
//! — the one property a pipe structurally cannot satisfy. (#321)
//!
//! # Why this lives in `tests/` and not in `src/`
//!
//! `portable-pty` is a **`[dev-dependency]`**: this is test infrastructure whose types never
//! appear in the shipped binary's runtime paths, and promoting it to `[dependencies]` would
//! grow the release binary against NFR-2's 10 MB ceiling for nothing. A dev-dependency is
//! only linkable from test/bench/example targets, so a harness in `src/` **could not compile**
//! against it. `tests/pty_harness/mod.rs` is a plain module of the `tests/pty.rs` integration
//! target — not a target of its own — which is what makes the dev-dependency reachable. (#321)
//!
//! # T-7 — the constraint that makes or breaks this harness
//!
//! **`child.wait()` before draining the pty DEADLOCKS.** Proven by experiment with
//! `yes hello | head -20000`: the child fills the pty buffer and blocks writing, the parent
//! blocks in `wait()`, and neither proceeds. Re-confirmed while building this module — a
//! throwaway probe with the drain thread removed hung until an external 20s timeout killed it.
//!
//! Two things a careful reader gets wrong here, recorded because the failure is a hang and
//! hangs are diagnosed badly:
//!
//! - **The first hypothesis was WRONG.** Undropped master handles blocking EOF is *not* the
//!   cause; dropping both did not fix it. **Check ordering before handle lifetimes** (BR-4).
//! - **Dropping the slave in the parent is necessary but NOT sufficient.** [`spawn`] does it,
//!   but it is not what prevents the deadlock.
//!
//! **Ordering is the design, not an optimisation** (BR-3). [`spawn`] opens the pty, spawns the
//! child on the slave, drops the slave, and **starts the drain thread before it returns**.
//! Nothing can `wait()` before the reader exists, because no `PtySession` exists until it does.
//!
//! # The four orderings (from `business-logic-model.md`)
//!
//! | Ordering | Result |
//! |---|---|
//! | drain, then wait | **Correct.** Buffer never fills; child proceeds |
//! | wait, then drain | **DEADLOCK.** Child blocks writing; parent blocks waiting. Proven |
//! | drain without bounds, child never exits | **HANG.** No deadlock, but an unbounded wait |
//! | parent keeps the slave open | EOF never arrives *in principle*; bounded reads still terminate the loop. **Measured on Darwin: EOF arrives anyway** — see [`READ_POLL_TIMEOUT`] |
//!
//! # No `unsafe`, deliberately
//!
//! `#![forbid(unsafe_code)]` holds crate-wide and pty work usually reaches for `libc`
//! directly. `portable-pty` was chosen precisely so this unit needs none: pty allocation,
//! `TIOCSWINSZ`, and the `setsid`/`TIOCSCTTY` dance all live behind its safe API, and the
//! bounded read uses `filedescriptor::poll` rather than a hand-rolled `libc::poll`. (#321)

use std::io::Read;
use std::os::fd::{AsRawFd, RawFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use filedescriptor::{pollfd, FileDescriptor, POLLIN};
use nix::errno::Errno;
use nix::sys::signal::{kill, killpg, Signal};
use nix::unistd::{getpgid, Pid};
// `PtySystem` is deliberately NOT imported: `native_pty_system()` hands back a
// `Box<dyn PtySystem + Send>`, and method resolution on a trait object does not require the
// trait in scope. Importing it warns as unused, and `-D warnings` is a hard gate. (#321)
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty};
use thiserror::Error;

/// Re-exported so a test can assert on an exit code without naming `portable_pty` itself.
pub use portable_pty::ExitStatus;

/// How long a single `poll` waits before the drain loop re-checks its own liveness.
///
/// **This is the per-read bound (BR-1, BR-5).** An unbounded read is how a pty drain hangs:
/// with `None` — `poll`'s "wait indefinitely" — a drain against a pty that never closes and
/// never writes again cannot observe [`PtySession::stop`], and `join()` blocks behind it.
///
/// # What the VR-3 mutation actually showed, recorded because it is a negative
///
/// VR-3 predicts that removing this bound stops the hung-child test terminating. **On macOS it
/// does not, and the honest reason is worth keeping:** the mutation was applied (poll timeout
/// to `None`, liveness check disabled) and all six tests stayed green. Investigating rather
/// than accepting that, the drain's exit reason was instrumented across six scenarios —
/// child exits normally, child killed after a timeout, a `setsid` grandchild, a same-session
/// grandchild, a `setsid`+`SIGHUP`-ignoring grandchild, and decision-table row 4 with the
/// **parent** deliberately retaining the slave. **Every one ended via `Ok(0)`/EOF, in
/// microseconds, bounded or not.** Darwin surfaces the hangup on the master even while another
/// fd holds the slave open, so EOF — not this timeout — is what ends the drain in every case
/// reachable from these tests.
///
/// So the bound is a **guard against a state these tests cannot currently reach**, not dead
/// code: BR-9 records that EOF and `EIO` semantics differ between Darwin and Linux, and
/// `rust-ci` (Bolt 2) runs the same harness on `ubuntu-latest`, where the parent-holds-slave
/// case is exactly where the two platforms are documented to diverge. Keeping it is cheap;
/// removing it on the strength of a macOS-only measurement would be exactly the
/// one-platform-proves-all reasoning BR-10 exists to prevent. Stated plainly so nobody reads
/// a passing mutation as proof this line is redundant. (#321)
const READ_POLL_TIMEOUT: Duration = Duration::from_millis(20);

/// Absolute ceiling on the drain thread's lifetime, independent of every other bound.
///
/// Belt to [`READ_POLL_TIMEOUT`]'s braces: if the poll bound were ever weakened, the loop
/// still cannot outlive this. Generous because it is a backstop, not a schedule — the drain
/// normally ends on EOF or the stop flag within milliseconds. (#321)
const DRAIN_LIFETIME_CAP: Duration = Duration::from_secs(60);

/// Gap between `try_wait` polls in the bounded wait.
const WAIT_POLL_INTERVAL: Duration = Duration::from_millis(5);

/// How long the drain thread is given to end **on its own** once the child is gone.
///
/// Load-bearing for output completeness, not just tidiness. Setting the stop flag the instant
/// `try_wait` reports an exit would race the drain: bytes still sitting in the pty buffer
/// would be dropped, and `Outcome::Timeout`'s `output_so_far` — the whole diagnostic value of
/// BR-6's "report" half — would silently truncate. The child is dead by now, so the slave is
/// closed and the drain reaches EOF in milliseconds; this window just lets it. (#321)
const DRAIN_SETTLE_GRACE: Duration = Duration::from_millis(500);

/// Grace between `SIGTERM` and `SIGKILL`, in [`TERM_GRACE_STEPS`] steps.
const TERM_GRACE_STEP: Duration = Duration::from_millis(100);

/// Number of [`TERM_GRACE_STEP`] waits before escalating to `SIGKILL`.
///
/// 5 x 100 ms = 500 ms, measured sufficient for a `sh` that traps and ignores `TERM`. Bounded
/// because BR-7 requires the kill to *actually* kill: a polite signal the child ignores turns
/// a bounded test into a leaked process, and leaked processes accumulate until they exhaust
/// the CI runner. (#321)
const TERM_GRACE_STEPS: u32 = 5;

/// Terminal geometry for a session.
///
/// Defaults to **80x24 — the NFR-6 minimum**, deliberately, so the default test environment
/// sits exactly on the boundary the requirement names. Exercising the sub-minimum path then
/// requires explicitly choosing a smaller size, which makes that test's intent visible in its
/// own body rather than hiding in a default. (#321)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PtySize {
    /// Rows of text.
    pub rows: u16,
    /// Columns of text.
    pub cols: u16,
}

impl Default for PtySize {
    fn default() -> Self {
        Self { rows: 24, cols: 80 }
    }
}

impl PtySize {
    /// Widens to `portable_pty`'s four-field size. Pixel dimensions are 0: no test asserts on
    /// them, and the kernel ignores them for the `stty size` geometry under test.
    fn to_portable(self) -> portable_pty::PtySize {
        portable_pty::PtySize {
            rows: self.rows,
            cols: self.cols,
            pixel_width: 0,
            pixel_height: 0,
        }
    }
}

/// The result of a **bounded** wait.
///
/// **A timeout is not an [`HarnessError`].** A bounded wait reaching its bound is the harness
/// working correctly, so it is an ordinary outcome rather than a failure.
///
/// **`Timeout` carries the output.** BR-6 requires kill *and* report, and modelling the bytes
/// on the variant makes reporting unavoidable rather than optional: a caller cannot observe a
/// timeout without being handed the diagnostics. Killing without reporting leaves a
/// diagnosis-free failure, which is precisely the pathology this unit exists to remove — a
/// hanging test already reports a timeout with no diagnosis. (#321)
#[derive(Debug)]
pub enum Outcome {
    /// The child finished on its own within the deadline.
    Exited(ExitStatus),
    /// The deadline passed. **The child was killed** and everything drained so far is here.
    Timeout {
        /// Bytes captured before the deadline. Possibly empty — empty is reported, not an error.
        output_so_far: Vec<u8>,
    },
}

/// Genuine environment failures only.
///
/// Deliberately **not** the crate-root `TuiError`. Affirmed practice is one crate-root error
/// type, and `src/error.rs` holds it — but `cao-tui` is a *binary* crate with no `lib` target,
/// so `TuiError` is unreachable from an integration test no matter what its visibility says.
/// The alternative would be restructuring the crate into `lib.rs` + `main.rs` purely to share
/// an error type with test scaffolding, which is not this unit's scope. `domain-entities.md`
/// specifies this unit's own `Error` with `PtyAlloc`/`Spawn` for the same reason. (#321)
#[derive(Debug, Error)]
pub enum HarnessError {
    /// The kernel would not give us a pty. A real environment failure, never a test bug.
    #[error("could not allocate a pty")]
    PtyAlloc(#[source] anyhow::Error),
    /// The child could not be spawned onto the pty slave.
    #[error("could not spawn the child onto the pty slave")]
    Spawn(#[source] anyhow::Error),
    /// `TIOCSWINSZ` failed. Surfaced rather than swallowed: a silently-ignored resize would
    /// make NFR-6's geometry test assert against a window that never changed. (#321)
    #[error("could not resize the pty")]
    Resize(#[source] anyhow::Error),
    /// Signalling the child's process group failed for a reason other than "already gone".
    #[error("could not signal the child process group")]
    Kill(#[source] Errno),
}

/// Lets `filedescriptor::dup` accept a bare `RawFd`.
///
/// `dup` wants `&F where F: AsRawFileDescriptor`, and `filedescriptor` blanket-implements that
/// for every `AsRawFd`. The std type for this job is `BorrowedFd`, but `BorrowedFd::borrow_raw`
/// is `unsafe` and `#![forbid(unsafe_code)]` is not negotiable — so this three-line safe impl
/// is the route to an owned duplicate. It borrows: it never closes the fd it wraps. (#321)
struct BorrowedRawFd(RawFd);

impl AsRawFd for BorrowedRawFd {
    fn as_raw_fd(&self) -> RawFd {
        self.0
    }
}

/// One child running under one real pty.
///
/// # `reader` is a field, and that is the design
///
/// It exists from construction, which makes "drain before wait" (BR-3) **structurally true**
/// rather than a discipline every caller has to remember: a `PtySession` that exists has a
/// running drain thread, so there is no window in which a `wait` can precede a drain.
///
/// It is `Option<JoinHandle<()>>` rather than a bare `JoinHandle<()>` for exactly one reason:
/// [`Drop`] must be able to join it. `join()` consumes the handle by value, and a `Drop` impl
/// only ever has `&mut self`, so a bare handle could not be joined during teardown — and
/// teardown is where it matters most. A test that panics mid-assertion unwinds *past*
/// [`PtySession::wait_with_timeout`], and without a `Drop` that kills and joins, that panic
/// leaks the child process (a `sleep 3600` outliving the suite) and the drain thread with it.
/// The invariant the design cares about is preserved: [`spawn`] always populates it, and only
/// the two consuming paths — the bounded wait and `Drop` — ever take it. (#321)
///
/// # The slave handle is deliberately NOT a field
///
/// [`spawn`] drops it immediately. Holding it would keep the pty from ever reaching EOF —
/// necessary to avoid, but **not** the cause of the observed deadlock (BR-4).
pub struct PtySession {
    /// The spawned process.
    child: Box<dyn Child + Send + Sync>,
    /// The parent's end of the pty. Kept for `resize`/`get_size`.
    master: Box<dyn MasterPty + Send>,
    /// The drain thread. See the type docs for why this is `Option`.
    reader: Option<JoinHandle<()>>,
    /// Filled concurrently by `reader`. See [`PtySession::output`] for the INV-5 lock rule.
    buffer: Arc<Mutex<Vec<u8>>>,
    /// Current geometry, updated by [`PtySession::resize`].
    size: PtySize,
    /// Asks the drain thread to stop. Needed because EOF is not guaranteed: anything still
    /// holding the slave open (a backgrounded grandchild) keeps the pty alive, so the drain
    /// would otherwise sit until [`DRAIN_LIFETIME_CAP`] and `join()` with it. Measured: with
    /// the flag, that join returns in ~30us. (#321)
    stop: Arc<AtomicBool>,
    /// The child's pid, cached because it is unavailable once the child is reaped.
    pid: u32,
    /// Whether the child has been reaped, so `Drop` does not re-kill and re-wait.
    finished: bool,
}

/// Opens a pty, spawns `argv` on the slave, and returns a session **already draining**.
///
/// The ordering is the whole point and it is not rearrangeable:
///
/// 1. `openpty(size)`
/// 2. spawn the child on the slave
/// 3. **drop the slave** — the parent must not hold it (BR-4: necessary, not sufficient)
/// 4. **start the drain thread**
/// 5. only then return
///
/// Nothing can `wait()` before step 4, because no `PtySession` exists until step 5. Moving
/// step 4 after a `wait` is the mutation VR-2 names, and it hangs. (#321)
///
/// # Errors
///
/// [`HarnessError::PtyAlloc`] if the kernel refuses a pty, [`HarnessError::Spawn`] if the
/// child cannot be started on the slave.
pub fn spawn(argv: &[&str], size: PtySize) -> Result<PtySession, HarnessError> {
    assert!(!argv.is_empty(), "spawn needs at least a program name");

    let pair = native_pty_system()
        .openpty(size.to_portable())
        .map_err(HarnessError::PtyAlloc)?;

    // An argv vector, never an interpolated shell string: the affirmed thin-shell rule.
    let mut command = CommandBuilder::new(argv[0]);
    command.args(&argv[1..]);

    let child = pair
        .slave
        .spawn_command(command)
        .map_err(HarnessError::Spawn)?;
    let pid = child
        .process_id()
        .expect("a unix child spawned onto a pty always reports a pid");

    // Step 3. The parent must not hold the slave; without this the pty never reaches EOF.
    // Necessary but NOT sufficient — see BR-4 and this module's header.
    drop(pair.slave);

    // The drain thread owns BOTH of its handles, so neither can dangle if the session is
    // dropped first:
    //   * `reader` — `try_clone_reader()` dups the master and, on unix, already maps `EIO` to
    //     `Ok(0)` for us; the loop below *also* handles `EIO` explicitly rather than trusting
    //     a dependency's internal translation for a rule BR-9 names outright.
    //   * `poll_handle` — an owned dup used only for readiness. Readiness is a property of the
    //     underlying pty, not of a particular fd, so polling one dup and reading the other is
    //     sound; owning it is what keeps the raw int in the `pollfd` valid. (#321)
    let mut reader = pair
        .master
        .try_clone_reader()
        .map_err(HarnessError::PtyAlloc)?;
    let master_fd = pair
        .master
        .as_raw_fd()
        .expect("a unix master pty always exposes a raw fd");
    let poll_handle = FileDescriptor::dup(&BorrowedRawFd(master_fd))
        .map_err(|e| HarnessError::PtyAlloc(anyhow::Error::new(e)))?;

    let buffer = Arc::new(Mutex::new(Vec::<u8>::new()));
    let stop = Arc::new(AtomicBool::new(false));
    let sink = Arc::clone(&buffer);
    let halt = Arc::clone(&stop);

    // Step 4: the drain thread, started BEFORE this function returns.
    let reader_thread = std::thread::spawn(move || {
        // Moved in so the dup outlives every poll that names its fd.
        let poll_handle = poll_handle;
        let poll_fd = poll_handle.as_raw_fd();
        let cap = Instant::now() + DRAIN_LIFETIME_CAP;
        // The stack buffer lives OUTSIDE the lock. INV-5: see the lock site below.
        let mut chunk = [0_u8; 8192];

        loop {
            if halt.load(Ordering::Relaxed) || Instant::now() >= cap {
                break;
            }

            let mut fds = [pollfd {
                fd: poll_fd,
                events: POLLIN,
                revents: 0,
            }];
            // The bound. `Some(..)`, never `None` (BR-1, BR-5).
            match filedescriptor::poll(&mut fds, Some(READ_POLL_TIMEOUT)) {
                // Nothing ready yet: re-check liveness and the cap, then poll again.
                Ok(0) => continue,
                // Any readiness at all -> attempt the read. Deliberately not gated on a
                // specific revents flag: a hangup arrives as POLLHUP on Linux and as
                // readability on Darwin, and both must reach the read that returns Ok(0).
                Ok(_) => {}
                Err(_) => break,
            }

            match reader.read(&mut chunk) {
                // EOF: the child closed its end.
                Ok(0) => break,
                Ok(n) => {
                    // INV-5 — the ONE lock site, and it holds the lock across nothing but
                    // this append. Holding it across the `read` above, a `wait`, or a `join`
                    // would reproduce the exact deadlock class this harness exists to prevent,
                    // with a mutex standing in for the pipe: the drain would stall behind a
                    // caller inspecting output, the pty buffer would fill, and the child would
                    // block writing. Read into the stack buffer first, lock only to append.
                    // (#321)
                    match sink.lock() {
                        Ok(mut held) => held.extend_from_slice(&chunk[..n]),
                        // A poisoned mutex means a *reader* panicked while holding it. Stop
                        // instead of unwrapping: a panic here would poison the drain thread
                        // and hide the real failure behind a confusing timeout (INV-4).
                        Err(_) => break,
                    }
                }
                // EINTR is transient, not a close.
                Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                // `EIO` on read is a NORMAL pty close on Linux, not an error (BR-9). Treating
                // it as a failure would fail every Linux test at teardown. Handled explicitly
                // here as well as inside `portable_pty`'s reader, because BR-9 is a named rule
                // of this unit and should not rest on a dependency's internal choice. (#321)
                Err(ref e) if e.raw_os_error() == Some(Errno::EIO as i32) => break,
                Err(_) => break,
            }
        }
    });

    Ok(PtySession {
        child,
        master: pair.master,
        reader: Some(reader_thread),
        buffer,
        size,
        stop,
        pid,
        finished: false,
    })
}

impl PtySession {
    /// The child's pid, valid for the life of the session even after the child is reaped.
    ///
    /// Exposed so a test can assert **process absence** independently of the harness's own
    /// return value: `kill()` returning `Ok` proves only that the call was made, while
    /// [`process_is_gone`] proves it worked (VR-4, BR-7). (#321)
    pub fn pid(&self) -> u32 {
        self.pid
    }

    /// Current geometry as the harness believes it to be.
    pub fn size(&self) -> PtySize {
        self.size
    }

    /// Geometry as the **kernel** reports it for this pty.
    ///
    /// A separate reading from [`Self::size`] on purpose: `size()` is the harness's record of
    /// what it asked for, and this is what actually took effect. A resize test that consulted
    /// only the harness's own field could pass while `TIOCSWINSZ` did nothing. (#321)
    ///
    /// # Errors
    ///
    /// [`HarnessError::Resize`] if the ioctl fails.
    pub fn kernel_size(&self) -> Result<PtySize, HarnessError> {
        let got = self.master.get_size().map_err(HarnessError::Resize)?;
        Ok(PtySize {
            rows: got.rows,
            cols: got.cols,
        })
    }

    /// Resizes the pty, which signals `SIGWINCH` to the child.
    ///
    /// Required by BR-14: without it NFR-6's sub-80x24 stacked-collapse path has no test at
    /// all. (#321)
    ///
    /// # Errors
    ///
    /// [`HarnessError::Resize`] if `TIOCSWINSZ` fails.
    pub fn resize(&mut self, size: PtySize) -> Result<(), HarnessError> {
        self.master
            .resize(size.to_portable())
            .map_err(HarnessError::Resize)?;
        self.size = size;
        Ok(())
    }

    /// A snapshot of everything drained so far, as **raw bytes**.
    ///
    /// Raw, never a `String` (BR-11). A TUI emits escape sequences that are not valid UTF-8,
    /// and decoding lossily at capture time would replace those bytes with U+FFFD — corrupting
    /// the very thing a rendering assertion is about. Callers may decode lossily **for
    /// display**; the capture path must not.
    ///
    /// The lock is held for one clone and across no blocking call, per INV-5. (#321)
    pub fn output(&self) -> Vec<u8> {
        match self.buffer.lock() {
            Ok(held) => held.clone(),
            // A poisoned lock means some reader panicked while holding it; the bytes are still
            // intact, so hand them over rather than compounding one panic with another.
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }

    /// A writer for the pty master: bytes written here arrive as the child's **terminal input**.
    ///
    /// Needed by the resize test, which drives an interactive shell rather than trapping
    /// `SIGWINCH` in a script — measured, a POSIX `sh` defers its `WINCH` trap until a
    /// foreground `sleep` finishes, so the script approach reports nothing.
    ///
    /// `portable_pty` allows this to be taken **once** per master and errors on a second call,
    /// which is surfaced rather than swallowed so a second caller sees the reason. (#321)
    ///
    /// # Errors
    ///
    /// [`HarnessError::PtyAlloc`] if the writer was already taken or cannot be cloned.
    pub fn write_handle(&self) -> Result<Box<dyn std::io::Write + Send>, HarnessError> {
        self.master.take_writer().map_err(HarnessError::PtyAlloc)
    }

    /// Polls the drained output until `predicate` accepts it, or `deadline` passes.
    ///
    /// Bounded, like every other wait here (INV-1): a test that needs to see a specific line
    /// must not be able to block forever waiting for it. Returns `Ok(snapshot)` on success and
    /// `Err(snapshot)` on the deadline, so a failing test can print what it *did* capture
    /// instead of just reporting that something was missing.
    ///
    /// The predicate runs on a **cloned snapshot**, never with the buffer lock held: calling
    /// arbitrary caller code while holding it would violate INV-5. (#321)
    pub fn wait_for_output<F>(&self, predicate: F, deadline: Duration) -> Result<Vec<u8>, Vec<u8>>
    where
        F: Fn(&[u8]) -> bool,
    {
        let until = Instant::now() + deadline;
        loop {
            let snapshot = self.output();
            if predicate(&snapshot) {
                return Ok(snapshot);
            }
            if Instant::now() >= until {
                return Err(snapshot);
            }
            std::thread::sleep(WAIT_POLL_INTERVAL);
        }
    }

    /// Waits at most `deadline` for the child, killing it if the deadline passes.
    ///
    /// The drain thread has been running since [`spawn`], so this **cannot** be the
    /// wait-before-drain deadlock (BR-2, BR-3).
    ///
    /// `try_wait` in a bounded loop rather than `child.wait()`: `wait()` is unbounded, and a
    /// `sleep 3600` child would hold it forever even with the drain running correctly. Every
    /// wait in this harness is bounded — that is INV-1, and it is the unit's whole purpose.
    ///
    /// # Panics
    ///
    /// If the drain thread panicked. `join()` returns `Result<_, Box<dyn Any + Send>>` whose
    /// `Err` arm means exactly that, and it is checked on **both** the exit and the timeout
    /// branch. INV-4 ("no panic in the drain path") is a goal, not a guarantee — a future
    /// refactor could introduce one — and swallowing `join()`'s `Err` would hide the real
    /// failure behind a confusing timeout, which is the pathology INV-4 exists to prevent.
    /// Panicking here is right: this is test infrastructure, and a panic in an assertion
    /// helper surfaces loudly in the test output. (#321)
    pub fn wait_with_timeout(&mut self, deadline: Duration) -> Outcome {
        let started = Instant::now();

        loop {
            match self.child.try_wait() {
                Ok(Some(status)) => {
                    self.finished = true;
                    self.settle_and_join("the child exited");
                    return Outcome::Exited(status);
                }
                Ok(None) => {}
                // The child is unwaitable. Treat it as gone rather than spinning to the
                // deadline: continuing would report a timeout for a child that is not running.
                Err(_) => {
                    self.finished = true;
                    self.settle_and_join("the child became unwaitable");
                    return Outcome::Timeout {
                        output_so_far: self.output(),
                    };
                }
            }

            if started.elapsed() >= deadline {
                // BR-6: kill AND report. Both halves, in that order.
                self.kill_hard();
                self.settle_and_join("the deadline passed");
                return Outcome::Timeout {
                    output_so_far: self.output(),
                };
            }

            std::thread::sleep(WAIT_POLL_INTERVAL);
        }
    }

    /// Lets the drain end on its own, then stops and joins it.
    ///
    /// See [`DRAIN_SETTLE_GRACE`] for why the grace exists: stopping the drain the instant the
    /// child is gone would race it and truncate `output_so_far`.
    ///
    /// `context` names the branch so a drain-thread panic reports *which* path surfaced it.
    fn settle_and_join(&mut self, context: &str) {
        let Some(handle) = self.reader.take() else {
            return;
        };

        // Bounded, and it never holds the buffer lock (INV-5).
        let settle_by = Instant::now() + DRAIN_SETTLE_GRACE;
        while !handle.is_finished() && Instant::now() < settle_by {
            std::thread::sleep(WAIT_POLL_INTERVAL);
        }
        self.stop.store(true, Ordering::Relaxed);

        // INV-3: joined on every path, so no drain thread leaks between tests.
        if let Err(panic) = handle.join() {
            let detail = panic_message(&panic);
            panic!(
                "the pty drain thread PANICKED (surfaced after {context}): {detail}. \
                 This is the real failure — it is reported rather than swallowed, because a \
                 swallowed drain panic shows up as a confusing timeout instead (INV-4)."
            );
        }
    }

    /// `SIGTERM`, then `SIGKILL` if the child ignores it. **"Kill" must actually kill** (BR-7).
    ///
    /// Signals the **process group**, not just the leader. `portable_pty` puts the child in its
    /// own session via `setsid`, so its pgid equals its pid (verified) and the group is exactly
    /// this child plus its descendants. Signalling the leader alone would leave a backgrounded
    /// grandchild running — measured: it survives, keeps the pty slave open, and so keeps the
    /// drain from ever seeing EOF. A kill that leaves a process running turns a bounded test
    /// into a leaked process, and leaked processes accumulate until they exhaust the runner.
    ///
    /// `ESRCH` is success, not failure: it means the target is already gone. (#321)
    fn kill_hard(&mut self) {
        let group = getpgid(Some(Pid::from_raw(self.pid as i32)))
            .unwrap_or_else(|_| Pid::from_raw(self.pid as i32));

        let _ = self.signal_group(group, Signal::SIGTERM);

        for _ in 0..TERM_GRACE_STEPS {
            std::thread::sleep(TERM_GRACE_STEP);
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                self.finished = true;
                return;
            }
        }

        // Ignored SIGTERM. Escalate — SIGKILL cannot be trapped.
        let _ = self.signal_group(group, Signal::SIGKILL);

        // Reap, but bounded like everything else here: a `wait()` after SIGKILL should return
        // at once, and "should" is not a bound. Reaping matters because an unreaped zombie is
        // still addressable by `kill(pid, 0)`, which would make the VR-4 absence assertion
        // pass or fail on timing rather than on the kill. (#321)
        let reap_by = Instant::now() + DRAIN_SETTLE_GRACE;
        while Instant::now() < reap_by {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                break;
            }
            std::thread::sleep(WAIT_POLL_INTERVAL);
        }
        self.finished = true;
    }

    /// Signals a process group, treating "already gone" as success.
    fn signal_group(&self, group: Pid, signal: Signal) -> Result<(), HarnessError> {
        match killpg(group, signal) {
            Ok(()) | Err(Errno::ESRCH) => Ok(()),
            Err(e) => Err(HarnessError::Kill(e)),
        }
    }
}

impl Drop for PtySession {
    /// Kills and joins if the session was never consumed by a bounded wait.
    ///
    /// This is the panicking-test path, and it is why `reader` is an `Option`. A failed
    /// assertion unwinds past [`PtySession::wait_with_timeout`], so without this the child
    /// (a `sleep 3600`, say) outlives the suite and the drain thread outlives the session.
    ///
    /// **Nothing here panics.** A panic during an unwind aborts the process, which would
    /// replace a legible assertion failure with an abort and no diagnosis — so a drain-thread
    /// panic is reported to stderr here instead of re-panicked. `wait_with_timeout` is the
    /// path that panics on it. (#321)
    fn drop(&mut self) {
        if !self.finished {
            self.kill_hard();
        }

        if let Some(handle) = self.reader.take() {
            self.stop.store(true, Ordering::Relaxed);
            if let Err(panic) = handle.join() {
                eprintln!(
                    "warning: the pty drain thread panicked and was observed during teardown \
                     (not re-panicked, because panicking while unwinding aborts): {}",
                    panic_message(&panic)
                );
            }
        }
    }
}

/// True when no process holds `pid` — `kill(pid, 0)` reporting `ESRCH`.
///
/// The assertion VR-4 requires: `kill()` returning `Ok` proves only that the call was made,
/// whereas absence proves it worked. Note this needs the child **reaped** — an unreaped zombie
/// is still addressable — which [`PtySession::kill_hard`] guarantees before returning. (#321)
pub fn process_is_gone(pid: u32) -> bool {
    matches!(kill(Pid::from_raw(pid as i32), None), Err(Errno::ESRCH))
}

/// Best-effort text for a panic payload, so a drain-thread panic reports its message rather
/// than an opaque `Any`.
fn panic_message(payload: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "<non-string panic payload>".to_string()
    }
}
