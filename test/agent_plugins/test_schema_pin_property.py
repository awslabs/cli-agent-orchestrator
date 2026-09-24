"""Structural properties of the vendored schema pin (W2).

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 23.1; Property P11**

``test_schema_pin.py`` already asserts the
things that can be checked by *reading the bytes*: the recorded hashes match, the
plugin schema is closed, ``--check`` fails on tampering or on a missing file, and
validation completes with every socket blocked. None of that is repeated here.

What this file adds is agreement **between** the artifacts that describe the pin —
``PIN.json``, the vendored files, the vendoring script, and the validator's own
notion of which schema id it supports. Each of those states the same facts, and
nothing previously required them to agree, so any one could drift and the suite
would stay green while CAO validated against a schema it no longer claimed to
support.

Deliberately **not** duplicated: ``test_vendored_bytes_hash_to_recorded_value``,
``test_manifest_schema_closes_additional_properties``,
``test_offline_check_*`` (four), ``test_valid_manifest_validates_with_sockets_blocked``,
``test_validator_builds_with_sockets_blocked`` and
``test_registry_refuses_to_retrieve_an_unvendored_ref`` — all already in
``test_schema_pin.py`` as ``test_vendored_bytes_hash_to_the_recorded_value``,
``test_vendored_plugin_schema_is_closed``, ``test_check_*``,
``test_valid_plugin_validates_with_sockets_blocked``,
``test_unknown_schema_is_rejected_offline_rather_than_fetched`` and
``test_remote_ref_resolution_is_refused``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.validation import (
    MCP_SCHEMA_FILENAME,
    PLUGIN_SCHEMA_FILENAME,
    load_pinned_schema,
    supported_schema_id,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "schemas" / "agent_plugins" / "1.0.0"
PIN_PATH = VENDOR_DIR / "PIN.json"
VENDOR_SCRIPT = REPO_ROOT / "scripts" / "vendor_agent_plugins_schemas.py"

SCHEMA_FILENAMES = [PLUGIN_SCHEMA_FILENAME, MCP_SCHEMA_FILENAME]

#: A full 40-hex git commit id. An abbreviation would make the pin ambiguous,
#: which defeats the point of recording it.
FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


@pytest.fixture(scope="module")
def pin() -> dict:
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


class TestThePinIsSelfDescribing:
    """Requirement 4.1 — a reader can tell what was pinned and from where."""

    def test_the_pin_names_the_specification_and_version(self, pin: dict) -> None:
        assert pin["specification"] == "Agent Plugins"
        assert pin["version"] == "1.0.0"

    def test_the_pinned_commit_is_a_full_hash(self, pin: dict) -> None:
        """An abbreviated hash is ambiguous, so it cannot serve as a pin.

        The commit is the only field that lets a reviewer fetch exactly what CAO
        vendored; a prefix would let two different upstream states satisfy it.
        """
        assert FULL_SHA.match(pin["specification_commit"]), pin["specification_commit"]

    def test_the_pin_records_exactly_the_files_that_are_vendored(self, pin: dict) -> None:
        """No unrecorded schema, and no recorded-but-absent one.

        Both directions matter. An unrecorded file would be loadable but
        unverified — exactly the gap the hash guard exists to close — and a
        recorded-but-absent one makes ``--check`` fail for a reason that looks like
        tampering.
        """
        recorded = set(pin["files"])
        on_disk = {path.name for path in VENDOR_DIR.glob("*.schema.json")}

        assert recorded == on_disk

    def test_the_pin_states_its_own_policy(self) -> None:
        """A pin nobody knows the rules for is a number, not a policy.

        The rule this repository follows — pin to the commit
        ``agent-plugins.org`` currently serves, and re-vendor deliberately rather
        than tracking upstream — has to be written down beside the pin, or the next
        person to see a drift failure cannot tell whether the correct response is
        to update the pin or to investigate.
        """
        text = PIN_PATH.read_text(encoding="utf-8")

        assert "pin_policy" in text
        assert "agent-plugins.org" in json.loads(text)["pin_policy"]


class TestTheArtifactsAgreeWithEachOther:
    """The cross-checks. Each fact is stated in more than one place."""

    @pytest.mark.parametrize("filename", SCHEMA_FILENAMES)
    def test_the_recorded_schema_id_matches_the_vendored_document(
        self, pin: dict, filename: str
    ) -> None:
        """``PIN.json``'s ``schema_id`` is the document's own ``$id``.

        If these disagree, ``--check`` still passes (the bytes hash correctly) while
        the registry resolves a different id than the pin advertises.
        """
        document = load_pinned_schema(filename)

        assert pin["files"][filename]["schema_id"] == document["$id"]

    @pytest.mark.parametrize("filename", SCHEMA_FILENAMES)
    def test_the_validator_supports_exactly_the_pinned_id(self, pin: dict, filename: str) -> None:
        """``supported_schema_id`` is what a plugin's ``$schema`` is compared against.

        This is the assertion that ties acceptance to the pin: without it, CAO could
        vendor 1.0.0 and accept a different version's id, or reject the very
        documents it ships.
        """
        assert supported_schema_id(filename) == pin["files"][filename]["schema_id"]

    @pytest.mark.parametrize("filename", SCHEMA_FILENAMES)
    def test_the_recorded_url_and_the_schema_id_describe_one_document(
        self, pin: dict, filename: str
    ) -> None:
        entry = pin["files"][filename]

        assert entry["url"].endswith(filename)
        assert entry["schema_id"].endswith(filename)
        assert entry["url"].startswith(pin["source_base_url"])

    def test_the_vendoring_script_targets_the_directory_the_validator_reads(self) -> None:
        """Script and runtime must mean the same directory.

        A vendoring script writing somewhere the validator never looks would leave
        ``make check-agent-plugins-schemas`` green against files nothing loads —
        the failure mode where the gate exists and guards nothing.
        """
        source = VENDOR_SCRIPT.read_text(encoding="utf-8")

        assert "agent_plugins" in source
        assert "1.0.0" in source
        for filename in SCHEMA_FILENAMES:
            assert filename in source

    def test_the_version_directory_matches_the_recorded_version(self, pin: dict) -> None:
        """The path is load-bearing: ``importlib.resources`` reads it by name."""
        assert VENDOR_DIR.name == pin["version"]


class TestTheVendoredDocumentsAreUsableSchemas:
    """Requirement 4.2 — vendored bytes are a schema, not merely bytes.

    ``test_schema_pin.py`` proves the bytes are *unchanged*. These prove they are
    *valid*, which is a different failure: a truncated or half-written file would
    hash to whatever it hashes to, and ``--check`` would faithfully confirm it.
    """

    @pytest.mark.parametrize("filename", SCHEMA_FILENAMES)
    def test_each_document_is_json_with_an_id_and_a_dialect(self, filename: str) -> None:
        document = load_pinned_schema(filename)

        assert isinstance(document, dict)
        assert document["$id"].endswith(filename)
        assert "2020-12" in document["$schema"], document["$schema"]

    @pytest.mark.parametrize("filename", SCHEMA_FILENAMES)
    def test_each_document_is_accepted_by_the_draft_it_declares(self, filename: str) -> None:
        """A schema that its own dialect rejects cannot validate anything.

        ``check_schema`` is the metavalidation step: it would catch a vendored file
        that parsed as JSON but used, say, a keyword incorrectly.
        """
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(load_pinned_schema(filename))

    def test_the_two_schemas_are_different_documents(self) -> None:
        """Guards a vendoring bug that fetched one URL twice.

        Both files would hash consistently with whatever was written, and every
        other check would pass, while MCP documents were validated against the
        plugin schema.
        """
        plugin = load_pinned_schema(PLUGIN_SCHEMA_FILENAME)
        mcp = load_pinned_schema(MCP_SCHEMA_FILENAME)

        assert plugin["$id"] != mcp["$id"]
