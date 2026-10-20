"""Plugin-root path containment — Agent Plugins §4.1.

§4.1 permits a symlink whose target resolves *within* the plugin root and
requires rejecting one that escapes it. That makes containment a **realpath**
question, never a lexical one: ``skills/good`` can be a symlink to
``/etc/passwd``, and ``skills/../skills/good`` is perfectly legitimate.

Why this is a sibling of ``utils/path_validation.py`` rather than a change to it
------------------------------------------------------------------------------
``path_validation.safe_join_under_base`` validates *path components* — it takes
untrusted segments and joins them under a trusted base, rejecting anything that
is not ``[A-Za-z0-9._-]``. That is the right shape for the memory subsystem's
composed keys and the wrong shape for §4.1, whose inputs are a ``./``-rooted
configuration value and a ``SKILL.md`` discovered by directory traversal —
neither of which is a sequence of caller-supplied segments.

:func:`resolve_within_root` therefore adds the missing shape using the *same*
realpath-then-explicit-guard technique, so CodeQL's ``PathNormalization`` →
``SafeAccessCheck`` taint model and the existing security-review posture carry
over to this module unchanged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


def canonical_root(root: Union[str, Path]) -> Optional[str]:
    """Realpath-canonicalize a plugin root.

    Returns ``None`` when the path cannot be canonicalized at all (a symlink
    loop on the root itself, or an OS-level failure). Callers treat that as
    "this is not a usable plugin root" rather than propagating the error —
    validation is total.
    """
    try:
        return os.path.realpath(os.path.abspath(os.fspath(root)))
    except OSError as exc:  # pragma: no cover - ELOOP/ENAMETOOLONG on the root
        logger.warning("Could not canonicalize plugin root '%s': %s", root, exc)
        return None


def resolve_within_root(
    root: Union[str, Path],
    candidate: Union[str, Path],
) -> Optional[Path]:
    """Canonicalize ``candidate`` and return it only if contained in ``root``.

    A relative ``candidate`` is interpreted relative to ``root`` — which is what
    §4.1's ``./``-rooted configuration values and plugin-relative discovery
    paths mean.

    Returns:
        The canonicalized absolute path when it is ``root`` itself or a
        descendant of it; ``None`` when it escapes, or when either path cannot
        be canonicalized.

    This function **never raises**. Every caller in this package is on a path
    that must produce a report rather than an exception, and a containment
    helper that can throw would undermine that at exactly the moment it matters
    (a hostile plugin).
    """
    root_real = canonical_root(root)
    if root_real is None:
        return None

    try:
        raw = os.fspath(candidate)
    except TypeError:
        return None

    if not os.path.isabs(raw):
        raw = os.path.join(root_real, raw)

    try:
        # os.path.realpath is recognized by CodeQL as a PathNormalization,
        # transitioning taint to NormalizedUnchecked; the guard below is the
        # matching SafeAccessCheck. Non-strict, so a broken or looping symlink
        # resolves as far as it can instead of raising.
        candidate_real = os.path.realpath(os.path.abspath(raw))
    except OSError as exc:
        logger.debug("Could not canonicalize '%s' under '%s': %s", candidate, root, exc)
        return None

    if candidate_real == root_real:
        return Path(candidate_real)
    if candidate_real.startswith(root_real + os.sep):
        return Path(candidate_real)
    return None


def is_within_root(root: Union[str, Path], candidate: Union[str, Path]) -> bool:
    """Boolean form of :func:`resolve_within_root`, for assertions and tests."""
    return resolve_within_root(root, candidate) is not None
