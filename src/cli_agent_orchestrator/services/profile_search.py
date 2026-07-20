"""Profile discovery search — keyword/BM25 ranking over profile metadata.

Backs both ``cao profile find`` (CLI) and the ``find_profiles`` MCP tool.
Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340

Design constraints (v1):
- Read-only: matches on metadata only. Files are read to parse frontmatter,
  but the prompt body is never indexed, matched against, or returned.
- Ephemeral: the corpus is rebuilt from ``list_agent_profiles()`` on every
  query. Profile counts are small (tens), so a persistent index would only
  add staleness risk (profiles are installed/removed outside CAO's control).
- BM25 via ``rank_bm25`` (same lazy-import + graceful-degradation pattern as
  ``memory_service``). If the library is unavailable, falls back to simple
  token-overlap scoring so ``find`` still works.
"""

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Fields included in each search result. The profile prompt body is never
# indexed or returned — discovery is metadata-only by design.
RESULT_FIELDS = ("name", "description", "capabilities", "tags", "role", "source")

DEFAULT_LIMIT = 10


def _tokenize(text: str) -> List[str]:
    """Lowercase, split on non-alphanumeric, drop empties."""
    return [t for t in re.split(r"[^a-zA-Z0-9]+", text.lower()) if t]


def _searchable_text(profile: Dict) -> str:
    """Concatenate the metadata fields a profile is discoverable by.

    Tolerates malformed inputs (``None`` description, non-list tags) so raw
    callers can't trigger a TypeError; the real pipeline already normalizes
    via ``_discovery_fields``. Tokenization is ASCII-alphanumeric, matching
    the memory search tokenizer.
    """
    tags = profile.get("tags")
    capabilities = profile.get("capabilities")
    parts = [
        str(profile.get("name") or ""),
        str(profile.get("description") or ""),
        " ".join(str(t) for t in tags) if isinstance(tags, list) else "",
        " ".join(str(c) for c in capabilities) if isinstance(capabilities, list) else "",
    ]
    return " ".join(p for p in parts if p)


def _result(profile: Dict, score: float, coverage: int = 0) -> Dict:
    """Shape a profile into the shared CLI/MCP result contract.

    ``tags``/``capabilities`` are always lists of strings in the contract,
    even if a raw caller passed malformed values (the real pipeline already
    normalizes via ``_discovery_fields``).

    ``coverage`` is the number of distinct query terms the profile matched.
    ``score`` is ``coverage`` plus a BM25 tie-break fraction in [0, 1), so
    descending score always agrees with the relevance ordering.
    """
    out = {field: profile.get(field, "") for field in RESULT_FIELDS}
    desc = profile.get("description")
    out["description"] = desc if isinstance(desc, str) else ""
    tags = profile.get("tags")
    capabilities = profile.get("capabilities")
    out["tags"] = [str(t) for t in tags] if isinstance(tags, list) else []
    out["capabilities"] = [str(c) for c in capabilities] if isinstance(capabilities, list) else []
    out["coverage"] = int(coverage)
    out["score"] = round(float(score), 4)
    return out


def _overlap_scores(query_tokens: List[str], corpus_tokens: List[List[str]]) -> List[float]:
    """Fallback scoring when rank_bm25 is unavailable: unique-term overlap count."""
    query_set = set(query_tokens)
    return [float(len(query_set & set(doc))) for doc in corpus_tokens]


def search_profiles(
    query: str,
    limit: int = DEFAULT_LIMIT,
    profiles: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Rank available agent profiles against ``query``.

    Args:
        query: Free-text keywords (e.g. "monitor sqs").
        limit: Maximum number of results to return.
        profiles: Optional pre-fetched profile list (tests); defaults to
            ``list_agent_profiles()``.

    Returns:
        Metadata-only result dicts sorted by descending relevance. Profiles
        with no token in common with the query are excluded, as are profiles
        that ``load_agent_profile()`` would reject. Each result carries
        ``coverage`` (distinct query terms matched) and ``score`` (coverage
        plus a BM25 tie-break fraction below 1, so descending score matches
        the result order). Never includes the profile prompt body.
    """
    query_tokens = _tokenize(query)
    if not query_tokens or limit <= 0:
        return []

    if profiles is None:
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        profiles = list_agent_profiles()
    # Exclude profiles that load_agent_profile() would reject: frontmatter
    # parse failures, metadata that fails AgentProfile model validation, or a
    # profile directory without agent.md. Recommending any of these would
    # break discover-then-load consistency for handoff/assign.
    profiles = [p for p in profiles if p.get("loadable", True)]
    if not profiles:
        return []

    corpus_tokens = [_tokenize(_searchable_text(p)) for p in profiles]
    if not any(corpus_tokens):
        # Every profile tokenized to empty text; BM25 would divide by zero
        # (avgdl == 0) and nothing could match anyway.
        return []

    try:
        from rank_bm25 import BM25Plus  # type: ignore[import-untyped]

        # BM25Plus lower-bounds term contributions so scores stay positive.
        # Plain BM25Okapi produces zero/negative IDF for terms present in most
        # of a small corpus, which can rank a profile matching every query
        # term below partial matches.
        scores = list(BM25Plus(corpus_tokens).get_scores(query_tokens))
    except ImportError:
        logger.debug("rank_bm25 not installed; using token-overlap fallback")
        scores = _overlap_scores(query_tokens, corpus_tokens)

    # Rank primarily by query-term coverage (how many distinct query tokens
    # the profile matches), with BM25 as the tie-breaker. This guarantees a
    # profile matching more of the query never ranks below a partial match,
    # regardless of corpus-level IDF effects.
    query_set = set(query_tokens)
    matched = [
        (profile, len(query_set & set(doc_tokens)), score)
        for profile, score, doc_tokens in zip(profiles, scores, corpus_tokens)
        if query_set & set(doc_tokens)
    ]
    matched.sort(key=lambda item: (-item[1], -item[2], item[0].get("name", "")))
    if not matched:
        return []
    # The returned score must agree with this ordering (a caller taking the
    # max score must pick the top-ranked profile), so encode coverage as the
    # integer part and the BM25 tie-break as a fraction strictly below 1:
    #     score = coverage + bm25 / (1 + max_bm25)
    # A profile matching more query terms therefore always scores above any
    # narrower match, and within equal coverage the BM25 order is preserved.
    # Like raw BM25, scores are relative to the result set, not absolute.
    max_bm25 = max(item[2] for item in matched)
    return [
        _result(profile, coverage + bm25 / (1.0 + max_bm25), coverage)
        for profile, coverage, bm25 in matched[:limit]
    ]
