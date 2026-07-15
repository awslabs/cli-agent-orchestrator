"""Tests for profile discovery search (cao profile find + find_profiles MCP tool).

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340
"""

import json
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.profile_search import (
    RESULT_FIELDS,
    _searchable_text,
    _tokenize,
    search_profiles,
)


@pytest.fixture
def sample_profiles():
    return [
        {
            "name": "sqs-dlq-check",
            "description": "Checks SQS dead-letter queues for stuck messages",
            "tags": ["sqs", "monitoring"],
            "capabilities": ["inspect sqs queues"],
            "role": "developer",
            "source": "local",
        },
        {
            "name": "cloudwatch-logs",
            "description": "Searches CloudWatch logs for error patterns",
            "tags": ["cloudwatch", "monitoring"],
            "capabilities": ["query cloudwatch logs"],
            "role": "developer",
            "source": "local",
        },
        {
            "name": "dynamodb-delete",
            "description": "Deletes items from DynamoDB tables",
            "tags": ["dynamodb"],
            "capabilities": [],
            "role": "developer",
            "source": "built-in",
        },
    ]


class TestTokenize:
    def test_lowercases_and_splits(self):
        assert _tokenize("Monitor SQS-Queues!") == ["monitor", "sqs", "queues"]

    def test_empty_and_symbols_only(self):
        assert _tokenize("") == []
        assert _tokenize("--- !!!") == []


class TestSearchableText:
    def test_includes_all_metadata_fields(self):
        text = _searchable_text(
            {
                "name": "n1",
                "description": "d1",
                "tags": ["t1"],
                "capabilities": ["c1"],
            }
        )
        for token in ("n1", "d1", "t1", "c1"):
            assert token in text

    def test_handles_missing_fields(self):
        assert _searchable_text({"name": "only-name"}) == "only-name"


class TestSearchProfiles:
    def test_ranks_direct_match_first(self, sample_profiles):
        results = search_profiles("sqs dead-letter", profiles=sample_profiles)
        assert results
        assert results[0]["name"] == "sqs-dlq-check"

    def test_excludes_profiles_with_no_token_hit(self, sample_profiles):
        results = search_profiles("sqs", profiles=sample_profiles)
        names = [r["name"] for r in results]
        assert "dynamodb-delete" not in names

    def test_no_match_returns_empty(self, sample_profiles):
        assert search_profiles("kubernetes helm", profiles=sample_profiles) == []

    def test_empty_query_returns_empty(self, sample_profiles):
        assert search_profiles("", profiles=sample_profiles) == []
        assert search_profiles("---", profiles=sample_profiles) == []

    def test_empty_profile_list_returns_empty(self):
        assert search_profiles("sqs", profiles=[]) == []

    def test_limit_respected(self, sample_profiles):
        results = search_profiles("monitoring", profiles=sample_profiles, limit=1)
        assert len(results) == 1

    def test_zero_and_negative_limit_return_empty(self, sample_profiles):
        assert search_profiles("sqs", profiles=sample_profiles, limit=0) == []
        assert search_profiles("sqs", profiles=sample_profiles, limit=-3) == []

    def test_all_empty_corpus_does_not_crash(self):
        """Regression: BM25Okapi raises ZeroDivisionError when avgdl == 0."""
        empty = [{"name": "", "description": "", "tags": [], "capabilities": []}]
        assert search_profiles("sqs", profiles=empty) == []

    def test_matches_on_tags(self, sample_profiles):
        results = search_profiles("dynamodb", profiles=sample_profiles)
        assert results and results[0]["name"] == "dynamodb-delete"

    def test_matches_on_capabilities(self, sample_profiles):
        results = search_profiles("inspect queues", profiles=sample_profiles)
        assert results and results[0]["name"] == "sqs-dlq-check"

    def test_result_contract_fields(self, sample_profiles):
        result = search_profiles("sqs", profiles=sample_profiles)[0]
        for field in RESULT_FIELDS:
            assert field in result
        assert "score" in result
        assert isinstance(result["capabilities"], list)
        assert isinstance(result["tags"], list)

    def test_never_exposes_prompt_body(self, sample_profiles):
        """Security boundary: discovery is metadata-only."""
        poisoned = [dict(p, prompt="SECRET SYSTEM PROMPT") for p in sample_profiles]
        for result in search_profiles("sqs monitoring dynamodb", profiles=poisoned):
            assert "prompt" not in result
            assert "SECRET" not in json.dumps(result)

    def test_fallback_when_rank_bm25_missing(self, sample_profiles):
        with patch.dict("sys.modules", {"rank_bm25": None}):
            results = search_profiles("sqs", profiles=sample_profiles)
        assert results and results[0]["name"] == "sqs-dlq-check"

    def test_scores_sorted_descending(self, sample_profiles):
        results = search_profiles("sqs monitoring", profiles=sample_profiles)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)


class TestSchemaAcceptsNewFields:
    def test_capabilities_and_tags_valid(self):
        from cli_agent_orchestrator.cli.commands.profile import _validate_frontmatter

        metadata = {
            "name": "test-agent",
            "description": "x",
            "capabilities": ["query dynamodb tables"],
            "tags": ["dynamodb", "aws"],
        }
        errors = [m for m in _validate_frontmatter(metadata) if m.startswith("[error]")]
        assert errors == []

    def test_bad_tag_pattern_rejected(self):
        from cli_agent_orchestrator.cli.commands.profile import _validate_frontmatter

        metadata = {"name": "test-agent", "tags": ["has spaces and $ymbols"]}
        errors = [m for m in _validate_frontmatter(metadata) if m.startswith("[error]")]
        assert errors


class TestSearchableTextRobustness:
    def test_none_description_does_not_match_query_none(self):
        """Regression: str(None) == "None" made `find none` match empty descriptions."""
        profiles = [{"name": "x-agent", "description": None, "tags": [], "capabilities": []}]
        assert search_profiles("none", profiles=profiles) == []

    def test_string_tags_do_not_raise(self):
        profiles = [{"name": "sqs-a", "description": "d", "tags": "sqs", "capabilities": 7}]
        results = search_profiles("sqs", profiles=profiles)  # must not raise TypeError
        assert isinstance(results, list)
        assert results and results[0]["name"] == "sqs-a"  # matched via name token
        # Regression (Copilot): contract must hold even for malformed input
        assert results[0]["tags"] == []
        assert results[0]["capabilities"] == []


class TestDiscoveryFields:
    """Read-time hardening: schema limits enforced even for profiles that
    never went through cao install validation."""

    def _fields(self, meta):
        from cli_agent_orchestrator.utils.agent_profiles import _discovery_fields

        return _discovery_fields(meta)

    def test_non_list_values_coerced_to_empty(self):
        out = self._fields({"tags": "sqs", "capabilities": 42, "role": ["dev"]})
        assert out == {"capabilities": [], "tags": [], "role": ""}

    def test_items_coerced_to_str_and_bounded(self):
        out = self._fields({"capabilities": [123, "x" * 500], "tags": ["ok_tag", 99]})
        assert out["capabilities"][0] == "123"
        assert len(out["capabilities"][1]) == 128
        assert out["tags"] == ["ok_tag", "99"]

    def test_invalid_tags_dropped(self):
        out = self._fields({"tags": ["good-tag", "has spaces", "bad$char", "x" * 65]})
        assert out["tags"] == ["good-tag"]

    def test_trailing_newline_tag_rejected(self):
        """Regression (Copilot): $ matches before a trailing newline with
        re.match; fullmatch must reject the whole string."""
        out = self._fields({"tags": ["good-tag\n", "ok-tag"]})
        assert out["tags"] == ["ok-tag"]

    def test_item_count_capped(self):
        out = self._fields(
            {"tags": [f"t{i}" for i in range(100)], "capabilities": [f"c{i}" for i in range(100)]}
        )
        assert len(out["tags"]) == 32
        assert len(out["capabilities"]) == 32


class TestEndToEndWiring:
    """Frontmatter on disk -> list_agent_profiles() -> search_profiles()."""

    def test_tagged_profile_found_via_store_scan(self, tmp_path, monkeypatch):
        import cli_agent_orchestrator.utils.agent_profiles as ap

        store = tmp_path / "store"
        store.mkdir()
        (store / "dlq-demo.md").write_text(
            "---\n"
            "name: dlq-demo\n"
            "description: Investigates stuck messages\n"
            "tags: [dlq, dead-letter, sqs]\n"
            'capabilities: ["inspect dead letter queues"]\n'
            "---\n\nSECRET PROMPT BODY\n"
        )
        monkeypatch.setattr(ap, "LOCAL_AGENT_STORE_DIR", store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            lambda: [],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        profiles = [p for p in ap.list_agent_profiles() if p["name"] == "dlq-demo"]
        assert profiles and profiles[0]["tags"] == ["dlq", "dead-letter", "sqs"]

        results = search_profiles("dead letter", profiles=profiles)
        assert results and results[0]["name"] == "dlq-demo"
        assert "SECRET" not in json.dumps(results)


class TestFindProfilesMcpTool:
    def test_tool_returns_contract(self, sample_profiles, monkeypatch):
        from cli_agent_orchestrator.mcp_server import server

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            lambda: sample_profiles,
        )
        results = server.find_profiles(query="monitor sqs", limit=5)
        assert results
        assert {r["name"] for r in results} <= {"sqs-dlq-check", "cloudwatch-logs"}
        for r in results:
            assert "prompt" not in r
            assert set(r) == {
                "name",
                "description",
                "capabilities",
                "tags",
                "role",
                "source",
                "score",
            }

    def test_tool_returns_empty_on_backend_exception(self, monkeypatch):
        from cli_agent_orchestrator.mcp_server import server

        def boom(*a, **k):
            raise RuntimeError("backend down")

        monkeypatch.setattr("cli_agent_orchestrator.services.profile_search.search_profiles", boom)
        assert server.find_profiles(query="sqs", limit=5) == []
