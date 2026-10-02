"""The CodeQL setup prerequisite is read-only unless explicitly requested."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_codeql_advanced.py"
    spec = importlib.util.spec_from_file_location("_codeql_setup_prerequisite", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = _load_module()
ARGS = ["--repo", "example/project", "--ref", "a" * 40]
ENDPOINT = "repos/example/project/code-scanning/default-setup"


@pytest.fixture
def gh(monkeypatch):
    command = Mock()
    monkeypatch.setattr(setup.subprocess, "check_output", command)
    return command


def test_enabled_default_setup_reports_the_upload_block_without_changing_it(gh, capsys):
    gh.side_effect = ["file\n", "configured\n"]

    assert setup.main(ARGS) == 1
    assert "blocks advanced CodeQL uploads" in capsys.readouterr().err
    assert gh.call_count == 2
    assert all("PATCH" not in call.args[0] for call in gh.call_args_list)


@pytest.mark.parametrize("apply", [False, True])
def test_already_disabled_setup_is_not_written_again(gh, apply, capsys):
    gh.side_effect = ["file\n", "not-configured\n"]

    assert setup.main(ARGS + (["--disable-default-setup"] if apply else [])) == 0
    assert gh.call_count == 2
    assert "does not prove" in capsys.readouterr().out


def test_explicit_opt_in_changes_only_the_setup_state_and_verifies_it(gh, capsys):
    gh.side_effect = ["file\n", "configured\n", "", "not-configured\n"]

    assert setup.main(ARGS + ["--disable-default-setup"]) == 0
    assert gh.call_args_list[2].args[0] == [
        "gh",
        "api",
        ENDPOINT,
        "--method",
        "PATCH",
        "--raw-field",
        "state=not-configured",
        "--silent",
    ]
    assert gh.call_args_list[3].args[0] == [
        "gh",
        "api",
        ENDPOINT,
        "--jq",
        ".state",
    ]
    assert "does not prove" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["dir", "symlink", "null", ""])
def test_missing_replacement_workflow_prevents_any_setting_change(gh, kind, capsys):
    gh.return_value = kind

    assert setup.main(ARGS + ["--disable-default-setup"]) == 1
    assert "workflow file" in capsys.readouterr().err
    assert gh.call_count == 1


@pytest.mark.parametrize("state", ["null", "", "unexpected"])
def test_unknown_setup_state_fails_closed(gh, state, capsys):
    gh.side_effect = ["file", state]

    assert setup.main(ARGS + ["--disable-default-setup"]) == 1
    assert "default-setup state" in capsys.readouterr().err
    assert gh.call_count == 2


def test_unconfirmed_update_is_not_reported_as_success(gh, capsys):
    gh.side_effect = ["file", "configured", "", "configured"]

    assert setup.main(ARGS + ["--disable-default-setup"]) == 1
    assert "could not be confirmed" in capsys.readouterr().err


@pytest.mark.parametrize("failure_at", [0, 1, 2, 3])
def test_github_errors_are_not_hidden_or_retried(gh, failure_at, capsys):
    replies = ["file", "configured", "", "not-configured"]
    gh.side_effect = replies[:failure_at] + [subprocess.CalledProcessError(1, ["gh", "api"])]

    assert setup.main(ARGS + ["--disable-default-setup"]) == 1
    message = capsys.readouterr().err
    assert "GitHub CLI request failed" in message
    assert ("may have changed" in message) is (failure_at >= 2)
    assert gh.call_count == failure_at + 1


def test_missing_github_cli_is_actionable(gh, capsys):
    gh.side_effect = FileNotFoundError("gh")

    assert setup.main(ARGS) == 1
    assert "Install and authenticate" in capsys.readouterr().err


def test_missing_cli_after_a_write_reports_the_uncertain_setting(gh, capsys):
    gh.side_effect = ["file", "configured", "", FileNotFoundError("gh")]

    assert setup.main(ARGS + ["--disable-default-setup"]) == 1
    assert "may have changed" in capsys.readouterr().err


def test_repository_and_revision_are_not_tied_to_one_pr(gh):
    gh.side_effect = ["file", "not-configured"]
    revision = "B" * 40

    assert setup.main(["--repo", "other/another.project", "--ref", revision]) == 0
    assert gh.call_args_list[0].args[0] == [
        "gh",
        "api",
        "repos/other/another.project/contents/.github/workflows/codeql.yml",
        "--method",
        "GET",
        "--raw-field",
        f"ref={revision}",
        "--jq",
        ".type",
    ]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--repo", "example/project"],
        ["--repo", "https://github.com/example/project", "--ref", "a" * 40],
        ["--repo", "../example/project", "--ref", "a" * 40],
        ["--repo", "../project", "--ref", "a" * 40],
        ["--repo", "example/..", "--ref", "a" * 40],
        ["--repo", "example/project", "--ref", "main"],
        ["--repo", "example/project", "--ref", "a" * 7],
        ["--repo", "example/project", "--ref", "x" * 40],
    ],
)
def test_invalid_arguments_are_rejected_before_contacting_github(gh, args):
    with pytest.raises(SystemExit) as result:
        setup.main(args)

    assert result.value.code == 2
    gh.assert_not_called()
