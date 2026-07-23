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
        assert out == {"description": "", "capabilities": [], "tags": [], "role": ""}

    def test_description_normalized(self):
        """Regression (#438 re-review): mapping/list descriptions crashed the
        CLI (KeyError on slice) and escaped MCP bounds; oversized strings
        expanded the corpus unbounded."""
        assert self._fields({"description": {"a": "b"}})["description"] == ""
        assert self._fields({"description": ["x"]})["description"] == ""
        assert self._fields({"description": None})["description"] == ""
        assert len(self._fields({"description": "x" * 5000})["description"]) == 1024

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
                "coverage",
                "score",
            }
            assert isinstance(r["coverage"], int) and r["coverage"] >= 1
            # score = coverage + tie-break fraction in [0, 1)
            assert r["coverage"] <= r["score"] < r["coverage"] + 1

    def test_tool_returns_empty_on_backend_exception(self, monkeypatch):
        from cli_agent_orchestrator.mcp_server import server

        def boom(*a, **k):
            raise RuntimeError("backend down")

        monkeypatch.setattr("cli_agent_orchestrator.services.profile_search.search_profiles", boom)
        assert server.find_profiles(query="sqs", limit=5) == []


class TestCoverageFirstRanking:
    """Regression (#438 re-review): Okapi BM25 IDF goes zero/negative when a
    term appears in most of a small corpus, ranking a profile that matches
    every query term below partial matches. Coverage-primary sort with
    BM25Plus tie-break guarantees broader query coverage ranks first."""

    def _p(self, name, description):
        return {"name": name, "description": description, "tags": [], "capabilities": []}

    def test_full_match_ranks_first_on_tiny_corpus(self):
        profiles = [
            self._p("monitor-only", "monitor things"),
            self._p("sqs-only", "sqs things"),
            self._p("both", "sqs monitor"),
        ]
        results = search_profiles("sqs monitor", profiles=profiles)
        assert results[0]["name"] == "both"

    def test_full_match_survives_limit(self):
        profiles = [
            self._p("monitor-only", "monitor things"),
            self._p("sqs-only", "sqs things"),
            self._p("both", "sqs monitor"),
        ]
        results = search_profiles("sqs monitor", profiles=profiles, limit=1)
        assert [r["name"] for r in results] == ["both"]

    def test_single_profile_corpus(self):
        results = search_profiles("sqs", profiles=[self._p("only", "sqs stuff")])
        assert results and results[0]["name"] == "only"

    def test_two_profile_common_term(self):
        profiles = [self._p("a-sqs", "sqs"), self._p("b-both", "sqs monitor")]
        results = search_profiles("sqs monitor", profiles=profiles)
        assert results[0]["name"] == "b-both"

    def test_scores_non_negative(self):
        profiles = [
            self._p("monitor-only", "monitor things"),
            self._p("sqs-only", "sqs things"),
            self._p("both", "sqs monitor"),
        ]
        for r in search_profiles("sqs monitor", profiles=profiles):
            assert r["score"] >= 0

    def test_bm25_tiebreak_within_equal_coverage(self):
        # Equal coverage (1 term each); heavier term frequency wins tie-break.
        profiles = [
            self._p("mentions-once", "sqs plus lots of other unrelated words here"),
            self._p("focused", "sqs sqs sqs"),
        ]
        results = search_profiles("sqs", profiles=profiles)
        assert results[0]["name"] == "focused"


class TestScoreOrderConsistency:
    """Regression (#438 re-review): the returned score must agree with the
    relevance ordering -- a caller taking max(score) must get the top result.
    Previously the raw BM25 tie-breaker was returned, so a long full-match
    document could rank first with a lower score than a focused partial
    match."""

    def _adversarial_profiles(self):
        filler = "filler words here that make this document quite long " * 8
        return [
            {
                "name": "full",
                "description": "rare common " + filler,
                "tags": [],
                "capabilities": [],
            },
            {
                "name": "partial",
                "description": "rare rare rare focused",
                "tags": [],
                "capabilities": [],
            },
            {"name": "c1", "description": "common", "tags": [], "capabilities": []},
            {"name": "c2", "description": "common stuff", "tags": [], "capabilities": []},
            {"name": "c3", "description": "common things", "tags": [], "capabilities": []},
        ]

    def test_scores_monotonic_with_rank(self):
        results = search_profiles("rare common", profiles=self._adversarial_profiles())
        assert results[0]["name"] == "full"
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_max_score_selects_top_ranked(self):
        results = search_profiles("rare common", profiles=self._adversarial_profiles())
        assert max(results, key=lambda r: r["score"])["name"] == results[0]["name"] == "full"

    def test_score_encodes_coverage_integer_part(self):
        results = search_profiles("rare common", profiles=self._adversarial_profiles())
        for r in results:
            assert r["coverage"] <= r["score"] < r["coverage"] + 1
        by_name = {r["name"]: r for r in results}
        assert by_name["full"]["coverage"] == 2
        assert by_name["partial"]["coverage"] == 1

    def test_fallback_scorer_scores_also_monotonic(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_bm25(name, *args, **kwargs):
            if name == "rank_bm25":
                raise ImportError("forced")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_bm25)
        results = search_profiles("rare common", profiles=self._adversarial_profiles())
        assert results[0]["name"] == "full"
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)


class TestLoadabilityExclusion:
    """Regression (#438 re-review): search must only recommend profiles that
    load_agent_profile() can actually load -- not merely ones whose YAML
    parses. Covers parse failures, model-invalid metadata, and profile
    directories without agent.md."""

    def test_unloadable_excluded_from_results(self):
        profiles = [
            {
                "name": "broken-monitor",
                "description": "",
                "tags": [],
                "capabilities": [],
                "loadable": False,
            },
            {
                "name": "monitor-ok",
                "description": "monitor",
                "tags": [],
                "capabilities": [],
                "loadable": True,
            },
        ]
        names = [r["name"] for r in search_profiles("monitor", profiles=profiles)]
        assert names == ["monitor-ok"]

    def test_missing_loadable_treated_as_valid(self):
        # Backwards compatibility: dicts without the flag are searchable.
        profiles = [
            {"name": "legacy-monitor", "description": "monitor", "tags": [], "capabilities": []}
        ]
        assert search_profiles("monitor", profiles=profiles)

    def test_valid_yaml_invalid_model_marked_unloadable(self, tmp_path):
        """Reviewer repro: provider as a list parses as YAML but fails
        AgentProfile validation, so load_agent_profile() would raise."""
        import cli_agent_orchestrator.utils.agent_profiles as ap

        (tmp_path / "bad-monitor.md").write_text(
            "---\nname: bad-monitor\ndescription: monitor things\n"
            "provider: [not, a, string]\n---\nbody\n"
        )
        profiles: dict = {}
        ap._scan_directory(tmp_path, "local", profiles)
        assert profiles["bad-monitor"]["loadable"] is False
        assert not search_profiles("monitor", profiles=list(profiles.values()))

    def test_directory_without_agent_md_marked_unloadable(self, tmp_path):
        """Reviewer repro: a bare directory is listable by name, but
        load_agent_profile() raises FileNotFoundError for it."""
        import cli_agent_orchestrator.utils.agent_profiles as ap

        (tmp_path / "empty-monitor").mkdir()
        profiles: dict = {}
        ap._scan_directory(tmp_path, "local", profiles)
        assert profiles["empty-monitor"]["loadable"] is False
        assert not search_profiles("monitor", profiles=list(profiles.values()))

    def test_dir_style_profile_follows_source_rules(self, tmp_path, monkeypatch):
        """Source rules, not just content, decide loadability: a valid
        <name>/agent.md is resolvable from provider/extra dirs but NOT from
        the local store (which _read_agent_profile_source treats as
        flat-file only). Search must never recommend the local-store one."""
        import cli_agent_orchestrator.utils.agent_profiles as ap

        local_store = tmp_path / "store"
        local_store.mkdir()
        d = local_store / "dirstyle-monitor"
        d.mkdir()
        (d / "agent.md").write_text(
            "---\nname: dirstyle-monitor\ndescription: monitor queues\n---\nbody\n"
        )
        provider_dir = tmp_path / "provider"
        p = provider_dir / "provider-monitor"
        p.mkdir(parents=True)
        (p / "agent.md").write_text(
            "---\nname: provider-monitor\ndescription: monitor queues\n---\nbody\n"
        )
        monkeypatch.setattr(ap, "LOCAL_AGENT_STORE_DIR", local_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"testprov": str(provider_dir)},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs", lambda: []
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        profiles = ap.list_agent_profiles()
        by_name = {p["name"]: p for p in profiles}
        # both listed; only the provider-dir one is loadable
        assert by_name["dirstyle-monitor"]["loadable"] is False
        assert by_name["provider-monitor"]["loadable"] is True
        results = search_profiles("monitor", profiles=profiles)
        assert [r["name"] for r in results] == ["provider-monitor"]
        # and the recommendation is honoured by the real load path
        loaded = ap.load_agent_profile("provider-monitor")
        assert loaded.name == "provider-monitor"

    def test_broken_yaml_on_disk_listed_but_not_searchable(self, tmp_path, monkeypatch):
        """End-to-end discover-then-load consistency: every profile that
        search recommends must actually load via load_agent_profile().

        Store contains all three unloadable shapes from the #438 re-review:
        broken YAML, valid-YAML/invalid-model metadata, and a profile
        directory without agent.md."""
        import cli_agent_orchestrator.utils.agent_profiles as ap

        store = tmp_path / "store"
        store.mkdir()
        (store / "broken-monitor.md").write_text(
            "---\nname: [unclosed\ndescription: {bad\n---\nbody\n"
        )
        (store / "bad-model-monitor.md").write_text(
            "---\nname: bad-model-monitor\ndescription: monitor things\n"
            "provider: [not, a, string]\n---\nbody\n"
        )
        (store / "empty-monitor").mkdir()
        (store / "good-monitor.md").write_text(
            "---\nname: good-monitor\ndescription: monitor queues\n---\nbody\n"
        )
        monkeypatch.setattr(ap, "LOCAL_AGENT_STORE_DIR", store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs", lambda: []
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )
        local = [p for p in ap.list_agent_profiles() if p["source"] == "local"]
        # list still shows all four (existing behavior preserved)
        assert {p["name"] for p in local} == {
            "broken-monitor",
            "bad-model-monitor",
            "empty-monitor",
            "good-monitor",
        }
        # search only recommends the loadable one...
        results = search_profiles("monitor", profiles=local)
        assert [r["name"] for r in results] == ["good-monitor"]
        # ...and every recommendation is honoured by the load path.
        for r in results:
            profile = ap.load_agent_profile(r["name"])
            assert profile.name == r["name"]
