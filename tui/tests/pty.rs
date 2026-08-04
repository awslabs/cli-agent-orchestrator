//! The real-terminal tests of the `skeleton-pty-harness` unit (issue #321).
//!
//! Every one has a real failure mode. Between them they close the gap the predecessor Python
//! TUI shipped with: **304 tests, 39 mutation proofs, 90.66% coverage, and not one real pty**
//! — the only three `openpty` references across its 273 test files are mocks, two of which
//! substitute fabricated file descriptors (`test/api/test_terminals.py:719`, `:761`).
//!
//! Test 1 is the one that suite structurally could not write.
//!
//! Seven tests, not the six the plan lists. **Test 3b was added because mutation-testing this
//! harness found a gap:** deleting the `SIGKILL` escalation from `kill_hard` left all six
//! original tests green, because test 3's `sleep 3600` honours `SIGTERM` and so never reaches
//! the escalation branch. BR-7 — "'kill' must actually kill" — was asserted by nothing. The
//! discovery is the argument for mutation-testing a guard rather than trusting that a passing
//! test covers it.
//!
//! # Not marked, gated, or excluded — deliberately
//!
//! VR-6: the Python suite's pty-adjacent tests live under `test/e2e/` or carry
//! `@pytest.mark.e2e`, and **every** pytest invocation in all 8 CI workflows applies both
//! `--ignore=test/e2e` and `-m "not e2e"`, so they never execute. That is accepted debt, not a
//! pattern to replicate: **a pty test that never runs is indistinguishable from the gap this
//! unit closes.** These run under a plain `cargo test`, with no feature gate and no `#[ignore]`.
//!
//! # Serial execution
//!
//! `cargo test` runs test functions in parallel threads by default, and `team.md` records that
//! serial execution is load-bearing for the Python pty tests. These are written to be
//! independent — each owns its own pty, drain thread, and process group — and pass under the
//! default parallelism (measured). Should they ever prove flaky, BR-15's remedy is to
//! serialise them (`--test-threads=1` for this target), **not** to reduce their scope. (#321)

mod pty_harness;

use std::io::Write;
use std::time::{Duration, Instant};

use pty_harness::{process_is_gone, spawn, Outcome, PtySize};

/// `/bin/sh` rather than `sh`: an absolute path so the probe does not depend on the `PATH` the
/// test runner happens to inherit. POSIX guarantees it on both macOS and Linux.
const SH: &str = "/bin/sh";

/// Decodes lossily **for a failure message only**. The capture path keeps raw bytes (BR-11);
/// this exists so a failure prints something legible instead of a byte array.
fn for_display(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).replace('\u{1b}', "<ESC>")
}

/// Test 1 — **`isatty` holds INSIDE the pty.**
///
/// The assertion a pipe structurally cannot satisfy, and the entire reason this unit exists.
///
/// Two things make it non-vacuous:
///
/// * **It runs inside the pty.** `test -t 0` is evaluated by the *child*, so it reports what
///   the child sees. A parent-side `isatty` check would prove nothing about the child — and
///   the child's view is what a TUI's contract is with.
/// * **It carries its own control.** The same `sh -c 'test -t 0'` is run through a pipe and
///   asserted to report the opposite. Without the control this test could pass against a
///   harness that had quietly degraded to pipes, since it would then only be asserting "some
///   process ran and exited 0". Measured: through a pipe the exit code is 1, and through
///   `Stdio::null()` also 1.
///
/// The mutation that reddens it is switching the harness to pipes; no in-crate edit fakes it.
/// (#321)
#[test]
fn isatty_holds_inside_the_pty_but_not_through_a_pipe() {
    let mut session = spawn(&[SH, "-c", "test -t 0"], PtySize::default())
        .expect("the environment must provide a pty");

    let outcome = session.wait_with_timeout(Duration::from_secs(10));

    match outcome {
        Outcome::Exited(status) => assert_eq!(
            status.exit_code(),
            0,
            "`test -t 0` must exit 0 INSIDE the pty: the child's stdin has to be a real \
             terminal, which is the one property a pipe cannot provide. Captured: {}",
            for_display(&session.output())
        ),
        Outcome::Timeout { output_so_far } => panic!(
            "`test -t 0` must exit promptly, not time out. Captured: {}",
            for_display(&output_so_far)
        ),
    }

    // The control. If this ever also exits 0, the assertion above has stopped distinguishing a
    // pty from a pipe and the whole test is worthless.
    let piped = std::process::Command::new(SH)
        .args(["-c", "test -t 0"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .expect("spawning the pipe control must succeed")
        .wait_with_output()
        .expect("the pipe control must be waitable");

    assert_eq!(
        piped.status.code(),
        Some(1),
        "the pipe control must exit NON-zero: if a piped `test -t 0` also passed, the pty \
         assertion above would prove nothing"
    );
}

/// Test 2 — **terminal geometry is 80x24 by default.**
///
/// `stty size` prints `rows cols`, so the expected text is `24 80`. Asserted as the child's own
/// output rather than via `kernel_size()`: the point is that the *child* sees the geometry, and
/// `stty` reads it from the kernel through the pty exactly as a TUI would.
///
/// The default is the **NFR-6 minimum**, so this pins the boundary the requirement names.
/// Changing `PtySize::default()` reddens it. (#321)
#[test]
fn default_geometry_is_eighty_by_twentyfour_as_the_child_sees_it() {
    let mut session =
        spawn(&[SH, "-c", "stty size"], PtySize::default()).expect("pty must be available");

    let outcome = session.wait_with_timeout(Duration::from_secs(10));
    assert!(
        matches!(outcome, Outcome::Exited(_)),
        "`stty size` must exit on its own, not time out"
    );

    let captured = session.output();
    let text = String::from_utf8_lossy(&captured);

    assert_eq!(
        text.trim(),
        "24 80",
        "`stty size` prints `rows cols`, so the 80x24 default must appear as `24 80` — the \
         NFR-6 minimum, which is untestable without a real pty. Captured: {}",
        for_display(&captured)
    );
}

/// NFR-6's real-terminal proof: the production binary paints and remains operable at 70x20.
///
/// The renderer unit test checks layout arithmetic in a `Buffer`; this test closes the distinct
/// integration risk that raw mode, alternate-screen setup, or the real backend produces a blank
/// terminal. Seeing both the shell title and results label proves two separate regions painted,
/// and sending `q` proves the stacked shell remains keyboard-reachable. (#321)
#[test]
fn cao_tui_paints_and_quits_in_a_real_seventy_by_twenty_pty() {
    let binary = env!("CARGO_BIN_EXE_cao-tui");
    let mut session = spawn(&[binary], PtySize { rows: 20, cols: 70 })
        .expect("the production TUI must start in a real 70x20 pty");

    let painted = session
        .wait_for_output(
            |output| {
                let text = String::from_utf8_lossy(output);
                text.contains("cao-tui") && text.contains("results")
            },
            Duration::from_secs(10),
        )
        .unwrap_or_else(|output| {
            panic!(
                "the 70x20 TUI must paint both its shell and results region; captured: {}",
                for_display(&output)
            )
        });
    assert!(
        !painted.is_empty(),
        "a blank pty is indistinguishable from a hung TUI"
    );

    session
        .write_handle()
        .expect("the pty must expose keyboard input")
        .write_all(b"q")
        .expect("the quit key must reach the TUI");

    match session.wait_with_timeout(Duration::from_secs(10)) {
        Outcome::Exited(status) => assert_eq!(
            status.exit_code(),
            0,
            "the TUI must exit cleanly after `q`; captured: {}",
            for_display(&session.output())
        ),
        Outcome::Timeout { output_so_far } => panic!(
            "the TUI painted at 70x20 but did not respond to `q`; captured: {}",
            for_display(&output_so_far)
        ),
    }
}

/// Test 3 — **THE ACCEPTANCE PROOF. A deliberately hung child is killed AND reported inside
/// the deadline.**
///
/// `team.md`'s affirmed walking-skeleton acceptance criterion, and the only test that proves
/// the harness cannot hang the suite (BR-8, INV-1).
///
/// Four assertions, and each one is load-bearing:
///
/// 1. **`Outcome::Timeout`** — the bound fired rather than the child exiting.
/// 2. **The child is GONE** — `kill(pid, 0)` reports `ESRCH`. `kill()` returning `Ok` proves
///    only that the call was made; absence proves it worked (VR-4, BR-7).
/// 3. **`output_so_far` is available** — possibly empty. BR-6 requires kill *and* report, and
///    killing without reporting leaves a diagnosis-free failure.
/// 4. **Total elapsed is bounded** — VR-1. *A test asserting only `Timeout` would pass even if
///    the harness took ten minutes to notice*, and "eventually notices" is not the property
///    under test. The ceiling is 5s against a 2s deadline: the excess covers the SIGTERM grace
///    (500 ms) plus the drain settle (500 ms) with room to spare, and is still far below any
///    plausible hang. Measured on this machine: ~2.1s for the full timeout path.
///
/// (#321)
#[test]
fn a_hung_child_is_killed_and_reported_within_the_deadline() {
    let deadline = Duration::from_secs(2);
    let ceiling = Duration::from_secs(5);

    // Never exits on its own. If the harness has no bound, this hangs the suite — which is
    // exactly what this test exists to make impossible.
    let mut session =
        spawn(&[SH, "-c", "sleep 3600"], PtySize::default()).expect("pty must be available");
    let pid = session.pid();

    let started = Instant::now();
    let outcome = session.wait_with_timeout(deadline);
    let elapsed = started.elapsed();

    // (a) the bound fired.
    let output_so_far = match outcome {
        Outcome::Timeout { output_so_far } => output_so_far,
        Outcome::Exited(status) => panic!(
            "`sleep 3600` cannot exit within {deadline:?}; got Exited({}). Either the child \
             was not what we think it was, or the deadline was not enforced",
            status.exit_code()
        ),
    };

    // (b) the child is GONE — not merely that kill() returned Ok.
    assert!(
        process_is_gone(pid),
        "the hung child (pid {pid}) must be GONE after the timeout, not merely signalled: \
         `kill()` returning Ok proves the call was made, absence proves it worked (VR-4). A \
         kill that leaves the process running turns a bounded test into a leaked process"
    );

    // (c) the output is reported. `sleep` prints nothing, so empty is the expected value —
    // what matters is that the timeout variant HANDS IT OVER rather than discarding it, which
    // is what makes BR-6's report half unavoidable.
    assert!(
        output_so_far.is_empty(),
        "`sleep 3600` writes nothing, so the reported output must be empty — but it must be \
         *reported*, because killing without reporting leaves a diagnosis-free failure. \
         Got: {}",
        for_display(&output_so_far)
    );

    // (d) the harness bounded ITSELF. Without this the test passes on a ten-minute timeout.
    assert!(
        elapsed >= deadline,
        "the wait must actually observe its {deadline:?} deadline, not return early: \
         returning in {elapsed:?} would mean the deadline was not what bounded it"
    );
    assert!(
        elapsed < ceiling,
        "the harness must notice and clean up within {ceiling:?}; took {elapsed:?}. This is \
         the property under test (VR-1): a test asserting only Timeout would pass even if the \
         harness took ten minutes"
    );
}

/// Test 3b — **a child that IGNORES `SIGTERM` is still killed.** BR-7's escalation.
///
/// Not in the plan's list of six; added because mutation-testing the harness found the gap.
/// Deleting the `SIGKILL` escalation from `kill_hard` left **all six original tests green**:
/// `sleep 3600` honours `SIGTERM`, so test 3 never reaches the escalation branch and cannot
/// see it removed. BR-7 ("'kill' must actually kill") and the affirmed acceptance proof would
/// have been asserted by nothing.
///
/// `trap '' TERM` makes the shell ignore `SIGTERM` outright. The trailing `:` matters and is
/// not decoration: without a following command, `sh` optimises `sleep` into an `exec` and the
/// trap is lost with the shell that held it — the child would then honour `SIGTERM` after all
/// and this test would silently stop testing escalation. Verified: with the `:` the escalation
/// branch is reached (measured 512 ms to kill), without it `SIGTERM` suffices (102 ms).
///
/// `SIGKILL` cannot be trapped, which is why escalation is the guarantee rather than a
/// courtesy. (#321)
#[test]
fn a_child_that_ignores_sigterm_is_still_killed() {
    let deadline = Duration::from_secs(1);
    // The escalation costs 500 ms of grace on top of the deadline; 5s leaves ample headroom
    // while still failing fast if the escalation never happens.
    let ceiling = Duration::from_secs(5);

    let mut session = spawn(
        &[SH, "-c", "trap '' TERM; sleep 3600; :"],
        PtySize::default(),
    )
    .expect("pty must be available");
    let pid = session.pid();

    let started = Instant::now();
    let outcome = session.wait_with_timeout(deadline);
    let elapsed = started.elapsed();

    assert!(
        matches!(outcome, Outcome::Timeout { .. }),
        "a child that ignores SIGTERM and sleeps 3600 must time out, not exit"
    );
    assert!(
        process_is_gone(pid),
        "the SIGTERM-ignoring child (pid {pid}) must STILL be gone: SIGTERM alone is not a \
         kill, and BR-7 requires escalation to SIGKILL, which cannot be trapped. A polite \
         signal the child ignores turns a bounded test into a leaked process"
    );
    assert!(
        elapsed < ceiling,
        "escalation must complete within {ceiling:?}; took {elapsed:?}"
    );
}

/// Test 4 — **large output does not deadlock.** The T-7 regression guard.
///
/// `yes hello | head -20000` is the exact command that *proved* the deadlock: with no
/// concurrent drain, the child fills the pty buffer and blocks writing while the parent blocks
/// in `wait()`, and neither proceeds (BR-2). Re-confirmed while building this harness — a probe
/// with the drain thread removed hung until an external 20s timeout killed it.
///
/// The volume assertion is what gives the test teeth. Asserting only `Exited` would pass
/// against a harness that captured nothing at all, and a harness that captures nothing cannot
/// deadlock — so it would be green for the wrong reason. 20,000 lines of `hello\r\n` is 140,000
/// bytes (measured exactly); the floor is set at 100,000 to tolerate a partial tail without
/// tolerating a harness that dropped most of the stream. (#321)
#[test]
fn large_output_does_not_deadlock_and_is_captured() {
    let lines = 20_000_usize;
    // A generous ceiling: this completes in ~110ms on this machine. It is a hang detector, not
    // a performance budget.
    let deadline = Duration::from_secs(30);
    let floor = 100_000_usize;

    let mut session = spawn(
        &[SH, "-c", &format!("yes hello | head -{lines}")],
        PtySize::default(),
    )
    .expect("pty must be available");

    let started = Instant::now();
    let outcome = session.wait_with_timeout(deadline);
    let elapsed = started.elapsed();

    match outcome {
        Outcome::Exited(status) => assert_eq!(
            status.exit_code(),
            0,
            "the pipeline must exit cleanly; captured {} bytes",
            session.output().len()
        ),
        Outcome::Timeout { output_so_far } => panic!(
            "T-7 REGRESSION: `yes hello | head -{lines}` timed out after {elapsed:?} having \
             captured {} bytes. This is the deadlock signature — the drain is not running \
             concurrently with the child, so the child blocked writing into a full pty buffer \
             while the parent waited (BR-2, BR-3)",
            output_so_far.len()
        ),
    }

    let captured = session.output();
    assert!(
        captured.len() >= floor,
        "must capture at least {floor} bytes of the {lines}-line stream, got {}. Asserting \
         only that the child exited would pass against a harness that captured nothing — and \
         a harness that captures nothing cannot deadlock, so it would be green for the wrong \
         reason",
        captured.len()
    );
    assert!(
        captured.windows(5).any(|w| w == b"hello"),
        "the captured bytes must actually be the child's output"
    );
}

/// Test 5 — **raw bytes survive capture verbatim.**
///
/// A TUI emits escape sequences that are **not valid UTF-8**, and lossy decoding at capture
/// time would replace those bytes with U+FFFD — corrupting the very thing a rendering assertion
/// is about (BR-11).
///
/// The payload is chosen so a lossy path cannot pass:
///
/// * `ESC[31m` / `ESC[0m` — real SGR sequences, asserted byte-exact.
/// * `0xFF` and `0xFE` — **never legal in UTF-8 in any position.** They are what makes the test
///   non-vacuous: `String::from_utf8_lossy` replaces each with U+FFFD, so a harness that
///   decoded on capture would lose them and go red. Verified: the captured bytes are invalid
///   UTF-8 and lossy decoding changes them.
///
/// A test using only ASCII escape sequences would pass through a lossy capture path unharmed
/// and prove nothing. (#321)
#[test]
fn raw_escape_and_non_utf8_bytes_survive_capture_verbatim() {
    // printf octal: \033 = ESC, \377 = 0xFF, \376 = 0xFE.
    let mut session = spawn(
        &[SH, "-c", r"printf '\033[31mRED\033[0m\377\376'"],
        PtySize::default(),
    )
    .expect("pty must be available");

    let outcome = session.wait_with_timeout(Duration::from_secs(10));
    assert!(
        matches!(outcome, Outcome::Exited(_)),
        "printf must exit on its own"
    );

    let captured = session.output();

    assert!(
        captured.windows(8).any(|w| w == b"\x1b[31mRED"),
        "the SGR sequence must survive byte-exact, ESC included. Captured: {captured:?}"
    );
    assert!(
        captured.windows(4).any(|w| w == b"\x1b[0m"),
        "the SGR reset must survive byte-exact. Captured: {captured:?}"
    );
    assert!(
        captured.contains(&0xFF) && captured.contains(&0xFE),
        "0xFF and 0xFE must survive verbatim. Neither byte is legal anywhere in UTF-8, so \
         their presence is what proves the capture path is raw rather than decoded. \
         Captured: {captured:?}"
    );

    // The guard on the guard: if the payload were accidentally valid UTF-8, the assertion
    // above would pass through a lossy capture path too and prove nothing.
    assert!(
        std::str::from_utf8(&captured).is_err(),
        "the captured bytes must NOT be valid UTF-8, or this test cannot distinguish a raw \
         capture from a lossy one. Captured: {captured:?}"
    );
    assert_ne!(
        String::from_utf8_lossy(&captured).as_bytes(),
        captured.as_slice(),
        "lossy decoding must demonstrably CHANGE these bytes — that is the corruption BR-11 \
         forbids at capture time"
    );
}

/// Test 6 — **`resize()` reaches the child.**
///
/// BR-14. Without a triggerable resize, NFR-6's sub-80x24 stacked-collapse path has no test.
///
/// Both halves are asserted, and the child-side half is the one that matters:
///
/// * **The child's view changes.** An interactive `sh` is driven over the pty master and asked
///   `stty size` before and after. `stty` reads the geometry from the kernel through the pty, so
///   this is the child observing the new window — the same way a TUI learns it was resized.
/// * **The kernel agrees**, via `kernel_size()`, which is a separate reading from the harness's
///   own `size()` field. A test consulting only `size()` would pass even if `TIOCSWINSZ` did
///   nothing at all, since that field is just a record of what was requested.
///
/// Driving a shell over the master rather than trapping `WINCH` in a script is deliberate:
/// measured, a POSIX `sh` running a foreground `sleep` defers its trap until the sleep
/// finishes, so a naive `trap 'stty size' WINCH; sleep N` script reports nothing and the test
/// would fail for a reason that has nothing to do with resize. (#321)
#[test]
fn resize_is_visible_to_the_child_and_to_the_kernel() {
    let before = PtySize::default();
    let after = PtySize {
        rows: 40,
        cols: 120,
    };

    let mut session = spawn(&[SH], before).expect("pty must be available");
    assert_eq!(
        session.kernel_size().expect("kernel must report a size"),
        before,
        "the pty must start at the requested geometry"
    );
    assert_eq!(
        session.size(),
        before,
        "the harness's own record must start out agreeing with the kernel"
    );

    let mut writer = session
        .write_handle()
        .expect("the master must yield a writer");

    writeln!(writer, "stty size").expect("writing to the pty master must succeed");
    writer.flush().expect("flush must succeed");
    let first = session
        .wait_for_output(
            |bytes| count_geometry(bytes, "24 80") >= 1,
            Duration::from_secs(10),
        )
        .unwrap_or_else(|captured| {
            panic!(
                "the child must report the initial geometry `24 80`. Captured: {}",
                for_display(&captured)
            )
        });
    assert!(
        !first.is_empty(),
        "the initial `stty size` must produce output"
    );

    session.resize(after).expect("resize must succeed");
    assert_eq!(
        session.kernel_size().expect("kernel must report a size"),
        after,
        "the KERNEL must report the new geometry — asserting only the harness's own `size()` \
         field would pass even if TIOCSWINSZ did nothing"
    );
    assert_eq!(
        session.size(),
        after,
        "the harness's record must track the resize too, so `size()` cannot drift from the \
         kernel's view"
    );

    writeln!(writer, "stty size").expect("writing to the pty master must succeed");
    writer.flush().expect("flush must succeed");
    let second = session
        .wait_for_output(
            |bytes| count_geometry(bytes, "40 120") >= 1,
            Duration::from_secs(10),
        )
        .unwrap_or_else(|captured| {
            panic!(
                "the CHILD must observe the new geometry `40 120` after resize: the kernel \
                 agreeing is not enough, since a TUI learns its size the way `stty` does. \
                 Captured: {}",
                for_display(&captured)
            )
        });

    // Geometry must have genuinely CHANGED, not merely been reported once. Without this a
    // harness whose resize was a no-op could pass by echoing the old size twice.
    assert_eq!(
        count_geometry(&second, "40 120"),
        1,
        "the new geometry must appear exactly once. Captured: {}",
        for_display(&second)
    );
    assert!(
        count_geometry(&second, "24 80") >= 1,
        "the pre-resize geometry must still be in the transcript, so the test is comparing a \
         real before and after. Captured: {}",
        for_display(&second)
    );

    writeln!(writer, "exit").expect("writing to the pty master must succeed");
    writer.flush().expect("flush must succeed");
    drop(writer);
    let _ = session.wait_with_timeout(Duration::from_secs(10));
}

/// Counts occurrences of a `stty size` reading in a pty transcript.
///
/// An interactive shell echoes the command it was sent, so the literal `stty size` text appears
/// in the transcript too — the *reading* is matched on its own line, delimited by the `\r\n` a
/// pty in cooked mode emits. Counting rather than testing containment is what lets test 6
/// distinguish "reported twice" from "changed once".
fn count_geometry(bytes: &[u8], reading: &str) -> usize {
    String::from_utf8_lossy(bytes)
        .split("\r\n")
        .filter(|line| is_geometry_reading(line, reading))
        .count()
}

/// True when `line` carries exactly `reading`, ignoring any shell prompt printed ahead of it.
///
/// **The prompt tolerance is the Linux fix, and this is the one place the two platforms differ.**
/// An exact `line.trim() == reading` comparison passed on macOS and failed on Ubuntu: `/bin/sh`
/// there is `dash`, which writes its `$ ` prompt and the command's output onto the SAME pty line,
/// so the transcript reads `$ 24 80` rather than `24 80`. The reading was present and correct and
/// the assertion still rejected it — a test failing on the shell's prompt style rather than on
/// terminal geometry, which is exactly the "green here, red there" class this suite exists to
/// catch. (#321)
///
/// Deliberately NOT `line.contains(reading)`. A substring match would make the count meaningless
/// in the direction test 6 depends on: with a bare `contains`, a reading of `40 120` is also
/// satisfied by `140 120`, and the "appears exactly once" assertion — the half that proves the
/// geometry genuinely CHANGED rather than being echoed twice — would be checking a weaker
/// property than it claims. So the reading must be the line's suffix, and whatever precedes it
/// may only be a prompt.
fn is_geometry_reading(line: &str, reading: &str) -> bool {
    let Some(prefix) = line.trim().strip_suffix(reading) else {
        return false;
    };
    // Every prompt sigil a POSIX shell may print here: `$` for a user shell (dash, bash's
    // `sh-3.2$`), `%` for zsh, `#` for root — which is the norm in a container. An empty prefix
    // is the macOS case, where the reading already lands on its own line.
    let prefix = prefix.trim_end();
    prefix.is_empty() || prefix.ends_with('$') || prefix.ends_with('%') || prefix.ends_with('#')
}

/// Test 9 — **the reading parser tolerates a prompt, and still counts.**
///
/// A regression test for the Linux-only failure above, asserted against the transcript CI
/// actually captured. It runs on every platform, so the Darwin development machine now reddens
/// for a defect that previously could only be discovered on Ubuntu — the whole reason the pty
/// suite is a two-platform matrix.
///
/// Both directions are asserted, because only fixing the first would have traded one silent
/// failure for another: the prompt must be tolerated, AND the tolerance must not decay into a
/// substring match. (#321)
#[test]
fn a_geometry_reading_is_found_behind_a_shell_prompt_but_is_not_a_substring_match() {
    // Verbatim from the failing Ubuntu run: dash's prompt shares the line with the output.
    let dash = b"stty size\r\n$ 24 80\r\n$ ";
    assert_eq!(
        count_geometry(dash, "24 80"),
        1,
        "dash prints `$ 24 80`; the reading is present and must be found"
    );

    // The macOS shape, where the reading already occupies its own line.
    assert_eq!(count_geometry(b"stty size\r\n24 80\r\n", "24 80"), 1);

    // The echoed command must never be mistaken for a reading.
    assert_eq!(count_geometry(b"stty size\r\n", "24 80"), 0);

    // NOT a substring match: `140 120` is not a reading of `40 120`. Without this the "appears
    // exactly once" assertion in test 6 would be weaker than it claims to be.
    assert_eq!(
        count_geometry(b"140 120\r\n", "40 120"),
        0,
        "a longer number ending in the reading must not count as the reading"
    );

    // Counting still discriminates a change from a repeat, which is test 6's load-bearing use.
    assert_eq!(count_geometry(b"$ 24 80\r\n$ 40 120\r\n", "40 120"), 1);
    assert_eq!(count_geometry(b"$ 24 80\r\n$ 24 80\r\n", "24 80"), 2);
}
