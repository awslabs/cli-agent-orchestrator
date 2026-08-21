//! The one crate-root error type for `cao-tui`.
//!
//! Affirmed practice: `thiserror` for crate-internal error types, `anyhow` only at
//! integration boundaries, and a single crate-root error type from commit one — later
//! units add variants here rather than minting their own top-level error types, which is
//! how the Python side ended up with `ProviderError` defined independently in six
//! modules. See issue #321.

use thiserror::Error;

/// Every failure `cao-tui` can report to the operator.
///
/// Operator-facing errors render as one styled line with a non-zero exit, never a
/// backtrace — the same boundary contract the Python CLI holds via `click.ClickException`.
/// `Debug` is required because `main` returns `Result<_, TuiError>` and `Termination`
/// formats the error with `Debug`. (#321)
///
/// # The six `server-client` variants, plus `Io` from the skeleton
///
/// INV-1 of `server-client` names **six** error variants — `Unreachable`, `Http`, `Decode`,
/// `Validation`, `NotFound`, `NoRoute` — and all six are below. [`TuiError::Io`] predates them
/// (it is `skeleton-crate`'s, and `main` still returns it for stdout failures), so the type
/// carries seven; the count in INV-1 is of this unit's boundary contract, not of the enum.
/// Extending this type rather than minting a second one is the affirmed practice, and the reason
/// is on the Python side: `ProviderError` is defined independently in six provider modules there.
///
/// **This enum IS the boundary contract**, not merely diagnostics: `renderer` matches on it to
/// choose a rendered state, so the variants are a UI-facing vocabulary. That is why
/// [`TuiError::Unreachable`] stays distinct from [`TuiError::Http`] — they call for different
/// remedies (start the server vs. read the status) — and why
/// [`TuiError::NotFound`] is not folded into `Http(404)`, which `handoff.rs`'s readiness poll
/// depends on. (#321)
#[derive(Debug, Error)]
pub enum TuiError {
    /// Terminal or file I/O failed. Writing to stdout is the skeleton's only I/O, but
    /// this variant is the landing spot for the pty and config-file units too.
    #[error("i/o error: {0}")]
    Io(#[from] std::io::Error),

    /// `cao-server` answered, but with an HTTP error status.
    ///
    /// Kept distinct from [`TuiError::Unreachable`] because `skeleton-handoff-proof`'s
    /// readiness poll treats the two differently and the distinction is the whole of BR-12:
    /// **only an explicit 5xx is conclusive**, so a 5xx stops the poll while any other read
    /// failure keeps it going. Collapsing the two variants would turn a recoverable blip into
    /// a launch failure. (#321)
    ///
    /// `allow(dead_code)` because this variant is **matched** in `handoff.rs` but only
    /// *constructed* by whoever performs HTTP — `server-client` (Bolt 3) and, today, this
    /// crate's tests. Rust's `dead_code` counts construction, not matching, so the bin cfg
    /// warns until Bolt 3 lands. `allow` and not `expect` for the reason recorded in
    /// `types.rs`: under `--all-targets` the test cfg *does* construct it, so an `expect`
    /// would be unfulfilled there and `-D warnings` would fail the gate. (#321)
    #[allow(dead_code)]
    #[error("cao-server returned HTTP {0}")]
    Http(u16),

    /// `cao-server` could not be read, **or** it reported a `terminal_backend` this build
    /// does not recognise.
    ///
    /// The two causes share one variant because `domain-entities.md` specifies exactly one
    /// meaningful failure for the hand-off driver: it cannot determine the backend. The
    /// carried string is what distinguishes them for the operator, so it must name the
    /// offending value rather than paraphrase it. (#321)
    ///
    /// `server-client` (Bolt 3) is the other constructor, for a refused connection or a
    /// per-request timeout. The message it carries **names the address actually tried**, because
    /// the remedy depends entirely on whether the client is pointed where the operator expects —
    /// which is the whole reason SR-4 makes `CAO_API_HOST`/`CAO_API_PORT` configurable. Hence the
    /// deliberately open format string: each constructor supplies its own whole sentence rather
    /// than sharing a prefix that would be wrong for the other. (#321)
    #[error("{0}")]
    Unreachable(String),

    /// The server's response could not be deserialised, or exceeded the response cap.
    ///
    /// **Carries its cause** (`domain-entities.md`): a shape change is only actionable with the
    /// underlying message, so "could not decode the server's response" alone sends the operator
    /// nowhere. `String` rather than `#[source] serde_json::Error` because the variant also
    /// carries a body preview and a size-cap breach, neither of which is a `serde` error — and a
    /// `#[from]` would make every `serde_json` failure in the crate implicitly a `Decode`,
    /// including the *serialisation* of a request, which is a different fault entirely.
    ///
    /// Rendered as a hard error: the endpoint's shape moved, and no retry fixes that. (#321)
    #[allow(dead_code)] // constructed by `server-client`; matched by `renderer` (Bolt 5). (#321)
    #[error("could not decode the cao-server response: {0}")]
    Decode(String),

    /// `POST /sessions` answered **422** — FastAPI's validation status.
    ///
    /// Carries the server's own `detail`, which names the rejected field. That is the difference
    /// between an actionable error and "the server said no", and it is why this is not folded
    /// into [`TuiError::Http`]: a 422 means *this request was wrong*, whose remedy is editing a
    /// field, while a 5xx means *the server is broken*. Also raised locally when a caller
    /// supplies the wrong number of path values for a route, which is the same class of fault
    /// caught one step earlier. (#321)
    #[allow(dead_code)] // constructed by `server-client`; matched by `renderer` (Bolt 5). (#321)
    #[error("cao-server rejected the request: {0}")]
    Validation(String),

    /// `GET /terminals/{id}` answered **404**, carrying the id.
    ///
    /// Deliberately **not** `Http(404)`. `handoff.rs`'s readiness poll keys on the distinction:
    /// only an explicit 5xx is conclusive (BR-12), and a 404 for a terminal row that has not
    /// appeared yet must keep the poll going rather than fail the launch. Collapsing this into
    /// `Http` would turn a race with terminal registration into a launch failure. (#321)
    #[allow(dead_code)] // constructed by `server-client`; matched by `renderer` (Bolt 5). (#321)
    #[error("cao-server has no terminal {0}")]
    NotFound(String),

    /// No HTTP route exists for the command, carrying the command id.
    ///
    /// **A typed condition, not an accident** (BR-18). `CommandCatalog::commands()` filters HIDE
    /// rows so they are unreachable through navigation, and a HANDOFF command runs in a real
    /// terminal and needs no route — so this should be unreachable in practice. It exists so a
    /// *programmatic* caller fails loudly rather than silently doing nothing, and it names the
    /// command so a misclassification is identifiable rather than merely reported. (#321)
    #[allow(dead_code)] // constructed by `server-client`; matched by `renderer` (Bolt 5). (#321)
    #[error("no HTTP route for command {0}; it cannot be run in-app")]
    NoRoute(String),
}

#[cfg(test)]
mod tests {
    use super::TuiError;

    /// Test (b) of this unit: the crate-root error type exists and behaves as a
    /// `std::error::Error`.
    ///
    /// The generic bound alone is a compile-time check that can never go red at runtime,
    /// so the assertions below target the two observable pieces of the impl instead:
    /// `Display` (the single operator-facing line) and `source()` (present only because
    /// the variant carries `#[from]`). Deleting `#[from]`, swapping the inner
    /// `std::io::Error` for a `String`, or editing the `#[error(..)]` format string each
    /// turns this red. (#321)
    #[test]
    fn tui_error_is_a_std_error_with_display_and_source() {
        fn requires_std_error<E: std::error::Error>(e: &E) -> &E {
            e
        }

        let err = TuiError::from(std::io::Error::from(std::io::ErrorKind::BrokenPipe));
        let err = requires_std_error(&err);

        assert_eq!(
            err.to_string(),
            "i/o error: broken pipe",
            "TuiError must render as one operator-facing line"
        );
        assert!(
            std::error::Error::source(err).is_some(),
            "TuiError::Io must expose the underlying io::Error as its source"
        );
    }
}
