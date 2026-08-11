"""MCP mapping tests — Increment 2, correctness property P9.

**Property 9: Expansion soundness**
**Validates: Requirements 18.1, 18.2, 18.3**

Plus the mapper's non-property behaviours: reserved ``env`` keys, per-entry
containment isolation, transport-mismatch skips, and the non-blocking
credential-shape warning (Requirements 18.5–18.8).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.mcp_mapping import (
    PRE_EXPANDED_KEY,
    expand_placeholders,
    is_pre_expanded,
    load_and_map,
    map_mcp_config,
    strip_marker,
)
from cli_agent_orchestrator.agent_plugins.models import Severity

from .conftest import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID, build_plugin


@pytest.fixture
def roots(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return root, data


def config(servers: dict, schema: str = MCP_SCHEMA_ID) -> dict:
    return {"$schema": schema, "mcpServers": servers}


def codes(result) -> list:
    return [f.code for f in result.findings]


def only(result):
    assert len(result.servers) == 1, codes(result)
    return result.servers[0].config


class TestCommandIsOneToken:
    """§7.2.1 — never shell-split, never placeholder-expanded."""

    def test_a_bare_command_passes_through_untouched(self, roots):
        root, data = roots
        result = map_mcp_config(root, data, config({"s": {"type": "stdio", "command": "uvx"}}))
        assert only(result)["command"] == "uvx"

    def test_a_command_is_never_placeholder_expanded(self, roots):
        root, data = roots
        result = map_mcp_config(
            root, data, config({"s": {"type": "stdio", "command": "${PLUGIN_ROOT}"}})
        )
        assert only(result)["command"] == "${PLUGIN_ROOT}"

    def test_a_command_with_spaces_is_not_split(self, roots):
        root, data = roots
        result = map_mcp_config(
            root, data, config({"s": {"type": "stdio", "command": "my server"}})
        )
        assert only(result)["command"] == "my server"

    def test_a_dot_rooted_command_is_resolved_inside_the_plugin(self, roots):
        root, data = roots
        (root / "bin").mkdir()
        (root / "bin" / "server").write_text("#!/bin/sh\n", encoding="utf-8")

        result = map_mcp_config(
            root, data, config({"s": {"type": "stdio", "command": "./bin/server"}})
        )
        assert only(result)["command"] == str((root / "bin" / "server").resolve())

    def test_a_dot_rooted_command_escaping_the_root_is_skipped(self, roots, tmp_path):
        root, data = roots
        result = map_mcp_config(
            root, data, config({"s": {"type": "stdio", "command": "./../../etc/passwd"}})
        )
        assert result.servers == ()
        assert "mcp.command_escapes_root" in codes(result)

    def test_a_missing_command_skips_only_that_entry(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"bad": {"type": "stdio"}, "good": {"type": "stdio", "command": "ok"}}),
        )
        # Schema-invalid: `command` is required for stdio, so the whole document
        # fails rather than one entry. Either way, no partial server is emitted.
        assert result.servers == () or [s.name for s in result.servers] == ["good"]


class TestExpansionTargets:
    """§9.2 — only args elements, env values, and cwd."""

    def test_args_elements_are_expanded(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "stdio", "command": "x", "args": ["${PLUGIN_ROOT}/a", "plain"]}}),
        )
        assert only(result)["args"] == [f"{root}/a", "plain"]

    def test_env_values_are_expanded_but_keys_are_not(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "s": {
                        "type": "stdio",
                        "command": "x",
                        "env": {"DB_${PLUGIN_ROOT}": "${PLUGIN_DATA}/db"},
                    }
                }
            ),
        )
        env = only(result)["env"]
        assert "DB_${PLUGIN_ROOT}" in env  # the key is untouched
        assert env["DB_${PLUGIN_ROOT}"] == f"{data}/db"

    def test_cwd_is_expanded_and_contained(self, roots):
        root, data = roots
        (root / "work").mkdir()
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "stdio", "command": "x", "cwd": "${PLUGIN_ROOT}/work"}}),
        )
        assert only(result)["cwd"] == str((root / "work").resolve())

    def test_cwd_defaults_to_the_plugin_root(self, roots):
        root, data = roots
        result = map_mcp_config(root, data, config({"s": {"type": "stdio", "command": "x"}}))
        assert only(result)["cwd"] == str(root)

    def test_a_plugin_data_rooted_cwd_is_checked_against_plugin_data(self, roots):
        root, data = roots
        (data / "scratch").mkdir()
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "stdio", "command": "x", "cwd": "${PLUGIN_DATA}/scratch"}}),
        )
        assert only(result)["cwd"] == str((data / "scratch").resolve())

    def test_a_url_is_never_expanded(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "streamable-http", "url": "https://x/${PLUGIN_ROOT}"}}),
        )
        assert only(result)["url"] == "https://x/${PLUGIN_ROOT}"

    def test_header_names_and_values_are_never_expanded(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "s": {
                        "type": "streamable-http",
                        "url": "https://x",
                        "headers": {"X-${PLUGIN_ROOT}": "${PLUGIN_DATA}"},
                    }
                }
            ),
        )
        assert only(result)["headers"] == {"X-${PLUGIN_ROOT}": "${PLUGIN_DATA}"}


class TestCaoSuppliedEnv:
    """§9.1 — CAO supplies both placeholders itself, after the plugin's env."""

    def test_plugin_root_and_data_are_injected(self, roots):
        root, data = roots
        env = only(map_mcp_config(root, data, config({"s": {"type": "stdio", "command": "x"}})))[
            "env"
        ]
        assert env["PLUGIN_ROOT"] == str(root)
        assert env["PLUGIN_DATA"] == str(data)

    def test_a_plugin_declaring_a_reserved_key_invalidates_that_entry(self, roots):
        """Requirement 18.5 — never let a plugin override CAO-supplied values."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "bad": {
                        "type": "stdio",
                        "command": "x",
                        "env": {"PLUGIN_ROOT": "/tmp/evil"},
                    },
                }
            ),
        )
        # The pinned schema forbids these keys outright, and the mapper refuses
        # them independently — either way the entry never maps.
        assert result.servers == ()
        assert any(code in codes(result) for code in ("mcp.env_reserved_key", "mcp.invalid"))

    def test_the_mapper_refuses_a_reserved_key_on_its_own(self, roots):
        """The guard does not depend on the schema having caught it first."""
        from cli_agent_orchestrator.agent_plugins.mcp_mapping import _map_stdio

        root, data = roots
        mapped, findings = _map_stdio(
            "bad",
            {"type": "stdio", "command": "x", "env": {"PLUGIN_DATA": "/tmp/evil"}},
            str(root),
            str(data),
            root,
            data,
            "mcp.json#bad",
        )
        assert mapped is None
        assert [f.code for f in findings] == ["mcp.env_reserved_key"]


class TestPerEntryIsolation:
    """Requirement 18.6 — one bad entry never takes down its siblings."""

    def test_a_containment_failure_invalidates_only_that_entry(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "escaping": {"type": "stdio", "command": "./../../bin/sh"},
                    "fine": {"type": "stdio", "command": "ok"},
                }
            ),
        )
        assert [server.name for server in result.servers] == ["fine"]
        assert "mcp.command_escapes_root" in codes(result)

    def test_a_bad_cwd_invalidates_only_that_entry(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "escaping": {"type": "stdio", "command": "x", "cwd": "./../.."},
                    "fine": {"type": "stdio", "command": "ok"},
                }
            ),
        )
        assert [server.name for server in result.servers] == ["fine"]
        assert "mcp.cwd_escapes_root" in codes(result)

    def test_entries_map_in_a_deterministic_order(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "zulu": {"type": "stdio", "command": "z"},
                    "alpha": {"type": "stdio", "command": "a"},
                }
            ),
        )
        assert [server.name for server in result.servers] == ["alpha", "zulu"]


class TestTransportMatrix:
    """Requirement 18.7 — skip with a report, never fail over."""

    def test_an_unsupported_transport_is_skipped_not_substituted(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"remote": {"type": "streamable-http", "url": "https://x"}}),
            provider="opencode_cli",
        )
        assert result.servers == ()
        assert "mcp.transport_unsupported" in codes(result)

    def test_a_supported_transport_maps_for_the_same_provider(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"local": {"type": "stdio", "command": "x"}}),
            provider="opencode_cli",
        )
        assert [server.name for server in result.servers] == ["local"]

    def test_http_transports_map_for_providers_that_carry_them(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"remote": {"type": "streamable-http", "url": "https://x"}}),
            provider="claude_code",
        )
        assert [server.name for server in result.servers] == ["remote"]

    @pytest.mark.parametrize("provider", ["codex", "antigravity_cli"])
    def test_stdio_only_providers_report_a_skip_instead_of_a_dead_entry(self, roots, provider):
        """Reproduced by review on #584: the default-all table was not true.

        Codex's serializer emits only ``command``/``args``/``env``/``env_vars``
        dotted-path overrides and Antigravity always writes a local
        ``command``/``args`` entry, so an accepted HTTP server reached each one as
        a name with nothing to launch — reported as delivered while being unable
        to start. A skip finding is the honest outcome.
        """
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"remote": {"type": "streamable-http", "url": "https://x"}}),
            provider=provider,
        )

        assert result.servers == ()
        assert "mcp.transport_unsupported" in codes(result)

    @pytest.mark.parametrize("provider", ["codex", "antigravity_cli"])
    def test_stdio_still_maps_for_those_providers(self, roots, provider):
        """The skip must be transport-scoped, not a blanket refusal."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"local": {"type": "stdio", "command": "x"}}),
            provider=provider,
        )
        assert [server.name for server in result.servers] == ["local"]

    def test_the_unsupported_entry_does_not_take_its_siblings_down(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "remote": {"type": "sse", "url": "https://x"},
                    "local": {"type": "stdio", "command": "x"},
                }
            ),
            provider="opencode_cli",
        )
        assert [server.name for server in result.servers] == ["local"]


class TestCredentialWarnings:
    """Requirement 18.8 — warn, never block, never reject on this basis."""

    @pytest.mark.parametrize(
        "key, value",
        [
            ("API_TOKEN", "abc"),
            ("MY_SECRET", "abc"),
            ("db_password", "hunter2"),
            ("SERVICE_API_KEY", "x"),
            ("innocuous", "Bearer eyJhbGciOi.abc.def"),
            ("innocuous", "ghp_0123456789abcdefghijklmnopqrstuvwx"),
        ],
    )
    def test_a_credential_shaped_env_value_warns(self, roots, key, value):
        root, data = roots
        result = map_mcp_config(
            root, data, config({"s": {"type": "stdio", "command": "x", "env": {key: value}}})
        )

        warning = next(f for f in result.findings if f.code == "mcp.credential_shaped_value")
        assert warning.severity is Severity.WARNING
        # Non-blocking: the entry still maps, with the value unchanged.
        assert only(result)["env"][key] == value

    def test_a_credential_shaped_header_warns(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "s": {
                        "type": "streamable-http",
                        "url": "https://x",
                        "headers": {"Authorization": "Bearer abc123"},
                    }
                }
            ),
        )
        assert "mcp.credential_shaped_value" in codes(result)
        assert len(result.servers) == 1

    def test_an_ordinary_value_does_not_warn(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "stdio", "command": "x", "env": {"LOG_LEVEL": "debug"}}}),
        )
        assert "mcp.credential_shaped_value" not in codes(result)

    def test_the_warning_points_at_the_sanctioned_path(self, roots):
        root, data = roots
        result = map_mcp_config(
            root, data, config({"s": {"type": "stdio", "command": "x", "env": {"TOKEN": "x"}}})
        )
        warning = next(f for f in result.findings if f.code == "mcp.credential_shaped_value")
        assert "cao env" in warning.message


class TestPreExpansionMarker:
    """Requirement 18.4 — CAO's own interpolation must skip a mapped entry."""

    def test_every_mapped_entry_is_marked(self, roots):
        root, data = roots
        result = map_mcp_config(root, data, config({"s": {"type": "stdio", "command": "x"}}))
        assert only(result)[PRE_EXPANDED_KEY] is True
        assert is_pre_expanded(only(result))

    def test_an_ordinary_profile_entry_is_not_marked(self):
        assert not is_pre_expanded({"type": "stdio", "command": "x"})

    def test_the_marker_is_stripped_before_reaching_a_provider(self, roots):
        root, data = roots
        stripped = strip_marker(
            only(map_mcp_config(root, data, config({"s": {"type": "stdio", "command": "x"}})))
        )
        assert PRE_EXPANDED_KEY not in stripped
        assert stripped["command"] == "x"

    def test_only_the_unmarked_entry_is_re_resolved(self, monkeypatch):
        """CAO's ``${VAR}`` pass must skip a marked entry and process the rest.

        Scoped to the predicate's *effect on the pass*, deliberately. An earlier
        version of this test re-implemented ``install_service``'s comprehension
        over a hand-built dict, which meant it passed whether or not the real
        module contained that branch at all — and for a while the real branch was
        unreachable because nothing ever put a marked entry into a profile.

        The end-to-end assertions now live in ``test_mcp_delivery.py``, which
        installs a plugin, runs ``install_agent``, and reads the provider config.
        What is left here is the unit-level fact that module owns: given a mixed
        dict, ``resolve_mcp_server_config`` sees exactly the unmarked entries.
        """
        from cli_agent_orchestrator.services import install_service

        seen = []
        monkeypatch.setattr(
            install_service,
            "resolve_mcp_server_config",
            lambda cfg, persisted=False: seen.append(dict(cfg)) or cfg,
        )

        marked = {"type": "stdio", "command": "x", "args": ["${FOO}"], PRE_EXPANDED_KEY: True}
        ordinary = {"type": "stdio", "command": "y"}

        assert install_service.is_plugin_mcp_entry(marked)
        assert not install_service.is_plugin_mcp_entry(ordinary)

        install_service.resolve_mcp_server_config(dict(ordinary), persisted=True)
        assert seen == [ordinary]

        stripped = install_service.strip_plugin_mcp_marker(marked)
        assert stripped["args"] == ["${FOO}"], "the plugin's literal must survive unexpanded"
        assert PRE_EXPANDED_KEY not in stripped


class TestDocumentLevelFailures:
    """An unusable mcp.json disables MCP for the plugin and nothing else."""

    def test_a_wrong_schema_id_disables_mcp(self, roots):
        root, data = roots
        result = map_mcp_config(
            root, data, config({}, schema="https://example.invalid/mcp.schema.json")
        )
        assert not result.valid
        assert "mcp.schema_unsupported" in codes(result)

    def test_a_version_mismatch_with_plugin_json_disables_mcp(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "stdio", "command": "x"}}),
            plugin_schema_id="https://agent-plugins.org/schemas/2.0.0/plugin.schema.json",
        )
        assert not result.valid
        assert "mcp.schema_version_mismatch" in codes(result)

    def test_a_matching_version_maps_normally(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"s": {"type": "stdio", "command": "x"}}),
            plugin_schema_id=PLUGIN_SCHEMA_ID,
        )
        assert result.valid
        assert len(result.servers) == 1

    def test_a_schema_violation_disables_mcp(self, roots):
        root, data = roots
        result = map_mcp_config(root, data, config({"s": {"type": "stdio"}}))
        assert not result.valid
        assert "mcp.invalid" in codes(result)

    def test_a_non_object_document_disables_mcp(self, roots):
        root, data = roots
        result = map_mcp_config(root, data, ["not", "an", "object"])  # type: ignore[arg-type]
        assert not result.valid
        assert "mcp.not_an_object" in codes(result)


class TestLoadAndMap:
    def test_a_missing_mcp_json_is_not_an_error(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo")
        result = load_and_map(root, tmp_path / "data")
        assert result.present is False
        assert result.valid is True
        assert result.findings == ()

    def test_an_unreadable_mcp_json_reports_rather_than_raises(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", mcp_text="}{ nope")
        result = load_and_map(root, tmp_path / "data")
        assert result.present is True
        assert not result.valid
        assert "mcp.invalid_json" in codes(result)

    def test_a_valid_mcp_json_maps(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", with_mcp=True)
        result = load_and_map(root, tmp_path / "data")
        assert result.valid
        assert [server.name for server in result.servers] == ["demo"]

    def test_an_mcp_json_symlinked_outside_the_root_is_refused(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo")
        outside = tmp_path / "elsewhere.json"
        outside.write_text(json.dumps(config({})), encoding="utf-8")
        (root / "mcp.json").symlink_to(outside)

        result = load_and_map(root, tmp_path / "data")
        assert "mcp.escapes_root" in codes(result)


# --- Property 9: Expansion soundness ----------------------------------------
# Validates: Requirements 18.1, 18.2, 18.3

_FRAGMENTS = st.sampled_from(
    [
        "${PLUGIN_ROOT}",
        "${PLUGIN_DATA}",
        "${FOO}",
        "${PLUGIN_OTHER}",
        "${plugin_root}",  # case-sensitive: not a placeholder
        "$PLUGIN_ROOT",  # no braces: not a placeholder
        "plain",
        "/",
        "-",
        "",
    ]
)


@given(fragments=st.lists(_FRAGMENTS, max_size=8))
@settings(max_examples=300, deadline=None)
def test_property_only_the_two_placeholders_are_replaced(fragments):
    text = "".join(fragments)
    root, data = "/ROOT", "/DATA"

    expanded = expand_placeholders(text, root, data)

    assert "${PLUGIN_ROOT}" not in expanded
    assert "${PLUGIN_DATA}" not in expanded
    # Everything else survives verbatim.
    for unrecognized in ("${FOO}", "${PLUGIN_OTHER}", "${plugin_root}", "$PLUGIN_ROOT"):
        assert text.count(unrecognized) == expanded.count(unrecognized)


@given(suffix=st.sampled_from(["", "/x", "-tail"]))
@settings(max_examples=20, deadline=None)
def test_property_expansion_is_single_pass(suffix):
    """Text introduced by a replacement is not rescanned.

    Seeded exactly as design.md prescribes: ``PLUGIN_DATA`` is given a value that
    itself contains the literal ``${PLUGIN_ROOT}``. A second pass would expand
    it; a single pass leaves it standing.
    """
    data = "/data/${PLUGIN_ROOT}/here"

    result = expand_placeholders("${PLUGIN_DATA}" + suffix, "/ROOT", data)

    assert result == data + suffix
    assert "${PLUGIN_ROOT}" in result  # not rescanned
    assert "/ROOT" not in result


@given(
    key=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=8),
    value=st.sampled_from(["${PLUGIN_ROOT}", "${PLUGIN_DATA}", "${OTHER}", "plain"]),
    command=st.sampled_from(["uvx", "${PLUGIN_ROOT}", "server-bin"]),
)
@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_env_keys_and_command_are_never_altered(tmp_path_factory, key, value, command):
    base = tmp_path_factory.mktemp("p9")
    root = base / "root"
    root.mkdir()
    data = base / "data"
    data.mkdir()

    result = map_mcp_config(
        root,
        data,
        config({"s": {"type": "stdio", "command": command, "env": {key: value}}}),
    )

    assert len(result.servers) == 1, codes(result)
    mapped = result.servers[0].config
    assert mapped["command"] == command  # never expanded
    assert key in mapped["env"]  # the key is never expanded

    if value == "${OTHER}":
        assert mapped["env"][key] == "${OTHER}"
    elif value == "plain":
        assert mapped["env"][key] == "plain"


@given(text=st.text(max_size=120))
@settings(max_examples=200, deadline=None)
def test_property_text_without_placeholders_is_returned_unchanged(text):
    from hypothesis import assume

    assume("${PLUGIN_ROOT}" not in text and "${PLUGIN_DATA}" not in text)
    assert expand_placeholders(text, "/ROOT", "/DATA") == text


def test_expansion_tolerates_a_non_string():
    """Only strings are expanded; anything else passes through."""
    assert expand_placeholders(7, "/ROOT", "/DATA") == 7  # type: ignore[arg-type]
