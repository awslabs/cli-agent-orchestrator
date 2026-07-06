"""Shared validation for user- or agent-supplied filesystem paths.

Extracted from ``clients/tmux.py::TmuxClient._resolve_and_validate_working_directory``
(issue #345, design D5) so archive export/import targets reuse the same
realpath canonicalization + blocked-system-directory policy instead of
reimplementing it. ``TmuxClient`` delegates here with its stricter
must-exist, directory-only settings.
"""

import os

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
