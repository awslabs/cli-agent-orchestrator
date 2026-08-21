"""Shared validation for user- or agent-supplied filesystem paths.

Extracted from ``clients/tmux.py::TmuxClient._resolve_and_validate_working_directory``
(issue #345, design D5) so archive export/import targets reuse the same
realpath canonicalization + blocked-system-directory policy instead of
reimplementing it. ``TmuxClient`` delegates here with its stricter
must-exist, directory-only settings.
"""

import os
import re

# Paths that should never be used as working directories or archive
# targets. Prevents user-supplied paths from pointing at sensitive system
# locations. Includes /private/* variants for macOS (where /etc ->
# /private/etc, etc.). Only the exact listed paths are blocked — not their
# subdirectories — so legitimate paths like /Volumes/workplace or
# /var/folders (macOS temp) stay allowed.
BLOCKED_SYSTEM_DIRECTORIES = frozenset(
    {
        "/",
        "/bin",
        "/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/etc",
        "/var",
        "/tmp",
        "/dev",
        "/proc",
        "/sys",
        "/root",
        "/boot",
        "/lib",
        "/lib64",
        "/private/etc",
        "/private/var",
        "/private/tmp",
    }
)


def resolve_and_validate_path(
    path: str,
    allow_create: bool = False,
    allow_file: bool = False,
    description: str = "Path",
) -> str:
    """Canonicalize and validate a user-supplied path.

    Canonicalizes the path (expands ``~``, resolves symlinks, normalizes
    ``..``) and rejects paths that point to sensitive system directories.

    Args:
        path: The path to validate.
        allow_create: Permit a target that does not exist yet (e.g. an
            export destination created after validation). The blocked-
            directory check is then applied to the nearest EXISTING
            ancestor instead of the target itself.
        allow_file: Permit an existing non-directory target (e.g. an
            ``-o out.tar.gz`` archive file). With the default False, an
            existing target must be a directory.
        description: Noun used in error messages (``TmuxClient`` passes
            "Working directory" so its errors stay byte-identical).

    Returns:
        Canonicalized absolute path.

    Raises:
        ValueError: If the path is relative after canonicalization, is a
            blocked system path, does not exist (without ``allow_create``),
            or has no valid existing ancestor (with ``allow_create``).
    """
    # Expand ~ to the server's home directory so clients can use portable
    # paths like ~/q/my-project without knowing the server's actual home.
    path = os.path.expanduser(path)

    # Step 1: Canonicalize via realpath to resolve symlinks and ``..``
    # sequences. os.path.realpath is recognized by CodeQL as a
    # PathNormalization (transitions taint to NormalizedUnchecked).
    real_path = os.path.realpath(os.path.abspath(path))

    # Step 2: Path-containment guard (CodeQL SafeAccessCheck). The "/"
    # prefix is always true after realpath(), but this explicit guard
    # satisfies CodeQL's two-state taint model and rejects relative paths.
    if not real_path.startswith("/"):
        raise ValueError(f"{description} must be an absolute path: {path}")

    # Step 3: Block sensitive system directories (exact matches only).
    if real_path in BLOCKED_SYSTEM_DIRECTORIES:
        raise ValueError(
            f"{description} not allowed: {path} " f"(resolves to blocked system path {real_path})"
        )

    # Step 4: Existence policy.
    if os.path.isdir(real_path):
        return real_path
    if os.path.exists(real_path):
        # Exists but is not a directory (regular file, socket, ...).
        if allow_file:
            return real_path
        raise ValueError(f"{description} does not exist: {path}")

    if not allow_create:
        raise ValueError(f"{description} does not exist: {path}")

    # Target does not exist yet: apply the blocked-directory policy to the
    # nearest EXISTING ancestor (design D5) so e.g. /etc/new-dir is still
    # rejected while ~/exports/new-dir passes and is created afterwards.
    ancestor = os.path.dirname(real_path)
    while ancestor and not os.path.exists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    ancestor_real = os.path.realpath(ancestor)
    if ancestor_real in BLOCKED_SYSTEM_DIRECTORIES:
        raise ValueError(
            f"{description} not allowed: {path} "
            f"(nearest existing ancestor resolves to blocked system path {ancestor_real})"
        )
    if not os.path.isdir(ancestor_real):
        raise ValueError(f"{description} has no existing ancestor directory: {path}")
    return real_path


# ── component-under-base confinement (memory wiki paths) ─────────────
#
# ``resolve_and_validate_path`` above validates *absolute* user paths with a
# blocked-system-directory policy. The memory subsystem has a different
# shape: it composes filesystem paths out of individual, user-derived
# *segments* (``key``, ``scope``, ``scope_id``) under a fixed base
# directory. The safe primitive there is strict per-segment validation plus
# realpath containment under the base, so the two helpers below are kept
# distinct from the absolute-path validator.

# A single safe path segment: strict allowlist, no separators, no traversal.
_SAFE_PATH_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def validate_path_component(component: str, description: str = "path component") -> str:
    """Validate that ``component`` is a single, safe path segment.

    A path segment is rejected when it is empty, equals ``.`` or ``..``,
    contains a NUL byte, contains any path separator (``/``, ``\\``,
    ``os.sep``, or ``os.altsep``), or falls outside the strict
    ``[A-Za-z0-9._-]`` allowlist. Any of these could let a user-derived
    value escape its intended parent directory when joined into a path.

    Returns the component unchanged when valid, so callers may assign the
    return value and let static analysis (CodeQL) see the checked value
    flow into subsequent path construction.

    Raises:
        ValueError: If the component is not a safe single path segment.
    """
    if not isinstance(component, str) or not component:
        raise ValueError(f"{description} must be a non-empty string")
    if component in (".", ".."):
        raise ValueError(f"{description} must not be '.' or '..': {component!r}")
    if "\x00" in component:
        raise ValueError(f"{description} must not contain a NUL byte: {component!r}")
    separators = {"/", "\\", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    if any(sep in component for sep in separators):
        raise ValueError(f"{description} must not contain a path separator: {component!r}")
    if not _SAFE_PATH_COMPONENT_RE.match(component):
        raise ValueError(f"{description} must match ^[A-Za-z0-9._-]+$: {component!r}")
    return component


def safe_join_under_base(
    base_dir: str,
    *components: str,
    description: str = "path component",
) -> str:
    """Validate each segment and join it under ``base_dir``, confined to it.

    Each element of ``components`` is checked with
    :func:`validate_path_component`, then joined under the
    realpath-canonicalized base directory. The joined path is canonicalized
    again with ``os.path.realpath`` (recognized by CodeQL as a
    PathNormalization) and an explicit containment guard rejects any result
    that is not the base itself or a descendant of it — satisfying CodeQL's
    two-state taint model for path injection while providing a genuine
    traversal defence.

    Args:
        base_dir: The trusted base directory the result must stay under.
        components: User-derived path segments to validate and join.
        description: Noun used in per-segment error messages.

    Returns:
        The canonicalized absolute path, guaranteed to be within ``base_dir``.

    Raises:
        ValueError: If any segment is unsafe or the joined path escapes the
            base directory.
    """
    base_real = os.path.realpath(os.path.abspath(base_dir))
    validated = [validate_path_component(c, description) for c in components]
    candidate = os.path.join(base_real, *validated)
    real_path = os.path.realpath(os.path.abspath(candidate))
    if real_path != base_real and not real_path.startswith(base_real + os.sep):
        raise ValueError(
            f"Path traversal detected: {real_path!r} escapes base directory {base_real!r}"
        )
    return real_path
