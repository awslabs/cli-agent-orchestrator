"""Path field validation for the ``cao tui`` thin shell (U5).

The builder (U3) collects working-directory / output-path arguments as free
text. This module is the single seam that turns a raw field value into a
canonical, policy-checked absolute path — or into a renderable field error.

It owns **no validation logic of its own** (SC-3 / FR-8.1). Every rule
(``~`` expansion, ``realpath`` canonicalization, the blocked-system-directory
policy, the existence / ``allow_create`` ancestor policy) lives in the shared
:mod:`cli_agent_orchestrator.utils.path_validation` module — the exact same
code path ``TmuxClient`` uses for working directories. :class:`PathInput` is a
thin delegate that re-raises the validator's :class:`ValueError` as a
:class:`PathInputError` the builder can render inline next to the field.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only the standard library and
``cli_agent_orchestrator.utils.path_validation`` are imported here. No heavy
in-process layer, no ``cli`` module.
"""

from __future__ import annotations

from cli_agent_orchestrator.utils.path_validation import (
    resolve_and_validate_path,
    safe_join_under_base,
    validate_path_component,
)


class PathInputError(ValueError):
    """A path field failed validation — renderable inline by the builder (U3).

    Subclasses :class:`ValueError` so callers that already handle the shared
    validator's ``ValueError`` keep working, while the distinct type lets the
    builder catch *field* errors specifically and show the message next to the
    offending input rather than crashing the shell.
    """


class PathInput:
    """Thin field validator delegating to the shared path validator (SC-3).

    Instances are cheap and stateless; the class exists so the builder can hold
    a ``PathInput`` per path-typed argument and call :meth:`validate` when the
    field loses focus or the command is previewed. All decisions are made by
    :mod:`cli_agent_orchestrator.utils.path_validation` — this class adds only
    the error-type translation and a sensible default ``description``.
    """

    def __init__(self, description: str = "Working directory") -> None:
        """Store the noun used in field-error messages.

        Args:
            description: Human-facing label for the path field (e.g.
                ``"Working directory"`` or ``"Output path"``). Passed straight
                through to the shared validator so error text reads naturally.
        """

        self.description = description

    def validate(self, raw: str, *, allow_create: bool = False) -> str:
        """Canonicalize and policy-check ``raw``, delegating to the validator.

        Args:
            raw: The user-entered path text.
            allow_create: When ``True``, a target that does not exist yet is
                permitted as long as its nearest existing ancestor is not a
                blocked system directory (the shared validator's D5 policy).
                Used for output paths the command creates on run.

        Returns:
            The canonicalized absolute path returned by
            :func:`resolve_and_validate_path`.

        Raises:
            PathInputError: If the shared validator rejects the path (relative
                after canonicalization, blocked system path, missing target
                without ``allow_create``, or no valid existing ancestor). The
                validator's message is preserved so the builder can render it.
        """

        try:
            return resolve_and_validate_path(
                raw,
                allow_create=allow_create,
                allow_file=False,
                description=self.description,
            )
        except ValueError as exc:
            # Re-raise as the field-error type without losing the validator's
            # message or its policy decision (SC-3: no local re-check).
            raise PathInputError(str(exc)) from exc


# Thin pass-throughs to the confinement helpers, re-exported so a builder that
# composes a path out of user-derived *segments* under a fixed base (rather than
# an absolute field value) reaches the same shared primitives without importing
# the utils module directly. No logic of their own — pure delegation (SC-3).

__all__ = [
    "PathInput",
    "PathInputError",
    "validate_path_component",
    "safe_join_under_base",
]
