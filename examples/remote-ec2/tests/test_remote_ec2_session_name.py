"""bin/box-session-name.sh: cao-<instance-id> from a mocked IMDSv2."""

from __future__ import annotations

from remote_ec2_support import FAKE_INSTANCE_ID, write_stub

TOKEN = "IMDS_TOKEN_CANARY"


def fake_imds(sandbox, token: str = TOKEN, instance_id: str = FAKE_INSTANCE_ID) -> None:
    """A fake curl that speaks IMDSv2: PUT for a token, then GET with it."""
    write_stub(
        sandbox.stubs,
        "curl",
        f'echo "curl $*" >> "{sandbox.calls}"\n'
        'case "$*" in\n'
        f'  *"-X PUT"*"/latest/api/token"*) printf "%s" "{token}" ;;\n'
        f'  *"X-aws-ec2-metadata-token: {TOKEN}"*"/latest/meta-data/instance-id"*)'
        f' printf "%s" "{instance_id}" ;;\n'
        "  *) exit 22 ;;\n"
        "esac\n",
    )


def test_prints_cao_instance_id_using_a_token(sandbox) -> None:
    fake_imds(sandbox)
    result = sandbox.run("bin/box-session-name.sh", IMDS_ENDPOINT="http://imds.invalid")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"cao-{FAKE_INSTANCE_ID}\n"
    calls = sandbox.recorded_calls()
    assert len(calls) == 2
    assert "-X PUT" in calls[0] and "ttl-seconds" in calls[0]
    assert f"X-aws-ec2-metadata-token: {TOKEN}" in calls[1]
    assert all("http://imds.invalid/latest/" in c for c in calls)


def test_fails_closed_without_a_token(sandbox) -> None:
    fake_imds(sandbox, token="")
    result = sandbox.run("bin/box-session-name.sh", IMDS_ENDPOINT="http://imds.invalid")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no IMDSv2 token" in result.stderr
    assert len(sandbox.recorded_calls()) == 1  # never falls back to IMDSv1


def test_rejects_a_malformed_instance_id(sandbox) -> None:
    fake_imds(sandbox, instance_id="i-NOT/AN-ID; rm")
    result = sandbox.run("bin/box-session-name.sh", IMDS_ENDPOINT="http://imds.invalid")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "unexpected instance id" in result.stderr


def test_fails_when_imds_is_unreachable(sandbox) -> None:
    write_stub(sandbox.stubs, "curl", "exit 7\n")
    result = sandbox.run("bin/box-session-name.sh", IMDS_ENDPOINT="http://imds.invalid")
    assert result.returncode == 1
    assert result.stdout == ""
