"""In-tree lifecycle eligibility, not confidentiality or authorization.

Sources fail open independently. Vault binding policy and provider redaction
remain separate, fail-closed confidentiality boundaries. Registry entries are
trusted in-tree code, not implicitly discovered third-party plugins.
"""

import logging
import re
from dataclasses import dataclass
from itertools import islice
from typing import (
    Callable,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
)

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
EligibilityPurpose = Literal["injected_context", "curated_recall"]
T = TypeVar("T")


class MemoryIdentity(NamedTuple):
    key: str
    scope: str
    scope_id: Optional[str]
    source_kind: str


@dataclass(frozen=True)
class EligibilityRequest:
    purpose: EligibilityPurpose
    terminal_id: Optional[str]
    session_name: Optional[str]
    agent_profile: Optional[str]
    provider: Optional[str]
    cwd: Optional[str]

    @classmethod
    def from_context(
        cls, purpose: EligibilityPurpose, terminal_context: Optional[dict]
    ) -> "EligibilityRequest":
        context = terminal_context or {}
        return cls(
            purpose,
            context.get("terminal_id"),
            context.get("session_name"),
            context.get("agent_profile"),
            context.get("provider"),
            context.get("cwd"),
        )


class EligibilitySource(Protocol):
    name: str

    def is_enabled(self) -> bool: ...

    def excluded(
        self, request: EligibilityRequest, candidates: Sequence[MemoryIdentity]
    ) -> Mapping[MemoryIdentity, str]: ...


_SOURCES: dict[str, EligibilitySource] = {}


def _failed_open(
    name: str,
    request: EligibilityRequest,
    count: int,
    errors: list[str],
    *,
    purpose: Optional[str] = None,
) -> None:
    if name not in errors:
        errors.append(name)
    logger.warning(
        "memory_eligibility_failed_open source=%s purpose=%s candidates=%d",
        name,
        purpose or request.purpose,
        count,
    )


class EligibilityCheck:
    """One check per build/curator recall; verdicts never survive the call."""

    def __init__(
        self,
        request: EligibilityRequest,
        sources: Sequence[EligibilitySource],
        errors: Optional[list[str]] = None,
    ):
        self.request = request
        self._sources = tuple(sources)
        self.errors = list(errors or [])
        self._memo: dict[MemoryIdentity, tuple[str, ...]] = {}
        self._origins: dict[MemoryIdentity, set[str]] = {}

    def report_source_failure(
        self, name: str, count: int, *, purpose: Optional[str] = None
    ) -> None:
        """Observe one failed read without disabling independent later candidate batches."""
        _failed_open(name, self.request, count, self.errors, purpose=purpose)

    def use_superseded_snapshot(
        self, snapshot: set, *, failed_groups: Optional[set] = None
    ) -> None:
        """Use a call-local supersedes snapshot for diagnostic verdicts, not a fresh read."""
        self._sources = tuple(
            (
                _SnapshotSupersedesSource(snapshot, failed_groups)
                if type(source) is SupersedesSource
                else source
            )
            for source in self._sources
        )

    def reasons(
        self, candidates: Sequence[MemoryIdentity]
    ) -> dict[MemoryIdentity, tuple[str, ...]]:
        pending = list(
            dict.fromkeys(identity for identity in candidates if identity not in self._memo)
        )
        if pending:
            verdicts: dict[MemoryIdentity, list[str]] = {identity: [] for identity in pending}
            for source in self._sources:
                try:
                    excluded = dict(source.excluded(self.request, pending))
                    if any(
                        not isinstance(reason, str) or not _TOKEN.fullmatch(reason)
                        for reason in excluded.values()
                    ):
                        raise ValueError("invalid eligibility reason token")
                    for identity, reason in excluded.items():
                        if identity in verdicts:
                            verdicts[identity].append(reason)
                            self._origins.setdefault(identity, set()).add(source.name)
                except Exception:
                    _failed_open(source.name, self.request, len(pending), self.errors)
            self._memo.update({identity: tuple(reasons) for identity, reasons in verdicts.items()})
        return {
            identity: self._memo[identity] for identity in candidates if self._memo.get(identity)
        }

    def eligible_in_order(
        self, items: Iterable[T], identity: Callable[[T], MemoryIdentity], chunk: int
    ) -> Iterator[T]:
        if chunk <= 0:
            raise ValueError("eligibility chunk must be positive")
        iterator = iter(items)
        while batch := list(islice(iterator, chunk)):
            reasons = self.reasons([identity(item) for item in batch])
            for item in batch:
                if identity(item) not in reasons:
                    yield item

    def exclusion_summary(self, scope: str, scope_id: Optional[str]) -> tuple[int, list[str]]:
        excluded = [
            identity
            for identity, reasons in self._memo.items()
            if reasons and identity.scope == scope and identity.scope_id == scope_id
        ]
        names = set().union(*(self._origins.get(identity, set()) for identity in excluded))
        return len(excluded), [source.name for source in self._sources if source.name in names]

    def exclusion_summary_for_scope(self, scope: str) -> tuple[int, list[str]]:
        """Aggregate curator exclusions across logical scope IDs without logging them."""
        excluded = [
            identity
            for identity, reasons in self._memo.items()
            if reasons and identity.scope == scope
        ]
        names = set().union(*(self._origins.get(identity, set()) for identity in excluded))
        return len(excluded), [source.name for source in self._sources if source.name in names]


def register_source(source: EligibilitySource) -> None:
    if not isinstance(source.name, str) or not _TOKEN.fullmatch(source.name):
        raise ValueError("invalid eligibility source name")
    if source.name in _SOURCES:
        raise ValueError("duplicate eligibility source name")
    _SOURCES[source.name] = source


def unregister_source(name: str) -> None:
    """Remove an in-tree source; intended for tests."""
    _SOURCES.pop(name, None)


def begin_check(request: EligibilityRequest) -> Optional[EligibilityCheck]:
    enabled = []
    errors: list[str] = []
    for source in tuple(_SOURCES.values()):
        try:
            if source.is_enabled():
                enabled.append(source)
        except Exception:
            _failed_open(source.name, request, 0, errors)
    return EligibilityCheck(request, enabled, errors) if enabled else None


class SupersedesSource:
    """Same-scope active supersedes targets; failures propagate to the check."""

    name = "lifecycle.supersedes"

    def is_enabled(self) -> bool:
        from cli_agent_orchestrator.services.settings_service import is_memory_lifecycle_enabled

        return is_memory_lifecycle_enabled()

    def excluded(
        self, request: EligibilityRequest, candidates: Sequence[MemoryIdentity]
    ) -> Mapping[MemoryIdentity, str]:
        from cli_agent_orchestrator.services.memory_relationship_service import (
            MemoryRelationshipService,
        )

        groups: dict[tuple[str, Optional[str]], list[MemoryIdentity]] = {}
        for identity in candidates:
            groups.setdefault((identity.scope, identity.scope_id), []).append(identity)
        result: dict[MemoryIdentity, str] = {}
        service = MemoryRelationshipService()
        for (scope, scope_id), members in groups.items():
            targets = service.superseded_targets(
                scope, scope_id, [identity.key for identity in members]
            )
            result.update(
                {identity: "superseded" for identity in members if identity.key in targets}
            )
        return result


class _SnapshotSupersedesSource(SupersedesSource):
    """A per-check immutable view of an already resolved graph snapshot."""

    def __init__(self, snapshot: set, failed_groups: Optional[set] = None):
        self._snapshot = frozenset(snapshot)
        self._failed_groups = frozenset(failed_groups or ())

    def excluded(
        self, request: EligibilityRequest, candidates: Sequence[MemoryIdentity]
    ) -> Mapping[MemoryIdentity, str]:
        return {
            identity: "superseded"
            for identity in candidates
            if (identity.scope, identity.scope_id) not in self._failed_groups
            and (identity.scope, identity.scope_id, identity.key) in self._snapshot
        }


register_source(SupersedesSource())
