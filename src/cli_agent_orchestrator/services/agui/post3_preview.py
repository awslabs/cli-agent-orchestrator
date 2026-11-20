"""Post-3.0 AG-UI previews behind one opt-in flag (#1162, #1164, #1194).

Three read-only previews of work that is otherwise blocked on the CAO 3.0 tenant
model (#778), the tenant audit store (#782) and A2UI 1.0:

* **Team composition** (#1162): ``agent_card`` and ``metric`` props projected from the
  fleet snapshot, scoped to the one implicit local tenant.
* **Decision log** (#1164): approve, deny and edit decisions appended to a local log
  keyed by tenant, and a read-only summary of it as a feedback signal.
* **A2UI surface** (#1194): one allow-listed ``emit_ui`` component (``metric``)
  rendered as an A2UI v0.9.1 surface, carried on ``ACTIVITY_SNAPSHOT``.

The whole module sits behind ``CAO_AGUI_POST3_PREVIEW``. With the flag unset the
routes answer 404 and the approval route writes nothing, so the server's behavior is
unchanged. Nothing here adds a component to the generative-UI allow-list, and nothing
takes a tenant from a request: there is one tenant, ``LOCAL_TENANT_ID``.

What changes when the blockers land: see ``docs/agui.md`` (Post-3.0 previews).
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from cli_agent_orchestrator.services.agui_stream import (
    _MAX_GENERATIVE_PROPS_BYTES,
    GENERATIVE_UI_COMPONENTS,
)

PREVIEW_ENV = "CAO_AGUI_POST3_PREVIEW"
_TRUTHY = ("1", "true", "yes")

#: The implicit tenant of a laptop install. Never read from a request.
LOCAL_TENANT_ID = "local"


def post3_preview_enabled() -> bool:
    """Whether the post-3.0 previews are live. Env-only, read at call time."""
    return os.environ.get(PREVIEW_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# (a) Team composition projection (#1162)
# ---------------------------------------------------------------------------

#: Upper bound on cards, so the projection stays small however large the fleet is.
MAX_TEAM_MEMBERS = 50


def _checked(component: str, props: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{component, props}`` after checking the allow-list and size cap."""
    if component not in GENERATIVE_UI_COMPONENTS:  # pragma: no cover - guards edits
        raise ValueError(f"{component!r} is not an allow-listed component")
    if len(json.dumps(props).encode("utf-8")) > _MAX_GENERATIVE_PROPS_BYTES:
        raise ValueError("props payload too large")  # pragma: no cover - fixed small shapes
    return {"component": component, "props": props}


def build_team_composition(
    snapshot: Dict[str, Any], profiles: Optional[Iterable[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Project the fleet snapshot into ``agent_card`` and ``metric`` props.

    Pure and read-only. Only fields already in the snapshot and the profile catalog
    are used (names, provider, status, role); no prompt, output or path is read.
    There is no cost metric because CAO records no cost per terminal.
    """
    catalog = {p.get("name"): p for p in (profiles or []) if isinstance(p, dict)}
    terminals = [t for t in snapshot.get("terminals", []) if isinstance(t, dict)]
    members: List[Dict[str, Any]] = []
    for t in terminals[:MAX_TEAM_MEMBERS]:
        profile = t.get("agent_profile")
        props: Dict[str, str] = {
            "name": str(profile or t.get("id") or "unknown"),
            "provider": str(t.get("provider") or "unknown"),
            "status": str(t.get("status") or "unknown"),
        }
        entry = catalog.get(profile) if profile else None
        if entry and entry.get("role"):
            props["role"] = str(entry["role"])
        members.append(_checked("agent_card", props))
    providers = {str(t.get("provider") or "unknown") for t in terminals}
    metrics = [
        _checked("metric", {"label": "members", "value": str(len(terminals))}),
        _checked("metric", {"label": "providers", "value": str(len(providers))}),
    ]
    return {
        "preview": True,
        "members": members,
        "metrics": metrics,
        "truncated": len(terminals) > MAX_TEAM_MEMBERS,
    }


# ---------------------------------------------------------------------------
# (b) Approval decision log (#1164)
# ---------------------------------------------------------------------------

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DECISIONS = ("approve", "deny", "edit")
#: The decisions a resume can commit; anything else (``"expired"``) is not logged.
DECISIONS = frozenset(_DECISIONS)
_ROTATE_BYTES = 1024 * 1024
_RECENT = 20


class DecisionLog:
    """Append-only, tenant-keyed JSON-lines log of approval decisions.

    One file per tenant under ``<base>/<tenant>.jsonl``, created owner-only. A record
    holds the interrupt id, terminal id, provider, decision, whether the text was
    edited, and the outcome. It never holds the edited text or the prompt. When the
    file passes 1 MiB it moves to ``<tenant>.jsonl.1`` (one generation kept) and reads cover both files.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)
        self._lock = threading.Lock()

    def _path(self, tenant_id: str) -> Path:
        if not _TENANT_RE.match(tenant_id or ""):
            raise ValueError("invalid tenant id")
        return self._base / f"{tenant_id}.jsonl"

    def append(
        self,
        tenant_id: str,
        *,
        interrupt_id: str,
        terminal_id: Optional[str],
        provider: Optional[str],
        decision: str,
        edited: bool,
        outcome: Optional[str],
    ) -> None:
        if decision not in _DECISIONS:
            raise ValueError(f"decision must be one of {list(_DECISIONS)}")
        path = self._path(tenant_id)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "interrupt_id": str(interrupt_id)[:128],
            "terminal_id": None if terminal_id is None else str(terminal_id)[:128],
            "provider": None if provider is None else str(provider)[:64],
            "decision": decision,
            "edited": bool(edited),
            "outcome": None if outcome is None else str(outcome)[:64],
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            if path.exists() and path.stat().st_size > _ROTATE_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)  # tighten a file that already existed with a looser mode
            # A crash can leave a torn last line with no newline. Start this record
            # on its own line, or it would be joined to the fragment and skipped.
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = "\n" + line
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)

    def records(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Records from both generations, oldest first, one per interrupt id.

        A corrupt line is skipped. When two lines carry the same interrupt id (two
        first resumes racing), the earlier one counts and the later one is ignored.
        """
        path = self._path(tenant_id)
        with self._lock:
            text = ""
            for p in (path.with_name(path.name + ".1"), path):
                if p.exists():
                    text += p.read_text(encoding="utf-8", errors="replace")
                    if text and not text.endswith("\n"):
                        text += "\n"
        out: List[Dict[str, Any]] = []
        seen = set()
        for raw in text.splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if (
                not isinstance(rec, dict)
                or rec.get("tenant_id") != tenant_id
                or rec.get("decision") not in _DECISIONS
                or not isinstance(rec.get("interrupt_id"), str)
                or not all(
                    isinstance(rec.get(k), (str, type(None)))
                    for k in ("provider", "terminal_id", "outcome")
                )
            ):
                continue  # corrupt, or a field this reader keys on has the wrong type
            iid = rec.get("interrupt_id")
            if iid in seen:
                continue
            seen.add(iid)
            out.append(rec)
        return out

    def summary(self, tenant_id: str) -> Dict[str, Any]:
        """Read-only feedback signal: counts by decision and provider, plus recent."""
        recs = self.records(tenant_id)
        by_decision = {d: 0 for d in _DECISIONS}
        by_provider: Dict[str, int] = {}
        for r in recs:
            by_decision[r["decision"]] = by_decision.get(r["decision"], 0) + 1
            key = r.get("provider") or "unknown"
            by_provider[key] = by_provider.get(key, 0) + 1
        return {
            "preview": True,
            "total": len(recs),
            "by_decision": by_decision,
            "by_provider": by_provider,
            "recent": recs[-_RECENT:],
        }


def get_decision_log() -> DecisionLog:
    """The decision log under the CAO home directory (resolved at call time)."""
    from cli_agent_orchestrator import constants

    return DecisionLog(constants.CAO_HOME_DIR / "agui" / "decisions")


def record_decision(
    interrupt: Any, decision: str, edited_text: Optional[str], outcome: Optional[str]
) -> None:
    """Append one decision for the local tenant. Never raises into the caller."""
    import logging

    try:
        meta = getattr(interrupt, "metadata", None) or {}
        get_decision_log().append(
            LOCAL_TENANT_ID,
            interrupt_id=getattr(interrupt, "id", ""),
            terminal_id=meta.get("terminal_id"),
            provider=meta.get("provider"),
            decision=decision,
            edited=edited_text is not None,
            outcome=outcome,
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "agui preview: decision log write failed", exc_info=True
        )


# ---------------------------------------------------------------------------
# (c) One allow-listed component as an A2UI surface (#1194)
# ---------------------------------------------------------------------------

A2UI_VERSION = "v0.9.1"
#: The Basic Catalog id from the A2UI v0.9.1 specification.
A2UI_BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
ACTIVITY_TYPE = "a2ui-surface"
#: The only components the preview can render. A subset of ``GENERATIVE_UI_COMPONENTS``.
A2UI_RENDERABLE = frozenset({"metric"})

_SURFACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEXT_MAX = 120
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f<>\[\]()`]")
_URLISH = re.compile(
    r"(?i)//|www\.|\b(?:https?|ftp|file|javascript|data|vbscript|mailto|blob|ws|wss|tel|sms):"
)


def clean_text(value: Any) -> str:
    """Make one prop value safe to show as A2UI ``Text``.

    The renderer may parse Markdown, so link syntax and anything URL-shaped is removed
    rather than escaped: a value that looks like a URL becomes empty.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = _UNSAFE_CHARS.sub("", str(value)).strip()
    if _URLISH.search(text):
        return ""
    return text[:_TEXT_MAX]


class A2uiRefusal(ValueError):
    """The component cannot be rendered as an A2UI surface."""


def to_a2ui_operations(
    component: str, props: Dict[str, Any], surface_id: str = "cao-metric"
) -> List[Dict[str, Any]]:
    """Return the A2UI v0.9.1 messages that render an allow-listed ``metric``.

    Raises ``A2uiRefusal`` for an off-list component, an allow-listed component the
    preview cannot render, a bad surface id, or a metric with no usable label or value.
    """
    if component not in GENERATIVE_UI_COMPONENTS:
        raise A2uiRefusal(f"component {component!r} is not on the emit_ui allow-list")
    if component not in A2UI_RENDERABLE:
        raise A2uiRefusal(f"component {component!r} has no A2UI rendering in this preview")
    if not _SURFACE_RE.match(surface_id or ""):
        raise A2uiRefusal("invalid surface id")
    label = clean_text(props.get("label"))
    value = clean_text(props.get("value"))
    if not label or not value:
        raise A2uiRefusal("metric needs a plain-text label and value")
    unit = clean_text(props.get("unit"))
    shown = f"{value} {unit}".strip()
    return [
        {
            "version": A2UI_VERSION,
            "createSurface": {"surfaceId": surface_id, "catalogId": A2UI_BASIC_CATALOG_ID},
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {"id": "root", "component": "Card", "child": "body"},
                    {"id": "body", "component": "Column", "children": ["label", "value"]},
                    {"id": "label", "component": "Text", "text": label, "variant": "caption"},
                    {"id": "value", "component": "Text", "text": shown, "variant": "h3"},
                ],
            },
        },
    ]


def activity_snapshot(operations: List[Dict[str, Any]], message_id: str) -> Dict[str, Any]:
    """Wrap A2UI messages as the ``ACTIVITY_SNAPSHOT`` frame ag-ui's A2UI middleware emits."""
    return {
        "type": "ACTIVITY_SNAPSHOT",
        "messageId": message_id,
        "activityType": ACTIVITY_TYPE,
        "content": {"a2ui_operations": operations},
        "replace": True,
    }


_RENDERABLE_IN_REFERENCE = frozenset({"Card", "Column", "Text"})


def render_text(operations: List[Dict[str, Any]]) -> str:
    """Reference renderer for the Basic Catalog subset this preview emits.

    Understands ``createSurface`` and ``updateComponents`` with ``Card``, ``Column`` and
    ``Text`` only, and refuses any other catalog, message or component. It stands in for
    a real A2UI renderer so the surface can be checked without a browser.
    """
    created: Dict[str, bool] = {}
    components: Dict[str, Dict[str, Any]] = {}
    for msg in operations:
        if msg.get("version") != A2UI_VERSION:
            raise A2uiRefusal("unsupported A2UI version")
        if "createSurface" in msg:
            cs = msg["createSurface"]
            if cs.get("catalogId") != A2UI_BASIC_CATALOG_ID:
                raise A2uiRefusal("unsupported catalog")
            created[cs["surfaceId"]] = True
        elif "updateComponents" in msg:
            uc = msg["updateComponents"]
            if not created.get(uc.get("surfaceId")):
                raise A2uiRefusal("updateComponents before createSurface")
            for comp in uc["components"]:
                if comp.get("component") not in _RENDERABLE_IN_REFERENCE:
                    raise A2uiRefusal(f"component {comp.get('component')!r} is not renderable")
                components[comp["id"]] = comp
        else:
            raise A2uiRefusal("unsupported A2UI message")
    if "root" not in components:
        raise A2uiRefusal("no root component")

    def draw(cid: str) -> List[str]:
        comp = components[cid]
        kind = comp["component"]
        if kind == "Text":
            return [str(comp.get("text", ""))]
        kids = [comp["child"]] if kind == "Card" else list(comp.get("children", []))
        return [line for k in kids for line in draw(k)]

    return "\n".join(draw("root"))


__all__ = [
    "A2UI_BASIC_CATALOG_ID",
    "A2UI_RENDERABLE",
    "A2UI_VERSION",
    "ACTIVITY_TYPE",
    "A2uiRefusal",
    "DecisionLog",
    "LOCAL_TENANT_ID",
    "MAX_TEAM_MEMBERS",
    "PREVIEW_ENV",
    "activity_snapshot",
    "build_team_composition",
    "clean_text",
    "get_decision_log",
    "post3_preview_enabled",
    "record_decision",
    "render_text",
    "to_a2ui_operations",
]
