"""Pure profile-value resolution and raw gated profile loading."""

from __future__ import annotations

import traceback
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

import frontmatter
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils import agent_profiles
from cli_agent_orchestrator.utils import env as env_utils
from cli_agent_orchestrator.utils import profile_value_resolution as resolution
from cli_agent_orchestrator.utils.profile_value_resolution import (
    BODY_PATH,
    AuthorityResolver,
    DestinationClass,
    NodeRole,
    ProfileValueRefusal,
    env_slot_key,
    structural_destination,
)

AUTHORITY = {
    "PATH": 'captured" $ ${OTHER}\nallowedTools:\n  - not-injected',
    "HOME": "/synthetic/home",
    "CODEX_HOME": "/synthetic/codex",
}
CREDENTIALS = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AWS_SESSION_TOKEN",
}
ENV_PATH = ("mcpServers", "local", "env", "PATH")


def _resolver() -> AuthorityResolver:
    return AuthorityResolver(
        authority_env=AUTHORITY,
        credential_names=CREDENTIALS,
    )


def _profile_text(*, metadata: str = "", body: str = "Body prompt") -> str:
    return (
        "---\n"
        "name: synthetic\n"
        "description: Synthetic profile\n"
        f"{metadata}"
        "---\n"
        f"{body}\n"
    )


def _refusal(
    resolver: AuthorityResolver,
    raw: Any,
    destination: DestinationClass,
    *,
    path: tuple[str | int, ...] = ("model",),
) -> ProfileValueRefusal:
    with pytest.raises(ProfileValueRefusal) as caught:
        resolver.resolve(path, destination, raw)
    return caught.value


def test_structural_destination_and_env_slot_key_are_closed_and_pure() -> None:
    assert env_slot_key(ENV_PATH) == "PATH"
    assert env_slot_key(("mcpServers", "local", "env", "PATH", "extra")) is None
    assert env_slot_key(("mcpServers", 7, "env", "PATH")) is None
    assert structural_destination(ENV_PATH, role=NodeRole.VALUE) is DestinationClass.MCP_ENV
    assert structural_destination(ENV_PATH, role=NodeRole.KEY) is DestinationClass.MAPPING_KEY
    assert structural_destination(BODY_PATH, role=NodeRole.BODY) is DestinationClass.BODY
    assert structural_destination(("model",), role=NodeRole.VALUE) is DestinationClass.UNCLASSIFIED
    assert not hasattr(resolution, "AUTHORITY_DESTINATIONS")
    assert not hasattr(resolution, "classify_destination")


@pytest.mark.parametrize(
    ("path", "destination"),
    [
        (ENV_PATH, DestinationClass.AUTHORITY),
        (("wrong",), DestinationClass.BODY),
        (("model",), DestinationClass.MCP_ENV),
    ],
)
def test_resolver_rejects_invalid_wiring(
    path: tuple[str | int, ...],
    destination: DestinationClass,
) -> None:
    with pytest.raises(ValueError, match="destination|requires|refined"):
        _resolver().resolve(path, destination, "literal")


@pytest.mark.parametrize(
    "authority_env",
    [
        [],
        {"BAD-NAME": "value"},
        {"A" * 129: "value"},
        {"PATH": 1},
        {1: "value"},
    ],
)
def test_constructor_rejects_invalid_authority_entries(authority_env: Any) -> None:
    with pytest.raises(ValueError, match="authority_env"):
        AuthorityResolver(authority_env=authority_env, credential_names=set())


@pytest.mark.parametrize(
    "credential_names",
    [
        "OPENAI_API_KEY",
        b"OPENAI_API_KEY",
        {"OPENAI_API_KEY": True},
        (name for name in ["OPENAI_API_KEY"]),
        {"BAD-NAME"},
        {"A" * 129},
        {1},
    ],
)
def test_constructor_rejects_invalid_credential_collections(
    credential_names: Any,
) -> None:
    with pytest.raises(ValueError, match="credential_names"):
        AuthorityResolver(
            authority_env={},
            credential_names=credential_names,
        )


def test_constructor_snapshots_inputs_and_allows_empty_sets() -> None:
    authority = {"PATH": "first"}
    credentials = ["OPENAI_API_KEY"]
    resolver = AuthorityResolver(
        authority_env=authority,
        credential_names=credentials,
    )
    authority["PATH"] = "mutated"
    authority["HOME"] = "late"
    credentials.append("LATE_SECRET")

    assert resolver.resolve(ENV_PATH, DestinationClass.MCP_ENV, "${PATH}") == "first"
    late_refusal = _refusal(
        resolver,
        "$LATE_SECRET",
        DestinationClass.UNCLASSIFIED,
    )
    assert late_refusal.code == "credential_reference_unsupported"
    assert (
        _refusal(
            resolver,
            "${HOME}",
            DestinationClass.UNCLASSIFIED,
        ).code
        == "credential_reference_unsupported"
    )
    empty = AuthorityResolver(authority_env={}, credential_names=())
    literal = object()
    assert empty.resolve(("metadata",), DestinationClass.UNCLASSIFIED, literal) is literal


def test_constructor_rejects_overlapping_authority_and_credentials() -> None:
    with pytest.raises(ValueError, match="disjoint"):
        AuthorityResolver(
            authority_env={"PATH": "captured"},
            credential_names={"PATH"},
        )


def test_constructor_rejects_str_subclasses_and_keeps_messages_input_safe() -> None:
    class StringProxy(str):
        pass

    private_name = StringProxy("PRIVATE_NAME")
    for kwargs in (
        {
            "authority_env": {private_name: "value"},
            "credential_names": set(),
        },
        {
            "authority_env": {"PATH": private_name},
            "credential_names": set(),
        },
        {
            "authority_env": {},
            "credential_names": {private_name},
        },
    ):
        with pytest.raises(ValueError) as caught:
            AuthorityResolver(**kwargs)
        assert "PRIVATE_NAME" not in str(caught.value)


def test_constructor_never_reads_caller_mapping_after_snapshot() -> None:
    class CountingMapping(Mapping[str, str]):
        def __init__(self) -> None:
            self.getitem_calls = 0
            self.blocked = False

        def __getitem__(self, key: str) -> str:
            if self.blocked:
                raise AssertionError("caller mapping read after construction")
            self.getitem_calls += 1
            if key != "PATH":
                raise KeyError(key)
            return "captured"

        def __iter__(self) -> Iterator[str]:
            if self.blocked:
                raise AssertionError("caller mapping iterated after construction")
            yield "PATH"

        def __len__(self) -> int:
            return 1

    authority = CountingMapping()
    resolver = AuthorityResolver(
        authority_env=authority,
        credential_names=[],
    )
    assert authority.getitem_calls == 1
    authority.blocked = True
    assert resolver.resolve(ENV_PATH, DestinationClass.MCP_ENV, "${PATH}") == "captured"


@pytest.mark.parametrize("raw", [42, 3.5, True, None])
def test_native_literals_are_identity_except_at_mcp_env(raw: Any) -> None:
    resolver = _resolver()
    assert resolver.resolve(("metadata",), DestinationClass.UNCLASSIFIED, raw) is raw
    assert resolver.resolve(("metadata",), DestinationClass.MAPPING_KEY, raw) is raw
    refusal = _refusal(
        resolver,
        raw,
        DestinationClass.MCP_ENV,
        path=ENV_PATH,
    )
    assert refusal.code == "mcp_env_value_not_plain_string"
    assert "type_mismatch" not in refusal.code


@pytest.mark.parametrize("form", ["$PATH", "${PATH}"])
def test_whole_same_key_authority_reference_accepts_exact_snapshot(form: str) -> None:
    resolver = _resolver()

    assert resolver.resolve(ENV_PATH, DestinationClass.MCP_ENV, form) == AUTHORITY["PATH"]
    first = resolver.accepted
    second = resolver.accepted
    assert first == {ENV_PATH: "PATH"}
    assert isinstance(first, MappingProxyType)
    assert first is not second
    with pytest.raises(TypeError):
        first[ENV_PATH] = "HOME"  # type: ignore[index]

    resolver.reset()
    assert resolver.accepted == {}
    assert resolver.resolve(ENV_PATH, DestinationClass.MCP_ENV, form) == AUTHORITY["PATH"]


@pytest.mark.parametrize(
    ("path", "raw", "code"),
    [
        (ENV_PATH, "${HOME}", "authority_reference_alias"),
        (
            ("mcpServers", "local", "env", "TOKEN"),
            "${PATH}",
            "authority_destination_unclassified",
        ),
        (("model",), "${PATH}", "authority_destination_unclassified"),
        (ENV_PATH, "prefix-${PATH}", "credential_reference_unsupported"),
        (ENV_PATH, "${PATH}-suffix", "credential_reference_unsupported"),
        (ENV_PATH, "${PATH}${PATH}", "credential_reference_unsupported"),
        (ENV_PATH, "${UNKNOWN}", "credential_reference_unsupported"),
    ],
)
def test_authority_reference_matrix(
    path: tuple[str | int, ...],
    raw: str,
    code: str,
) -> None:
    refusal = _refusal(
        _resolver(),
        raw,
        structural_destination(path, role=NodeRole.VALUE),
        path=path,
    )
    assert refusal.code == code


@pytest.mark.parametrize(
    ("path", "destination", "raw", "code"),
    [
        (
            ENV_PATH,
            DestinationClass.MCP_ENV,
            "${OPENAI_API_KEY}",
            "mcp_credential_destination_unsupported",
        ),
        (
            ("mcpServers", "local", "env", "TOKEN"),
            DestinationClass.MCP_ENV,
            "${OPENAI_API_KEY}",
            "mcp_credential_destination_unsupported",
        ),
        (
            ("description",),
            DestinationClass.UNCLASSIFIED,
            "${OPENAI_API_KEY}",
            "credential_destination_unsupported",
        ),
        (
            ("toolsSettings", "${OPENAI_API_KEY}"),
            DestinationClass.MAPPING_KEY,
            "${OPENAI_API_KEY}",
            "credential_reference_in_key",
        ),
    ],
)
def test_credential_destination_matrix(
    path: tuple[str | int, ...],
    destination: DestinationClass,
    raw: str,
    code: str,
) -> None:
    refusal = _refusal(_resolver(), raw, destination, path=path)
    assert refusal.code == code
    assert refusal.destination is destination


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("${BROKEN", "credential_reference_unsupported"),
        ("$5", "credential_reference_unsupported"),
        ("${UNKNOWN}", "credential_reference_unsupported"),
        (
            "${BROKEN ${OPENAI_API_KEY}",
            "credential_reference_unsupported",
        ),
        ("${UNKNOWN}-${PATH}", "credential_reference_unsupported"),
        (
            "${UNKNOWN}-${OPENAI_API_KEY}-${PATH}",
            "credential_destination_unsupported",
        ),
    ],
)
def test_metadata_form_validation_and_precedence(raw: str, code: str) -> None:
    refusal = _refusal(
        _resolver(),
        raw,
        DestinationClass.UNCLASSIFIED,
    )
    assert refusal.code == code


@pytest.mark.parametrize("raw", ["literal", "$$X", "$$HOME", "pay $$5"])
def test_reference_free_metadata_is_returned_without_template_transform(raw: str) -> None:
    assert _resolver().resolve(("model",), DestinationClass.UNCLASSIFIED, raw) is raw


@pytest.mark.parametrize(
    ("path", "destination"),
    [
        (ENV_PATH, DestinationClass.MCP_ENV),
        (
            ("mcpServers", "local", "env", "TOKEN"),
            DestinationClass.MCP_ENV,
        ),
        (("model",), DestinationClass.UNCLASSIFIED),
        (("toolsSettings", "$$HOME"), DestinationClass.MAPPING_KEY),
    ],
)
@pytest.mark.parametrize("raw", ["$$X", "$$HOME", "pay $$5"])
def test_escaped_metadata_is_byte_identical_at_every_destination(
    path: tuple[str | int, ...],
    destination: DestinationClass,
    raw: str,
) -> None:
    assert _resolver().resolve(path, destination, raw) is raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "costs $5 today",
        "$HOME",
        "${HOME}",
        "$$HOME",
        "$$LITERAL",
        "$5",
        "${}",
        "${UNCLOSED",
        "${A-B}",
        "${ NAME }",
        "$",
        "$UNKNOWN_NAME",
        "${ANTHROPIC_API_KEY",
        "${ANTHROPIC_API_KEY:-x}",
        "$ANTHROPIC_API_KEYX",
        "$" + "A" * 129,
    ],
)
def test_body_scanner_preserves_literals_and_malformed_forms(raw: str) -> None:
    resolver = _resolver()
    assert resolver.resolve(BODY_PATH, DestinationClass.BODY, raw) is raw


@pytest.mark.parametrize(
    "raw",
    [
        "$ANTHROPIC_API_KEY",
        "${ANTHROPIC_API_KEY}",
        "$$ANTHROPIC_API_KEY",
        "before $OPENAI_API_KEY after",
    ],
)
def test_body_scanner_refuses_exact_known_credential_references(raw: str) -> None:
    refusal = _refusal(
        _resolver(),
        raw,
        DestinationClass.BODY,
        path=BODY_PATH,
    )
    assert refusal.code == "credential_reference_in_prompt_body"
    assert refusal.destination is DestinationClass.BODY


@pytest.mark.parametrize(
    ("mapping_key", "code"),
    [
        ("${PATH}", "credential_reference_in_key"),
        ("${OPENAI_API_KEY}", "credential_reference_in_key"),
        ("${UNKNOWN}", "credential_reference_unsupported"),
        ("${BROKEN", "credential_reference_unsupported"),
    ],
)
def test_mapping_key_reference_is_inspected_at_full_path_but_never_rewritten(
    mapping_key: str,
    code: str,
) -> None:
    with pytest.raises(ProfileValueRefusal) as caught:
        agent_profiles.parse_agent_profile_text(
            _profile_text(
                metadata=("toolsSettings:\n" f'  "{mapping_key}": should-not-be-resolved\n')
            ),
            "synthetic",
            value_resolver=_resolver().resolve,
        )

    assert caught.value.code == code
    assert caught.value.destination is DestinationClass.MAPPING_KEY
    assert mapping_key not in str(caught.value)


def test_parser_uses_one_parse_and_ordered_scalar_only_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _profile_text(
        metadata=(
            "system_prompt: declared metadata prompt\n"
            "model: literal-model\n"
            "mcpServers:\n"
            "  local:\n"
            "    command: synthetic-command\n"
            "    args: [first, second]\n"
            "    env:\n"
            "      PLAIN: literal\n"
        ),
        body="  assigned body prompt  ",
    )
    actual_loads = agent_profiles.frontmatter.loads
    parse_count = 0
    calls: list[tuple[tuple[str | int, ...], DestinationClass, Any]] = []

    def counting_loads(profile_text: str) -> frontmatter.Post:
        nonlocal parse_count
        parse_count += 1
        return actual_loads(profile_text)

    def resolver(
        path: tuple[str | int, ...],
        destination: DestinationClass,
        raw: Any,
    ) -> Any:
        assert not isinstance(raw, (dict, list, tuple))
        calls.append((path, destination, raw))
        return raw

    monkeypatch.setattr(agent_profiles.frontmatter, "loads", counting_loads)
    profile = agent_profiles.parse_agent_profile_text(
        source,
        "synthetic",
        value_resolver=resolver,
    )

    assert parse_count == 1
    prompt_values = [
        call
        for call in calls
        if call[0] == BODY_PATH and call[2] in {"declared metadata prompt", "assigned body prompt"}
    ]
    assert prompt_values == [
        (
            BODY_PATH,
            DestinationClass.UNCLASSIFIED,
            "declared metadata prompt",
        ),
        (
            BODY_PATH,
            DestinationClass.BODY,
            "assigned body prompt",
        ),
    ]
    assert (
        ENV_PATH[:-1] + ("PLAIN",),
        DestinationClass.MCP_ENV,
        "literal",
    ) in calls
    assert profile.system_prompt == "assigned body prompt"


def test_same_system_prompt_path_has_distinct_metadata_and_body_grammar() -> None:
    resolver = _resolver()
    declared = _profile_text(
        metadata='system_prompt: "${}"\n',
        body="${}",
    )
    for _ in range(2):
        with pytest.raises(ProfileValueRefusal) as caught:
            agent_profiles.parse_agent_profile_text(
                declared,
                "synthetic",
                value_resolver=resolver.resolve,
            )
        assert caught.value.code == "credential_reference_unsupported"
        assert caught.value.destination is DestinationClass.UNCLASSIFIED
        resolver.reset()

    body_only = _profile_text(body="${}")
    first = agent_profiles.parse_agent_profile_text(
        body_only,
        "synthetic",
        value_resolver=resolver.resolve,
    )
    resolver.reset()
    second = agent_profiles.parse_agent_profile_text(
        body_only,
        "synthetic",
        value_resolver=resolver.resolve,
    )
    assert first.system_prompt == "${}"
    assert second == first
    assert resolver.accepted == {}


def test_empty_body_is_assigned_and_classified_exactly_once() -> None:
    calls: list[tuple[tuple[str | int, ...], DestinationClass, Any]] = []

    def resolver(
        path: tuple[str | int, ...],
        destination: DestinationClass,
        raw: Any,
    ) -> Any:
        calls.append((path, destination, raw))
        return raw

    profile = agent_profiles.parse_agent_profile_text(
        _profile_text(body="   "),
        "synthetic",
        value_resolver=resolver,
    )
    assert profile.system_prompt == ""
    assert [call for call in calls if call[1] is DestinationClass.BODY] == [
        (BODY_PATH, DestinationClass.BODY, "")
    ]


def test_none_resolver_preserves_legacy_result_and_skips_both_passes() -> None:
    source = _profile_text(
        metadata=(
            "system_prompt: ${OPENAI_API_KEY}\n"
            "model: $$MODEL\n"
            "mcpServers:\n"
            "  local:\n"
            "    command: synthetic-command\n"
            "    env:\n"
            "      ORDINARY: ${UNRESOLVED}\n"
        ),
        body=" body with $OPENAI_API_KEY and $$DOUBLE ",
    )
    parsed = frontmatter.loads(source)
    expected_metadata = parsed.metadata
    expected_metadata["system_prompt"] = parsed.content.strip()
    expected_metadata.setdefault("name", "synthetic")
    expected_metadata.setdefault("description", "")

    actual = agent_profiles.parse_agent_profile_text(
        source,
        "synthetic",
        value_resolver=None,
    )

    assert actual == AgentProfile(**expected_metadata)
    assert actual.model == "$$MODEL"
    assert actual.system_prompt == "body with $OPENAI_API_KEY and $$DOUBLE"


@pytest.mark.parametrize(
    "env_yaml",
    [
        "[]",
        "literal",
        "42",
        "{PLAIN: [nested]}",
        "{PLAIN: {nested: value}}",
    ],
)
def test_parser_refuses_non_mapping_mcp_env_before_callback(env_yaml: str) -> None:
    callback_values: list[Any] = []
    callback_paths: list[tuple[str | int, ...]] = []

    def callback(
        path: tuple[str | int, ...],
        destination: DestinationClass,
        raw: Any,
    ) -> Any:
        callback_paths.append(path)
        callback_values.append(raw)
        return raw

    with pytest.raises(ProfileValueRefusal) as caught:
        agent_profiles.parse_agent_profile_text(
            _profile_text(
                metadata=(
                    "mcpServers:\n"
                    "  local:\n"
                    "    command: synthetic-command\n"
                    f"    env: {env_yaml}\n"
                )
            ),
            "synthetic",
            value_resolver=callback,
        )

    assert caught.value.code == "mcp_env_value_not_plain_string"
    assert all(not isinstance(value, (dict, list, tuple)) for value in callback_values)
    assert not any(len(path) >= 5 and path[:3] == ENV_PATH[:3] for path in callback_paths)


def test_parser_allows_absent_or_null_env_without_slot_callbacks() -> None:
    absent = agent_profiles.parse_agent_profile_text(
        _profile_text(metadata=("mcpServers:\n" "  local:\n" "    command: synthetic-command\n")),
        "synthetic",
        value_resolver=_resolver().resolve,
    )
    null = agent_profiles.parse_agent_profile_text(
        _profile_text(
            metadata=(
                "mcpServers:\n" "  local:\n" "    command: synthetic-command\n" "    env: null\n"
            )
        ),
        "synthetic",
        value_resolver=_resolver().resolve,
    )
    assert absent.mcpServers is not None
    assert null.mcpServers is not None
    assert absent.mcpServers["local"].get("env") is None
    assert null.mcpServers["local"]["env"] is None


@pytest.mark.parametrize(
    "env_yaml",
    [
        "{7: literal}",
        "{PLAIN: 42}",
        "{PLAIN: true}",
        "{PLAIN: null}",
        "{PLAIN: [nested]}",
    ],
)
def test_parser_refuses_non_plain_mcp_env_key_or_value(env_yaml: str) -> None:
    with pytest.raises(ProfileValueRefusal) as caught:
        agent_profiles.parse_agent_profile_text(
            _profile_text(
                metadata=(
                    "mcpServers:\n"
                    "  local:\n"
                    "    command: synthetic-command\n"
                    f"    env: {env_yaml}\n"
                )
            ),
            "synthetic",
            value_resolver=_resolver().resolve,
        )
    assert caught.value.code == "mcp_env_value_not_plain_string"
    assert caught.value.destination is DestinationClass.MCP_ENV


def test_hostile_authority_value_stays_one_scalar_and_records_acceptance() -> None:
    resolver = _resolver()
    profile = agent_profiles.parse_agent_profile_text(
        _profile_text(
            metadata=(
                "mcpServers:\n"
                "  local:\n"
                "    command: synthetic-command\n"
                '    env: {PATH: "${PATH}"}\n'
            )
        ),
        "synthetic",
        value_resolver=resolver.resolve,
    )
    assert profile.mcpServers is not None
    assert profile.mcpServers["local"]["env"] == {"PATH": AUTHORITY["PATH"]}
    assert profile.allowedTools is None
    assert resolver.accepted == {ENV_PATH: "PATH"}


def test_empty_policy_accepts_literals_and_fails_closed_for_metadata_references() -> None:
    resolver = AuthorityResolver(authority_env={}, credential_names=())
    literal = _profile_text(metadata="model: literal\n", body="$UNKNOWN")
    profile = agent_profiles.parse_agent_profile_text(
        literal,
        "synthetic",
        value_resolver=resolver.resolve,
    )
    assert profile.model == "literal"
    assert profile.system_prompt == "$UNKNOWN"

    with pytest.raises(ProfileValueRefusal) as caught:
        agent_profiles.parse_agent_profile_text(
            _profile_text(metadata='model: "${UNKNOWN}"\n'),
            "synthetic",
            value_resolver=resolver.resolve,
        )
    assert caught.value.code == "credential_reference_unsupported"
    resolver.reset()


def test_later_refusal_keeps_no_document_and_owner_resets_state_before_reuse() -> None:
    resolver = _resolver()
    with pytest.raises(ProfileValueRefusal):
        agent_profiles.parse_agent_profile_text(
            _profile_text(
                metadata=(
                    "mcpServers:\n"
                    "  local:\n"
                    "    command: synthetic-command\n"
                    "    env:\n"
                    '      PATH: "${PATH}"\n'
                    '      TOKEN: "${OPENAI_API_KEY}"\n'
                )
            ),
            "synthetic",
            value_resolver=resolver.resolve,
        )
    assert resolver.accepted == {ENV_PATH: "PATH"}
    resolver.reset()
    assert resolver.accepted == {}


def test_refusal_is_safe_local_exception_without_cause_or_input() -> None:
    secret = "PRIVATE_INPUT_FRAGMENT"
    try:
        _resolver().resolve(
            ("description",),
            DestinationClass.UNCLASSIFIED,
            f"${{{secret}}}",
        )
    except ProfileValueRefusal as error:
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        assert not isinstance(error, ValueError)
        assert set(error.__dict__) == {"code", "destination"}
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in rendered
        assert error.__cause__ is None
        assert error.__context__ is None
    else:
        pytest.fail("expected a local refusal")


def test_gated_loader_reads_and_parses_raw_source_once_without_legacy_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _profile_text(
        metadata=(
            "mcpServers:\n"
            "  local:\n"
            "    command: synthetic-command\n"
            '    env: {TOKEN: "${OPENAI_API_KEY}"}\n'
        )
    )
    source_reads = 0
    parse_calls = 0
    actual_parse = agent_profiles.parse_agent_profile_text

    def read_once(agent_name: str) -> str:
        nonlocal source_reads
        source_reads += 1
        assert agent_name == "synthetic"
        return raw

    def parse_once(
        profile_text: str,
        profile_name: str,
        *,
        value_resolver: agent_profiles.ValueResolver | None = None,
    ) -> AgentProfile:
        nonlocal parse_calls
        parse_calls += 1
        assert profile_text == raw
        return actual_parse(
            profile_text,
            profile_name,
            value_resolver=value_resolver,
        )

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy environment/store path was reached")

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-expand")
    monkeypatch.setattr(agent_profiles, "_read_agent_profile_source", read_once)
    monkeypatch.setattr(agent_profiles, "parse_agent_profile_text", parse_once)
    monkeypatch.setattr(agent_profiles, "resolve_env_vars", explode)
    monkeypatch.setattr(agent_profiles, "load_agent_profile", explode)
    monkeypatch.setattr(env_utils, "load_env_vars", explode)

    with pytest.raises(ProfileValueRefusal) as caught:
        agent_profiles.load_gated_agent_profile(
            "synthetic",
            value_resolver=_resolver().resolve,
        )
    assert source_reads == 1
    assert parse_calls == 1
    assert caught.value.code == "mcp_credential_destination_unsupported"


def test_gated_loader_success_returns_complete_validated_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _profile_text(
        metadata=("provider: synthetic-provider\n" "allowedTools: [Read, Write]\n"),
        body="  complete synthetic body  ",
    )
    reads = 0

    def read_once(agent_name: str) -> str:
        nonlocal reads
        reads += 1
        assert agent_name == "synthetic"
        return raw

    monkeypatch.setattr(agent_profiles, "_read_agent_profile_source", read_once)
    profile = agent_profiles.load_gated_agent_profile(
        "synthetic",
        value_resolver=_resolver().resolve,
    )
    assert reads == 1
    assert isinstance(profile, AgentProfile)
    assert profile.model_dump(exclude_none=True) == {
        "name": "synthetic",
        "description": "Synthetic profile",
        "provider": "synthetic-provider",
        "system_prompt": "complete synthetic body",
        "allowedTools": ["Read", "Write"],
    }


def test_gated_parse_refuses_recursive_mapping_alias_without_echoing_data() -> None:
    private_marker = "PRIVATE_CYCLE_FRAGMENT"
    source = _profile_text(
        metadata=("hooks: &recursive\n" f"  marker: {private_marker}\n" "  child: *recursive\n")
    )

    with pytest.raises(ProfileValueRefusal) as caught:
        agent_profiles.parse_agent_profile_text(
            source,
            "synthetic",
            value_resolver=_resolver().resolve,
        )

    assert caught.value.code == "profile_document_cycle"
    assert caught.value.destination is DestinationClass.UNCLASSIFIED
    assert set(caught.value.__dict__) == {"code", "destination"}
    assert private_marker not in str(caught.value)
    assert private_marker not in repr(caught.value)
    rendered = "".join(
        traceback.format_exception(
            type(caught.value),
            caught.value,
            caught.value.__traceback__,
        )
    )
    assert private_marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_metadata_traversal_refuses_recursive_sequence() -> None:
    recursive: list[Any] = []
    recursive.append(recursive)

    with pytest.raises(ProfileValueRefusal) as caught:
        resolution._resolve_metadata_value(
            recursive,
            _resolver().resolve,
        )

    assert caught.value.code == "profile_document_cycle"
    assert caught.value.destination is DestinationClass.UNCLASSIFIED


def _nested_mapping(container_count: int) -> dict[str, Any]:
    nested: Any = "leaf"
    for _ in range(container_count):
        nested = {"child": nested}
    assert isinstance(nested, dict)
    return nested


def test_metadata_depth_allows_64_and_refuses_container_at_65() -> None:
    visited_paths: list[tuple[str | int, ...]] = []

    def identity(
        path: tuple[str | int, ...],
        destination: DestinationClass,
        raw: Any,
    ) -> Any:
        visited_paths.append(path)
        return raw

    allowed = _nested_mapping(65)
    assert (
        resolution._resolve_metadata_value(
            allowed,
            identity,
        )
        == allowed
    )

    visited_paths.clear()
    too_deep = _nested_mapping(66)
    with pytest.raises(ProfileValueRefusal) as caught:
        resolution._resolve_metadata_value(
            too_deep,
            identity,
        )
    assert caught.value.code == "profile_document_depth_exceeded"
    assert caught.value.destination is DestinationClass.UNCLASSIFIED
    assert max(map(len, visited_paths)) == 65


def test_shared_alias_diamond_and_equal_distinct_containers_remain_valid() -> None:
    source = _profile_text(
        metadata=(
            "hooks:\n"
            "  shared: &shared\n"
            "    value: literal\n"
            "  left:\n"
            "    child: *shared\n"
            "  right:\n"
            "    child: *shared\n"
        )
    )
    profile = agent_profiles.parse_agent_profile_text(
        source,
        "synthetic",
        value_resolver=_resolver().resolve,
    )
    assert profile.hooks == {
        "shared": {"value": "literal"},
        "left": {"child": {"value": "literal"}},
        "right": {"child": {"value": "literal"}},
    }

    equal_but_distinct = {
        "left": {"value": "literal"},
        "right": {"value": "literal"},
    }
    assert (
        resolution._resolve_metadata_value(
            equal_but_distinct,
            _resolver().resolve,
        )
        == equal_but_distinct
    )


def test_recursive_alias_legacy_none_behavior_is_unchanged() -> None:
    source = _profile_text(
        metadata=("hooks: &recursive\n" "  value: literal\n" "  child: *recursive\n")
    )

    profile = agent_profiles.parse_agent_profile_text(
        source,
        "synthetic",
        value_resolver=None,
    )
    assert profile.hooks is not None
    assert profile.hooks["value"] == "literal"
    assert profile.hooks["child"]["value"] == "literal"


def test_pure_resolution_module_has_no_services_import() -> None:
    source = resolution.__file__
    assert source is not None
    with open(source, encoding="utf-8") as module_file:
        module_source = module_file.read()
    assert "cli_agent_orchestrator.services" not in module_source
    assert "ANTHROPIC_API_KEY" not in module_source
    assert "CLAUDE_CODE_USE_BEDROCK" not in module_source
