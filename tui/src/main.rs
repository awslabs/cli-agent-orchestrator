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
/// The wire vocabulary six later units share. Declared here so every consumer imports the
/// types from one place rather than redeclaring the server's shapes locally. (#321)
mod types;

use error::TuiError;

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
fn main() -> Result<(), TuiError> {
    let (cols, rows) = crossterm::terminal::size().unwrap_or((80, 24));
    let interactive = io::stdout().is_terminal();

    let server = Arc::new(server::ServerClient::from_env());
    let host = handoff::RealHost::new();
    let mut shell = renderer::Renderer::new(server.as_ref(), &host, cols, rows)
        .with_concurrent_pickers(Arc::clone(&server));

    // A `Fatal` here exits non-zero with one styled line — never a traceback (SR-1). Mapped into
    // `TuiError` because `main`'s signature is the boundary contract, and `Fatal`'s own `Display`
    // already carries the whole operator-facing sentence.
    shell
        .run()
        .map_err(|fatal| TuiError::Unreachable(fatal.to_string()))?;

    // Pipes cannot host an interactive TUI. `Renderer::run` deliberately returns after one tick
    // in that case, and this textual frame keeps `cao-tui | ...` populated rather than hanging or
    // emitting alternate-screen control sequences. (#321)
    if !interactive {
        let frame = shell.render();
        let mut out = io::stdout().lock();
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
}
