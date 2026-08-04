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
