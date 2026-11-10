"""#1199 (provisional): CAO's built-in matcher, which picks a helper's CLI and tier locally.

A decider describes the task with four yes/no features; this module combines the routing ones
with the operator's local catalog (``decisions.match``) to pick a (CLI, tier) pair. It is a
pure function: no I/O, no network call, no settings read of its own. It is the reserved
answerer ``match`` for ``provider.route``, the way ``fixed_table`` is built in, but it is not a
plugin and is never resolved through the decider registry.

It can never widen anything. It picks only from the options it is given (provider.route's
options, #1099: already filtered by allowed_providers, the equivalence set, the tools and the
effort), only tiers in the band it is given (never below the size answer, never above the
ceiling), and only models ``model_tiers`` maps; a pick always covers a need, so its model is
one the operator declared. It never sees or changes tools. A defensive check refuses a pick
whose enforcement label would pair advisory with hard or sandboxed.

#810, as amended on 2026-11-05: a decider can never raise a helper's tier, providers or tools.
This match, CAO's own local code, may raise the tier, only to a declared model and only up to
``ephemeral.max_tier``. It never raises providers or tools.

The rule, in order: no needs → ``none_needed``; the candidates are every option × band tier
that ``model_tiers`` maps; the best cover the most important need first (long_context, then
deep_reasoning, then read_only, compared left to right); none covering anything →
``no_cover``; ties go to the lower declared cost, then the lower tier, then the fallback (the
creator's CLI), then the options' order, which is capability-table order.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping

from cli_agent_orchestrator.decisions.catalog import COSTS, MatchCatalog, ModelFacts
from cli_agent_orchestrator.decisions.types import ROUTING_FEATURES
from cli_agent_orchestrator.services.model_tiers import TIERS
from cli_agent_orchestrator.utils.ephemeral_capabilities import ADVISORY, enforcement_label

# The reserved answerer name for provider.route, never resolved through the registry.
MATCH_DECIDER = "match"
MATCH_DECIDER_VERSION = "match-1"
MATCH_RULES = ("none_needed", "covered", "no_cover", "no_features", "catalog_invalid", "rejected")
TIEBREAKS = ("only", "cost", "tier", "fallback", "order")


@dataclass(frozen=True)
class MatchInputs:
    """Everything one match reads, all from one settings snapshot (D5)."""

    # The CLIs this launch may run on, in capability-table order, with the fallback among them.
    options: tuple[str, ...]
    labels: Mapping[str, str]
    fallback: str
    # The band: tiers in order, from max(size, floor) to the ceiling, or the launch's one tier.
    tiers: tuple[str | None, ...]
    # The tier the launch runs without a match: the size answer, or the launch's tier.
    size: str | None
    needs: tuple[str, ...]
    model_tiers: Mapping[str, Mapping[str, str]]
    catalog: MatchCatalog
    # False when the decider gave no usable feature reply at all: the rule is no_features.
    answered: bool = True


@dataclass(frozen=True)
class Candidate:
    provider: str
    tier: str
    model: str
    facts: ModelFacts | None
    cover: tuple[int, ...]


@dataclass(frozen=True)
class MatchResult:
    rule: str
    needs: tuple[str, ...]
    # What the launch runs: the pick when the rule is covered, otherwise the default.
    provider: str
    tier: str | None
    covered: tuple[str, ...] = ()
    tiebreak: str | None = None
    candidates: tuple[Candidate, ...] = field(default=(), compare=False)

    @property
    def picked(self) -> bool:
        return self.rule == "covered"

    def pick_label(self) -> str | None:
        """The record's match_pick: ``"<provider>/<tier>"``, or None when nothing is picked."""
        return f"{self.provider}/{self.tier}" if self.picked else None


def routing_needs(yes: frozenset[str] | set[str], catalog: MatchCatalog) -> tuple[str, ...]:
    """The needs: routing features answered a usable yes, minus ignore_features, in order.

    needs_network is never a need: it is advisory only.
    """
    ignored = catalog.ignore_features if catalog.valid else ()
    return tuple(name for name in ROUTING_FEATURES if name in yes and name not in ignored)


def band(
    size: str | None, floor: str | None, ceiling: str | None, *, raise_allowed: bool
) -> tuple[str | None, ...]:
    """The tiers a match may choose from.

    With a raise allowed (an on-mode size answer), from max(size, floor) up to the ceiling;
    otherwise, or with no ceiling, only the size itself. Never below the size.
    """
    if size is None or size not in TIERS or not raise_allowed or ceiling not in TIERS:
        return (size,)
    start = max(TIERS.index(size), TIERS.index(floor) if floor in TIERS else 0)
    end = TIERS.index(ceiling)
    return TIERS[start : end + 1] if start <= end else (size,)


def _covers(need: str, facts: ModelFacts | None, catalog: MatchCatalog) -> int:
    if facts is None:
        return 0
    if need == "long_context":
        return int(facts.context_tokens >= catalog.long_context_tokens)
    return int(need in facts.good_for)


def candidates(inputs: MatchInputs) -> tuple[Candidate, ...]:
    """Every option × band tier that model_tiers maps, with its cover vector."""
    result = []
    for provider in inputs.options:
        table = inputs.model_tiers.get(provider, {})
        for tier in inputs.tiers:
            if tier is None:
                continue
            model = table.get(tier)
            if not model:
                continue
            facts = inputs.catalog.facts(provider, model)
            cover = tuple(_covers(need, facts, inputs.catalog) for need in inputs.needs)
            result.append(Candidate(provider, tier, model, facts, cover))
    return tuple(result)


def _default(
    rule: str, inputs: MatchInputs, needs: tuple[str, ...], found: tuple[Candidate, ...] = ()
) -> MatchResult:
    return MatchResult(rule, needs, inputs.fallback, inputs.size, candidates=found)


def match(inputs: MatchInputs) -> MatchResult:
    """Pick a (CLI, tier) for the needs, or say why the launch runs its default."""
    if not inputs.catalog.valid:
        return _default("catalog_invalid", inputs, ())
    if not inputs.answered:
        return _default("no_features", inputs, ())
    needs = inputs.needs
    if not needs:
        return _default("none_needed", inputs, needs)
    found = candidates(inputs)
    if not found:
        return _default("no_cover", inputs, needs, found)
    # The cover vectors compare left to right: the most important need first, which is the
    # documented rule, not the most needs covered. With all three needs, a model covering only
    # long_context beats one covering deep_reasoning and read_only. A known limitation (see
    # docs/decisions.md); a rule that covers the most needs is a follow-up. Not changed here.
    best_cover = max(candidate.cover for candidate in found)
    if not any(best_cover):
        return _default("no_cover", inputs, needs, found)
    remaining = [candidate for candidate in found if candidate.cover == best_cover]
    tiebreak = "only"
    if len(remaining) > 1:
        tiebreak, remaining = _break_tie(remaining, inputs)
    (pick,) = remaining
    covered = tuple(need for need, bit in zip(needs, pick.cover) if bit)
    # Defensive: the options already keep advisory and hard or sandboxed apart (#1099).
    fallback_label = inputs.labels.get(inputs.fallback)
    pick_label = inputs.labels.get(pick.provider)
    if (
        pick.provider not in inputs.options
        or pick.tier not in inputs.tiers
        or pick_label is None
        or fallback_label is None
        or (pick_label != fallback_label and ADVISORY in (pick_label, fallback_label))
    ):
        return _default("rejected", inputs, needs, found)
    return MatchResult(
        "covered", needs, pick.provider, pick.tier, covered, tiebreak, candidates=found
    )


def _break_tie(remaining: list[Candidate], inputs: MatchInputs) -> tuple[str, list[Candidate]]:
    """Lower cost, then lower tier, then the fallback, then the options' order."""
    # Every best candidate covers a need, so every one has a declaration and a cost.
    cheapest = min(COSTS.index(c.facts.cost) for c in remaining if c.facts is not None)
    remaining = [
        c for c in remaining if c.facts is not None and COSTS.index(c.facts.cost) == cheapest
    ]
    if len(remaining) == 1:
        return "cost", remaining
    lowest = min(TIERS.index(c.tier) for c in remaining)
    remaining = [c for c in remaining if TIERS.index(c.tier) == lowest]
    if len(remaining) == 1:
        return "tier", remaining
    fallback = [c for c in remaining if c.provider == inputs.fallback]
    if fallback:
        return "fallback", fallback
    first = min(inputs.options.index(c.provider) for c in remaining)
    return "order", [c for c in remaining if inputs.options.index(c.provider) == first]


def explain_rows(result: MatchResult) -> list[dict[str, Any]]:
    """The candidate table ``cao decisions match explain`` prints, in candidate order."""
    return [
        {
            "candidate": f"{c.provider}/{c.tier}",
            "model": c.model,
            "context_tokens": c.facts.context_tokens if c.facts else None,
            "cost": c.facts.cost if c.facts else None,
            "covers": {need: bool(bit) for need, bit in zip(result.needs, c.cover)},
            "pick": result.picked and (c.provider, c.tier) == (result.provider, result.tier),
        }
        for c in result.candidates
    ]


def match_launch(
    settings: Any,
    options: tuple[str, ...],
    labels: Mapping[str, str] | None,
    fallback: str,
    tiers: tuple[str | None, ...],
    size: str | None,
    yes: frozenset[str] | None,
) -> MatchResult:
    """One launch's match from its DecisionSettings snapshot, the only catalog it reads.

    ``labels`` defaults to the capability table's, for a match on one CLI. ``yes`` is the
    features answered a usable yes, or None when the decider gave no usable reply.
    """
    catalog: MatchCatalog = settings.match_catalog
    return match(
        MatchInputs(
            options=options,
            labels=(
                labels
                if labels is not None
                else {provider: enforcement_label(provider) for provider in options}
            ),
            fallback=fallback,
            tiers=tiers,
            size=size,
            needs=routing_needs(yes, catalog) if yes is not None else (),
            model_tiers=settings.model_tiers,
            catalog=catalog,
            answered=yes is not None,
        )
    )


def match_none(settings: Any, fallback: str, rule: str) -> MatchResult:
    """A match that never ran (no_features), unless the catalog is invalid, which says so."""
    catalog: MatchCatalog = settings.match_catalog
    return MatchResult("catalog_invalid" if not catalog.valid else rule, (), fallback, None)


def match_decider() -> dict[str, str]:
    """The decider columns of a provider.route row answered by the matcher."""
    return {"decider": MATCH_DECIDER, "decider_version": MATCH_DECIDER_VERSION}


def record_columns(
    result: MatchResult, settings: Any, *, size: str | None = None, model_row: bool = False
) -> dict[str, Any]:
    """The match columns of one record: closed vocabularies, plus the catalog's text to hash.

    The store hashes ``catalog_canonical`` under the records' key and never stores it.
    """
    catalog: MatchCatalog = settings.match_catalog
    columns: dict[str, Any] = dict(
        match_rule=result.rule,
        match_needs=list(result.needs),
        match_covered=list(result.covered),
        match_tiebreak=result.tiebreak,
        match_pick=result.pick_label(),
        catalog_version=catalog.version,
        catalog_canonical=catalog.canonical(),
    )
    if model_row:
        columns["match_from"] = size
    return columns
