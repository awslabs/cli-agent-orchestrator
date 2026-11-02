"""#1199 (provisional): CAO's built-in matcher, which picks a helper's CLI and tier locally.

A decider describes the task with four yes/no features; this module combines the routing ones
with the operator's local catalog (``decisions.catalog``) to pick a (CLI, tier) pair. It is a
pure function: no I/O, no network call, no settings read of its own. It is the reserved
answerer ``match`` for ``provider.route``, the way ``fixed_table`` is built in, but it is not a
plugin and is never resolved through the decider registry.

It can never widen anything. It picks only from the options it is given (#1174's, already
filtered by allowed_providers, the equivalence set, the tools and the effort), only tiers in
the band it is given (never below the size answer, never above the ceiling), and only models
``model_tiers`` maps. It never sees or changes tools. A defensive check refuses a pick whose
enforcement label would pair advisory with hard or sandboxed.

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
from cli_agent_orchestrator.utils.ephemeral_capabilities import ADVISORY

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
    tiers: tuple[str, ...]
    # The tier the launch runs without a match: the size answer, or the launch's tier.
    size: str
    needs: tuple[str, ...]
    model_tiers: Mapping[str, Mapping[str, str]]
    catalog: MatchCatalog


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
    tier: str
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
    size: str, floor: str | None, ceiling: str | None, *, raise_allowed: bool
) -> tuple[str, ...]:
    """The tiers a match may choose from.

    With a raise allowed (an on-mode size answer), from max(size, floor) up to the ceiling;
    otherwise, or with no ceiling, only the size itself. Never below the size.
    """
    if not raise_allowed or ceiling is None or size not in TIERS or ceiling not in TIERS:
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
    needs = inputs.needs
    if not needs:
        return _default("none_needed", inputs, needs)
    found = candidates(inputs)
    if not found:
        return _default("no_cover", inputs, needs, found)
    best_cover = max(candidate.cover for candidate in found)
    if not any(best_cover):
        return _default("no_cover", inputs, needs, found)
    remaining = [candidate for candidate in found if candidate.cover == best_cover]
    tiebreak = "only"
    if len(remaining) > 1:
        tiebreak, remaining = _break_tie(remaining, inputs)
    (pick,) = remaining
    covered = tuple(need for need, bit in zip(needs, pick.cover) if bit)
    # Defensive: the options already keep advisory and hard or sandboxed apart (#1174).
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


def explain_rows(result: MatchResult, inputs: MatchInputs) -> list[dict[str, Any]]:
    """The candidate table ``cao decisions match explain`` prints, in candidate order."""
    return [
        {
            "candidate": f"{c.provider}/{c.tier}",
            "model": c.model,
            "context_tokens": c.facts.context_tokens if c.facts else None,
            "cost": c.facts.cost if c.facts else None,
            "covers": {need: bool(bit) for need, bit in zip(inputs.needs, c.cover)},
            "pick": result.picked and (c.provider, c.tier) == (result.provider, result.tier),
        }
        for c in result.candidates
    ]
