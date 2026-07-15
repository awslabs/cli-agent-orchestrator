"""Profile discovery search — keyword/BM25 ranking over profile metadata.

Backs both ``cao profile find`` (CLI) and the ``find_profiles`` MCP tool.
Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340

Design constraints (v1):
- Read-only: searches metadata only; never reads or returns prompt bodies.
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

# Fields included in each search result. The profile prompt body is NEVER
# read or returned — discovery is metadata-only by design.
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


def _result(profile: Dict, score: float) -> Dict:
    """Shape a profile into the shared CLI/MCP result contract."""
    out = {field: profile.get(field, "") for field in RESULT_FIELDS}
    out["capabilities"] = profile.get("capabilities") or []
    out["tags"] = profile.get("tags") or []
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
        with no token in common with the query are excluded. Never includes
        the profile prompt body.
    """
    query_tokens = _tokenize(query)
    if not query_tokens or limit <= 0:
        return []

    if profiles is None:
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        profiles = list_agent_profiles()
    if not profiles:
        return []

    corpus_tokens = [_tokenize(_searchable_text(p)) for p in profiles]
    if not any(corpus_tokens):
        # Every profile tokenized to empty text; BM25Okapi would divide by
        # zero (avgdl == 0) and nothing could match anyway.
        return []

    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

        scores = list(BM25Okapi(corpus_tokens).get_scores(query_tokens))
    except ImportError:
        logger.debug("rank_bm25 not installed; using token-overlap fallback")
        scores = _overlap_scores(query_tokens, corpus_tokens)

    # BM25 IDF can go negative/zero on tiny corpora, so gate inclusion on an
    # actual token hit rather than on score > 0 (same rationale as
    # memory_service._bm25_relevance).
    query_set = set(query_tokens)
    matched = [
        (profile, score)
        for profile, score, doc_tokens in zip(profiles, scores, corpus_tokens)
        if query_set & set(doc_tokens)
    ]
    matched.sort(key=lambda pair: (-pair[1], pair[0].get("name", "")))
    return [_result(profile, score) for profile, score in matched[:limit]]
