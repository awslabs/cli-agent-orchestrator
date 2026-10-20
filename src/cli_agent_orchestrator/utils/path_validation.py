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
# /var/folders (macOS temp) stay allowed. Directories whose WHOLE subtree is
# off limits are listed separately in BLOCKED_SYSTEM_SUBTREES below.
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

# Directories under which NOTHING is an acceptable working directory or
# archive target, at any depth: system configuration, kernel and device
# pseudo-filesystems, boot files, the system binaries and libraries, and the
# per-user crontab spool. Distinct from the exact-match set above because
# projects legitimately live beneath /tmp, /var/folders, /home or /Users, so
# those stay exact-only. The paths are compared AFTER ``os.path.realpath``, so
# every canonical spelling a root can resolve to must be listed: the macOS
# ``/etc`` -> ``/private/etc``, and on usr-merged Linux ``/lib`` ->
# ``/usr/lib`` and ``/lib64`` -> ``/usr/lib64``, where the ``/lib`` entries on
# their own never fire. ``/root`` is a subtree too: ``/root/.ssh/authorized_keys``
# and ``/root/.bashrc`` are persistence for whoever can reach the API of a
# cao-server that runs as root, and nothing in the repository's container or
# cluster manifests runs it as root with projects under ``/root``. A deployment
# that does must keep its projects elsewhere (``/workspace``, ``/srv``).
BLOCKED_SYSTEM_SUBTREES = frozenset(
    {
        "/bin",
        "/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/etc",
        "/dev",
        "/proc",
        "/sys",
        "/boot",
        "/root",
        "/lib",
        "/lib64",
        "/usr/lib",
        "/usr/lib64",
        "/var/spool/cron",
        "/private/etc",
    }
)


# Subtrees carved back out of BLOCKED_SYSTEM_SUBTREES: /dev/shm is the
# tmpfs scratch area on Linux and a legitimate archive or working location
# in containers, unlike the device nodes around it.
_BLOCKED_SUBTREE_EXCEPTIONS = ("/dev/shm",)


def _blocked_reason(real_path: str) -> str:
    """Return why ``real_path`` is refused, or an empty string if it is allowed."""
    if real_path in BLOCKED_SYSTEM_DIRECTORIES:
        return f"blocked system path {real_path}"
    for allowed in _BLOCKED_SUBTREE_EXCEPTIONS:
        if real_path == allowed or real_path.startswith(allowed + "/"):
            return ""
    for root in BLOCKED_SYSTEM_SUBTREES:
        if real_path == root or real_path.startswith(root + "/"):
            return f"path beneath blocked system path {root}"
    return ""


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
            blocked system path or lies inside a blocked system subtree, does
            not exist (without ``allow_create``),
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

    # Step 3: Block sensitive system directories: the exact-match set and
    # everything beneath the subtree set, so /etc/passwd is refused even with
    # allow_file and an existing /etc/ssh is refused as a working directory.
    reason = _blocked_reason(real_path)
    if reason:
        raise ValueError(f"{description} not allowed: {path} (resolves to {reason})")

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
    reason = _blocked_reason(ancestor_real)
    if reason:
        raise ValueError(
            f"{description} not allowed: {path} (nearest existing ancestor resolves to {reason})"
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


def flatten_path_separators(value: str) -> str:
    """Flatten every path separator in ``value`` to ``__``.

    Used where a user- or profile-derived string becomes a single filename
    component and the separators should be folded rather than rejected (the
    provider agent-file sinks, which historically accepted namespaced names).
    Both ``/`` and ``\\`` are flattened: backslash is a path separator on
    Windows, so leaving it intact would let a name like ``..\\..\\x`` traverse
    out of the provider directory there.

    Prefer :func:`validate_path_component` when the value should be *rejected*
    rather than rewritten. This is the weaker, lossy primitive; it guarantees
    the result contains no separator, not that the result is a sensible name.

    Idempotent: a value with no separator is returned unchanged.
    """
    return value.replace("/", "__").replace("\\", "__")


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
