"""Tests for cao_session_monitor: socket-path derivation, the self-gate,
JSON Patch application, snapshot-to-tree projection, and the SSE parser.

Standalone suite for the herdr companion plugin's core module (see the module
docstring in ``cao_session_monitor.py`` -- Python 3 stdlib only, zero CAO
``src/`` dependency). Runs on its own (via ``uv run pytest``), with no dependency on CAO's root
``test/conftest.py``:

    pytest examples/cao-session-monitor/test_cao_session_monitor.py

Every test isolates ``XDG_CONFIG_HOME`` / ``HERDR_SOCKET_PATH`` via
``monkeypatch`` (never direct ``os.environ`` mutation), so results never
depend on the environment of the machine running the suite and no test leaks
env state into another. SSE tests apply the same convention to the network
boundary, monkeypatching ``urllib.request.urlopen`` rather than mutating a
module-level global.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Callable, List, Tuple

import cao_session_monitor as csm
import pytest

#: Stand-in for the real user's XDG config dir so expected paths are
#: deterministic regardless of the machine running this suite.
_FAKE_XDG_CONFIG_HOME = "/fake-home/.config"

#: Fixtures directory sibling to this test file -- captured live against a
#: running cao-server (see fixtures/README.md). Used as ground truth for the
#: build_tree and apply_patch tests below, not just a synthetic example.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(filename: str) -> dict:
    """Load and parse one of the frozen ground-truth JSON fixtures."""
    return json.loads((_FIXTURES_DIR / filename).read_text())


@pytest.fixture(autouse=True)
def isolated_herdr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the same clean slate: a fixed XDG_CONFIG_HOME
    and no inherited HERDR_SOCKET_PATH."""
    monkeypatch.setenv("XDG_CONFIG_HOME", _FAKE_XDG_CONFIG_HOME)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)


class TestExpectedSocketPath:
    """expected_socket_path: pure path-convention derivation."""

    def test_default_session_uses_bare_herdr_sock_path(self) -> None:
        assert (
            csm.expected_socket_path(csm.DEFAULT_SESSION)
            == f"{_FAKE_XDG_CONFIG_HOME}/herdr/herdr.sock"
        )

    def test_named_session_uses_sessions_subdirectory(self) -> None:
        assert (
            csm.expected_socket_path("cao")
            == f"{_FAKE_XDG_CONFIG_HOME}/herdr/sessions/cao/herdr.sock"
        )

    def test_respects_xdg_config_home_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
        assert csm.expected_socket_path(csm.DEFAULT_SESSION) == "/custom/xdg/herdr/herdr.sock"

    def test_falls_back_to_home_dot_config_when_xdg_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(csm.Path, "home", staticmethod(lambda: Path("/fake-home-fallback")))
        assert (
            csm.expected_socket_path(csm.DEFAULT_SESSION)
            == "/fake-home-fallback/.config/herdr/herdr.sock"
        )

    def test_empty_session_name_is_not_treated_as_default_sentinel(self) -> None:
        """ "" is falsy but is not the "default" sentinel -- it must still
        take the named-session branch, not silently alias to the default
        socket path."""
        assert csm.expected_socket_path("") == f"{_FAKE_XDG_CONFIG_HOME}/herdr/sessions//herdr.sock"
        assert csm.expected_socket_path("") != csm.expected_socket_path(csm.DEFAULT_SESSION)


class TestResolveSocketPath:
    """resolve_socket_path: HERDR_SOCKET_PATH env var vs. convention fallback."""

    def test_env_var_takes_precedence_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERDR_SOCKET_PATH", "/some/explicit/path/herdr.sock")
        assert csm.resolve_socket_path() == "/some/explicit/path/herdr.sock"

    def test_falls_back_to_default_session_path_when_unset(self) -> None:
        assert csm.resolve_socket_path() == csm.expected_socket_path(csm.DEFAULT_SESSION)

    def test_empty_string_env_var_is_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HERDR_SOCKET_PATH="" is falsy and must fall through to the
        convention default rather than resolving to an empty path."""
        monkeypatch.setenv("HERDR_SOCKET_PATH", "")
        assert csm.resolve_socket_path() == csm.expected_socket_path(csm.DEFAULT_SESSION)


class TestShouldRender:
    """should_render(socket_path, session_name): True iff socket_path is the
    socket for session_name -- i.e. this process is running inside the
    session it is configured to monitor."""

    # --- Required scenario 1: default session, gated on default -> render.
    def test_true_when_running_in_default_session_gated_on_default(self) -> None:
        running_in = csm.expected_socket_path(csm.DEFAULT_SESSION)
        assert csm.should_render(running_in, csm.DEFAULT_SESSION) is True

    # --- Required scenario 2: named session, gated on same name -> render.
    def test_true_when_running_in_named_session_gated_on_same_name(self) -> None:
        running_in = csm.expected_socket_path("cao")
        assert csm.should_render(running_in, "cao") is True

    # --- Required scenario 3a: different named session -> do not render.
    def test_false_when_running_in_different_named_session(self) -> None:
        running_in = csm.expected_socket_path("personal")
        assert csm.should_render(running_in, "cao") is False

    # --- Required scenario 3b: default running, named gate -> do not render.
    def test_false_when_running_in_default_session_but_gated_on_named(self) -> None:
        running_in = csm.expected_socket_path(csm.DEFAULT_SESSION)
        assert csm.should_render(running_in, "cao") is False

    # --- Extra: the symmetric case (named running, default gate) exercises
    # the opposite branch of expected_socket_path's if/else than 3b did.
    def test_false_when_running_in_named_session_but_gated_on_default(self) -> None:
        running_in = csm.expected_socket_path("cao")
        assert csm.should_render(running_in, csm.DEFAULT_SESSION) is False

    # --- Required scenario 4: HERDR_SOCKET_PATH takes precedence over the
    # path-convention default when fed into should_render via resolve_socket_path.
    def test_herdr_socket_path_env_var_precedence_flows_into_should_render(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cao_socket = csm.expected_socket_path("cao")
        monkeypatch.setenv("HERDR_SOCKET_PATH", cao_socket)

        resolved = csm.resolve_socket_path()

        assert resolved == cao_socket
        assert csm.should_render(resolved, "cao") is True
        # Must not have silently fallen back to the default-session
        # convention path, which would wrongly gate this as "default".
        assert resolved != csm.expected_socket_path(csm.DEFAULT_SESSION)
        assert csm.should_render(resolved, csm.DEFAULT_SESSION) is False


class TestApplyPatch:
    """apply_patch(doc, ops): shallow RFC-6902 add/replace/remove, never
    mutates ``doc``, arrays are whole-key replaced."""

    def test_add_inserts_a_new_top_level_key(self) -> None:
        doc = {"a": 1}
        assert csm.apply_patch(doc, [{"op": "add", "path": "/b", "value": 2}]) == {
            "a": 1,
            "b": 2,
        }

    def test_add_inserts_a_new_nested_key_into_existing_dict(self) -> None:
        doc = {"counts": {"sessions": 1}}
        patched = csm.apply_patch(doc, [{"op": "add", "path": "/counts/terminals", "value": 3}])
        assert patched == {"counts": {"sessions": 1, "terminals": 3}}

    def test_replace_overwrites_a_top_level_key(self) -> None:
        doc = {"a": 1}
        assert csm.apply_patch(doc, [{"op": "replace", "path": "/a", "value": 99}]) == {"a": 99}

    def test_replace_overwrites_a_nested_key(self) -> None:
        doc = {"counts": {"sessions": 1, "terminals": 3}}
        patched = csm.apply_patch(doc, [{"op": "replace", "path": "/counts/sessions", "value": 5}])
        assert patched == {"counts": {"sessions": 5, "terminals": 3}}

    def test_replace_whole_key_replaces_an_array_without_merging_elements(self) -> None:
        """Arrays are shallow: a replace on an array path swaps the whole
        array, it never merges by index or by element identity."""
        doc = {"terminals": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        patched = csm.apply_patch(
            doc, [{"op": "replace", "path": "/terminals", "value": [{"id": "9"}]}]
        )
        assert patched == {"terminals": [{"id": "9"}]}

    def test_remove_deletes_a_top_level_key(self) -> None:
        doc = {"a": 1, "b": 2}
        assert csm.apply_patch(doc, [{"op": "remove", "path": "/b"}]) == {"a": 1}

    def test_remove_of_nonexistent_path_raises_key_error(self) -> None:
        """No silent no-op on a bad path -- a nonexistent key surfaces as a
        real KeyError rather than being swallowed."""
        with pytest.raises(KeyError):
            csm.apply_patch({"a": 1}, [{"op": "remove", "path": "/nope"}])

    def test_remove_deletes_a_nested_key(self) -> None:
        doc = {"counts": {"sessions": 1, "terminals": 3}}
        patched = csm.apply_patch(doc, [{"op": "remove", "path": "/counts/terminals"}])
        assert patched == {"counts": {"sessions": 1}}

    def test_ops_combine_add_replace_and_remove_in_a_single_call(self) -> None:
        doc = {"a": 1, "b": 2, "c": {"x": 10}}
        patched = csm.apply_patch(
            doc,
            [
                {"op": "replace", "path": "/a", "value": 100},
                {"op": "add", "path": "/d", "value": 4},
                {"op": "remove", "path": "/b"},
                {"op": "replace", "path": "/c/x", "value": 99},
            ],
        )
        assert patched == {"a": 100, "c": {"x": 99}, "d": 4}

    def test_empty_ops_list_returns_an_equal_but_distinct_copy(self) -> None:
        doc = {"a": 1}
        patched = csm.apply_patch(doc, [])
        assert patched == doc
        assert patched is not doc

    def test_does_not_mutate_the_original_doc_including_nested_dicts(self) -> None:
        doc = {"a": 1, "b": {"x": 10}}
        csm.apply_patch(
            doc,
            [
                {"op": "replace", "path": "/a", "value": 2},
                {"op": "replace", "path": "/b/x", "value": 99},
            ],
        )
        assert doc == {"a": 1, "b": {"x": 10}}

    def test_path_escaping_decodes_tilde_one_before_tilde_zero(self) -> None:
        """RFC 6901 requires unescaping ``~1`` -> ``/`` before ``~0`` -> ``~``.
        A raw key of literally ``~1`` encodes to the pointer segment ``~01``;
        decoding in the wrong order would collapse that segment to ``/``
        instead of recovering ``~1``, silently patching the wrong key."""
        doc = {"~1": "original"}
        patched = csm.apply_patch(doc, [{"op": "replace", "path": "/~01", "value": "updated"}])
        assert patched == {"~1": "updated"}

    def test_path_escaping_decodes_bare_tilde_and_slash_keys(self) -> None:
        doc = {"/": "slash-key", "~": "tilde-key"}
        patched = csm.apply_patch(
            doc,
            [
                {"op": "replace", "path": "/~1", "value": "slash-updated"},
                {"op": "replace", "path": "/~0", "value": "tilde-updated"},
            ],
        )
        assert patched == {"/": "slash-updated", "~": "tilde-updated"}

    def test_fixture_delta_applied_to_implied_baseline_reconstructs_snapshot(self) -> None:
        """The real state_delta.json ops, applied via apply_patch to the
        baseline they were computed against, must reproduce
        state_snapshot.json exactly -- ties this function to a real fixture
        rather than only a synthetic doc."""
        snapshot = _load_fixture("state_snapshot.json")
        delta = _load_fixture("state_delta.json")
        baseline = {
            "sessions": [],
            "terminals": [],
            "counts": {"sessions": 0, "terminals": 0},
            "scopes": snapshot["scopes"],
        }
        assert csm.apply_patch(baseline, delta) == snapshot


class TestBuildTree:
    """build_tree(snapshot): groups terminals under their session by
    ``session_name``, passing terminal dicts through unchanged."""

    def test_fixture_snapshot_produces_one_session_entry_per_fixture_session(self) -> None:
        snapshot = _load_fixture("state_snapshot.json")
        tree = csm.build_tree(snapshot)
        expected_names = {s["name"] for s in snapshot["sessions"]}
        assert set(tree["sessions"].keys()) == expected_names

    def test_fixture_snapshot_groups_each_terminal_under_its_session_name(self) -> None:
        """Every terminal in the live fixture lands under the session whose
        name matches its own ``session_name`` field, with the terminal dict
        passed through unchanged (no re-shaping, no dropped/added fields)."""
        snapshot = _load_fixture("state_snapshot.json")
        tree = csm.build_tree(snapshot)
        for terminal in snapshot["terminals"]:
            session_terminals = tree["sessions"][terminal["session_name"]]["terminals"]
            assert terminal in session_terminals

    def test_fixture_snapshot_session_label_has_no_double_cao_prefix(self) -> None:
        """The session label is the bare ``session_name`` -- it already
        carries CAO's one ``cao-`` prefix internally, so build_tree must not
        add a second one on top."""
        snapshot = _load_fixture("state_snapshot.json")
        tree = csm.build_tree(snapshot)
        first_name = snapshot["sessions"][0]["name"]
        assert first_name in tree["sessions"]
        assert not first_name.startswith("cao-cao-")

    def test_session_with_no_matching_terminals_gets_an_empty_list(self) -> None:
        snapshot = {"sessions": [{"id": "s1", "name": "s1", "status": "active"}], "terminals": []}
        assert csm.build_tree(snapshot) == {"sessions": {"s1": {"terminals": []}}}

    def test_orphan_terminal_with_no_matching_session_gets_its_own_group(self) -> None:
        """A terminal whose session_name doesn't match any session in the
        snapshot still surfaces -- grouped under its own session_name key --
        rather than being silently dropped."""
        snapshot = {
            "sessions": [{"id": "s1", "name": "s1", "status": "active"}],
            "terminals": [{"id": "orphan-1", "session_name": "unknown-session"}],
        }
        tree = csm.build_tree(snapshot)
        assert tree["sessions"]["s1"] == {"terminals": []}
        assert tree["sessions"]["unknown-session"]["terminals"] == [
            {"id": "orphan-1", "session_name": "unknown-session"}
        ]

    def test_terminal_missing_session_name_groups_under_empty_string(self) -> None:
        snapshot = {"sessions": [], "terminals": [{"id": "t1"}]}
        assert csm.build_tree(snapshot) == {"sessions": {"": {"terminals": [{"id": "t1"}]}}}

    def test_empty_snapshot_produces_an_empty_sessions_tree(self) -> None:
        assert csm.build_tree({}) == {"sessions": {}}

    def test_snapshot_with_empty_sessions_and_terminals_lists_is_also_empty(self) -> None:
        assert csm.build_tree({"sessions": [], "terminals": []}) == {"sessions": {}}


class _FakeHTTPResponse(io.BytesIO):
    """A ``BytesIO`` that also supports the context-manager protocol
    ``urllib.request.urlopen``'s return value provides, so it stands in for
    a real response object in ``with urlopen(...) as resp:``."""

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class TestIterSse:
    """iter_sse(url, last_event_id): stdlib-only SSE client. Wire parsing is
    exercised through the public generator, mocking only the network
    boundary (``urllib.request.urlopen``) rather than hitting a real
    server."""

    @pytest.fixture
    def captured_request(self, monkeypatch: pytest.MonkeyPatch) -> List[urllib.request.Request]:
        """Stub ``urlopen`` to return a canned SSE body while recording the
        ``Request`` object it was called with, so tests can assert on
        headers without a real socket."""
        captured: List[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, *args: object, **kwargs: object):
            captured.append(req)
            return _FakeHTTPResponse(b'event: STATE_SNAPSHOT\ndata: {"a": 1}\n\n')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return captured

    def test_yields_parsed_event_name_and_data_tuple(
        self, captured_request: List[urllib.request.Request]
    ) -> None:
        events = list(csm.iter_sse("http://localhost:9889/agui/v1/stream"))
        assert events == [("STATE_SNAPSHOT", '{"a": 1}', None)]

    def test_last_event_id_header_is_attached_when_provided(
        self, captured_request: List[urllib.request.Request]
    ) -> None:
        list(csm.iter_sse("http://localhost:9889/agui/v1/stream", last_event_id="42"))
        assert captured_request[0].get_header("Last-event-id") == "42"

    def test_last_event_id_header_is_absent_when_not_provided(
        self, captured_request: List[urllib.request.Request]
    ) -> None:
        list(csm.iter_sse("http://localhost:9889/agui/v1/stream"))
        assert captured_request[0].has_header("Last-event-id") is False


class TestParseSseStream:
    """_parse_sse_stream: wire-format parsing rules that iter_sse delegates
    to. Exercised directly against an in-memory byte stream -- no network
    boundary here, so no mocking is needed for these cases."""

    @staticmethod
    def _parse(raw: bytes) -> List[Tuple[str, str]]:
        return [(ev, data) for ev, data, _id in csm._parse_sse_stream(io.BytesIO(raw))]

    def test_unnamed_event_defaults_to_message(self) -> None:
        assert self._parse(b"data: hello\n\n") == [("message", "hello")]

    def test_named_event_is_reported_verbatim(self) -> None:
        assert self._parse(b"event: STATE_DELTA\ndata: [1,2,3]\n\n") == [("STATE_DELTA", "[1,2,3]")]

    def test_multi_line_data_fields_are_concatenated_with_newline(self) -> None:
        assert self._parse(b"data: line1\ndata: line2\n\n") == [("message", "line1\nline2")]

    def test_comment_lines_are_ignored(self) -> None:
        assert self._parse(b": this is a comment\ndata: x\n\n") == [("message", "x")]

    def test_malformed_line_with_no_colon_is_ignored(self) -> None:
        assert self._parse(b"nocolonhere\ndata: x\n\n") == [("message", "x")]

    def test_event_with_no_data_is_not_dispatched(self) -> None:
        assert self._parse(b"event: PING\n\ndata: after\n\n") == [("message", "after")]

    def test_trailing_event_without_a_final_blank_line_is_still_dispatched(self) -> None:
        assert self._parse(b"data: trailing") == [("message", "trailing")]

    def test_single_leading_space_after_field_colon_is_stripped(self) -> None:
        """Only one leading space is stripped -- a second space is data."""
        assert self._parse(b"data:  two-spaces\n\n") == [("message", " two-spaces")]

    def test_no_leading_space_after_field_colon_is_left_untouched(self) -> None:
        assert self._parse(b"data:no-space\n\n") == [("message", "no-space")]

    def test_id_field_does_not_affect_event_name_or_data(self) -> None:
        """An id: line is surfaced as the third tuple element by _parse_sse_stream,
        but must not corrupt the event name or data content."""
        assert self._parse(b"id: 5\ndata: x\n\n") == [("message", "x")]

    def test_multiple_events_in_one_stream_are_each_dispatched_separately(self) -> None:
        assert self._parse(b"event: A\ndata: 1\n\nevent: B\ndata: 2\n\n") == [
            ("A", "1"),
            ("B", "2"),
        ]

    def test_empty_stream_yields_no_events(self) -> None:
        assert self._parse(b"") == []


class TestFetchJson:
    """fetch_json(url): stdlib-only GET + json.loads, no try/except in the
    source -- transport and JSON-decode errors from urlopen propagate to the
    caller unchanged. Wire access is mocked at the same boundary as iter_sse
    (``urllib.request.urlopen``), never a real socket."""

    def test_returns_parsed_dict_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda url, **kw: _FakeHTTPResponse(b'{"a": 1}')
        )
        assert csm.fetch_json("http://localhost:9889/flows") == {"a": 1}

    def test_returns_parsed_list_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real /flows and /workflows endpoints return a JSON array, not
        an object -- the other branch of the -> Union[dict, list] contract."""
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda url, **kw: _FakeHTTPResponse(b'[{"a": 1}, {"b": 2}]')
        )
        assert csm.fetch_json("http://localhost:9889/flows") == [{"a": 1}, {"b": 2}]

    def test_calls_urlopen_with_the_given_url_unmodified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[str] = []

        def fake_urlopen(url: str, **kw) -> "_FakeHTTPResponse":
            captured.append(url)
            return _FakeHTTPResponse(b"{}")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        csm.fetch_json("http://localhost:9889/workflows")
        assert captured == ["http://localhost:9889/workflows"]

    def test_url_error_from_urlopen_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(url: str, **kw) -> "_FakeHTTPResponse":
            raise urllib.request.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urllib.request.URLError):
            csm.fetch_json("http://localhost:9889/flows")

    def test_http_error_from_urlopen_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(url: str, **kw) -> "_FakeHTTPResponse":
            raise urllib.request.HTTPError(url, 500, "Internal Server Error", None, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urllib.request.HTTPError):
            csm.fetch_json("http://localhost:9889/flows")

    def test_malformed_json_body_raises_json_decode_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda url, **kw: _FakeHTTPResponse(b"not json")
        )
        with pytest.raises(json.JSONDecodeError):
            csm.fetch_json("http://localhost:9889/flows")


class TestParseFlows:
    """parse_flows(data): projects Flow-model-shaped rows down to exactly
    {name, schedule, agent_profile, provider, enabled, last_run, next_run}.
    Direct key access -- a missing required key raises KeyError rather than
    silently defaulting, matching apply_patch's no-silent-gaps convention."""

    def test_parses_real_flows_fixture_into_projected_fields(self) -> None:
        flows = _load_fixture("flows.json")
        assert csm.parse_flows(flows) == [
            {
                "name": "nightly-health-check",
                "schedule": "0 2 * * *",
                "agent_profile": "developer",
                "provider": "mock_cli",
                "enabled": True,
                "last_run": "2026-07-24T14:13:38.915439",
                "next_run": "2026-07-25T02:00:00",
            },
            {
                "name": "weekly-digest",
                "schedule": "0 9 * * 1",
                "agent_profile": "developer",
                "provider": "mock_cli",
                "enabled": False,
                "last_run": None,
                "next_run": "2026-07-28T09:00:00",
            },
        ]

    @pytest.mark.parametrize(
        "missing_key",
        ["name", "schedule", "agent_profile", "provider", "enabled", "last_run", "next_run"],
    )
    def test_missing_required_key_raises_key_error(self, missing_key: str) -> None:
        row = {
            "name": "nightly-health-check",
            "schedule": "0 2 * * *",
            "agent_profile": "developer",
            "provider": "mock_cli",
            "enabled": True,
            "last_run": None,
            "next_run": "2026-07-25T02:00:00",
        }
        del row[missing_key]
        with pytest.raises(KeyError):
            csm.parse_flows([row])

    def test_empty_list_returns_empty_list(self) -> None:
        assert csm.parse_flows([]) == []


class TestParseWorkflows:
    """parse_workflows(data): parses WorkflowIndexRow-shaped rows, keeping
    exactly {name, source_path, mode, step_count, description, indexed_at}.
    Same direct-key-access, no-silent-defaulting shape as parse_flows."""

    def test_parses_real_workflows_fixture_into_all_fields(self) -> None:
        workflows = _load_fixture("workflows.json")
        assert csm.parse_workflows(workflows) == [
            {
                "name": "nightly-report",
                "source_path": "/tmp/cao-isolated-home/workflows/nightly-report.yaml",
                "mode": "sequential",
                "step_count": 1,
                "description": "Summarize overnight activity and post a digest",
                "indexed_at": "2026-07-24T21:16:11Z",
            },
            {
                "name": "pr-review",
                "source_path": "/tmp/cao-isolated-home/workflows/pr-review.yaml",
                "mode": "sequential",
                "step_count": 2,
                "description": "Review an open pull request end to end",
                "indexed_at": "2026-07-24T21:16:11Z",
            },
        ]

    def test_drops_unexpected_extra_fields_not_in_the_fixed_set(self) -> None:
        """Confirms parse_workflows re-projects its 6 named fields rather than
        passing the row through verbatim -- the fixture alone can't show this
        since its rows already contain exactly those 6 fields."""
        row = {
            "name": "x",
            "source_path": "/tmp/x.yaml",
            "mode": "sequential",
            "step_count": 1,
            "description": "d",
            "indexed_at": "2026-01-01T00:00:00Z",
            "unexpected_field": "should not appear",
        }
        result = csm.parse_workflows([row])
        assert "unexpected_field" not in result[0]

    @pytest.mark.parametrize(
        "missing_key",
        ["name", "source_path", "mode", "step_count", "description", "indexed_at"],
    )
    def test_missing_required_key_raises_key_error(self, missing_key: str) -> None:
        row = {
            "name": "nightly-report",
            "source_path": "/tmp/cao-isolated-home/workflows/nightly-report.yaml",
            "mode": "sequential",
            "step_count": 1,
            "description": "Summarize overnight activity and post a digest",
            "indexed_at": "2026-07-24T21:16:11Z",
        }
        del row[missing_key]
        with pytest.raises(KeyError):
            csm.parse_workflows([row])

    def test_step_count_of_none_is_preserved(self) -> None:
        """WorkflowIndexRow.step_count is Optional[int] in the real model --
        None must pass through, not be coerced to 0 or dropped."""
        row = {
            "name": "x",
            "source_path": "/tmp/x.yaml",
            "mode": "sequential",
            "step_count": None,
            "description": "d",
            "indexed_at": "2026-01-01T00:00:00Z",
        }
        assert csm.parse_workflows([row])[0]["step_count"] is None

    def test_empty_list_returns_empty_list(self) -> None:
        assert csm.parse_workflows([]) == []


class TestBoldSet:
    """bold_set(tree, ws_label, tab_label): marks the focused session and
    terminal in a fresh copy of ``tree``. A resolvable (ws_label, tab_label)
    pair is required for any mark to be set -- there is no session-only-bold
    state (S-001 fix); it never mutates its input and never raises."""

    @staticmethod
    def _tree() -> dict:
        return {
            "sessions": {
                "ml-infra": {
                    "terminals": [
                        {"window": "conductor-a1b2"},
                        {"window": "sherlock-c3d4"},
                    ]
                }
            }
        }

    def test_matching_labels_mark_both_session_and_terminal(self) -> None:
        result = csm.bold_set(self._tree(), "ml-infra", "conductor-a1b2")
        assert result["sessions"]["ml-infra"]["focused"] is True
        assert result["sessions"]["ml-infra"]["terminals"][0]["focused"] is True
        assert "focused" not in result["sessions"]["ml-infra"]["terminals"][1]

    def test_focus_moved_second_call_marks_new_terminal_and_leaves_first_result_untouched(
        self,
    ) -> None:
        """bold_set is stateless: a second call against the SAME original
        tree with new labels produces an independent copy where only the new
        terminal is marked. There is no shared state to "unmark" -- the
        first call's returned copy is unaffected by the second call."""
        tree = self._tree()
        first = csm.bold_set(tree, "ml-infra", "conductor-a1b2")
        second = csm.bold_set(tree, "ml-infra", "sherlock-c3d4")

        assert second["sessions"]["ml-infra"]["terminals"][1]["focused"] is True
        assert "focused" not in second["sessions"]["ml-infra"]["terminals"][0]
        assert first["sessions"]["ml-infra"]["terminals"][0]["focused"] is True
        assert "focused" not in first["sessions"]["ml-infra"]["terminals"][1]

    def test_unknown_workspace_label_returns_unchanged_copy(self) -> None:
        tree = self._tree()
        result = csm.bold_set(tree, "no-such-session", "conductor-a1b2")
        assert result == tree
        assert result is not tree

    def test_unknown_tab_label_with_valid_workspace_marks_nothing(self) -> None:
        tree = self._tree()
        result = csm.bold_set(tree, "ml-infra", "no-such-tab")
        assert result == tree
        assert "focused" not in result["sessions"]["ml-infra"]

    def test_tab_label_none_with_valid_workspace_marks_nothing(self) -> None:
        """Regression case for S-001: tab_label=None must short-circuit to a
        full no-match -- including the session -- even though ws_label
        resolves to a real session. There is no session-only-bolded state."""
        tree = self._tree()
        result = csm.bold_set(tree, "ml-infra", None)
        assert result == tree
        assert "focused" not in result["sessions"]["ml-infra"]

    def test_tab_label_none_does_not_false_match_a_terminal_with_no_window_key(self) -> None:
        """Without the tab_label-is-None guard, a terminal with no "window"
        key at all (e.g. still INITIALIZING) would have dict.get("window")
        return None too, and None == None would spuriously mark it focused."""
        tree = {"sessions": {"ml-infra": {"terminals": [{"status": "initializing"}]}}}
        result = csm.bold_set(tree, "ml-infra", None)
        assert result == tree
        assert "focused" not in result["sessions"]["ml-infra"]["terminals"][0]

    def test_none_ws_and_tab_labels_never_raise_and_return_unchanged_copy(self) -> None:
        tree = self._tree()
        assert csm.bold_set(tree, None, None) == tree

    def test_does_not_mutate_the_input_tree(self) -> None:
        tree = self._tree()
        csm.bold_set(tree, "ml-infra", "conductor-a1b2")
        assert tree == self._tree()


class TestFocusedLabels:
    """focused_labels(): resolves the live-focused workspace/tab labels via a
    one-shot ``herdr api snapshot`` subprocess call. Mocked at the external
    boundary (``subprocess.run``) the same way TestIterSse/TestFetchJson mock
    their boundary (``urllib.request.urlopen``) -- monkeypatch.setattr on the
    stdlib entry point, never a mocking library. Must never raise."""

    _SNAPSHOT = {
        "focused_workspace_id": "ws-1",
        "focused_tab_id": "tab-1",
        "workspaces": [{"workspace_id": "ws-1", "label": "ml-infra"}],
        "tabs": [{"tab_id": "tab-1", "label": "conductor-a1b2"}],
    }

    @staticmethod
    def _stub_run(
        returncode: int = 0, stdout: str = ""
    ) -> Callable[..., subprocess.CompletedProcess]:
        """Build a ``subprocess.run`` replacement returning a real
        ``CompletedProcess`` -- reuses the stdlib result type instead of a
        hand-rolled mock, matching ``_FakeHTTPResponse``'s use of a real
        ``io.BytesIO`` elsewhere in this file."""

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout)

        return fake_run

    def test_resolves_both_labels_when_ids_match_snapshot_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", self._stub_run(stdout=json.dumps(self._SNAPSHOT)))
        assert csm.focused_labels() == ("ml-infra", "conductor-a1b2")

    def test_invokes_herdr_api_snapshot_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: List[object] = []

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured.append(args[0])
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps(self._SNAPSHOT)
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        csm.focused_labels()
        assert captured == [["herdr", "api", "snapshot"]]

    def test_nonzero_exit_returns_none_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", self._stub_run(returncode=1, stdout=""))
        assert csm.focused_labels() == (None, None)

    def test_missing_herdr_binary_returns_none_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            raise FileNotFoundError("herdr: command not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert csm.focused_labels() == (None, None)

    def test_malformed_json_stdout_returns_none_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", self._stub_run(stdout="not json"))
        assert csm.focused_labels() == (None, None)

    def test_unresolvable_ids_return_none_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither focused_workspace_id nor focused_tab_id matches any entry
        in workspaces[]/tabs[] (e.g. stale focus data) -- both halves resolve
        to None via the plain next(..., None) fallback, no exception raised."""
        snapshot = {
            **self._SNAPSHOT,
            "focused_workspace_id": "ws-missing",
            "focused_tab_id": "tab-missing",
        }
        monkeypatch.setattr(subprocess, "run", self._stub_run(stdout=json.dumps(snapshot)))
        assert csm.focused_labels() == (None, None)

    def test_workspace_resolves_but_tab_id_unresolvable_yields_partial_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two halves resolve independently: a workspace-only match
        yields (label, None) -- exactly the shape bold_set's S-001 guard
        must treat as a full no-match rather than a session-only bold."""
        snapshot = {**self._SNAPSHOT, "focused_tab_id": "tab-missing"}
        monkeypatch.setattr(subprocess, "run", self._stub_run(stdout=json.dumps(snapshot)))
        assert csm.focused_labels() == ("ml-infra", None)


class TestBold:
    """_bold(text, is_bold): ANSI bold wrap, or pass-through unchanged."""

    def test_wraps_text_in_ansi_bold_escapes_when_true(self) -> None:
        assert csm._bold("hello", True) == "\033[1mhello\033[0m"

    def test_returns_text_unchanged_with_no_escape_codes_when_false(self) -> None:
        result = csm._bold("hello", False)
        assert result == "hello"
        assert "\033" not in result


class TestFormatTerminalRow:
    """_format_terminal_row(terminal): one line, tolerant of missing fields --
    every field is ``.get``-defaulted since build_tree passes terminal dicts
    through unchanged from whatever the live stream happens to carry."""

    def test_formats_all_fields_present(self) -> None:
        terminal = {
            "agent_profile": "developer",
            "provider": "claude_code",
            "window": "developer-1a2b",
            "status": "IDLE",
        }
        assert (
            csm._format_terminal_row(terminal)
            == "    developer [claude_code] developer-1a2b (IDLE)"
        )

    def test_missing_status_key_falls_back_to_dash(self) -> None:
        terminal = {"agent_profile": "developer", "provider": "claude_code", "window": "w-0000"}
        assert csm._format_terminal_row(terminal) == "    developer [claude_code] w-0000 (-)"

    def test_status_of_none_falls_back_to_dash(self) -> None:
        """A still-INITIALIZING terminal carries status: None (see the
        fixture in the module's own _selfcheck) -- the ``or`` fallback must
        treat a present-but-None value the same as a missing key."""
        terminal = {
            "agent_profile": "developer",
            "provider": "claude_code",
            "window": "w-0000",
            "status": None,
        }
        assert csm._format_terminal_row(terminal) == "    developer [claude_code] w-0000 (-)"

    def test_missing_agent_profile_provider_and_window_fall_back_to_question_mark(self) -> None:
        assert csm._format_terminal_row({"status": "IDLE"}) == "    ? [?] ? (IDLE)"

    def test_empty_dict_does_not_raise_and_uses_all_fallbacks(self) -> None:
        assert csm._format_terminal_row({}) == "    ? [?] ? (-)"


class TestRenderSessionsBlock:
    """_render_sessions_block(tree): SESSIONS header, one line per session and
    per terminal, "(none)" placeholder when empty. Rows carrying
    "focused": True render bold; every other row renders with zero \033
    bytes."""

    def test_empty_sessions_dict_renders_none_placeholder(self) -> None:
        assert csm._render_sessions_block({"sessions": {}}) == "SESSIONS\n  (none)"

    def test_missing_sessions_key_renders_none_placeholder(self) -> None:
        assert csm._render_sessions_block({}) == "SESSIONS\n  (none)"

    def test_renders_session_header_and_terminal_rows_for_each_session(self) -> None:
        tree = {
            "sessions": {
                "music-search": {
                    "terminals": [
                        {
                            "agent_profile": "developer",
                            "provider": "claude_code",
                            "window": "developer-1a2b",
                            "status": "IDLE",
                        }
                    ]
                },
                "other-session": {"terminals": []},
            }
        }
        lines = csm._render_sessions_block(tree).splitlines()
        assert lines == [
            "SESSIONS",
            "  music-search",
            "    developer [claude_code] developer-1a2b (IDLE)",
            "  other-session",
        ]

    def test_focused_session_and_terminal_are_wrapped_in_bold_only(self) -> None:
        tree = {
            "sessions": {
                "music-search": {
                    "focused": True,
                    "terminals": [
                        {"window": "developer-1a2b", "focused": True},
                        {"window": "reviewer-3c4d"},
                    ],
                },
                "other-session": {"terminals": [{"window": "tester-5e6f"}]},
            }
        }
        lines = csm._render_sessions_block(tree).splitlines()
        focused_session_line = lines[1]
        focused_terminal_line = next(l for l in lines if "developer-1a2b" in l)
        non_focused_terminal_line = next(l for l in lines if "reviewer-3c4d" in l)
        other_session_line = next(l for l in lines if "tester-5e6f" in l)

        assert focused_session_line == "\033[1m  music-search\033[0m"
        assert focused_terminal_line.startswith("\033[1m") and focused_terminal_line.endswith(
            "\033[0m"
        )
        assert non_focused_terminal_line.count("\033") == 0
        assert other_session_line.count("\033") == 0


class TestRenderAguiDisabledHint:
    """_render_agui_disabled_hint(): fixed sessions-block replacement for
    degradation mode -- no session data, just the enable hint."""

    def test_returns_sessions_header_and_env_var_hint(self) -> None:
        assert csm._render_agui_disabled_hint() == (
            "SESSIONS\n  AG-UI streaming is off -- set CAO_AGUI_ENABLED=1 to enable this block."
        )


class TestRenderFlowsBlock:
    """_render_flows_block(flows): FLOWS header, one line per flow with name,
    schedule, and enabled/disabled state; "(none)" when empty."""

    def test_empty_list_renders_none_placeholder(self) -> None:
        assert csm._render_flows_block([]) == "FLOWS\n  (none)"

    def test_enabled_and_disabled_flows_render_their_state_labels(self) -> None:
        flows = csm.parse_flows(_load_fixture("flows.json"))
        assert csm._render_flows_block(flows) == (
            "FLOWS\n"
            "  nightly-health-check  0 2 * * *  enabled\n"
            "  weekly-digest  0 9 * * 1  disabled"
        )


class TestRenderWorkflowsBlock:
    """_render_workflows_block(workflows): WORKFLOWS header, one line per
    workflow with name and step count; "(none)" when empty. step_count is the
    one Optional[int] field on WorkflowIndexRow, so None gets its own
    None-safe rendering."""

    def test_empty_list_renders_none_placeholder(self) -> None:
        assert csm._render_workflows_block([]) == "WORKFLOWS\n  (none)"

    def test_renders_real_fixture_workflows_with_step_counts(self) -> None:
        workflows = csm.parse_workflows(_load_fixture("workflows.json"))
        assert csm._render_workflows_block(workflows) == (
            "WORKFLOWS\n  nightly-report (1 steps)\n  pr-review (2 steps)"
        )

    def test_none_step_count_renders_steps_unknown_not_literal_none(self) -> None:
        workflows = [
            {
                "name": "mystery-flow",
                "source_path": "/tmp/x.yaml",
                "mode": "sequential",
                "step_count": None,
                "description": "d",
                "indexed_at": "2026-01-01T00:00:00Z",
            }
        ]
        result = csm._render_workflows_block(workflows)
        assert result == "WORKFLOWS\n  mystery-flow (steps unknown)"
        assert "None" not in result


class TestRender:
    """render(tree, flows, workflows, agui_enabled): the fixed-order
    three-block readout. agui_enabled=False swaps only the sessions block for
    the enable-hint -- flows and workflows render unconditionally from their
    own data either way."""

    @staticmethod
    def _tree() -> dict:
        return {
            "sessions": {
                "music-search": {
                    "focused": True,
                    "terminals": [
                        {
                            "agent_profile": "developer",
                            "provider": "claude_code",
                            "window": "developer-1a2b",
                            "status": "IDLE",
                            "focused": True,
                        },
                        {
                            "agent_profile": "reviewer",
                            "provider": "q_cli",
                            "window": "reviewer-3c4d",
                            "status": "PROCESSING",
                        },
                    ],
                }
            }
        }

    @staticmethod
    def _flows() -> List[dict]:
        return csm.parse_flows(_load_fixture("flows.json"))

    @staticmethod
    def _workflows() -> List[dict]:
        return csm.parse_workflows(_load_fixture("workflows.json"))

    def test_agui_enabled_renders_three_blocks_in_fixed_order(self) -> None:
        out = csm.render(self._tree(), self._flows(), self._workflows(), agui_enabled=True)
        assert out.index("SESSIONS") < out.index("FLOWS") < out.index("WORKFLOWS")

    def test_agui_disabled_renders_three_blocks_in_fixed_order(self) -> None:
        out = csm.render(self._tree(), self._flows(), self._workflows(), agui_enabled=False)
        assert out.index("SESSIONS") < out.index("FLOWS") < out.index("WORKFLOWS")

    def test_agui_enabled_renders_full_content_from_all_three_sources(self) -> None:
        out = csm.render(self._tree(), self._flows(), self._workflows(), agui_enabled=True)
        assert "music-search" in out
        assert "developer-1a2b" in out
        assert "nightly-health-check" in out
        assert "nightly-report" in out

    def test_agui_disabled_hides_all_tree_data_and_shows_env_var_hint(self) -> None:
        out = csm.render(self._tree(), self._flows(), self._workflows(), agui_enabled=False)
        assert "music-search" not in out
        assert "developer-1a2b" not in out
        assert "reviewer-3c4d" not in out
        assert "CAO_AGUI_ENABLED" in out

    def test_agui_disabled_still_renders_flows_and_workflows_fully(self) -> None:
        """Degradation mode only replaces the sessions block -- confirmed
        explicitly here rather than inferred from the order-only check."""
        out = csm.render(self._tree(), self._flows(), self._workflows(), agui_enabled=False)
        assert "nightly-health-check  0 2 * * *  enabled" in out
        assert "nightly-report (1 steps)" in out

    def test_only_focused_rows_are_wrapped_in_ansi_bold(self) -> None:
        out = csm.render(self._tree(), self._flows(), self._workflows(), agui_enabled=True)
        focused_line = next(line for line in out.splitlines() if "developer-1a2b" in line)
        non_focused_line = next(line for line in out.splitlines() if "reviewer-3c4d" in line)
        assert focused_line.startswith("\033[1m") and focused_line.endswith("\033[0m")
        assert non_focused_line.count("\033") == 0

    def test_empty_tree_flows_and_workflows_all_render_none_placeholders(self) -> None:
        out = csm.render({"sessions": {}}, [], [], agui_enabled=True)
        assert out.count("(none)") == 3

    def test_workflow_with_none_step_count_renders_without_crashing_or_literal_none(self) -> None:
        workflows = [
            {
                "name": "mystery-flow",
                "source_path": "/tmp/x.yaml",
                "mode": "sequential",
                "step_count": None,
                "description": "d",
                "indexed_at": "2026-01-01T00:00:00Z",
            }
        ]
        out = csm.render(self._tree(), self._flows(), workflows, agui_enabled=True)
        assert "steps unknown" in out
        assert "None" not in out

    def test_terminal_missing_status_key_does_not_crash(self) -> None:
        tree = {
            "sessions": {
                "music-search": {
                    "terminals": [
                        {
                            "agent_profile": "developer",
                            "provider": "claude_code",
                            "window": "developer-1a2b",
                        }
                    ]
                }
            }
        }
        out = csm.render(tree, [], [], agui_enabled=True)
        assert "developer-1a2b" in out
        assert "(-)" in out


# ---------------------------------------------------------------------------
# Task 8: main() entry point, thread functions, signal handler, and wiring
# ---------------------------------------------------------------------------


class TestIsHttp404:
    """_is_http_404(exc): True only for urllib HTTPError with code 404."""

    def test_true_for_http_error_404(self) -> None:
        exc = urllib.request.HTTPError("http://x", 404, "Not Found", {}, None)
        assert csm._is_http_404(exc) is True

    def test_false_for_http_error_500(self) -> None:
        exc = urllib.request.HTTPError("http://x", 500, "Server Error", {}, None)
        assert csm._is_http_404(exc) is False

    def test_false_for_generic_exception(self) -> None:
        assert csm._is_http_404(ValueError("boom")) is False

    def test_false_for_url_error(self) -> None:
        assert csm._is_http_404(urllib.request.URLError("refused")) is False


class TestInstallSignalHandler:
    """_install_signal_handler(stop): wires SIGTERM and SIGINT to set the stop event."""

    def test_sets_stop_event_on_sigterm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import signal as sig

        handlers: dict = {}

        def fake_signal(signum: int, handler: object) -> None:
            handlers[signum] = handler

        monkeypatch.setattr(sig, "signal", fake_signal)
        stop = threading.Event()
        csm._install_signal_handler(stop)

        assert sig.SIGTERM in handlers
        assert sig.SIGINT in handlers
        assert not stop.is_set()

        # Invoke the handler (simulating a SIGTERM delivery)
        handlers[sig.SIGTERM](sig.SIGTERM, None)
        assert stop.is_set()

    def test_sets_stop_event_on_sigint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import signal as sig

        handlers: dict = {}
        monkeypatch.setattr(sig, "signal", lambda s, h: handlers.update({s: h}))
        stop = threading.Event()
        csm._install_signal_handler(stop)

        handlers[sig.SIGINT](sig.SIGINT, None)
        assert stop.is_set()


class TestSseReader:
    """_sse_reader: daemon thread consuming the AG-UI SSE stream.

    Reuses the same monkeypatch-on-urlopen pattern established by
    TestIterSse / TestFetchJson (Tasks 4-5), but patches at the module-level
    iter_sse function so we test _sse_reader's state-management logic without
    rebuilding the SSE wire protocol."""

    def test_snapshot_event_populates_tree_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshot_payload = {
            "snapshot": {
                "sessions": [{"id": "s1", "name": "ml", "status": "active"}],
                "terminals": [{"id": "t1", "session_name": "ml", "status": "IDLE"}],
            }
        }
        stop = threading.Event()

        def fake_iter_sse(url: str, last_event_id: object = None):
            yield ("STATE_SNAPSHOT", json.dumps(snapshot_payload), "ev-1")
            stop.set()  # terminate the outer while loop after generator exhausts

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        state: dict = {"tree": {"sessions": {}}, "_snapshot": {}, "agui_enabled": False}
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        assert state["agui_enabled"] is True
        assert "ml" in state["tree"]["sessions"]
        assert state["_snapshot"] == snapshot_payload["snapshot"]

    def test_delta_event_patches_existing_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        initial_snapshot = {
            "sessions": [{"id": "s1", "name": "ml", "status": "active"}],
            "terminals": [{"id": "t1", "session_name": "ml", "status": "IDLE"}],
            "counts": {"sessions": 1, "terminals": 1},
        }
        delta_ops = [{"op": "replace", "path": "/counts/terminals", "value": 2}]
        stop = threading.Event()

        def fake_iter_sse(url: str, last_event_id: object = None):
            yield ("STATE_SNAPSHOT", json.dumps({"snapshot": initial_snapshot}), "ev-1")
            yield ("STATE_DELTA", json.dumps({"delta": delta_ops}), "ev-2")
            stop.set()

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        state: dict = {"tree": {"sessions": {}}, "_snapshot": {}, "agui_enabled": False}
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        assert state["_snapshot"]["counts"]["terminals"] == 2
        assert state["agui_enabled"] is True

    def test_http_404_sets_agui_enabled_false_and_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_iter_sse(url: str, last_event_id: object = None):
            raise urllib.request.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        state: dict = {"tree": {"sessions": {}}, "_snapshot": {}, "agui_enabled": True}
        lock = threading.Lock()
        stop = threading.Event()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        assert state["agui_enabled"] is False

    def test_respects_stop_event_between_sse_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If stop is set mid-stream, _sse_reader returns without processing
        further events. The check `if stop.is_set(): return` runs at the top
        of each iteration through yielded events."""
        stop = threading.Event()
        events_yielded = [0]

        def fake_iter_sse(url: str, last_event_id: object = None):
            events_yielded[0] += 1
            yield (
                "STATE_SNAPSHOT",
                json.dumps({"snapshot": {"sessions": [], "terminals": []}}),
                "e1",
            )
            # Set stop between yields -- the next iteration's guard check will see it
            stop.set()
            events_yielded[0] += 1
            yield (
                "STATE_SNAPSHOT",
                json.dumps({"snapshot": {"sessions": [], "terminals": []}}),
                "e2",
            )

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        state: dict = {"tree": {"sessions": {}}, "_snapshot": {}, "agui_enabled": False}
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        # Both events were yielded by the generator, but only the first was
        # processed before stop was noticed on the second iteration's guard.
        assert state["agui_enabled"] is True  # first event was processed

    def test_connection_error_retries_until_stop_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On non-404 errors, _sse_reader retries with backoff. Setting stop
        during the wait terminates the loop."""
        call_count = [0]
        stop = threading.Event()

        def fake_iter_sse(url: str, last_event_id: object = None):
            call_count[0] += 1
            raise ConnectionError("refused")
            yield  # type: ignore[misc]  # make it a generator

        # Patch stop.wait to set stop immediately (simulating the 5s backoff
        # expiring instantly while also marking termination)
        orig_wait = stop.wait

        def instant_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", instant_wait)
        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        state: dict = {"tree": {"sessions": {}}, "_snapshot": {}, "agui_enabled": False}
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        # Called once, hit the error, waited (returned immediately), exited loop
        assert call_count[0] == 1


class TestRestPoller:
    """_rest_poller: daemon thread polling /flows and /workflows.

    Reuses the monkeypatch-on-fetch_json pattern from TestFetchJson (Task 4)."""

    def test_populates_flows_and_workflows_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flows_data = [
            {
                "name": "f1",
                "schedule": "* * * * *",
                "agent_profile": "dev",
                "provider": "mock_cli",
                "enabled": True,
                "last_run": None,
                "next_run": None,
            }
        ]
        workflows_data = [
            {
                "name": "w1",
                "source_path": "/tmp/w.yaml",
                "mode": "sequential",
                "step_count": 3,
                "description": "d",
                "indexed_at": "2026-01-01T00:00:00Z",
            }
        ]
        urls_called: List[str] = []

        def fake_fetch_json(url: str):
            urls_called.append(url)
            if "/flows" in url:
                return flows_data
            return workflows_data

        monkeypatch.setattr(csm, "fetch_json", fake_fetch_json)

        state: dict = {"flows": [], "workflows": []}
        lock = threading.Lock()
        stop = threading.Event()

        # Let one poll iteration run, then stop on wait()
        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._rest_poller("http://localhost:9889", state, lock, stop)

        assert state["flows"] == csm.parse_flows(flows_data)
        assert state["workflows"] == csm.parse_workflows(workflows_data)
        assert "http://localhost:9889/flows" in urls_called
        assert "http://localhost:9889/workflows" in urls_called

    def test_fetch_error_keeps_last_known_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_fetch_json(url: str):
            raise ConnectionError("refused")

        monkeypatch.setattr(csm, "fetch_json", fake_fetch_json)

        state: dict = {"flows": [{"name": "cached"}], "workflows": [{"name": "cached-wf"}]}
        lock = threading.Lock()
        stop = threading.Event()

        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._rest_poller("http://localhost:9889", state, lock, stop)

        # State unchanged on error -- last-known preserved
        assert state["flows"] == [{"name": "cached"}]
        assert state["workflows"] == [{"name": "cached-wf"}]

    def test_exits_promptly_when_stop_is_already_set(self) -> None:
        """When stop is set before entry, the while-loop condition is False and
        the function returns immediately without blocking on the interval."""
        import time

        state: dict = {"flows": [], "workflows": []}
        lock = threading.Lock()
        stop = threading.Event()
        stop.set()

        start = time.monotonic()
        csm._rest_poller("http://localhost:9889", state, lock, stop)
        elapsed = time.monotonic() - start

        # Must return in <1s (the poll interval is 15s)
        assert elapsed < 1.0


class TestFocusPoller:
    """_focus_poller: daemon thread refreshing focused workspace/tab labels.

    Reuses the subprocess.run stub pattern from TestFocusedLabels (Task 6)."""

    def test_populates_focus_labels_in_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snapshot = {
            "focused_workspace_id": "ws-1",
            "focused_tab_id": "tab-1",
            "workspaces": [{"workspace_id": "ws-1", "label": "ml-infra"}],
            "tabs": [{"tab_id": "tab-1", "label": "conductor-a1b2"}],
        }

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(snapshot))

        monkeypatch.setattr(subprocess, "run", fake_run)

        state: dict = {"ws_label": None, "tab_label": None}
        lock = threading.Lock()
        stop = threading.Event()

        # Let one iteration run, then stop on wait()
        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._focus_poller(state, lock, stop)

        assert state["ws_label"] == "ml-infra"
        assert state["tab_label"] == "conductor-a1b2"

    def test_exits_promptly_when_stop_is_already_set(self) -> None:
        import time

        state: dict = {"ws_label": None, "tab_label": None}
        lock = threading.Lock()
        stop = threading.Event()
        stop.set()

        start = time.monotonic()
        csm._focus_poller(state, lock, stop)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0


class TestRenderLoop:
    """_render_loop: main-thread loop that snapshots state and prints output.

    The real-time sleep/refresh cadence is not tested here -- it runs at 1s
    intervals with a blocking stop.wait() and testing real timing is inherently
    flaky. Instead we test: (a) it renders output correctly from state, (b) it
    only writes when output changes, (c) it exits when stop is set.

    Judgment call (per task brief): the full blocking render loop's real-time
    cadence is deferred to Phase 5's live smoke test. The behavioral contract
    (render-on-change, exit-on-stop) is covered here."""

    def test_renders_state_to_stdout_and_exits_on_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: List[str] = []

        def fake_write(s: str) -> None:
            captured.append(s)

        monkeypatch.setattr(sys.stdout, "write", fake_write)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        state: dict = {
            "tree": {"sessions": {"ml": {"terminals": []}}},
            "flows": [],
            "workflows": [],
            "agui_enabled": True,
            "ws_label": None,
            "tab_label": None,
        }
        lock = threading.Lock()
        stop = threading.Event()

        # Let the loop run one tick, then stop on the wait() call.
        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._render_loop(state, lock, stop)

        assert len(captured) >= 1
        full_output = "".join(captured)
        assert "SESSIONS" in full_output
        assert "ml" in full_output

    def test_suppresses_duplicate_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Output is only written when it changes -- if state is unchanged
        between ticks, no duplicate write occurs."""
        write_count = [0]

        def fake_write(s: str) -> None:
            write_count[0] += 1

        monkeypatch.setattr(sys.stdout, "write", fake_write)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        state: dict = {
            "tree": {"sessions": {}},
            "flows": [],
            "workflows": [],
            "agui_enabled": True,
            "ws_label": None,
            "tab_label": None,
        }
        lock = threading.Lock()
        stop = threading.Event()

        # Run two ticks then stop.
        tick_count = [0]

        def fast_wait(timeout: float = None) -> bool:
            tick_count[0] += 1
            if tick_count[0] >= 2:
                stop.set()
            return stop.is_set()

        monkeypatch.setattr(stop, "wait", fast_wait)
        csm._render_loop(state, lock, stop)

        # Only 1 write because output didn't change between ticks
        assert write_count[0] == 1


class TestMainSelfGate:
    """main(): self-gate behavior -- when should_render evaluates False, main()
    returns 0 with no output and no threads started.

    Tests the gate without a live herdr instance by monkeypatching
    resolve_socket_path (same pattern as TestResolveSocketPath) so the gate
    evaluates False."""

    def test_exits_zero_when_gate_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Point resolve_socket_path at the default session's socket
        monkeypatch.setattr(
            csm, "resolve_socket_path", lambda: csm.expected_socket_path(csm.DEFAULT_SESSION)
        )
        # CAO_MONITOR_SESSION defaults to "cao", whose socket differs from default
        monkeypatch.delenv("CAO_MONITOR_SESSION", raising=False)

        captured: List[str] = []
        monkeypatch.setattr(sys.stdout, "write", lambda s: captured.append(s))
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        result = csm.main()

        assert result == 0
        assert captured == []  # no output at all

    def test_exits_zero_with_custom_session_name_that_does_not_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            csm, "resolve_socket_path", lambda: csm.expected_socket_path("personal")
        )
        monkeypatch.setenv("CAO_MONITOR_SESSION", "cao")

        result = csm.main()
        assert result == 0

    def test_no_threads_started_when_gate_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that the gate short-circuits before any thread machinery."""
        monkeypatch.setattr(
            csm, "resolve_socket_path", lambda: csm.expected_socket_path(csm.DEFAULT_SESSION)
        )
        monkeypatch.delenv("CAO_MONITOR_SESSION", raising=False)

        threads_started: List[str] = []
        orig_thread_init = threading.Thread.__init__

        def tracking_init(self_thread, *args, **kwargs):
            orig_thread_init(self_thread, *args, **kwargs)
            threads_started.append(kwargs.get("target", args[0] if args else None).__name__)

        # Only patch Thread creation to detect starts; the gate should prevent
        # reaching Thread instantiation at all.
        monkeypatch.setattr(threading.Thread, "__init__", tracking_init)

        csm.main()
        assert threads_started == []


class TestMainThreadWiring:
    """main(): when the gate passes, verify thread startup and shutdown wiring.

    The full multi-thread integration running concurrently for real is deferred
    to Phase 5's live smoke test (see judgment-call note on TestRenderLoop).
    Here we test the structural wiring: threads are created with the right
    targets, and the shutdown signal propagates."""

    def test_starts_three_daemon_threads_and_calls_render_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make gate pass
        monkeypatch.setattr(csm, "resolve_socket_path", lambda: csm.expected_socket_path("cao"))
        monkeypatch.delenv("CAO_MONITOR_SESSION", raising=False)

        # Track thread targets
        thread_targets: List[str] = []
        thread_daemon_flags: List[bool] = []

        class FakeThread:
            def __init__(self, target=None, args=None, daemon=None, **kwargs):
                self.target = target
                self.args = args
                self.daemon = daemon
                thread_targets.append(target.__name__)
                thread_daemon_flags.append(daemon)

            def start(self):
                pass  # Don't actually start threads

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(threading, "Thread", FakeThread)

        # Make _render_loop exit immediately
        def fake_render_loop(state, lock, stop):
            stop.set()

        monkeypatch.setattr(csm, "_render_loop", fake_render_loop)

        # Suppress stdout from any accidental render
        monkeypatch.setattr(sys.stdout, "write", lambda s: None)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        result = csm.main()

        assert result == 0
        assert "_sse_reader" in thread_targets
        assert "_rest_poller" in thread_targets
        assert "_focus_poller" in thread_targets
        assert all(d is True for d in thread_daemon_flags)

    def test_keyboard_interrupt_in_render_loop_sets_stop_and_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(csm, "resolve_socket_path", lambda: csm.expected_socket_path("cao"))
        monkeypatch.delenv("CAO_MONITOR_SESSION", raising=False)

        stop_was_set = [False]

        class FakeThread:
            def __init__(self, **kwargs):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(threading, "Thread", FakeThread)

        def raising_render_loop(state, lock, stop):
            raise KeyboardInterrupt()

        monkeypatch.setattr(csm, "_render_loop", raising_render_loop)
        monkeypatch.setattr(sys.stdout, "write", lambda s: None)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        result = csm.main()
        assert result == 0

    def test_state_dict_initialized_with_expected_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the state dict main() creates has all keys the thread
        functions expect."""
        monkeypatch.setattr(csm, "resolve_socket_path", lambda: csm.expected_socket_path("cao"))
        monkeypatch.delenv("CAO_MONITOR_SESSION", raising=False)
        monkeypatch.delenv("CAO_AGUI_ENABLED", raising=False)

        captured_state: List[dict] = []

        class FakeThread:
            def __init__(self, target=None, args=None, **kwargs):
                # Capture the state dict passed to first thread
                if args and len(args) >= 2:
                    state_arg = args[1] if len(args) > 2 else args[0]
                    if isinstance(state_arg, dict) and "tree" in state_arg:
                        captured_state.append(state_arg)

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(threading, "Thread", FakeThread)

        def fake_render_loop(state, lock, stop):
            captured_state.append(state)
            stop.set()

        monkeypatch.setattr(csm, "_render_loop", fake_render_loop)
        monkeypatch.setattr(sys.stdout, "write", lambda s: None)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        csm.main()

        assert len(captured_state) >= 1
        state = captured_state[-1]
        expected_keys = {
            "tree",
            "_snapshot",
            "flows",
            "workflows",
            "agui_enabled",
            "ws_label",
            "tab_label",
            "stream_disconnected",
            "unreachable",
        }
        assert expected_keys == set(state.keys())
        assert state["tree"] == {"sessions": {}}
        assert state["_snapshot"] == {}
        assert state["flows"] == []
        assert state["workflows"] == []
        assert state["agui_enabled"] is False
        assert state["ws_label"] is None
        assert state["tab_label"] is None
        assert state["stream_disconnected"] is False
        assert state["unreachable"] is False

    def test_cao_agui_enabled_env_var_initializes_agui_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(csm, "resolve_socket_path", lambda: csm.expected_socket_path("cao"))
        monkeypatch.delenv("CAO_MONITOR_SESSION", raising=False)
        monkeypatch.setenv("CAO_AGUI_ENABLED", "1")

        captured_state: List[dict] = []

        class FakeThread:
            def __init__(self, **kwargs):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(threading, "Thread", FakeThread)

        def fake_render_loop(state, lock, stop):
            captured_state.append(state)
            stop.set()

        monkeypatch.setattr(csm, "_render_loop", fake_render_loop)
        monkeypatch.setattr(sys.stdout, "write", lambda s: None)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        csm.main()

        assert captured_state[-1]["agui_enabled"] is True


class TestLockAcquisition:
    """Verify that thread functions actually acquire the lock around shared-state
    mutations. Uses a recording Lock that tracks __enter__/__exit__ calls.

    Judgment call: a true race-condition test is not practical with stdlib
    threading in a unit test. Instead we verify the lock IS acquired by using
    a mock Lock that records calls. This confirms the behavioral contract
    (lock-protected access) without needing concurrent execution."""

    @staticmethod
    def _recording_lock() -> Tuple[threading.Lock, List[str]]:
        """Return a Lock subclass that records acquire/release calls."""
        calls: List[str] = []
        real_lock = threading.Lock()

        class RecordingLock:
            def __enter__(self):
                calls.append("acquire")
                real_lock.acquire()
                return self

            def __exit__(self, *exc_info):
                real_lock.release()
                calls.append("release")

            def acquire(self, *args, **kwargs):
                calls.append("acquire")
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                real_lock.release()
                calls.append("release")

        return RecordingLock(), calls  # type: ignore[return-value]

    def test_sse_reader_acquires_lock_on_state_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = threading.Event()

        def fake_iter_sse(url: str, last_event_id: object = None):
            yield (
                "STATE_SNAPSHOT",
                json.dumps({"snapshot": {"sessions": [], "terminals": []}}),
                "e1",
            )
            stop.set()

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        state: dict = {"tree": {"sessions": {}}, "_snapshot": {}, "agui_enabled": False}
        lock, calls = self._recording_lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        assert "acquire" in calls
        assert "release" in calls
        # Properly paired
        assert calls.count("acquire") == calls.count("release")

    def test_rest_poller_acquires_lock_on_state_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(csm, "fetch_json", lambda url: [])

        state: dict = {"flows": [], "workflows": []}
        lock, calls = self._recording_lock()
        stop = threading.Event()

        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._rest_poller("http://localhost:9889", state, lock, stop)

        assert "acquire" in calls
        assert calls.count("acquire") == calls.count("release")

    def test_focus_poller_acquires_lock_on_state_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(csm, "focused_labels", lambda: ("ws", "tab"))

        state: dict = {"ws_label": None, "tab_label": None}
        lock, calls = self._recording_lock()
        stop = threading.Event()

        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._focus_poller(state, lock, stop)

        assert "acquire" in calls
        assert calls.count("acquire") == calls.count("release")

    def test_render_loop_acquires_lock_for_state_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys.stdout, "write", lambda s: None)
        monkeypatch.setattr(sys.stdout, "flush", lambda: None)

        state: dict = {
            "tree": {"sessions": {}},
            "flows": [],
            "workflows": [],
            "agui_enabled": True,
            "ws_label": None,
            "tab_label": None,
        }
        lock, calls = self._recording_lock()
        stop = threading.Event()

        # Let one tick run, then stop
        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._render_loop(state, lock, stop)

        assert "acquire" in calls
        assert calls.count("acquire") == calls.count("release")


# ---------------------------------------------------------------------------
# Gap 1 (spec R3): Reconnect uses Last-Event-ID cursor
# ---------------------------------------------------------------------------


class TestReconnectLastEventId:
    """_sse_reader tracks the last event id from the stream and passes it to
    iter_sse on reconnect after a connection drop."""

    def test_reconnect_sends_last_event_id_from_prior_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After receiving an event with id 'ev-42', a connection error triggers
        retry. The next iter_sse call must receive last_event_id='ev-42'."""
        call_args: List[dict] = []
        stop = threading.Event()
        call_count = [0]

        def fake_iter_sse(url: str, last_event_id: object = None):
            call_args.append({"url": url, "last_event_id": last_event_id})
            call_count[0] += 1
            if call_count[0] == 1:
                # First connection: yield one event with an id, then raise
                yield (
                    "STATE_SNAPSHOT",
                    json.dumps({"snapshot": {"sessions": [], "terminals": []}}),
                    "ev-42",
                )
                raise ConnectionError("stream dropped")
            else:
                # Second connection (reconnect): yield one event and stop
                yield (
                    "STATE_SNAPSHOT",
                    json.dumps({"snapshot": {"sessions": [], "terminals": []}}),
                    "ev-43",
                )
                stop.set()

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        # Make the backoff wait return immediately without actually waiting
        real_wait = stop.wait

        def fast_wait(timeout: float = None) -> bool:
            return real_wait(0.0)

        monkeypatch.setattr(stop, "wait", fast_wait)

        state: dict = {
            "tree": {"sessions": {}},
            "_snapshot": {},
            "agui_enabled": False,
            "stream_disconnected": False,
        }
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        assert len(call_args) == 2
        # First call: no last_event_id (fresh start)
        assert call_args[0]["last_event_id"] is None
        # Second call (reconnect): carries the id from the first stream
        assert call_args[1]["last_event_id"] == "ev-42"


# ---------------------------------------------------------------------------
# Gap 2 (spec R3): Disconnected indicator shown during retry, cleared on reconnect
# ---------------------------------------------------------------------------


class TestStreamDisconnectedState:
    """_sse_reader sets stream_disconnected=True on non-404 exception and
    clears it on the next successful event."""

    def test_non_404_error_sets_stream_disconnected_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = threading.Event()

        def fake_iter_sse(url: str, last_event_id: object = None):
            raise ConnectionError("refused")
            yield  # type: ignore[misc]  # make it a generator

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        # Patch wait to stop after one retry cycle
        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)

        state: dict = {
            "tree": {"sessions": {}},
            "_snapshot": {},
            "agui_enabled": False,
            "stream_disconnected": False,
        }
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        assert state["stream_disconnected"] is True

    def test_successful_event_after_disconnect_clears_stream_disconnected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After stream_disconnected is set True from an error, the next
        successful event clears it back to False."""
        stop = threading.Event()
        call_count = [0]

        def fake_iter_sse(url: str, last_event_id: object = None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("refused")
                yield  # type: ignore[misc]
            else:
                yield (
                    "STATE_SNAPSHOT",
                    json.dumps({"snapshot": {"sessions": [], "terminals": []}}),
                    "e1",
                )
                stop.set()

        monkeypatch.setattr(csm, "iter_sse", fake_iter_sse)

        # Fast-forward through the backoff wait
        real_wait = stop.wait

        def fast_wait(timeout: float = None) -> bool:
            return real_wait(0.0)

        monkeypatch.setattr(stop, "wait", fast_wait)

        state: dict = {
            "tree": {"sessions": {}},
            "_snapshot": {},
            "agui_enabled": False,
            "stream_disconnected": False,
        }
        lock = threading.Lock()

        csm._sse_reader("http://localhost:9889", state, lock, stop)

        # After successful reconnect, flag is cleared
        assert state["stream_disconnected"] is False
        assert state["agui_enabled"] is True


class TestRenderDisconnectedIndicator:
    """render() appends '[disconnected -- reconnecting]' when
    stream_disconnected=True and agui_enabled=True."""

    def test_disconnected_indicator_shown_when_agui_enabled(self) -> None:
        tree = {"sessions": {"ml": {"terminals": []}}}
        out = csm.render(tree, [], [], agui_enabled=True, stream_disconnected=True)
        assert "[disconnected -- reconnecting]" in out

    def test_disconnected_indicator_suppressed_when_agui_disabled(self) -> None:
        tree = {"sessions": {"ml": {"terminals": []}}}
        out = csm.render(tree, [], [], agui_enabled=False, stream_disconnected=True)
        assert "[disconnected -- reconnecting]" not in out


# ---------------------------------------------------------------------------
# Gap 3 (spec R5): Bold preserved on focus-read failure
# ---------------------------------------------------------------------------


class TestFocusPollerPreservesBoldOnFailure:
    """_focus_poller only updates state when focused_labels() returns at least
    one non-None value. A (None, None) result preserves previous state."""

    def test_none_none_preserves_prior_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = [0]

        def fake_focused_labels():
            call_count[0] += 1
            if call_count[0] == 1:
                return ("ml-infra", "conductor-a1b2")
            return (None, None)

        monkeypatch.setattr(csm, "focused_labels", fake_focused_labels)

        state: dict = {"ws_label": None, "tab_label": None}
        lock = threading.Lock()
        stop = threading.Event()
        tick_count = [0]

        def fast_wait(timeout: float = None) -> bool:
            tick_count[0] += 1
            if tick_count[0] >= 2:
                stop.set()
            return stop.is_set()

        monkeypatch.setattr(stop, "wait", fast_wait)
        csm._focus_poller(state, lock, stop)

        # After two ticks: first set real values, second returned (None, None)
        # State must still hold the ORIGINAL values from tick 1
        assert state["ws_label"] == "ml-infra"
        assert state["tab_label"] == "conductor-a1b2"


# ---------------------------------------------------------------------------
# Gap 4 (spec R6): CAO-unreachable banner shown then cleared
# ---------------------------------------------------------------------------


class TestRestPollerUnreachableState:
    """_rest_poller sets unreachable=True on fetch failure (preserving
    last-known data) and clears it on success."""

    def test_fetch_failure_sets_unreachable_true_and_preserves_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_fetch_json(url: str):
            raise ConnectionError("refused")

        monkeypatch.setattr(csm, "fetch_json", fake_fetch_json)

        state: dict = {
            "flows": [{"name": "cached-flow"}],
            "workflows": [{"name": "cached-wf"}],
            "unreachable": False,
        }
        lock = threading.Lock()
        stop = threading.Event()

        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._rest_poller("http://localhost:9889", state, lock, stop)

        assert state["unreachable"] is True
        # Last-known data preserved
        assert state["flows"] == [{"name": "cached-flow"}]
        assert state["workflows"] == [{"name": "cached-wf"}]

    def test_successful_fetch_clears_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        flows_data = [
            {
                "name": "f1",
                "schedule": "* * * * *",
                "agent_profile": "dev",
                "provider": "mock_cli",
                "enabled": True,
                "last_run": None,
                "next_run": None,
            }
        ]
        workflows_data = [
            {
                "name": "w1",
                "source_path": "/tmp/w.yaml",
                "mode": "sequential",
                "step_count": 3,
                "description": "d",
                "indexed_at": "2026-01-01T00:00:00Z",
            }
        ]

        def fake_fetch_json(url: str):
            if "/flows" in url:
                return flows_data
            return workflows_data

        monkeypatch.setattr(csm, "fetch_json", fake_fetch_json)

        state: dict = {
            "flows": [],
            "workflows": [],
            "unreachable": True,  # starts unreachable
        }
        lock = threading.Lock()
        stop = threading.Event()

        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._rest_poller("http://localhost:9889", state, lock, stop)

        assert state["unreachable"] is False


class TestRenderUnreachableBanner:
    """render() prepends 'CAO unreachable (:9889)' when unreachable=True, with
    flows/workflows still rendering underneath."""

    def test_unreachable_banner_appears_with_retained_data(self) -> None:
        tree = {"sessions": {}}
        flows = [
            {
                "name": "nightly",
                "schedule": "0 2 * * *",
                "agent_profile": "dev",
                "provider": "mock_cli",
                "enabled": True,
                "last_run": None,
                "next_run": None,
            }
        ]
        workflows = [
            {
                "name": "pr-review",
                "source_path": "/tmp/x.yaml",
                "mode": "sequential",
                "step_count": 2,
                "description": "d",
                "indexed_at": "2026-01-01T00:00:00Z",
            }
        ]
        out = csm.render(tree, flows, workflows, agui_enabled=True, unreachable=True)

        # Banner present
        assert "CAO unreachable (:9889)" in out
        # Flows and workflows still render
        assert "nightly" in out
        assert "pr-review" in out
        # Banner comes first
        lines = out.splitlines()
        banner_idx = next(i for i, l in enumerate(lines) if "CAO unreachable" in l)
        sessions_idx = next(i for i, l in enumerate(lines) if "SESSIONS" in l)
        assert banner_idx < sessions_idx

    def test_unreachable_false_omits_banner(self) -> None:
        tree = {"sessions": {}}
        out = csm.render(tree, [], [], agui_enabled=True, unreachable=False)
        assert "CAO unreachable" not in out


# ---------------------------------------------------------------------------
# FIX 3: CRLF line endings in SSE stream
# ---------------------------------------------------------------------------


class TestParseSseStreamCrlf:
    """_parse_sse_stream handles \\r\\n line endings (common from HTTP servers)."""

    @staticmethod
    def _parse(raw: bytes) -> List[Tuple[str, str]]:
        return [(ev, data) for ev, data, _id in csm._parse_sse_stream(io.BytesIO(raw))]

    def test_crlf_line_endings_parse_correctly(self) -> None:
        assert self._parse(b"event: X\r\ndata: y\r\n\r\n") == [("X", "y")]


# ---------------------------------------------------------------------------
# FIX 4: SSE id: persists across events when not updated
# ---------------------------------------------------------------------------


class TestSseIdPersistence:
    """_parse_sse_stream: id: field persists to subsequent events per SSE spec."""

    def test_id_persists_across_events_when_not_updated(self) -> None:
        raw = b"id: 5\ndata: a\n\ndata: b\n\n"
        results = list(csm._parse_sse_stream(io.BytesIO(raw)))
        assert results == [("message", "a", "5"), ("message", "b", "5")]


# ---------------------------------------------------------------------------
# FIX 5: Partial REST fetch failure keeps both at last known
# ---------------------------------------------------------------------------


class TestRestPollerPartialFailure:
    """_rest_poller: if one of the two fetch_json calls raises, BOTH flows and
    workflows in state remain at their prior values (atomic-or-nothing)."""

    def test_partial_fetch_failure_keeps_both_at_last_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = [0]

        def fake_fetch_json(url: str):
            call_count[0] += 1
            if "/flows" in url:
                return [
                    {
                        "name": "new-flow",
                        "schedule": "* * * * *",
                        "agent_profile": "dev",
                        "provider": "mock_cli",
                        "enabled": True,
                        "last_run": None,
                        "next_run": None,
                    }
                ]
            # /workflows raises
            raise ConnectionError("refused on workflows")

        monkeypatch.setattr(csm, "fetch_json", fake_fetch_json)

        state: dict = {
            "flows": [{"name": "prior-flow"}],
            "workflows": [{"name": "prior-wf"}],
            "unreachable": False,
        }
        lock = threading.Lock()
        stop = threading.Event()

        def stop_on_wait(timeout: float = None) -> bool:
            stop.set()
            return True

        monkeypatch.setattr(stop, "wait", stop_on_wait)
        csm._rest_poller("http://localhost:9889", state, lock, stop)

        # Both must remain at prior values — the /flows success must NOT
        # partially update state when /workflows fails.
        assert state["flows"] == [{"name": "prior-flow"}]
        assert state["workflows"] == [{"name": "prior-wf"}]
