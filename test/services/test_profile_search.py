"""Tests for profile discovery search (cao profile find + find_profiles MCP tool).

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/340
"""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.profile import profile
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


class TestFindCmd:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_find_table_output(self, runner, sample_profiles):
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            return_value=sample_profiles,
        ):
            result = runner.invoke(profile, ["find", "sqs"])
        assert result.exit_code == 0
        assert "sqs-dlq-check" in result.output
        assert "SCORE" in result.output

    def test_find_json_output(self, runner, sample_profiles):
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            return_value=sample_profiles,
        ):
            result = runner.invoke(profile, ["find", "sqs", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["name"] == "sqs-dlq-check"

    def test_find_no_match_message(self, runner, sample_profiles):
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            return_value=sample_profiles,
        ):
            result = runner.invoke(profile, ["find", "kubernetes"])
        assert result.exit_code == 0
        assert "No profiles matched" in result.output

    def test_find_limit_option(self, runner, sample_profiles):
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            return_value=sample_profiles,
        ):
            result = runner.invoke(profile, ["find", "monitoring", "--limit", "1", "--json"])
        assert result.exit_code == 0
        assert len(json.loads(result.output)) == 1


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
