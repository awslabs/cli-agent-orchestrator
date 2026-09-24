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
    PROVIDER_TRANSPORTS,
    expand_placeholders,
    is_pre_expanded,
    load_and_map,
    map_mcp_config,
    strip_marker,
)
from cli_agent_orchestrator.agent_plugins.models import Severity
from cli_agent_orchestrator.models.provider import ProviderType

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
        # Tightened by review 5222539218 on #584 (item 3). This read
        # `result.servers == () or [...] == ["good"]`, which accepted BOTH the
        # defective outcome (the whole document rejected) and the correct one --
        # so the test named the right behaviour while permitting the wrong one,
        # and the defect survived it. The disjunction is the finding, not the
        # boundary it was hiding.
        assert [s.name for s in result.servers] == ["good"]
        assert result.valid is True
        assert "mcp.server_invalid" in codes(result)


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
        """§9.2 -- asserted on the value, which is where a placeholder can reach.

        Narrowed by review 5222539218 on #584 (item 3): this previously used the
        header NAME ``X-${PLUGIN_ROOT}`` too, but ``{`` and ``}`` are not RFC 9110
        token characters, so such a name could never have been delivered by any
        HTTP client. It is now refused outright -- asserted immediately below --
        which makes the non-expansion claim about it unreachable rather than
        false. The value is the reachable case and carries the property.
        """
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "s": {
                        "type": "streamable-http",
                        "url": "https://x",
                        "headers": {"X-Root": "${PLUGIN_DATA}"},
                    }
                }
            ),
        )
        assert only(result)["headers"] == {"X-Root": "${PLUGIN_DATA}"}

    def test_a_header_name_that_could_never_be_delivered_is_refused(self, roots):
        """The other half of the case above, now that it has a defined outcome."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "s": {
                        "type": "streamable-http",
                        "url": "https://x",
                        "headers": {"X-${PLUGIN_ROOT}": "v"},
                    }
                }
            ),
        )
        assert result.servers == ()
        assert "mcp.headers_invalid" in codes(result)


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
        #
        # `mcp.server_invalid` was added by review 5222539218 on #584 (item 3):
        # the schema's refusal used to be reported document-wide as `mcp.invalid`,
        # taking every valid sibling with it. The entry-level outcome asserted
        # here is unchanged; only the blast radius shrank.
        assert result.servers == ()
        assert any(
            code in codes(result)
            for code in ("mcp.env_reserved_key", "mcp.server_invalid", "mcp.invalid")
        )

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

    def test_the_matrix_names_every_provider_type_exactly(self):
        """Reproduced by review 3 on #584: three providers had no row at all.

        Absence is not neutral — an unlisted provider silently inherits
        ``DEFAULT_TRANSPORTS`` (stdio only), so ``omp``, ``grok_cli`` and ``mcode``
        were quietly refusing HTTP servers their serializers can express. Set
        equality rather than a subset check, so a stale row for a deleted provider
        is caught as well.
        """
        assert set(PROVIDER_TRANSPORTS) == {p.value for p in ProviderType}

    @pytest.mark.parametrize("provider", ["omp", "grok_cli", "mcode"])
    def test_omp_grok_and_minimax_carry_http_transports(self, roots, provider):
        """All three write a url entry their target can consume."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"remote": {"type": "streamable-http", "url": "https://x"}}),
            provider=provider,
        )
        assert [server.name for server in result.servers] == ["remote"], codes(result)

    @pytest.mark.parametrize("provider", ["hermes", "mock_cli"])
    def test_a_provider_with_no_mcp_path_reports_provider_unsupported(self, roots, provider):
        """An empty transport row is not "stdio only" — it is "nothing at all".

        Reporting ``transport_unsupported`` here would name a transport and list
        the supported ones, which reads as advice the operator could act on. There
        is no transport that would work, so the finding has to say so.
        """
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"local": {"type": "stdio", "command": "x"}}),
            provider=provider,
        )
        assert result.servers == ()
        assert result.valid is True
        assert "mcp.provider_unsupported" in codes(result)
        assert "mcp.transport_unsupported" not in codes(result)

    def test_a_name_the_provider_cannot_carry_is_skipped_with_a_report(self, roots):
        """MiniMax's serializer *raises* on such a name, during terminal creation.

        The vendored ``mcp.schema.json`` puts no pattern on ``mcpServers`` keys, so
        ``Acme`` is a perfectly valid plugin-authored name — wiring MiniMax without
        this gate turned it into an unlaunchable agent rather than a missing tool.
        Siblings must still map: the skip is name-scoped.
        """
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "Acme": {"type": "stdio", "command": "x"},
                    "fine-name": {"type": "stdio", "command": "y"},
                }
            ),
            provider="mcode",
        )
        assert [server.name for server in result.servers] == ["fine-name"]
        assert "mcp.server_name_unsupported" in codes(result)

    def test_that_name_is_accepted_by_a_provider_without_the_constraint(self, roots):
        """The constraint is the provider's, not CAO's — nobody else is penalised."""
        root, data = roots
        result = map_mcp_config(
            root, data, config({"Acme": {"type": "stdio", "command": "x"}}), provider="kiro_cli"
        )
        assert [server.name for server in result.servers] == ["Acme"], codes(result)

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

    def test_a_server_level_schema_violation_no_longer_disables_mcp(self, roots):
        """Inverted by review 5222539218 on #584 (item 3).

        This asserted the reported defect directly: one structurally invalid
        server set ``valid=False`` and discarded the whole document, siblings
        included. The document here is well-formed -- only the entry is not -- so
        the entry is skipped and the file stays usable. The genuinely
        document-level failures keep their old behaviour, immediately below.
        """
        root, data = roots
        result = map_mcp_config(root, data, config({"s": {"type": "stdio"}}))
        assert result.valid is True
        assert result.servers == ()
        assert "mcp.server_invalid" in codes(result)
        assert "mcp.invalid" not in codes(result)

    def test_an_unknown_top_level_key_still_disables_mcp(self, roots):
        """The envelope half of the same schema, still fatal."""
        root, data = roots
        result = map_mcp_config(root, data, {**config({}), "extra": True})
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


class TestCodexIdentifiersAreIsolatedNotFatal:
    """Reported by review 5222539218 on #584 (item 6).

    ``acme.tools`` is a schema-valid Agent Plugins server name and ``LOG.LEVEL`` a
    schema-valid environment key, but Codex builds each field as a ``-c`` override
    whose PATH is a TOML dotted path, so a dot in the *name* silently nests the
    entry under the wrong table and ``_validate_config_key`` raises during terminal
    creation -- costing the operator the whole agent rather than one tool.

    The name is isolated here, at mapping time, exactly as MiniMax's already is.
    The env KEY is a different problem with a different answer: it lives on the
    value side, which Codex parses as TOML, so it is expressible as a quoted key in
    an inline table and needs no gate (see the Codex provider tests).
    """

    def test_codex_isolates_a_dotted_server_name(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "acme.tools": {"type": "stdio", "command": "x"},
                    "good": {"type": "stdio", "command": "y"},
                }
            ),
            provider="codex",
        )
        assert [server.name for server in result.servers] == ["good"]
        assert "mcp.server_name_unsupported" in codes(result)

    def test_codex_accepts_a_bare_name(self, roots):
        root, data = roots
        result = map_mcp_config(
            root, data, config({"acme-tools": {"type": "stdio", "command": "x"}}), provider="codex"
        )
        assert [server.name for server in result.servers] == ["acme-tools"], codes(result)

    def test_the_constraint_is_codex_only(self, roots):
        """Nobody else is penalised for Codex's TOML path grammar."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"acme.tools": {"type": "stdio", "command": "x"}}),
            provider="kiro_cli",
        )
        assert [server.name for server in result.servers] == ["acme.tools"], codes(result)


class TestCwdDelivery:
    """Reported by review 5222539218 on #584 (item 4).

    The mapper always supplies an absolute, contained ``cwd`` (defaulting to the
    plugin root), but four native serializers dropped it, so a plugin whose command
    or args are relative to its own directory executed from the provider/session
    directory instead. Which mechanism carries it is a per-provider fact, and the
    table below is the single place that fact lives.
    """

    def test_the_cwd_delivery_table_is_exhaustive_over_provider_type(self):
        """A new provider must not silently inherit a working-directory policy.

        The same discipline ``PROVIDER_TRANSPORTS`` already has: an omission here
        would leave a shipped provider dropping the plugin's declared directory with
        nothing failing.
        """
        from cli_agent_orchestrator.agent_plugins.mcp_mapping import PROVIDER_CWD_DELIVERY
        from cli_agent_orchestrator.models.provider import ProviderType

        assert set(PROVIDER_CWD_DELIVERY) == {p.value for p in ProviderType}
        assert set(PROVIDER_CWD_DELIVERY.values()) <= {"native", "shim", "none"}

    def test_the_rows_match_the_verified_vendor_evidence(self):
        """Each row is a claim about a vendor format, verified 2026-09-16."""
        from cli_agent_orchestrator.agent_plugins.mcp_mapping import PROVIDER_CWD_DELIVERY

        assert PROVIDER_CWD_DELIVERY["codex"] == "native"
        assert PROVIDER_CWD_DELIVERY["antigravity_cli"] == "native"
        assert PROVIDER_CWD_DELIVERY["opencode_cli"] == "native"
        assert PROVIDER_CWD_DELIVERY["kimi_cli"] == "native"
        for shimmed in (
            "grok_cli",
            "mcode",
            "kiro_cli",
            "claude_code",
            "cursor_cli",
            "copilot_cli",
            "omp",
        ):
            assert PROVIDER_CWD_DELIVERY[shimmed] == "shim", shimmed
        assert PROVIDER_CWD_DELIVERY["hermes"] == "none"
        assert PROVIDER_CWD_DELIVERY["mock_cli"] == "none"


class TestCwdShim:
    """Reported by review 5222539218 on #584 (item 4).

    Seven providers' formats have no working-directory field at all, so the
    directory has to be carried by the command itself. The shim is deliberately
    PURE and never raises: it is called on the delivery path, where an exception
    would cost the operator the whole agent rather than one server.
    """

    def test_the_shim_is_identity_without_both_command_and_cwd(self):
        from cli_agent_orchestrator.utils.mcp_resolution import apply_cwd_shim

        for entry in (
            {"command": "srv"},
            {"cwd": "/p"},
            {"command": "srv", "cwd": ""},
            {"command": "", "cwd": "/p"},
            {"type": "sse", "url": "https://x/y"},
            {},
        ):
            assert apply_cwd_shim(dict(entry)) == entry

    def test_the_shim_rewrites_command_and_args_and_drops_cwd(self):
        from cli_agent_orchestrator.utils.mcp_resolution import apply_cwd_shim

        out = apply_cwd_shim({"command": "srv", "args": ["--flag", "a b"], "cwd": "/p"})
        assert out["command"] == "/bin/sh"
        assert out["args"] == [
            "-c",
            'cd -- "$1" && shift && exec "$@"',
            "cao-cwd-shim",
            "/p",
            "srv",
            "--flag",
            "a b",
        ]
        assert "cwd" not in out

    def test_the_shim_never_mutates_its_argument(self):
        from cli_agent_orchestrator.utils.mcp_resolution import apply_cwd_shim

        original = {"command": "srv", "cwd": "/p"}
        apply_cwd_shim(original)
        assert original == {"command": "srv", "cwd": "/p"}

    def test_the_shim_never_raises_on_hostile_input(self):
        from cli_agent_orchestrator.utils.mcp_resolution import apply_cwd_shim

        for entry in (
            {"command": 5, "cwd": "/p"},
            {"command": "srv", "cwd": 5},
            {"command": "srv", "cwd": "/p", "args": "nope"},
        ):
            apply_cwd_shim(dict(entry))  # must not raise


class TestCwdUnavailable:
    """Reported by review 5222539218 on #584 (item 4).

    A host with no ``/bin/sh`` cannot carry the directory for a shim provider. That
    is reported, not silently dropped: the operator is told the declared working
    directory cannot be honoured, which is actionable, and the entry never reaches
    launch in a state that ignores it.
    """

    def test_stdio_entries_are_skipped_and_remote_siblings_still_map(self, roots, monkeypatch):
        from cli_agent_orchestrator.agent_plugins import mcp_mapping

        monkeypatch.setattr(mcp_mapping, "_cwd_shim_available", lambda: False)
        root, data = roots
        result = mcp_mapping.map_mcp_config(
            root,
            data,
            config(
                {
                    "local": {"type": "stdio", "command": "srv"},
                    "remote": {"type": "sse", "url": "https://x/events"},
                }
            ),
            provider="grok_cli",
        )
        assert [s.name for s in result.servers] == ["remote"], codes(result)
        assert "mcp.cwd_unsupported" in codes(result)
        assert result.valid is True

    def test_a_native_provider_is_not_gated_by_the_shim(self, roots, monkeypatch):
        from cli_agent_orchestrator.agent_plugins import mcp_mapping

        monkeypatch.setattr(mcp_mapping, "_cwd_shim_available", lambda: False)
        root, data = roots
        result = mcp_mapping.map_mcp_config(
            root, data, config({"local": {"type": "stdio", "command": "srv"}}), provider="codex"
        )
        assert [s.name for s in result.servers] == ["local"], codes(result)
        assert "mcp.cwd_unsupported" not in codes(result)


class TestTheDocumentEntryBoundary:
    """Reported by review 5222539218 on #584 (item 3).

    One structurally invalid server rejected the whole document and dropped its
    valid siblings, because ``_schema_errors`` validated the entire file --
    servers included -- and any error set ``valid=False``. Document-level defects
    and per-server defects are different failures with different blast radii, so
    they are now checked separately.
    """

    def test_one_invalid_server_does_not_drop_its_valid_siblings(self, roots):
        """The reported defect, stated as the mapper's contract."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "good": {"type": "stdio", "command": "ok"},
                    "bad": {"type": "stdio", "args": ["--no-command"]},
                    "also-good": {"type": "streamable-http", "url": "https://example.test/mcp"},
                }
            ),
        )

        assert result.valid is True, "a per-entry defect must not disable the document"
        assert sorted(s.name for s in result.servers) == ["also-good", "good"]
        assert "mcp.server_invalid" in codes(result)
        assert "mcp.invalid" not in codes(result), "that code is document-level only"

    def test_the_invalid_entry_is_named_in_its_finding(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config({"broken": {"type": "sse"}, "fine": {"type": "stdio", "command": "ok"}}),
        )

        finding = next(f for f in result.findings if f.code == "mcp.server_invalid")
        assert "broken" in finding.message
        assert finding.severity is Severity.SKIPPED
        assert [s.name for s in result.servers] == ["fine"]

    @pytest.mark.parametrize(
        "document, expected_code, why",
        [
            ({"mcpServers": {}}, "mcp.schema_unsupported", "no $schema"),
            (
                {"$schema": "https://example.test/other.json", "mcpServers": {}},
                "mcp.schema_unsupported",
                "wrong $schema",
            ),
            (
                {"$schema": MCP_SCHEMA_ID, "mcpServers": {}, "extra": 1},
                "mcp.invalid",
                "unknown top-level key",
            ),
            (
                {"$schema": MCP_SCHEMA_ID, "mcpServers": []},
                "mcp.invalid",
                "mcpServers not an object",
            ),
        ],
    )
    def test_a_document_level_defect_still_disables_the_whole_file(
        self, roots, document, expected_code, why
    ):
        """The boundary moved for entries, NOT for the envelope.

        Each of these makes the document itself unusable, so there is no
        well-formed sibling to preserve -- ``valid`` stays False. ``$schema``
        keeps its own dedicated code, which is more specific than ``mcp.invalid``
        and predates this change; the two mcpServers-shape cases are the ones the
        envelope check is responsible for.
        """
        root, data = roots
        result = map_mcp_config(root, data, document)

        assert result.valid is False, why
        assert result.servers == ()
        assert expected_code in codes(result), why
        assert "mcp.server_invalid" not in codes(result), "an envelope defect is not per-entry"

    def test_a_document_level_defect_is_reported_once_not_per_server(self, roots):
        """An envelope failure must not also emit a finding for every entry."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}},
                "extra": 1,
            },
        )

        assert codes(result).count("mcp.invalid") == 1

    def test_every_entry_being_invalid_still_leaves_the_document_valid(self, roots):
        """``valid`` describes the FILE, not whether anything survived it.

        Nothing is delivered, but the distinction is load-bearing: `cao plugin
        validate` exits non-zero only on an unloadable package, and a document
        whose entries were all individually skipped is still a loadable package.
        """
        root, data = roots
        result = map_mcp_config(root, data, config({"a": {"type": "stdio"}, "b": {"type": "sse"}}))

        assert result.valid is True
        assert result.servers == ()
        assert codes(result).count("mcp.server_invalid") == 2


class TestRemoteUrlSemantics:
    """Reported by review 5222539218 on #584 (item 3).

    The url branch assigned ``entry.get("url")`` verbatim, so an independently
    supplied remote entry could carry cleartext HTTP to an arbitrary host,
    userinfo, or a fragment -- none of which the schema's ``format: uri``
    constrains.
    """

    @pytest.mark.parametrize(
        "url, why",
        [
            ("http://example.test/mcp", "cleartext to a non-loopback host"),
            ("http://10.0.0.5:8080/mcp", "private but not loopback"),
        ],
    )
    def test_cleartext_http_is_refused_off_loopback(self, roots, url, why):
        root, data = roots
        result = map_mcp_config(root, data, config({"remote": {"type": "sse", "url": url}}))

        assert result.servers == (), why
        assert "mcp.url_insecure" in codes(result), why
        assert result.valid is True, "one bad entry is not a bad document"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:3000/mcp",
            "http://127.0.0.1:3000/mcp",
            "http://127.1.2.3/mcp",
            "http://[::1]:3000/mcp",
        ],
    )
    def test_cleartext_http_is_allowed_on_loopback(self, roots, url):
        """A local sidecar is the legitimate cleartext case and must keep working."""
        root, data = roots
        result = map_mcp_config(root, data, config({"remote": {"type": "sse", "url": url}}))

        assert len(result.servers) == 1, codes(result)
        assert result.servers[0].config["url"] == url

    @pytest.mark.parametrize(
        "url, why",
        [
            ("https://user:pw@example.test/mcp", "userinfo"),
            ("https://user@example.test/mcp", "userinfo without a password"),
            ("https://example.test/mcp#frag", "fragment"),
            ("ftp://example.test/mcp", "scheme is neither http nor https"),
            ("https:///mcp", "no host"),
            ("not-a-url", "unparseable as an absolute URL"),
        ],
    )
    def test_a_structurally_prohibited_url_is_refused(self, roots, url, why):
        root, data = roots
        result = map_mcp_config(root, data, config({"remote": {"type": "sse", "url": url}}))

        assert result.servers == (), why
        assert "mcp.url_invalid" in codes(result), why

    def test_a_valid_https_url_is_passed_through_byte_for_byte(self, roots):
        """§9.2 -- `url` is never placeholder-expanded or otherwise rewritten."""
        root, data = roots
        url = "https://example.test:8443/mcp?tenant=${PLUGIN_ROOT}"
        result = map_mcp_config(
            root, data, config({"remote": {"type": "streamable-http", "url": url}})
        )

        assert result.servers[0].config["url"] == url

    def test_a_bad_url_does_not_take_its_siblings_with_it(self, roots):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "bad": {"type": "sse", "url": "http://example.test/mcp"},
                    "good": {"type": "sse", "url": "https://example.test/mcp"},
                }
            ),
        )

        assert [s.name for s in result.servers] == ["good"]
        assert result.valid is True


class TestRemoteHeaderSemantics:
    """Reported by review 5222539218 on #584 (item 3)."""

    @pytest.mark.parametrize(
        "headers, why",
        [
            ({"Bad Name": "v"}, "space is not an RFC 9110 token character"),
            ({"Bad:Name": "v"}, "colon is not a token character"),
            ({"Bad\nName": "v"}, "newline in the name"),
            ({"": "v"}, "empty name"),
            ({"X-Ok": "bad\r\nInjected: yes"}, "CRLF injection in the value"),
            ({"X-Ok": "bad\x00value"}, "NUL in the value"),
            ({"X-Ok": "café"}, "non-ASCII outside obs-text"),
        ],
    )
    def test_an_invalid_header_refuses_only_that_entry(self, roots, headers, why):
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "remote": {
                        "type": "sse",
                        "url": "https://example.test/mcp",
                        "headers": headers,
                    },
                    "sibling": {"type": "stdio", "command": "ok"},
                }
            ),
        )

        assert [s.name for s in result.servers] == ["sibling"], why
        assert "mcp.headers_invalid" in codes(result), why
        assert result.valid is True

    def test_names_differing_only_in_case_are_a_conflict(self, roots):
        """HTTP field names are case-insensitive, so these are one header twice.

        Delivered as a dict, the later silently wins -- the operator's intent is
        unknowable, so the entry is refused rather than guessed at.
        """
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "remote": {
                        "type": "sse",
                        "url": "https://example.test/mcp",
                        "headers": {"X-Tenant": "a", "x-tenant": "b"},
                    }
                }
            ),
        )

        assert result.servers == ()
        finding = next(f for f in result.findings if f.code == "mcp.headers_invalid")
        assert "x-tenant" in finding.message.lower()

    def test_valid_headers_survive_untouched(self, roots):
        root, data = roots
        headers = {"X-Tenant": "acme", "Accept": "application/json", "X-Trace": "a\tb"}
        result = map_mcp_config(
            root,
            data,
            config(
                {"remote": {"type": "sse", "url": "https://example.test/mcp", "headers": headers}}
            ),
        )

        assert result.servers[0].config["headers"] == headers

    def test_an_authorization_shaped_header_stays_warning_only(self, roots):
        """Explicitly preserved by the reviewer: 'headers remain warning-only'."""
        root, data = roots
        result = map_mcp_config(
            root,
            data,
            config(
                {
                    "remote": {
                        "type": "sse",
                        "url": "https://example.test/mcp",
                        "headers": {"Authorization": "Bearer sk-live-abcdefghijklmnop"},
                    }
                }
            ),
        )

        assert len(result.servers) == 1, "a credential shape must never skip the entry"
        assert "mcp.headers_invalid" not in codes(result)
        assert any(f.severity is Severity.WARNING for f in result.findings)
