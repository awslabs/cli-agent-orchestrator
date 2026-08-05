//! Test (c) of the `skeleton-crate` unit: the binary actually runs and exits 0.
//!
//! This lives in `tests/` rather than beside `main.rs` because `CARGO_BIN_EXE_<name>` is
//! only set for integration test targets — a unit test inside the binary crate has no
//! supported way to locate the built executable, and reconstructing a path under
//! `target/` by hand would break under `--release` and cross-compilation. (#321)

use std::process::Command;

#[test]
fn binary_runs_and_exits_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_cao-tui"))
        .output()
        .expect("failed to spawn the cao-tui binary that cargo just built");

    assert!(
        output.status.success(),
        "cao-tui exited with {:?}; stderr: {}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        output.status.code(),
        Some(0),
        "cao-tui must exit 0, not merely avoid a signal"
    );
}

/// **`--help` prints help and exits 0 without opening the TUI.**
///
/// `cao tui` forwards its argv verbatim (`cli/commands/tui.py:87-109` sets
/// `ignore_unknown_options` and `allow_extra_args` for exactly that purpose), and the binary read
/// **no argv at all** — so `cao tui --help` painted a TUI frame and exited 0. Verified against the
/// built binary before the fix.
///
/// Asserted on real process output, which is the only place this is observable: the defect was in
/// `main`'s argv handling, and a unit test inside the crate cannot exercise `std::env::args`.
/// (Reported by review on PR #547.)
#[test]
fn help_and_version_print_and_exit_zero_without_starting_the_tui() {
    for flag in ["--help", "-h"] {
        let output = Command::new(env!("CARGO_BIN_EXE_cao-tui"))
            .arg(flag)
            .output()
            .expect("failed to spawn cao-tui");
        let stdout = String::from_utf8_lossy(&output.stdout);

        assert_eq!(output.status.code(), Some(0), "{flag} must exit 0");
        assert!(
            stdout.contains("USAGE"),
            "{flag} must print usage. Got: {stdout:?}"
        );
        assert!(
            stdout.contains("CAO_API_HOST"),
            "{flag} should name the env vars that redirect the client, since that is the only \
             configuration the TUI takes. Got: {stdout:?}"
        );
        // The negative half: the TUI's own frame must NOT appear. `server:` is the header's
        // standing indicator line, which is what `--help` used to print instead of help.
        assert!(
            !stdout.contains("server:"),
            "{flag} must not paint the TUI frame — printing the header instead of help is the \
             defect. Got: {stdout:?}"
        );
    }

    for flag in ["--version", "-V"] {
        let output = Command::new(env!("CARGO_BIN_EXE_cao-tui"))
            .arg(flag)
            .output()
            .expect("failed to spawn cao-tui");
        let stdout = String::from_utf8_lossy(&output.stdout);

        assert_eq!(output.status.code(), Some(0), "{flag} must exit 0");
        assert!(
            stdout.starts_with("cao-tui "),
            "{flag} must print the binary name and version. Got: {stdout:?}"
        );
        assert!(
            !stdout.contains("server:"),
            "{flag} must not paint the TUI frame. Got: {stdout:?}"
        );
    }
}

/// **An unrecognised argument is refused by name, non-zero, without opening the TUI.**
///
/// `--agents` is the adversarial choice: the launch form has a field of that name, so
/// `cao tui --agents planner` looks like it ought to work. Accepting it silently — which is what
/// reading no argv amounts to — teaches the operator that the flag was honoured.
///
/// One line, to **stderr**, non-zero: the Python CLI's error-boundary contract, which this binary
/// inherits. (Reported by review on PR #547.)
#[test]
fn an_unrecognised_argument_is_refused_by_name_and_exits_non_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_cao-tui"))
        .args(["--agents", "planner"])
        .output()
        .expect("failed to spawn cao-tui");

    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);

    assert_ne!(
        output.status.code(),
        Some(0),
        "an argument the binary does not accept must be an ERROR — exiting 0 is how it silently \
         looked honoured. stderr: {stderr:?}"
    );
    assert!(
        stderr.contains("--agents"),
        "the refusal must NAME the rejected argument, or the operator cannot tell which one was \
         wrong. stderr: {stderr:?}"
    );
    assert!(
        stderr.contains("--help"),
        "it should point at `--help`, which is the actionable next step. stderr: {stderr:?}"
    );
    assert!(
        !stdout.contains("server:"),
        "the TUI frame must not be painted for a rejected argument. stdout: {stdout:?}"
    );
}
