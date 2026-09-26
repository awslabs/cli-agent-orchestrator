"""The owner of deferred work, and the revocation asymmetry (#745, criterion 14).

These tests pin the *decisions* in ``security/principal.py`` rather than its
shapes: that an unreadable owner never degrades into the local user, that an
unrecorded owner is not treated as revoked, and that revocation withdraws the
authority to START work while leaving the authority to STOP it intact.
"""

import json

import pytest

from cli_agent_orchestrator.security.principal import (
    LOCAL_ISSUER,
    LOCAL_PRINCIPAL,
    REVOKED_ENV,
    Principal,
    PrincipalError,
    may_start_work,
    may_stop_work,
    revocation,
)


@pytest.fixture(autouse=True)
def _clean_revocations(monkeypatch):
    monkeypatch.delenv(REVOKED_ENV, raising=False)
    revocation.reset()
    yield
    revocation.reset()


# --- canonical form -------------------------------------------------------


def test_id_round_trips_through_parse():
    principal = Principal(subject="user|abc123", issuer="https://idp.example/")
    assert Principal.parse(principal.id) == principal


def test_local_principal_is_named_not_null():
    # The criterion forbids deferred work becoming anonymous. "local" is an
    # answer; None is the absence of one, and the two must not be spelled alike.
    assert LOCAL_PRINCIPAL.id == f"{LOCAL_ISSUER}#local"
    assert LOCAL_PRINCIPAL.is_local is True
    assert Principal.parse(LOCAL_PRINCIPAL.id) == LOCAL_PRINCIPAL


@pytest.mark.parametrize("field", ["subject", "issuer"])
def test_separator_is_rejected_in_either_half(field):
    kwargs = {"subject": "sub", "issuer": "iss"}
    kwargs[field] = "has#separator"
    with pytest.raises(PrincipalError):
        Principal(**kwargs)


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_values_are_rejected(bad):
    with pytest.raises(PrincipalError):
        Principal(subject=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_reads_missing_as_unknown(raw):
    assert Principal.parse(raw) is None


def test_parse_refuses_to_guess_at_a_malformed_id():
    # The failure that matters: a truncated or newer-format value silently
    # read as the local user would hand a stranger's work to the local owner.
    with pytest.raises(PrincipalError):
        Principal.parse("just-a-subject")


def test_json_form_round_trips():
    principal = Principal(subject="s", issuer="https://idp.example/")
    assert Principal.from_json(principal.to_json()) == principal


def test_json_form_is_sorted_so_two_writers_agree():
    assert Principal(subject="s", issuer="i").to_json() == json.dumps(
        {"iss": "i", "sub": "s"}, sort_keys=True
    )


def test_from_json_tolerates_a_future_tenant_key():
    # #774/#778 will add a tenant to this document. An older server must carry
    # what it understands rather than refuse the work.
    raw = json.dumps({"iss": "https://idp.example/", "sub": "s", "tenant": "acme"})
    assert Principal.from_json(raw) == Principal(subject="s", issuer="https://idp.example/")


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_from_json_reads_missing_as_unknown(raw):
    assert Principal.from_json(raw) is None


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"a string"'])
def test_from_json_refuses_a_non_document(raw):
    with pytest.raises(PrincipalError):
        Principal.from_json(raw)


def test_principal_is_immutable():
    principal = Principal(subject="s")
    with pytest.raises(Exception):
        principal.subject = "someone-else"  # type: ignore[misc]


# --- revocation -----------------------------------------------------------


def test_revoked_owner_may_not_start_but_may_still_be_stopped():
    """The asymmetry the criterion turns on.

    Getting this backwards leaves a removed member's agent running with nobody
    authorized to kill it, which is worse than the access it withdrew.
    """
    principal = Principal(subject="removed-member", issuer="https://idp.example/")
    revocation.revoke(principal)
    assert may_start_work(principal) is False
    assert may_stop_work(principal) is True


def test_reinstating_restores_the_authority_to_start():
    principal = Principal(subject="s")
    revocation.revoke(principal)
    revocation.reinstate(principal)
    assert may_start_work(principal) is True


def test_unknown_owner_is_not_revoked():
    # Rows written before the owner column existed must keep working on upgrade.
    assert may_start_work(None) is True
    assert may_stop_work(None) is True


def test_revocation_is_per_principal_not_per_subject():
    # Same subject at a different issuer is a different person.
    revoked = Principal(subject="s", issuer="https://a.example/")
    other = Principal(subject="s", issuer="https://b.example/")
    revocation.revoke(revoked)
    assert may_start_work(revoked) is False
    assert may_start_work(other) is True


def test_env_seeding_is_lazy_so_the_server_reads_its_own_environment(monkeypatch):
    principal = Principal(subject="seeded", issuer="https://idp.example/")
    # Set AFTER import, which is the real ordering: the module graph is built
    # before the process reads its configuration.
    monkeypatch.setenv(REVOKED_ENV, f"  {principal.id}  ")
    revocation.reset()
    assert may_start_work(principal) is False


def test_env_seeding_accepts_newline_and_comma_separated_lists(monkeypatch):
    first = Principal(subject="a", issuer="https://idp.example/")
    second = Principal(subject="b", issuer="https://idp.example/")
    monkeypatch.setenv(REVOKED_ENV, f"{first.id},\n{second.id}\n")
    revocation.reset()
    assert revocation.revoked_ids() == {first.id, second.id}


def test_malformed_env_entry_is_logged_and_skipped_without_losing_the_rest(monkeypatch, caplog):
    good = Principal(subject="a", issuer="https://idp.example/")
    monkeypatch.setenv(REVOKED_ENV, f"garbage-no-separator,{good.id}")
    revocation.reset()
    with caplog.at_level("ERROR"):
        assert may_start_work(good) is False
    assert any(REVOKED_ENV in record.getMessage() for record in caplog.records)


def test_any_revoked_is_false_on_a_clean_installation():
    # The fast path the inbox gate depends on: no revocations means no owner
    # lookup, so single-user installations pay nothing for this feature.
    assert revocation.any_revoked() is False
    revocation.revoke(Principal(subject="s"))
    assert revocation.any_revoked() is True
