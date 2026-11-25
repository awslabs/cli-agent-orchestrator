import os
import re
import subprocess
import sys

import pytest

from conftest import KIT


BIN = KIT / "bin"
KINDS = ("private_path", "private_name", "internal_id", "account_id", "instance_id", "host_name",
         "html_comment", "email", "credential")


def helper(name, *args, input=None, cwd=None):
    return subprocess.run([sys.executable, BIN / name, *map(str, args)], input=input,
                          text=True, capture_output=True, cwd=cwd)


def test_body_equals_normalises_newlines(tmp_path):
    posted, source = tmp_path / "posted", tmp_path / "source"
    posted.write_bytes(b"same\r\n\r\n")
    source.write_text("same\n")
    result = helper("body_equals.py", posted, source)
    assert (result.returncode, result.stdout.strip()) == (0, "BODY-OK")


def test_body_equals_reports_short_diff(tmp_path):
    posted, source = tmp_path / "posted", tmp_path / "source"
    posted.write_text("new\n")
    source.write_text("old\n")
    result = helper("body_equals.py", posted, source)
    assert result.returncode == 1 and result.stdout.startswith("BODY-DIFF\n")


def test_leak_check_clean_and_redacted_hit(tmp_path):
    clean = tmp_path / "clean"
    clean.write_text("Public text /Users/<you> uses <instance-id>.\n")
    assert helper("leak_check.py", clean).stdout.strip() == "CLEAN"
    leaky = tmp_path / "leaky"
    leaky.write_text("private /Users/example/project\n")
    result = helper("leak_check.py", leaky)
    assert (result.returncode, result.stdout.strip()) == (1, "HIT private_path line=1")
    assert "example" not in result.stdout


@pytest.mark.parametrize("kind,text", [
    ("private_path", "/home/person/x"), ("internal_id", "brief 37"),
    ("account_id", "123456789012"), ("instance_id", "i-deadbeef"),
    ("host_name", "ip-10-0-0-1"), ("html_comment", "<!-- private -->"),
    # the patterns an old playbook grep had, now in the one check
    ("private_path", "/tmp/x"), ("private_path", "see /private/var/folders"),
    ("private_path", "/Users/person/x"),
    ("private_name", "the my-workplace repo"), ("private_name", "my-local-server box"),
    ("internal_id", "terminal 1a2b3c4d"), ("internal_id", "short sha 0e7ab60f"),
    ("private_name", "my-decider-x"),
    ("email", "person@example.com"),
    ("credential", "GH_TOKEN=CANARY1234abcd"),
    ("credential", "client_" + "secret: " + "CANARY1234abcd"),
    ("credential", '"api_' + 'token": "' + "CANARY1234abcd" + '"'),
    ("credential", "ghp_" + "CANARY" * 3),
    ("credential", "gho_" + "CANARY" * 3), ("credential", "github_pat_" + "CANARY" * 3),
    ("credential", "Authorization: Bearer " + "CANARY" * 3),
    ("credential", "AKIA" + "CANARY" * 2 + "1234"),
    ("credential", "-----BEGIN OPENSSH PRIVATE KEY-----"),
])
def test_leak_check_reports_each_kind_without_value(tmp_path, kind, text):
    path = tmp_path / "input"
    path.write_text(text + "\n")
    result = helper("leak_check.py", path)
    # a text may hit more than one kind (i-deadbeef is also an 8-hex word); its own kind must be there
    assert result.returncode == 1 and f"HIT {kind} line=1" in result.stdout.splitlines()
    assert set(result.stdout.splitlines()) <= {f"HIT {k} line=1" for k in KINDS}
    assert text not in result.stdout


@pytest.mark.parametrize("text", [
    "the token file", "a secret-free example", "kitpub reads with its own token.",
    "token: unavailable", "secret: none", "bearer authentication",
    "TOKEN_FILE = the configured path", "/Users/<you>/x and <instance-id>",
    "commit 0e7ab60f0123456789abcdef0123456789abcdef",
    "the bot's private chat", "files under /tmp", "a cao workplace",
])
def test_leak_check_harmless_prose_and_full_shas_are_clean(tmp_path, text):
    path = tmp_path / "input"
    path.write_text(text + "\n")
    result = helper("leak_check.py", path)
    assert (result.returncode, result.stdout.strip()) == (0, "CLEAN")


def test_leak_check_full_sha_clean_short_sha_hits(tmp_path):
    full, short = "0e7ab60f" + "0123456789abcdef" * 2, "0e7ab60f"
    path = tmp_path / "input"
    path.write_text(f"merged {full}\n")
    assert helper("leak_check.py", path).stdout.strip() == "CLEAN"
    path.write_text(f"merged {short}\n")
    result = helper("leak_check.py", path)
    assert (result.returncode, result.stdout.strip()) == (1, "HIT internal_id line=1")


def test_leak_check_reports_every_kind_on_its_line(tmp_path):
    path = tmp_path / "input"
    path.write_text("clean line\nme@example.com and my-local-server\n")
    result = helper("leak_check.py", path)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["HIT private_name line=2", "HIT email line=2"]


def test_changeset_check_same_and_different(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "test@example.invalid"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "a").write_text("one\n")
    subprocess.run(["git", "add", "a"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "one"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                          capture_output=True, check=True).stdout.strip()
    (repo / "a").write_text("two\n")
    subprocess.run(["git", "commit", "-am", "two"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                          capture_output=True, check=True).stdout.strip()
    assert helper("changeset_check.py", repo, f"{base}..{head}", f"{base}..{head}").stdout.strip() == "SAME"
    result = helper("changeset_check.py", repo, f"{base}..{head}", f"{base}..{base}")
    assert result.returncode == 1 and result.stdout.startswith("DIFF")


def test_test_env_run_isolated_and_lists_registry(tmp_path):
    result = helper("test_env.py", "run", "--label", "unit", "--", sys.executable, "-c",
                    "import os; assert os.environ['HOME'].startswith('/tmp/cao-test-env-')")
    assert result.returncode == 0
    scratch = result.stdout.splitlines()[0]
    listed = helper("test_env.py", "list")
    assert scratch in listed.stdout
    cleaned = helper("test_env.py", "clean", scratch)
    assert cleaned.returncode == 0 and not os.path.exists(scratch)


def test_test_env_clean_refuses_unrecorded_or_unsafe_paths(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = helper("test_env.py", "clean", outside)
    assert result.returncode == 2 and result.stderr.strip() == "ERROR unrecorded_or_unsafe_path"


def test_test_env_clean_refuses_registered_path_with_open_file():
    result = helper("test_env.py", "run", "--label", "open-file", "--", sys.executable, "-c", "pass")
    scratch = result.stdout.splitlines()[0]
    held = os.path.join(scratch, "held")
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sys,time; f=open(sys.argv[1], 'w'); print('ready', flush=True); time.sleep(20)",
                               held], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        refused = helper("test_env.py", "clean", scratch)
        assert refused.returncode == 1 and refused.stderr.strip() == "ERROR path_in_use"
    finally:
        holder.terminate()
        holder.wait()
    assert helper("test_env.py", "clean", scratch).returncode == 0


def test_direct_gate_hints_preserve_stdout(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    check = helper("check_mandate.py", "--mandates", bad, "--holder", "shepherd",
                   "--action", "open_pr", "--pr-author", "me", "--base", "main", "--repo", "o/r")
    assert check.stdout.strip() == "DENY unreadable_input:JSONDecodeError"
    assert check.stderr.startswith("hint: resolve mandate condition")
    resource = helper("resource_gate.py", "--config", tmp_path / "missing")
    assert resource.stdout.strip() == "STOP unreadable_config:FileNotFoundError"
    assert "resolve that measured condition" in resource.stderr
    ledger = helper("go_ledger.py", "next-id", "--pr", "bad", "--action", "bad", "--head", "bad")
    assert ledger.stdout.startswith("ERROR bad --pr")
    assert "numeric PR" in ledger.stderr
    auto = helper("automerge_gate.py", "--action", "arm", "--pr", tmp_path / "missing",
                  "--reviews", tmp_path / "missing", "--commits", tmp_path / "missing",
                  "--issue-comments", tmp_path / "missing", "--review-comments", tmp_path / "missing",
                  "--identities", tmp_path / "missing", "--required", tmp_path / "missing")
    assert auto.stdout.strip() == "DENY unreadable_input:FileNotFoundError"
    assert auto.stderr.startswith("hint: resolve")
    body = tmp_path / "body"
    go = tmp_path / "go.jsonl"
    body.write_text("x")
    go.write_text("")
    holds = helper("holds.py", "clear", "--repo", "o/r", "--pr-author", "me", "--base", "main",
                   "--pr", "1", "--item", "review:1", "--body-file", body, "--reason", "clean",
                   "--go-id", "missing", "--go-file", go,
                   "--mandates", KIT / "mandates.example.json")
    assert holds.stdout.startswith("REFUSED go_missing:")
    assert "go_ledger.py show" not in holds.stderr and "go_ledger.py add" in holds.stderr


def test_check_mandate_go_hints_name_real_go_ledger_commands(tmp_path):
    mandates = KIT / "mandates.example.json"
    go = tmp_path / "go.jsonl"
    go.write_text("")
    common = ["--mandates", mandates, "--holder", "shepherd", "--pr-author", "me", "--base", "main",
              "--repo", "o/r"]
    forbidden = helper("check_mandate.py", *common, "--action", "force_push")
    assert forbidden.stdout.startswith("DENY forbidden_without_user:force_push")
    assert "kitpub approval card" not in forbidden.stderr
    assert forbidden.stderr.startswith("hint: ")
    assert "ask the user" in forbidden.stderr
    assert "go_ledger.py add --granted-by user --relayed-by <conductor>" in forbidden.stderr
    commands = set(re.findall(r"go_ledger\.py (\S+)", forbidden.stderr))
    help_text = helper("go_ledger.py", "--help").stdout
    assert commands and all(c in help_text for c in commands)
