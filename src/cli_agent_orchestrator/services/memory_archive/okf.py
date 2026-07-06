"""OKF v0.1 export backend for CAO memory (#345, Unit 2 — export half of D5).

Exports one scope's wiki topics into a plain directory of OKF markdown
files (D2 bundle shape). The exporter is deterministic and idempotent
(D3): fixed frontmatter key order, LF endings, single trailing newline,
byte-compare before every write, ``index.md`` regenerated as a pure
function of the exported set.

Export is a declassification boundary (D5): every body (and history
sections when included) passes through the secret gate. Default policy
skips the topic and records the pattern NAME only; ``redact=True``
exports with ``[REDACTED:<name>]`` markers. Content bytes never appear
in the report or logs.

Import (#345 Unit 3 — D5 inbound controls) treats the bundle as
untrusted input: explicit target scope (``global``/``project``/
``federated`` only), filename-stem sanitizer round-trip, See-Also blocks
stripped (export-only in PR 1), structural-marker escaping against body
spoofing, and every write routed through ``MemoryService.store()``.
"""

import asyncio
import json
import logging
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import frontmatter

from cli_agent_orchestrator.models.memory import MemoryScope, MemoryType
from cli_agent_orchestrator.services.memory_archive.base import (
    ExportReport,
    ImportReport,
    MemoryArchiveBackend,
)
from cli_agent_orchestrator.services.secret_gate import redact_secrets, scan_for_secrets
from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path

if TYPE_CHECKING:
    from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Matches the ``## <ISO-ts>`` section headings store() writes. Deliberately
# unanchored to mirror _parse_wiki_file's existing parsing behavior.
_TIMESTAMP_HEADING_RE = re.compile(r"## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# Canonical See-Also link line shape rendered by MemoryService._render_see_also.
_SEE_ALSO_LINK_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)$")

# Reserved bundle paths (D2): never OKF topics, never pruned by name match.
_RESERVED_FILES = frozenset({"index.md", "manifest.md"})
_HISTORY_DIR = "history"

# Scopes whose topics nest under <scope_id>/<key>.md in the bundle (D2):
# two scope_ids can hold the same key, so a flat directory would collide.
_NESTED_SCOPES = frozenset({MemoryScope.SESSION.value, MemoryScope.AGENT.value})

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Import targets (D5): agent is banned outright, session is not offered —
# bulk-writing another agent's/session's private tier is exactly the
# cross-scope contamination store()'s guard exists to prevent.
_IMPORT_TARGET_SCOPES = frozenset(
    {MemoryScope.GLOBAL.value, MemoryScope.PROJECT.value, MemoryScope.FEDERATED.value}
)
_CONFLICT_POLICIES = frozenset({"skip", "replace", "merge"})

# Metadata header comment marker written by store(). Any occurrence in an
# untrusted imported body must be escaped (D5 body-spoofing rule) — the
# store/parse regexes are unanchored, so a substring match is enough to
# spoof; the escape must break the token itself, not just prefix the line.
_HEADER_COMMENT_RE = re.compile(r"<!--\s*id:")


@dataclass
class _Topic:
    """One topic collected for export (post secret-gate)."""

    key: str
    scope_id: Optional[str]  # set only for nested (session/agent) scopes
    nested: bool
    memory_type: str
    tags: List[str]
    description: str
    created_iso: str
    updated_iso: str
    body: str  # latest-section content (post-redaction when redact=True)
    history: str = ""  # older ## <ts> sections verbatim ("" when none/not requested)

    @property
    def rel_path(self) -> str:
        """Bundle-relative topic path (index.md link target)."""
        return f"{self.scope_id}/{self.key}.md" if self.nested else f"{self.key}.md"


class OkfArchiveBackend(MemoryArchiveBackend):
    """OKF v0.1 directory-bundle backend (first ``MemoryArchiveBackend``)."""

    format_name = "okf"

    def __init__(self, memory_service: "MemoryService"):
        self._svc = memory_service

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------

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

        See the module docstring for the D2/D3/D5 contract. ``prune``
        (default off — deleting files in a user-supplied directory is
        destructive) removes destination topics no longer in the exported
        set, along with their history files.
        """
        MemoryScope(scope)  # narrow ValueError on unknown scope
        if scope == MemoryScope.PROJECT.value and not scope_id:
            raise ValueError("project-scope export requires a scope_id (resolved project id)")

        dest_real = Path(
            resolve_and_validate_path(
                str(dest), allow_create=True, description="Export destination"
            )
        )

        report = ExportReport()
        topics = self._collect_topics(scope, scope_id, include_history, redact, report)
        dest_real.mkdir(parents=True, exist_ok=True)

        # Bundle-relative path per surviving (scope_id, key) — the See-Also
        # normalization target set. A link whose key is absent here (e.g.
        # its topic was secret-skipped) degrades to plain text.
        rel_paths: Dict[Tuple[Optional[str], str], str] = {
            (t.scope_id, t.key): t.rel_path for t in topics
        }

        written_rel: Set[str] = set()
        for topic in sorted(topics, key=lambda t: (t.scope_id or "", t.key)):
            body = self._normalize_see_also(topic.body, topic.scope_id, rel_paths, report)
            content = self._render_topic(topic, body)
            if self._write_if_changed(dest_real / topic.rel_path, content):
                report.exported += 1
            else:
                report.unchanged += 1
            written_rel.add(topic.rel_path)
            if include_history and topic.history:
                history_rel = f"{_HISTORY_DIR}/{topic.rel_path}"
                self._write_if_changed(dest_real / history_rel, topic.history)
                written_rel.add(history_rel)

        self._write_if_changed(dest_real / "index.md", self._render_index(topics))
        self._write_if_changed(dest_real / "manifest.md", self._render_manifest(scope, scope_id))

        if prune:
            self._prune(dest_real, written_rel, report)

        logger.info(
            "okf_export_completed scope=%s exported=%d unchanged=%d skipped_secret=%d "
            "redacted=%d pruned=%d links_dropped=%d",
            scope,
            report.exported,
            report.unchanged,
            report.skipped_secret,
            report.redacted,
            report.pruned,
            report.links_dropped,
        )
        return report

    def import_bundle(
        self,
        src: Path,
        target_scope: str,
        conflict_policy: str,
        dry_run: bool,
        terminal_context: Optional[dict] = None,
    ) -> ImportReport:
        """Import an OKF directory bundle into ``target_scope`` (D5 inbound).

        Every file is untrusted input: filename stems must round-trip the
        store's key sanitizer unchanged, ``## See Also`` blocks are
        stripped (export-only in PR 1), and structural markers that would
        spoof entry boundaries or metadata headers are escaped. Writes go
        through ``store()`` only; ``dry_run`` runs the full parse/validate/
        secret pipeline and reports everything without writing.
        """
        if target_scope not in _IMPORT_TARGET_SCOPES:
            raise ValueError(
                f"import target scope must be one of "
                f"{sorted(_IMPORT_TARGET_SCOPES)}, got {target_scope!r}"
            )
        if conflict_policy not in _CONFLICT_POLICIES:
            raise ValueError(
                f"conflict policy must be one of {sorted(_CONFLICT_POLICIES)}, "
                f"got {conflict_policy!r}"
            )
        src_real = Path(resolve_and_validate_path(str(src), description="Import source"))
        if not src_real.is_dir():
            raise ValueError(f"Import source must be a directory: {src}")

        scope_id = self._svc.resolve_scope_id(target_scope, terminal_context)
        if target_scope == MemoryScope.PROJECT.value and scope_id is None:
            raise ValueError(
                "Cannot import into project scope without a resolvable "
                "project identity. Pass terminal_context with 'cwd' set."
            )

        report = ImportReport(target_scope=target_scope, target_scope_id=scope_id, dry_run=dry_run)

        for path in sorted(src_real.rglob("*.md")):
            rel = path.relative_to(src_real).as_posix()
            if path.name in _RESERVED_FILES or rel.split("/", 1)[0] == _HISTORY_DIR:
                continue
            # rglob follows symlinks; a bundle entry linking outside src_real
            # would let untrusted content pull arbitrary readable files into
            # memory. Belt and braces: reject symlinks outright AND anything
            # whose resolved path escapes the validated bundle root.
            if path.is_symlink() or not path.resolve().is_relative_to(src_real):
                self._reject(report, rel, "symlink or path escaping the bundle root")
                continue
            if "/" in rel:
                self._reject(
                    report,
                    rel,
                    "nested topic paths are not supported; place topics in the bundle root",
                )
                continue
            self._import_one_topic(
                path,
                rel,
                target_scope,
                scope_id,
                conflict_policy,
                dry_run,
                terminal_context,
                report,
            )

        logger.info(
            "okf_import_completed scope=%s scope_id=%s dry_run=%s imported=%d "
            "replaced=%d merged=%d skipped_conflict=%d rejected=%d "
            "see_also_dropped=%d bodies_escaped=%d timestamps_clamped=%d",
            target_scope,
            scope_id,
            dry_run,
            report.imported,
            report.replaced,
            report.merged,
            report.skipped_conflict,
            report.rejected,
            report.see_also_dropped,
            report.bodies_escaped,
            report.timestamps_clamped,
        )
        return report

    def _import_one_topic(
        self,
        path: Path,
        rel: str,
        target_scope: str,
        scope_id: Optional[str],
        conflict_policy: str,
        dry_run: bool,
        terminal_context: Optional[dict],
        report: ImportReport,
    ) -> None:
        """Run one bundle file through the D5 inbound pipeline.

        Per-file failures record a key-only rejection and return — the
        import continues with the remaining files. Only pattern names and
        filenames appear in the report/logs, never content bytes.
        """
        key = path.stem
        try:
            sanitized = self._sanitize_key_safe(key)
        except ValueError:
            sanitized = None
        if sanitized != key:
            self._reject(report, rel, "filename stem fails key sanitizer round-trip")
            return

        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            self._reject(report, rel, f"unreadable file: {e.__class__.__name__}")
            return
        except Exception as e:  # noqa: BLE001 — untrusted YAML can raise any parser error
            logger.warning("okf_import_unparseable key=%s error=%s", key, e.__class__.__name__)
            self._reject(report, rel, f"frontmatter parse failed: {e.__class__.__name__}")
            return

        meta = post.metadata
        # ``type`` is required by OKF §9 — its absence is a per-file
        # rejection (design test 8); an unknown value coerces to
        # ``reference`` per the mapping table. Unknown keys are tolerated
        # silently (test 7).
        raw_type = meta.get("type")
        if raw_type is None:
            self._reject(report, rel, "missing required frontmatter key: type")
            return
        try:
            memory_type = MemoryType(str(raw_type)).value
        except ValueError:
            memory_type = MemoryType.REFERENCE.value

        tags = self._coerce_tags(meta.get("tags"))
        occurred_at = self._coerce_timestamp(meta.get("timestamp"), key)

        # Body pipeline, in order: strip See-Also (export-only in PR 1),
        # escape structural markers, drop the leading H1 (store() writes
        # its own).
        body, dropped = self._strip_see_also_block(post.content)
        report.see_also_dropped += dropped
        body, escaped = self._escape_structural_markers(body)
        if escaped:
            report.bodies_escaped += 1
        body = self._strip_leading_h1(body, key).strip()

        # Federated is credential-gated (parity with store()'s gate, but
        # reported per file so dry_run sees it too). Pattern NAME only.
        if target_scope == MemoryScope.FEDERATED.value:
            hit = scan_for_secrets(body)
            if hit:
                logger.warning("okf_import_secret_rejected key=%s", key)
                self._reject(report, rel, f"matched credential pattern {hit!r}")
                return

        wiki_path = self._svc.get_wiki_path(target_scope, scope_id, key)
        exists = wiki_path.exists()

        if exists and conflict_policy == "skip":
            report.skipped_conflict += 1
            return

        if dry_run:
            # No store()/forget() calls — replicate store()'s D5 clamp rule
            # so the dry-run report matches what a real run would count.
            if self._would_clamp(occurred_at, wiki_path, exists, conflict_policy):
                report.timestamps_clamped += 1
            if exists and conflict_policy == "replace":
                report.replaced += 1
            elif exists:  # merge
                report.merged += 1
            else:
                report.imported += 1
            return

        try:
            if exists and conflict_policy == "replace":
                asyncio.run(
                    self._svc.forget(
                        key,
                        scope=target_scope,
                        scope_id=scope_id,
                        terminal_context=terminal_context,
                    )
                )
            memory = asyncio.run(
                self._svc.store(
                    content=body,
                    scope=target_scope,
                    memory_type=memory_type,
                    key=key,
                    tags=tags,
                    terminal_context=terminal_context,
                    occurred_at=occurred_at,
                )
            )
        except ValueError as e:
            # store()'s own validation (incl. its federated gate) — the
            # message carries pattern names / field names only, no content.
            logger.warning("okf_import_store_rejected key=%s", key)
            self._reject(report, rel, str(e))
            return

        if memory.timestamp_clamped:
            report.timestamps_clamped += 1
        if exists and conflict_policy == "replace":
            report.replaced += 1
        elif exists:  # merge
            report.merged += 1
        else:
            report.imported += 1

    @staticmethod
    def _reject(report: ImportReport, rel: str, reason: str) -> None:
        """Record a per-file rejection (key/filename + reason, never content)."""
        report.rejected += 1
        report.errors[rel] = reason
        logger.warning("okf_import_rejected file=%s", rel)

    @staticmethod
    def _sanitize_key_safe(key: str) -> str:
        """Round-trip ``key`` through the store's sanitizer (may raise ValueError).

        Local import: memory_service ↔ okf would otherwise be a cycle
        (same rationale as ``_entry_path_components_safe``).
        """
        from cli_agent_orchestrator.services.memory_service import MemoryService

        return MemoryService._sanitize_key(key)

    @staticmethod
    def _coerce_tags(raw: object) -> str:
        """Frontmatter ``tags`` (YAML list per the mapping table) → CAO csv."""
        if isinstance(raw, list):
            return ",".join(str(t).strip() for t in raw if str(t).strip())
        if isinstance(raw, str):
            return raw
        return ""

    @staticmethod
    def _coerce_timestamp(raw: object, key: str) -> Optional[datetime]:
        """Frontmatter ``timestamp`` → ``occurred_at`` (None when absent/invalid).

        YAML parses bare ISO timestamps into datetime already; a quoted
        string is parsed here. An unparseable value degrades to None (the
        entry imports with now()) rather than rejecting the file.
        """
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, _ISO_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                logger.debug("okf_import_bad_timestamp key=%s", key)
        return None

    @staticmethod
    def _strip_see_also_block(body: str) -> Tuple[str, int]:
        """Strip any ``## See Also`` block; count dropped links (D5, PR-1 lossy).

        Bundle-escaping links are stripped like any other — the block never
        reaches ``store()``, so its targets are never resolved.
        """
        out: List[str] = []
        dropped = 0
        in_block = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped == "## See Also":
                in_block = True
                continue
            if in_block and stripped.startswith("## "):
                in_block = False
            if in_block:
                if re.match(r"^- \[[^\]]*\]\([^)]*\)", stripped):
                    dropped += 1
                continue
            out.append(line)
        return "\n".join(out), dropped

    @staticmethod
    def _escape_structural_markers(body: str) -> Tuple[str, bool]:
        """Escape spoofed ``## <ISO-ts>`` headings and ``<!-- id:`` headers.

        The wiki parsers match these tokens UNanchored, so a prefix alone
        would not stop them — the token itself is broken: ``##`` becomes
        ``\\#\\#`` and ``<!--`` becomes ``<!\\--`` on any line carrying a
        marker. Returns ``(escaped_body, any_line_escaped)``.
        """
        out: List[str] = []
        escaped = False
        for line in body.splitlines():
            if _TIMESTAMP_HEADING_RE.search(line):
                line = line.replace("##", r"\#\#")
                escaped = True
            if _HEADER_COMMENT_RE.search(line):
                line = line.replace("<!--", r"<!\--")
                escaped = True
            out.append(line)
        return "\n".join(out), escaped

    @staticmethod
    def _strip_leading_h1(body: str, key: str) -> str:
        """Drop a leading ``# <key>`` H1 — ``store()`` writes its own."""
        lines = body.lstrip("\n").splitlines()
        if lines and lines[0].strip() == f"# {key}":
            return "\n".join(lines[1:])
        return body

    def _would_clamp(
        self,
        occurred_at: Optional[datetime],
        wiki_path: Path,
        exists: bool,
        conflict_policy: str,
    ) -> bool:
        """Dry-run replica of ``store()``'s D5 clamp rule.

        Reads the topic file to recover ``latest_section_at`` (``replace``
        starts from a fresh file, so no latest section applies), then
        delegates the decision to the shared
        ``MemoryService._occurred_at_would_clamp`` — the single source of
        the clamp rule, so this report cannot drift from a real run.
        """
        if occurred_at is None:
            return False
        now = datetime.now(timezone.utc)
        if self._svc._occurred_at_would_clamp(occurred_at, None, now):
            return True  # future — no file read needed
        latest: Optional[datetime] = None
        if exists and conflict_policy != "replace":
            try:
                existing_ts = re.findall(
                    r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
                    wiki_path.read_text(encoding="utf-8"),
                )
            except OSError as e:
                logger.warning("okf_import_dry_run_read_failed key=%s: %s", wiki_path.stem, e)
                return False
            if existing_ts:
                latest = datetime.strptime(existing_ts[-1], _ISO_FORMAT).replace(
                    tzinfo=timezone.utc
                )
        return self._svc._occurred_at_would_clamp(occurred_at, latest, now)

    # -------------------------------------------------------------------------
    # Topic collection (index walk + secret gate)
    # -------------------------------------------------------------------------

    def _collect_topics(
        self,
        scope: str,
        scope_id: Optional[str],
        include_history: bool,
        redact: bool,
        report: ExportReport,
    ) -> List[_Topic]:
        """Enumerate the scope's topics via the container index walk.

        Uses the index walk (not ``recall()``, whose ``limit`` caps results)
        so every topic of the scope is enumerated. Entries are filtered
        strictly to ``scope`` (and to ``scope_id`` when given for nested
        scopes). Secret-gated per D5 before anything leaves the store.
        """
        index_path = self._svc.get_index_path(scope, scope_id)
        if not index_path.exists():
            return []
        entries = self._svc._parse_index(index_path)

        topics: List[_Topic] = []
        for entry in entries:
            if entry["scope"] != scope:
                continue
            entry_scope_id = entry.get("scope_id")
            if scope in _NESTED_SCOPES and scope_id is not None and entry_scope_id != scope_id:
                continue

            # Defense-in-depth: entry key/scope_id become bundle path
            # components (rel_path). Round-trip them through the store's
            # sanitizers — a tampered index.md must not steer writes above
            # dest. On mismatch, skip and log the key only.
            if not self._entry_path_components_safe(entry["key"], entry_scope_id):
                logger.warning("okf_export_unsafe_index_entry key=%s", entry["key"])
                continue

            # Defense-in-depth: relative_path is index-derived and used as a
            # READ path — a tampered entry (../ segments, symlink hop) must
            # not read files outside the container's wiki dir. Key-only log.
            wiki_file = (index_path.parent / entry["relative_path"]).resolve()
            wiki_root = index_path.parent.resolve()
            if not wiki_file.is_relative_to(wiki_root):
                logger.warning("okf_export_unsafe_index_path key=%s", entry["key"])
                continue
            if not wiki_file.exists():
                logger.debug("okf_export_missing_file key=%s", entry["key"])
                continue
            try:
                file_content = wiki_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("okf_export_read_failed key=%s: %s", entry["key"], e)
                continue

            memory = self._svc._parse_wiki_file(wiki_file, file_content, entry)
            if memory is None:
                logger.debug("okf_export_unparseable key=%s", entry["key"])
                continue

            headings = list(_TIMESTAMP_HEADING_RE.finditer(file_content))
            latest_text = file_content[headings[-1].end() :].strip()
            history_text = ""
            if include_history and len(headings) > 1:
                history_text = (
                    file_content[headings[0].start() : headings[-1].start()].rstrip("\n") + "\n"
                )

            gated = self._apply_secret_gate(entry["key"], latest_text, history_text, redact, report)
            if gated is None:
                continue
            latest_text, history_text = gated

            topics.append(
                _Topic(
                    key=entry["key"],
                    scope_id=entry_scope_id if scope in _NESTED_SCOPES else None,
                    nested=scope in _NESTED_SCOPES and entry_scope_id is not None,
                    memory_type=memory.memory_type,
                    tags=[t for t in (memory.tags or "").split(",") if t],
                    description=self._derive_description(latest_text),
                    created_iso=memory.created_at.strftime(_ISO_FORMAT),
                    updated_iso=memory.updated_at.strftime(_ISO_FORMAT),
                    body=latest_text,
                    history=history_text,
                )
            )
        return topics

    @staticmethod
    def _entry_path_components_safe(key: str, entry_scope_id: Optional[str]) -> bool:
        """True iff ``key``/``scope_id`` survive the store's sanitizers unchanged.

        Reuses ``MemoryService._sanitize_key`` / ``_sanitize_scope_id`` as a
        round-trip check on index-derived values used as path components.
        Imported locally: memory_service ↔ okf would otherwise be a cycle
        once the service grows an archive entry point.
        """
        from cli_agent_orchestrator.services.memory_service import MemoryService

        try:
            if MemoryService._sanitize_key(key) != key:
                return False
        except ValueError:
            return False
        if (
            entry_scope_id is not None
            and MemoryService._sanitize_scope_id(entry_scope_id) != entry_scope_id
        ):
            return False
        return True

    @staticmethod
    def _apply_secret_gate(
        key: str,
        latest_text: str,
        history_text: str,
        redact: bool,
        report: ExportReport,
    ) -> Optional[Tuple[str, str]]:
        """Run the D5 declassification gate on one topic's outbound content.

        Default policy: any hit skips the topic (returns ``None``) and
        records the pattern NAME in ``skip_reasons``. With ``redact``,
        matches are replaced and the topic exports. Only pattern names are
        ever logged — never content bytes.
        """
        if redact:
            latest_text, fired = redact_secrets(latest_text)
            if history_text:
                history_text, history_fired = redact_secrets(history_text)
                fired.extend(n for n in history_fired if n not in fired)
            if fired:
                report.redacted += 1
                logger.warning("okf_export_secret_redacted key=%s patterns=%s", key, fired)
            return latest_text, history_text

        fired = []
        hit = scan_for_secrets(latest_text)
        if hit:
            fired.append(hit)
        if history_text:
            history_hit = scan_for_secrets(history_text)
            if history_hit and history_hit not in fired:
                fired.append(history_hit)
        if fired:
            report.skipped_secret += 1
            report.skip_reasons[key] = fired
            logger.warning("okf_export_secret_skipped key=%s patterns=%s", key, fired)
            return None
        return latest_text, history_text

    # -------------------------------------------------------------------------
    # Rendering (deterministic — D3)
    # -------------------------------------------------------------------------

    @staticmethod
    def _derive_description(body: str) -> str:
        """First sentence of the latest content (derivable-only, may be "").

        Headings and link-list lines (the See-Also shape, ``- [key](...)``)
        are skipped: the description is derived pre-normalization, and raw
        ``../<scope_id>/`` link targets must never leak into frontmatter or
        index.md.
        """
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("- ["):
                continue
            match = re.match(r"(.+?[.!?])(?:\s|$)", stripped)
            return (match.group(1) if match else stripped)[:200]
        return ""

    @staticmethod
    def _normalize_see_also(
        body: str,
        own_scope_id: Optional[str],
        rel_paths: Dict[Tuple[Optional[str], str], str],
        report: ExportReport,
    ) -> str:
        """Rewrite ``## See Also`` links to bundle-relative paths.

        CAO's ``../<scope_id>/<key>.md`` links are same-scope by
        construction, so the target is looked up under the topic's own
        scope_id. A link whose target is not in the bundle (e.g. a
        secret-skipped topic) degrades to plain text and is counted.
        """
        out: List[str] = []
        in_see_also = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped == "## See Also":
                in_see_also = True
                out.append(line)
                continue
            if in_see_also and stripped.startswith("## "):
                in_see_also = False
            if in_see_also:
                match = _SEE_ALSO_LINK_RE.match(stripped)
                if match:
                    target_key = match.group(1)
                    rel = rel_paths.get((own_scope_id, target_key))
                    if rel is not None:
                        out.append(f"- [{target_key}]({rel})")
                    else:
                        out.append(f"- {target_key}")
                        report.links_dropped += 1
                    continue
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _render_topic(topic: _Topic, body: str) -> str:
        """Serialize one topic: fixed-order YAML frontmatter + H1 + body.

        Key order is fixed per D3 (type, title, description, tags,
        timestamp, created); ``description``/``tags`` are emitted only when
        derivable/non-empty. LF endings, single trailing newline, nothing
        run-varying.
        """
        lines = ["---", f"type: {topic.memory_type}", f"title: {topic.key}"]
        if topic.description:
            # json.dumps yields a double-quoted, escape-safe YAML scalar.
            lines.append(f"description: {json.dumps(topic.description)}")
        if topic.tags:
            # json.dumps each tag: quotes/colons/# in a tag value must not be
            # able to break the flow sequence or inject frontmatter keys.
            lines.append("tags: [" + ", ".join(json.dumps(t) for t in topic.tags) + "]")
        lines.append(f"timestamp: {topic.updated_iso}")
        lines.append(f"created: {topic.created_iso}")
        lines.extend(["---", "", f"# {topic.key}"])
        if body:
            lines.extend(["", body])
        return "\n".join(lines).rstrip() + "\n"

    def _render_index(self, topics: List[_Topic]) -> str:
        """Regenerate ``index.md`` in OKF line form — a pure function of the set."""
        lines = ["# Index", ""]
        for topic in sorted(topics, key=lambda t: t.rel_path):
            line = f"* [{topic.key}]({topic.rel_path})"
            if topic.description:
                line += f" - {topic.description}"
            lines.append(line)
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_manifest(scope: str, scope_id: Optional[str]) -> str:
        """Provenance note (D4). Deterministic — no timestamps or run-varying data."""
        lines = [
            "# CAO Memory Export",
            "",
            "Generated by CAO — edits here are not synced back.",
            "",
            "- format: okf",
            f"- scope: {scope}",
        ]
        if scope_id:
            lines.append(f"- scope_id: {scope_id}")
        return "\n".join(lines).rstrip() + "\n"

    # -------------------------------------------------------------------------
    # Idempotent writes + prune (D3)
    # -------------------------------------------------------------------------

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        """Write ``content`` unless the file already holds those exact bytes."""
        data = content.encode("utf-8")
        try:
            if path.exists() and path.read_bytes() == data:
                return False
        except OSError as e:
            # Unreadable existing file: fall through and rewrite it.
            logger.warning("okf_export_compare_failed path=%s: %s", path.name, e)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    def _prune(self, dest: Path, keep_rel: Set[str], report: ExportReport) -> None:
        """Delete destination ``*.md`` files no longer in the exported set.

        Reserved paths (``index.md``, ``manifest.md``) are never pruned. A
        ``history/<rel>`` file is pruned exactly when its topic is gone;
        stale history for a still-exported topic (e.g. this run omitted
        ``include_history``) is kept.
        """
        for path in sorted(dest.rglob("*.md")):
            rel = path.relative_to(dest).as_posix()
            if rel in _RESERVED_FILES or rel in keep_rel:
                continue
            if rel.startswith(f"{_HISTORY_DIR}/"):
                topic_rel = rel[len(_HISTORY_DIR) + 1 :]
                if topic_rel in keep_rel:
                    continue
            try:
                path.unlink()
                report.pruned += 1
            except OSError as e:
                logger.warning("okf_export_prune_failed path=%s: %s", rel, e)


def export_bundle_to_tar(
    backend: MemoryArchiveBackend,
    scope: str,
    scope_id: Optional[str],
    tar_path: Path,
    *,
    include_history: bool = False,
    redact: bool = False,
) -> ExportReport:
    """Export into a temp dir and pack it as ``<tar_path>`` (gzip tarball).

    The ``-o out.tar.gz`` convenience wrapper (D2): the same directory
    writer runs into a temp dir which is then tar'd — no separate archive
    code path. The file target is validated with ``allow_file=True``.
    """
    target = resolve_and_validate_path(
        str(tar_path), allow_create=True, allow_file=True, description="Export archive target"
    )
    if os.path.isdir(target):
        raise ValueError(f"Export archive target is a directory, expected a file path: {tar_path}")

    with tempfile.TemporaryDirectory(prefix="cao-okf-export-") as tmp:
        tmp_path = Path(tmp)
        report = backend.export_bundle(scope, scope_id, tmp_path, include_history, redact)

        # Deterministic tar.gz: stable member ordering/metadata and gzip mtime=0.
        import gzip

        def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mtime = 0
            return ti

        with open(target, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for member in sorted(tmp_path.rglob("*")):
                        if member.is_file():
                            tar.add(
                                member,
                                arcname=member.relative_to(tmp_path).as_posix(),
                                filter=_filter,
                            )
    return report
