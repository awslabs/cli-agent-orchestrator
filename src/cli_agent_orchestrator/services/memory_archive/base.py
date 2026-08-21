"""Memory archive backend seam (#345 D1).

``MemoryArchiveBackend`` is the ABC every import/export format implements
(OKF first; the parked CAO-native tar.gz registers later as ``cao``).
Backends are constructed with a ``MemoryService`` instance and use only
its public/validated surfaces; they never write wiki files directly.

The report dataclasses carry counters only plus per-topic skip reasons /
per-file errors — never content bytes. Secret skips reference pattern
NAMES only (D5).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ExportReport:
    """Outcome of one ``export_bundle`` run.

    ``skip_reasons`` maps topic key → matched secret-gate pattern names
    (names only, never matched bytes — D5 logging rule).
    """

    exported: int = 0
    skipped_secret: int = 0
    redacted: int = 0
    pruned: int = 0
    unchanged: int = 0
    links_dropped: int = 0
    skip_reasons: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class ImportReport:
    """Outcome of one ``import_bundle`` run.

    ``errors`` maps bundle-relative file path → parse/validation error
    message. ``target_scope``/``target_scope_id`` echo the RESOLVED
    destination (for ``--scope project``, the cwd-resolved project id —
    D5) so the operator can verify it, including on ``--dry-run``.
    """

    imported: int = 0
    skipped_conflict: int = 0
    replaced: int = 0
    merged: int = 0
    rejected: int = 0
    see_also_dropped: int = 0
    bodies_escaped: int = 0
    timestamps_clamped: int = 0
    errors: Dict[str, str] = field(default_factory=dict)
    target_scope: str = ""
    target_scope_id: Optional[str] = None
    dry_run: bool = False


class MemoryArchiveBackend(ABC):
    """One import/export format for the CAO memory store."""

    format_name: str  # registry key, e.g. "okf"

    @abstractmethod
    def export_bundle(
        self,
        scope: str,
        scope_id: Optional[str],
        dest: Path,
        include_history: bool,
        redact: bool,
        prune: bool = False,
    ) -> ExportReport:
        """Export every topic of ``scope``/``scope_id`` into ``dest``.

        ``prune`` (D3, default off — destructive in a user-supplied
        directory) deletes destination topics no longer in the exported set.
        """

    @abstractmethod
    def import_bundle(
        self,
        src: Path,
        target_scope: str,
        conflict_policy: str,
        dry_run: bool,
        terminal_context: Optional[dict] = None,
    ) -> ImportReport:
        """Import a bundle from ``src`` into ``target_scope``.

        ``terminal_context`` feeds ``MemoryService.resolve_scope_id`` —
        for ``project`` targets the cwd it carries binds the import to a
        resolved project id (D5), echoed in the ``ImportReport``.
        """
