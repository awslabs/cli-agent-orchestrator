"""Wire models for ``GET /annotations`` — the one additive fork seam (§9.5).

Every field here is either the fork's own envelope bookkeeping or an opaque
carrier for something the conductor authored. Nothing in this file enumerates a
kind, a role, a subject type or a facet name, because every such enumeration
would be a second fork change waiting to happen. The types are as wide as the
contract honestly is: ``str`` where the conductor owns the vocabulary, ``int``
where the fork bounds it.

``details`` is ``Dict[str, str]`` rather than ``Dict[str, Any]`` on purpose. The
service stringifies scalars and drops nested structures, so the renderer can
display the bag with no type switch and no risk of a deep object reaching a
template — and §7's "no worker-authored free text" is enforced upstream by the
conductor, with the fork adding a hard length bound as the second line.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class AnnotationSubject(BaseModel):
    """What an annotation is about.

    A subject may name a terminal generation, a task, or a campaign, so an
    unbound gate and an orphaned run both have somewhere to render rather than
    being dropped for want of a terminal to hang on. ``type`` is a free string:
    a fourth subject type must be able to arrive without a fork release, and the
    renderer keeps a terminal-independent surface for anything it cannot attach
    to a projection row.

    ``extra="allow"`` IS THE OTHER HALF OF THAT PROMISE. Placement is durable
    without it; identity is not. A subject type invented later arrives with an
    identifier none of the four named fields hold, and a model that silently
    drops it turns "needs no fork change" into an unattributable chip. The
    reader bounds how many such keys it will carry and how long each may be, so
    allowing them here can admit nothing it did not already accept.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    terminal_id: Optional[str] = None
    #: The generation the annotation was derived against. The renderer fences on
    #: it: an annotation whose generation differs from the row's is dropped, so
    #: a stale claim cannot outlive the obligation that produced it.
    generation: Optional[str] = None
    task_id: Optional[str] = None
    campaign: Optional[str] = None


class AnnotationItem(BaseModel):
    """One chip, exactly as the conductor authored it."""

    namespace: str
    #: Opaque. Nothing in the fork branches on this value, by design.
    kind: str
    #: The producer's own item-schema number. Nothing in the fork reads it, so
    #: an unrecognisable value degrades to 1 rather than costing the item.
    version: int = 1
    label: str
    #: Resolved by the renderer against the six roles in
    #: ``design-tokens/tokens.json``; anything else degrades to ``neutral``.
    semantic_role: str
    priority: int
    subject: AnnotationSubject
    #: When this claim stops being current (§9.6). Past it the renderer greys
    #: the chip: a stale annotation must look stale, never authoritative.
    valid_until: Optional[str] = None
    #: An OPAQUE identity token. The conductor decides what identity it
    #: expresses; the fork only hashes it into a palette slot, so a change of
    #: grouping policy is a conductor change alone.
    #:
    #: THREE STATES, AND THE EMPTY STRING IS ONE OF THEM. Absent means "this
    #: chip is a severity chip, colour it by ``semantic_role``"; ``""`` means
    #: "this is an identity chip that has no colour"; a non-empty value means
    #: "identity chip, colour by the hash". Folding ``""`` into absent would
    #: delete the uncoloured identity rendering, which is why the reader uses
    #: the bounding helper that keeps an empty string rather than the one that
    #: treats it as no value.
    colour_key: Optional[str] = None
    #: Derived facets — task, role, round, parked-for, lifecycle, phase,
    #: dependencies. Bounded in count and length by the reader.
    details: Dict[str, str] = {}
    #: Which conductor project directory published it. A filtered name, never a
    #: path.
    source: Optional[str] = None


class AnnotationSourceReason(BaseModel):
    """Why one source contributed nothing, or contributed less than it holds.

    Deliberately incapable of carrying a path, a payload excerpt or an
    exception string: this dashboard is unauthenticated by default, and a
    diagnostic that echoes the filesystem is an information leak with a
    reassuring name.
    """

    source: str
    reason: str


class AnnotationsResponse(BaseModel):
    """The whole route. Bounded, typed, and never an error surface.

    ``coverage`` is the honest summary an operator reads first:

    * ``complete`` — every source read, nothing dropped, nothing omitted;
    * ``partial`` — a source failed or an item was unrepresentable;
    * ``truncated`` — a cap was hit; the counters say which and by how much;
    * ``unavailable`` — no conductor state root, or it could not be read. This
      is the ordinary state on a machine with no conductor, and it renders
      exactly as the dashboard did before this route existed.
    """

    annotation_schema: str
    coverage: str
    sources_read: int
    sources_failed: int
    items_dropped: int
    items_omitted: int
    #: Facets that did not fit the detail bag, or had no displayable form. The
    #: bag is the only channel the conductor can grow without a fork change, so
    #: a loss there is the one truncation an operator could never detect from
    #: the rendered chip — it is counted, and it moves ``coverage``.
    facets_dropped: int = 0
    reasons: List[AnnotationSourceReason] = []
    annotations: List[AnnotationItem] = []
