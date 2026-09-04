from unicodedata import normalize

import pytest

from cli_agent_orchestrator.services.vault.identity import (
    cao_key,
    derive_cao_key,
    derive_note_uid,
)


def test_long_derived_keys_are_distinct_and_never_exceed_memory_key_limit():
    prefix = "Deeply/Nested/"
    first = derive_cao_key(prefix + "a" * 62 + "1.md")
    second = derive_cao_key(prefix + "a" * 62 + "2.md")

    assert first != second
    assert len(first) == len(second) == 60
    assert first[:-9] == second[:-9]


def test_nfc_and_nfd_paths_have_identical_key_and_note_uid():
    nfc = "Références/Design.md"
    nfd = normalize("NFD", nfc)

    assert derive_cao_key(nfc) == derive_cao_key(nfd)
    assert derive_note_uid("primary", "global", None, derive_cao_key(nfc)) == derive_note_uid(
        "primary", "global", None, derive_cao_key(nfd)
    )


def test_authored_cao_key_is_rejected_not_sanitized():
    with pytest.raises(ValueError, match=r"cao.key must match"):
        cao_key("Author Key", "Folder/Design.md")


def test_empty_sanitized_stem_uses_a_valid_deterministic_fallback():
    assert derive_cao_key("Folder/!!!.md").startswith("note-")
