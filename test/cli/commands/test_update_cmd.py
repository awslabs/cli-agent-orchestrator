"""Tests for the ``cao update`` command (issue #26)."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.update import _DIRECTORY as _DIR
from cli_agent_orchestrator.cli.commands.update import (
    _EDITABLE,
    _GIT,
    _PATH,
    _REGISTRY,
    _REGISTRY_CONSTRAINED,
    _build_command,
    _classify_source,
    _receipt_path,
    update,
)

_MOD = "cli_agent_orchestrator.cli.commands.update"
_PACKAGE = "cli-agent-orchestrator"

# Real uv formats (verified against uv 0.8.x / 0.9.x).
_GIT_RECEIPT_REAL = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", git = "https://github.com/awslabs/cli-agent-orchestrator.git?rev=main" }]
"""

_GIT_RECEIPT_NO_REF = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", git = "https://github.com/awslabs/cli-agent-orchestrator.git" }]
"""

_GIT_RECEIPT_SEPARATE_REV = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", git = "https://github.com/awslabs/cli-agent-orchestrator.git", branch = "main" }]
"""

# Unpinned registry install: a bare name, no specifier.
_REGISTRY_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator" }]
"""

# Version-constrained registry installs (verified real shapes with uv, e.g.
# `uv tool install ruff==0.11.0` / `'ruff<0.12'`). ANY constraint can hold the
# install below the latest release, so all route through the @latest unpin path.
_REGISTRY_EXACT_PIN_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", specifier = "==2.1.0" }]
"""

_REGISTRY_UPPER_BOUND_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", specifier = "<2.4" }]
"""

_REGISTRY_COMPATIBLE_RELEASE_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", specifier = "~=2.3" }]
"""

_DIRECTORY_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", directory = "/home/me/cli-agent-orchestrator" }]
"""

_PATH_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", path = "/home/me/dist/cli_agent_orchestrator-2.3.0-py3-none-any.whl" }]
"""

# Editable clone (verified real shape via `uv tool install --editable .`).
_EDITABLE_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", editable = "/home/me/cli-agent-orchestrator" }]
"""

# Structurally corrupt but syntactically valid TOML (wrong shapes).
_TOOL_NOT_TABLE = 'tool = "corrupt"\n'
_REQS_NOT_LIST = "[tool]\nrequirements = 42\n"
_DIR_NOT_STRING = (
    "[tool]\nrequirements = [{ name = " '"cli-agent-orchestrator", directory = true }]\n'
)


def _completed(returncode):
    result = MagicMock()
    result.returncode = returncode
    return result


def _dir_run(stdout="", returncode=0):
    """A subprocess.run result for the `uv tool dir` call."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


def _write(tmp_path, content):
    r = tmp_path / "uv-receipt.toml"
    r.write_text(content)
    return r


class TestReceiptPath:
    """_receipt_path locates uv's receipt via `uv tool dir` (or degrades)."""

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}.shutil.which", return_value=None)
    def test_none_when_uv_missing(self, _which, mock_run):
        assert _receipt_path() is None
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_returns_receipt_when_present(self, _which, mock_run, tmp_path):
        (tmp_path / _PACKAGE).mkdir()
        receipt = tmp_path / _PACKAGE / "uv-receipt.toml"
        receipt.write_text(_REGISTRY_RECEIPT)
        mock_run.return_value = _dir_run(stdout=f"{tmp_path}\n")

        assert _receipt_path() == receipt
        mock_run.assert_called_once_with(
            ["uv", "tool", "dir"], capture_output=True, text=True, check=True
        )

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_none_when_receipt_file_absent(self, _which, mock_run, tmp_path):
        mock_run.return_value = _dir_run(stdout=f"{tmp_path}\n")
        assert _receipt_path() is None

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_none_when_uv_tool_dir_empty_output(self, _which, mock_run):
        mock_run.return_value = _dir_run(stdout="\n")
        assert _receipt_path() is None

    @patch(f"{_MOD}.subprocess.run", side_effect=OSError("boom"))
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_none_when_uv_tool_dir_raises_oserror(self, _which, _run):
        assert _receipt_path() is None

    @patch(
        f"{_MOD}.subprocess.run",
        side_effect=__import__("subprocess").CalledProcessError(1, ["uv", "tool", "dir"]),
    )
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_none_when_uv_tool_dir_nonzero_exit(self, _which, _run):
        # `uv tool dir` exits non-zero -> degrade to None (not raise).
        assert _receipt_path() is None


class TestClassifySource:
    """_classify_source maps a receipt to (kind, detail)."""

    def test_none_receipt_is_registry(self):
        assert _classify_source(None) == (_REGISTRY, None)

    def test_git_with_rev_query(self, tmp_path):
        assert _classify_source(_write(tmp_path, _GIT_RECEIPT_REAL)) == (
            _GIT,
            "git+https://github.com/awslabs/cli-agent-orchestrator.git?rev=main",
        )

    def test_git_no_ref(self, tmp_path):
        assert _classify_source(_write(tmp_path, _GIT_RECEIPT_NO_REF)) == (
            _GIT,
            "git+https://github.com/awslabs/cli-agent-orchestrator.git",
        )

    def test_git_separate_rev_key(self, tmp_path):
        assert _classify_source(_write(tmp_path, _GIT_RECEIPT_SEPARATE_REV)) == (
            _GIT,
            "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
        )

    def test_registry_unconstrained(self, tmp_path):
        assert _classify_source(_write(tmp_path, _REGISTRY_RECEIPT)) == (_REGISTRY, None)

    def test_registry_exact_pin_is_constrained(self, tmp_path):
        assert _classify_source(_write(tmp_path, _REGISTRY_EXACT_PIN_RECEIPT)) == (
            _REGISTRY_CONSTRAINED,
            "==2.1.0",
        )

    def test_registry_upper_bound_is_constrained(self, tmp_path):
        # `<2.4` can hold the install below latest — must NOT be plain registry.
        assert _classify_source(_write(tmp_path, _REGISTRY_UPPER_BOUND_RECEIPT)) == (
            _REGISTRY_CONSTRAINED,
            "<2.4",
        )

    def test_registry_compatible_release_is_constrained(self, tmp_path):
        assert _classify_source(_write(tmp_path, _REGISTRY_COMPATIBLE_RELEASE_RECEIPT)) == (
            _REGISTRY_CONSTRAINED,
            "~=2.3",
        )

    def test_directory(self, tmp_path):
        assert _classify_source(_write(tmp_path, _DIRECTORY_RECEIPT)) == (
            _DIR,
            "/home/me/cli-agent-orchestrator",
        )

    def test_path(self, tmp_path):
        assert _classify_source(_write(tmp_path, _PATH_RECEIPT)) == (
            _PATH,
            "/home/me/dist/cli_agent_orchestrator-2.3.0-py3-none-any.whl",
        )

    def test_editable(self, tmp_path):
        assert _classify_source(_write(tmp_path, _EDITABLE_RECEIPT)) == (
            _EDITABLE,
            "/home/me/cli-agent-orchestrator",
        )

    # --- robustness: malformed / wrong-shape receipts degrade to registry ---

    def test_unparseable_toml_degrades(self, tmp_path):
        assert _classify_source(_write(tmp_path, "not : valid : toml [[[")) == (_REGISTRY, None)

    def test_unreadable_file_degrades(self, tmp_path):
        assert _classify_source(tmp_path / "missing.toml") == (_REGISTRY, None)

    def test_tool_not_a_table_degrades(self, tmp_path):
        # `tool = "corrupt"` — decoded tool is a str; must not raise AttributeError.
        assert _classify_source(_write(tmp_path, _TOOL_NOT_TABLE)) == (_REGISTRY, None)

    def test_requirements_not_a_list_degrades(self, tmp_path):
        assert _classify_source(_write(tmp_path, _REQS_NOT_LIST)) == (_REGISTRY, None)

    def test_non_string_directory_degrades(self, tmp_path):
        # A boolean `directory` must not be treated as a path / reach shlex.quote.
        assert _classify_source(_write(tmp_path, _DIR_NOT_STRING)) == (_REGISTRY, None)

    def test_no_matching_requirement_degrades(self, tmp_path):
        r = _write(
            tmp_path,
            '[tool]\nrequirements = ["stray", { name = "other", directory = "/x" }]\n',
        )
        assert _classify_source(r) == (_REGISTRY, None)


class TestBuildCommand:
    def test_git_reinstalls(self):
        assert _build_command(_GIT, "git+https://example.com/x.git") == [
            "uv",
            "tool",
            "install",
            "git+https://example.com/x.git",
            "--upgrade",
            "--reinstall",
        ]

    def test_registry_upgrades(self):
        assert _build_command(_REGISTRY, None) == ["uv", "tool", "upgrade", _PACKAGE]

    def test_constrained_registry_unpins_via_at_latest(self):
        assert _build_command(_REGISTRY_CONSTRAINED, "<2.4") == [
            "uv",
            "tool",
            "install",
            f"{_PACKAGE}@latest",
            "--upgrade",
        ]


class TestUpdateCommand:
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_REGISTRY, None))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_registry_runs_upgrade(self, _which, _rp, _cls, mock_run):
        mock_run.return_value = _completed(0)
        result = CliRunner().invoke(update, [])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(["uv", "tool", "upgrade", _PACKAGE])
        assert "up to date" in result.output

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_REGISTRY_CONSTRAINED, "<2.4"))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_unpins(self, _which, _rp, _cls, mock_run):
        mock_run.return_value = _completed(0)
        result = CliRunner().invoke(update, [])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "install", f"{_PACKAGE}@latest", "--upgrade"]
        )
        assert "unpinning from <2.4" in result.output

    @patch(f"{_MOD}.subprocess.run")
    @patch(
        f"{_MOD}._classify_source",
        return_value=(_GIT, "git+https://github.com/awslabs/cli-agent-orchestrator.git"),
    )
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_git_forces_reinstall(self, _which, _rp, _cls, mock_run):
        mock_run.return_value = _completed(0)
        result = CliRunner().invoke(update, [])
        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            [
                "uv",
                "tool",
                "install",
                "git+https://github.com/awslabs/cli-agent-orchestrator.git",
                "--upgrade",
                "--reinstall",
            ]
        )

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_DIR, "/home/me/cli-agent-orchestrator"))
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_directory_informs_without_running_uv(self, _which, _rp, _cls, mock_run):
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "local directory" in result.output
        assert "/home/me/cli-agent-orchestrator" in result.output
        # Reinstall shown as the definite step; git pull only as the common case.
        assert "uv tool install /home/me/cli-agent-orchestrator --reinstall" in result.output
        assert "git checkout" in result.output
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_PATH, "/home/me/dist/cao.whl"))
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_path_informs_without_running_uv(self, _which, _rp, _cls, mock_run):
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "local path" in result.output
        assert "/home/me/dist/cao.whl" in result.output
        assert "rebuild" in result.output
        assert "git -C" not in result.output  # no git pull for a wheel
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_EDITABLE, "/home/me/cli-agent-orchestrator"))
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_editable_informs_without_running_uv(self, _which, _rp, _cls, mock_run):
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "local editable" in result.output
        # Guidance must preserve the editable install (not convert to regular).
        assert (
            "uv tool install --editable /home/me/cli-agent-orchestrator --reinstall"
            in result.output
        )
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}.shutil.which", return_value=None)
    def test_missing_uv_is_a_clickexception(self, _which, mock_run):
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "uv is not on PATH" in result.output
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_REGISTRY, None))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_nonzero_exit_surfaces_error(self, _which, _rp, _cls, mock_run):
        mock_run.return_value = _completed(2)
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "exited with code 2" in result.output

    @patch(f"{_MOD}.subprocess.run", side_effect=OSError("cannot exec"))
    @patch(f"{_MOD}._classify_source", return_value=(_REGISTRY, None))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_oserror_is_wrapped(self, _which, _rp, _cls, _run):
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "Failed to run uv" in result.output
