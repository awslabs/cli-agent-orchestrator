"""#1199 (provisional): the operator's local model declarations, ``decisions.match``.

The catalog says what each model a CLI runs is good for: its context window, a relative cost
and the task features it suits. CAO's local matcher (``decisions.match``, the module) reads it
to pick a CLI and tier for an ephemeral helper. It is never sent to any decider, and records
carry only its keyed hash.

This module is the catalog's only parser and the only place its canonical form is built, so
the matcher, the CLI and the record hash all see the same validated catalog. A structural
error disables matching for every launch (``catalog_invalid``); a bad entry is dropped with a
warning that names its key path. Either way launches run as they would without matching.

Every leaf is an integer, a closed word, a subset of closed words, a provider id or a model
id that MODEL_ID_RE accepts: there is no free-text field, so nothing needs redacting.
"""

import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from cli_agent_orchestrator.decisions.types import ROUTING_FEATURES
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services.model_tiers import TIERS, valid_model

logger = logging.getLogger(__name__)

CATALOG_VERSION = 1
COSTS = ("low", "medium", "high")
# long_context comes from context_tokens, a fact, so it is never a declared strength;
# needs_network never routes, so it can't be one either.
GOOD_FOR = ("deep_reasoning", "read_only")
TOP_KEYS = ("version", "long_context_tokens", "models", "ignore_features")
DECLARATION_KEYS = ("context_tokens", "cost", "good_for")
REQUIRED_DECLARATION_KEYS = ("context_tokens", "cost")
DEFAULT_LONG_CONTEXT_TOKENS = 500_000
LONG_CONTEXT_TOKENS_RANGE = (1_000, 100_000_000)
CONTEXT_TOKENS_RANGE = (1, 100_000_000)
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._:/-]{1,128}")
_warned: set[str] = set()


@dataclass(frozen=True)
class ModelFacts:
    """One declared model: its window, its relative cost and what it suits."""

    context_tokens: int
    cost: str
    good_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchCatalog:
    """A validated catalog, or an invalid one that disables matching (``invalid`` is set)."""

    version: int | None = CATALOG_VERSION
    long_context_tokens: int = DEFAULT_LONG_CONTEXT_TOKENS
    models: Mapping[str, Mapping[str, ModelFacts]] = field(default_factory=dict)
    ignore_features: tuple[str, ...] = ()
    invalid: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.invalid is None

    def facts(self, provider: str, model: str) -> ModelFacts | None:
        return self.models.get(provider, {}).get(model) if self.valid else None

    def canonical(self) -> str | None:
        """The text the record hash covers: the validated catalog, defaults filled in."""
        if not self.valid:
            return None
        return json.dumps(
            {
                "version": self.version,
                "long_context_tokens": self.long_context_tokens,
                "ignore_features": list(self.ignore_features),
                "models": {
                    provider: {
                        model: {
                            "context_tokens": facts.context_tokens,
                            "cost": facts.cost,
                            "good_for": list(facts.good_for),
                        }
                        for model, facts in entries.items()
                    }
                    for provider, entries in self.models.items()
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def model_count(self) -> int:
        return sum(len(entries) for entries in self.models.values()) if self.valid else 0


def _segment(value: object) -> str:
    # Key paths name settings keys only when they look like ids, never arbitrary text.
    return value if isinstance(value, str) and _SAFE_SEGMENT.fullmatch(value) else "<invalid>"


def _path(*segments: object) -> str:
    return ".".join(("decisions", "match") + tuple(_segment(s) for s in segments))


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning("Decision match catalog: %s", message)


def _is_int(value: object, bounds: tuple[int, int]) -> bool:
    return type(value) is int and bounds[0] <= value <= bounds[1]


def _distinct_words(value: object, words: tuple[str, ...]) -> tuple[str, ...] | None:
    """A list of distinct closed words, in the vocabulary's order, or None if it isn't one."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    if len(set(value)) != len(value) or not set(value) <= set(words):
        return None
    return tuple(word for word in words if word in value)


def _declaration(provider: str, model: str, value: object) -> tuple[ModelFacts | None, str]:
    where = _path("models", provider, model)
    if not isinstance(value, dict):
        return None, f"{where} must be an object; declaration dropped"
    unknown = sorted(set(value) - set(DECLARATION_KEYS), key=str)
    if unknown:
        return None, f"{where}.{_segment(unknown[0])} is not a known key; declaration dropped"
    missing = [key for key in REQUIRED_DECLARATION_KEYS if key not in value]
    if missing:
        return None, f"{where}.{missing[0]} is required; declaration dropped"
    if not _is_int(value["context_tokens"], CONTEXT_TOKENS_RANGE):
        return None, (
            f"{where}.context_tokens must be an integer from 1 to 100000000; declaration dropped"
        )
    if value["cost"] not in COSTS or not isinstance(value["cost"], str):
        return None, f"{where}.cost must be low, medium or high; declaration dropped"
    good_for = _distinct_words(value.get("good_for", []), GOOD_FOR)
    if good_for is None:
        return None, (
            f"{where}.good_for must list distinct deep_reasoning or read_only; "
            "declaration dropped"
        )
    return ModelFacts(value["context_tokens"], value["cost"], good_for), ""


def _invalid(reason: str) -> MatchCatalog:
    _warn_once(reason + "; matching is off for every launch")
    return MatchCatalog(version=None, invalid=reason)


def load_catalog(
    raw: Mapping[str, Any], model_tiers: Mapping[str, Mapping[str, str]] | None = None
) -> MatchCatalog:
    """Validate ``decisions.match`` from one raw settings snapshot.

    An absent block is a valid empty catalog: nothing covers any need, so every launch runs
    as it would without matching. ``model_tiers`` from the same snapshot, when given, names
    declarations no tier maps (unreachable: kept, warned about and never a candidate).
    """
    decisions = raw.get("decisions")
    if not isinstance(decisions, dict) or "match" not in decisions:
        return MatchCatalog()
    block = decisions["match"]
    if not isinstance(block, dict):
        return _invalid("decisions.match must be an object")
    unknown = sorted(set(block) - set(TOP_KEYS), key=str)
    if unknown:
        return _invalid(f"{_path(unknown[0])} is not a known key")
    version = block.get("version")
    if type(version) is not int or version != CATALOG_VERSION:
        return _invalid("decisions.match.version must be 1")
    threshold = block.get("long_context_tokens", DEFAULT_LONG_CONTEXT_TOKENS)
    if not _is_int(threshold, LONG_CONTEXT_TOKENS_RANGE):
        return _invalid(
            "decisions.match.long_context_tokens must be an integer from 1000 to 100000000"
        )
    ignored = _distinct_words(block.get("ignore_features", []), ROUTING_FEATURES)
    if ignored is None:
        return _invalid(
            "decisions.match.ignore_features must list distinct long_context, deep_reasoning "
            "or read_only"
        )
    declared = block.get("models", {})
    if not isinstance(declared, dict):
        return _invalid("decisions.match.models must be an object")
    providers = {p.value for p in ProviderType}
    warnings: list[str] = []
    models: dict[str, dict[str, ModelFacts]] = {}
    for provider, entries in declared.items():
        if provider not in providers:
            warnings.append(f"{_path('models', provider)} is not a CAO provider; dropped")
            continue
        if not isinstance(entries, dict):
            warnings.append(f"{_path('models', provider)} must be an object; dropped")
            continue
        for model, value in entries.items():
            if not valid_model(model):
                warnings.append(
                    f"{_path('models', provider, model)} is not a valid model id; "
                    "declaration dropped"
                )
                continue
            facts, problem = _declaration(provider, model, value)
            if facts is None:
                warnings.append(problem)
                continue
            models.setdefault(provider, {})[model] = facts
    if model_tiers is not None:
        for provider, model in unreachable(models, model_tiers):
            warnings.append(
                f"{_path('models', provider, model)} is unreachable: no model_tiers.{provider} "
                "tier maps it, so it is never used"
            )
    for warning in warnings:
        _warn_once(warning)
    return MatchCatalog(
        version=CATALOG_VERSION,
        long_context_tokens=threshold,
        models=models,
        ignore_features=ignored,
        warnings=tuple(warnings),
    )


def unreachable(
    models: Mapping[str, Mapping[str, ModelFacts]], model_tiers: Mapping[str, Mapping[str, str]]
) -> list[tuple[str, str]]:
    """Declared models that no ``model_tiers`` tier maps, in declaration order."""
    return [
        (provider, model)
        for provider, entries in models.items()
        for model in entries
        if model not in model_tiers.get(provider, {}).values()
    ]


def read_hash_key(path: Path) -> bytes | None:
    """The records' hash key, read only if it already exists; never created or changed."""
    try:
        with path.open("rb") as stream:
            key = stream.read(33)
    except OSError:
        return None
    return key if len(key) == 32 else None


def catalog_hash(catalog: MatchCatalog, key: bytes) -> tuple[str, str] | None:
    """The record's catalog_hash and hash_key_id, as DecisionStore computes them."""
    text = catalog.canonical()
    if text is None:
        return None
    return (
        hmac.new(key, text.encode("utf-8"), sha256).hexdigest(),
        sha256(key).hexdigest()[:8],
    )


def check(
    catalog: MatchCatalog,
    model_tiers: Mapping[str, Mapping[str, str]],
    supported: tuple[str, ...],
) -> tuple[list[tuple[str, str]], bool]:
    """``cao decisions match check``: (level, line) pairs, and whether anything is wrong.

    Wrong (exit 1): an invalid catalog, a dropped entry or an unreachable declaration. Notes
    (exit 0): a mapped model with no declaration, a provider that can't run ephemeral
    helpers, and which declared models cover long_context.
    """
    if not catalog.valid:
        return [("error", f"catalog invalid: {catalog.invalid}")], True
    lines: list[tuple[str, str]] = []
    problems = False
    for warning in catalog.warnings:
        if " is unreachable: " not in warning:
            lines.append(("WARN", warning))
            problems = True
    lost = unreachable(catalog.models, model_tiers)
    for provider, model in lost:
        lines.append(
            (
                "WARN",
                f"{provider}/{model} is unreachable: no model_tiers.{provider} tier maps it, "
                "so it is never used",
            )
        )
        problems = True
    for provider in sorted(set(catalog.models) | set(model_tiers)):
        table = model_tiers.get(provider, {})
        declared = catalog.models.get(provider, {})
        reached = [
            f"{model} ({tier})"
            for tier in TIERS
            for model in (table.get(tier),)
            if model is not None and model in declared
        ]
        if reached:
            lines.append(("ok", f"{provider}: {', '.join(reached)} declared and reachable"))
        for tier in TIERS:
            mapped = table.get(tier)
            if mapped is not None and mapped not in declared:
                lines.append(
                    (
                        "note",
                        f"{provider}/{tier} maps {mapped}, which has no declaration, so it "
                        "covers nothing",
                    )
                )
        if declared and provider not in supported:
            lines.append(
                (
                    "note",
                    f"{provider} can't run ephemeral helpers, so its declarations are never "
                    "candidates",
                )
            )
    covering = [
        f"{provider}/{model}"
        for provider, entries in catalog.models.items()
        for model, facts in entries.items()
        if facts.context_tokens >= catalog.long_context_tokens and (provider, model) not in lost
    ]
    lines.append(
        (
            "note",
            f"long_context ({catalog.long_context_tokens}): covered by "
            + (", ".join(covering) if covering else "no reachable declared model"),
        )
    )
    return lines, problems


def status_view(effective: dict[str, Any], snapshot: Any) -> dict[str, Any]:
    """``cao decisions status``: replace the catalog body with a summary, never its content.

    ``snapshot`` is the DecisionSettings that ``effective`` was built from.
    """
    catalog: MatchCatalog = snapshot.match_catalog
    effective.pop("match_catalog", None)
    effective["match"] = {
        "catalog_valid": catalog.valid,
        "catalog_version": catalog.version,
        "models": catalog.model_count(),
        "warnings": len(catalog.warnings),
        "ignore_features": list(catalog.ignore_features),
    }
    return effective
