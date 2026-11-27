"""Tests for the ``cao update`` command (issue #26)."""

from unittest.mock import MagicMock, patch

import pytest
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
            "upgrade",
            _PACKAGE,
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
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_git_reuses_receipt_with_pin_and_extra(self, _which, receipt_path, mock_run, tmp_path):
        receipt_path.return_value = _write(
            tmp_path,
            """
[tool]
requirements = [
    { name = "cli-agent-orchestrator", git = "https://example.com/cao.git?rev=deadbeef" },
    { name = "cao-decider-example", specifier = "==1.2.3" },
]
""",
        )
        mock_run.return_value = _completed(0)

        result = CliRunner().invoke(update, [])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(["uv", "tool", "upgrade", _PACKAGE, "--reinstall"])

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_keeps_extra_requirements(
        self, _which, receipt_path, mock_run, tmp_path
    ):
        # `uv tool install <pkg>@latest --upgrade` replaces the receipt's requirements,
        # so each extra package has to be passed back with --with.
        wheel = tmp_path / "cao_decider_wheel-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        directory = tmp_path / "decider"
        directory.mkdir()
        receipt_path.return_value = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==2.1.0" }},
    {{ name = "cao-decider-example", extras = ["fast"], specifier = ">=1.2" }},
    {{ name = "cao-decider-wheel", path = "{wheel}" }},
    {{ name = "cao-decider-local", directory = "{directory}" }},
]
""",
        )
        mock_run.return_value = _completed(0)

        result = CliRunner().invoke(update, [])

        assert result.exit_code == 0
        # The name goes back with the location, so uv refuses a wheel or directory
        # that now holds a different project.
        mock_run.assert_called_once_with(
            [
                "uv",
                "tool",
                "install",
                f"{_PACKAGE}@latest",
                "--upgrade",
                "--with",
                "cao-decider-example[fast]>=1.2",
                "--with",
                f"cao-decider-wheel @ {wheel}",
                "--with",
                f"cao-decider-local @ {directory}",
            ]
        )

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_refuses_extra_it_cannot_rebuild(
        self, _which, receipt_path, mock_run, tmp_path
    ):
        receipt_path.return_value = _write(
            tmp_path,
            """
[tool]
requirements = [
    { name = "cli-agent-orchestrator", specifier = "<2.4" },
    { name = "cao-decider-git", git = "https://example.com/decider.git?rev=v1" },
]
""",
        )

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "cao-decider-git" in result.output
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_refusal_names_the_receipt_and_prints_a_complete_command(
        self, _which, receipt_path, mock_run, tmp_path
    ):
        # Running the printed command as written must not drop the packages and
        # extras that could be rebuilt, and the git entry is in the form uv honours:
        # the receipt's "<url>?rev=<ref>" pasted back would lose the pin.
        receipt = _write(
            tmp_path,
            """
[tool]
requirements = [
    { name = "cli-agent-orchestrator", extras = ["agui"], specifier = "==2.1.0" },
    { name = "fastdep" },
    { name = "cao-decider-jev", git = "https://example.com/decider.git?rev=v1" },
]
""",
        )
        receipt_path.return_value = receipt

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert str(receipt) in result.output
        assert (
            "uv tool install 'cli-agent-orchestrator[agui]@latest' --upgrade "
            "--with fastdep --with 'cao-decider-jev @ git+https://example.com/decider.git@v1'"
        ) in result.output
        assert "?rev=" not in result.output.split("Reinstall it", 1)[1]
        assert "replacing each <...>" not in result.output
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "entry,printed",
        [
            (
                '{ name = "d", editable = "/home/me/d" }',
                "--with-editable /home/me/d",
            ),
            (
                '{ name = "d", git = "https://example.com/d.git" }',
                "--with 'd @ git+https://example.com/d.git'",
            ),
            (
                '{ name = "d", git = "https://example.com/d.git?rev=release/1.2" }',
                "--with 'd @ git+https://example.com/d.git@release/1.2'",
            ),
            (
                '{ name = "d", git = "https://example.com/d.git?rev=feature%2Fx" }',
                "--with 'd @ git+https://example.com/d.git@feature%2Fx'",
            ),
            (
                '{ name = "d", git = "https://example.com/d.git?rev=release%2f1.2" }',
                "--with 'd @ git+https://example.com/d.git@release%2f1.2'",
            ),
        ],
        ids=["editable", "git", "git-rev", "git-branch-with-slash", "git-branch-lower-escape"],
    )
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_refusal_prints_the_form_uv_needs_for_editable_and_git(
        self, _which, receipt_path, mock_run, tmp_path, entry, printed
    ):
        # `--with <path>` for an editable entry makes a static copy, and a recorded
        # "<url>?rev=<ref>" passed back makes uv ignore the ref. The refusal still
        # stops before uv runs; only the command it prints is different. uv records
        # the "/" of a branch as "%2F" and takes it back in that form.
        checkout = tmp_path / "d"
        checkout.mkdir()
        entry = entry.replace("/home/me/d", str(checkout))
        printed = printed.replace("/home/me/d", str(checkout))
        receipt_path.return_value = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {entry},
]
""",
        )

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "the extra package 'd' is from a source it can't rebuild" in result.output
        assert f"cli-agent-orchestrator@latest --upgrade {printed}\n" in result.output
        assert "<d source>" not in result.output
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "entry",
        [
            '{ name = "d", git = "https://example.com/d.git?rev=v1&subdirectory=pkg" }',
            '{ name = "d", git = "https://example.com/d.git?subdirectory=pkg" }',
            '{ name = "d", git = "https://example.com/d.git?rev=" }',
            '{ name = "d", git = "https://example.com/d.git?rev=v1 --index-url=x" }',
            '{ name = "d", git = "https://example.com/d.git?rev=v1%20--index-url%3Dx" }',
            '{ name = "d", git = "https://example.com/d.git?rev=%2Fetc%2Fpasswd" }',
            '{ name = "d", git = "https://example.com/d.git?rev=v1%2" }',
            '{ name = "d", git = "https://example.com/d.git", subdirectory = "pkg" }',
            '{ name = "d", editable = "/home/me/d", marker = "sys_platform == \'linux\'" }',
            '{ name = "d", url = "https://example.com/d-1.0-py3-none-any.whl" }',
        ],
    )
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_refusal_leaves_a_placeholder_and_points_at_the_runbook_when_unsure(
        self, _which, receipt_path, mock_run, tmp_path, entry
    ):
        checkout = tmp_path / "d"
        checkout.mkdir()
        entry = entry.replace("/home/me/d", str(checkout))
        receipt_path.return_value = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {entry},
]
""",
        )

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "--with '<d source>'" in result.output
        assert "replacing each <...>" in result.output
        # A URL: an install from PyPI has no docs/ directory to look in.
        assert (
            "https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/"
            "ephemeral-agents-agent-setup.md#3-install-the-decider-into-the-servers-python"
        ) in result.output
        assert "what that file records" not in result.output
        mock_run.assert_not_called()

    @pytest.mark.parametrize("key", ["path", "directory", "editable"])
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_refuses_a_carried_location_that_is_gone(
        self, _which, receipt_path, mock_run, tmp_path, key
    ):
        # uv would fail on the missing path, and the hint CAO prints after a uv
        # failure says to use another package manager. Name the entry instead. For
        # an editable entry this also stops a ready-made `--with-editable <path>`
        # from being printed for a checkout that has moved.
        gone = tmp_path / "moved-away"
        receipt = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {{ name = "cao-decider-demo", {key} = "{gone}" }},
]
""",
        )
        receipt_path.return_value = receipt

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert (
            f"the extra package 'cao-decider-demo' is recorded at {gone}, which is gone"
            in result.output
        )
        assert str(receipt) in result.output
        assert "--with '<cao-decider-demo source>'" in result.output
        assert "--with-editable" not in result.output
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_keeps_cao_extras(self, _which, receipt_path, mock_run, tmp_path):
        receipt_path.return_value = _write(
            tmp_path,
            """
[tool]
requirements = [
    { name = "cli-agent-orchestrator", extras = ["agui", "otel"], specifier = "==1.0.0" },
    { name = "cao-decider-demo", extras = ["fast"], specifier = ">=1.0,<1.2" },
]
""",
        )
        mock_run.return_value = _completed(0)

        result = CliRunner().invoke(update, [])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            [
                "uv",
                "tool",
                "install",
                f"{_PACKAGE}[agui,otel]@latest",
                "--upgrade",
                "--with",
                "cao-decider-demo[fast]>=1.0,<1.2",
            ]
        )

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_refuses_a_cao_requirement_it_cannot_carry(
        self, _which, receipt_path, mock_run, tmp_path
    ):
        receipt_path.return_value = _write(
            tmp_path,
            """
[tool]
requirements = [
    { name = "cli-agent-orchestrator", specifier = "==1.0.0", marker = "python_version >= '3.10'" },
]
""",
        )

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "CAO's own requirement" in result.output
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "option",
        [
            # The form uv writes for --index, --default-index and UV_DEFAULT_INDEX.
            'index = [{ url = "https://packages.example.com/simple", explicit = false, '
            'default = true, format = "simple", authenticate = "auto" }]',
            'index-url = "https://packages.example.com/simple"',
            'extra-index-url = ["https://packages.example.com/simple"]',
            'find-links = ["file:///opt/wheels"]',
            "no-index = true",
        ],
        ids=lambda option: option.split(" ", 1)[0],
    )
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_refuses_each_index_setting_with_a_registry_extra(
        self, _which, receipt_path, mock_run, tmp_path, option
    ):
        # `uv tool install` does not reuse the receipt's [tool.options], so the
        # decider would be looked up on the default index instead.
        receipt = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {{ name = "cao-decider-demo", specifier = ">=1.0" }},
]

[tool.options]
{option}
""",
        )
        receipt_path.return_value = receipt

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert f"the install sets {option.split(' ', 1)[0]}," in result.output
        assert str(receipt) in result.output
        assert "--with 'cao-decider-demo>=1.0'" in result.output
        assert "'<the [tool.options] settings as uv flags>'" in result.output
        mock_run.assert_not_called()

    @pytest.mark.parametrize("key", ["path", "directory"])
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_index_settings_block_a_local_extra_too(
        self, _which, receipt_path, mock_run, tmp_path, key
    ):
        # A local wheel or directory is not looked up on an index, but uv resolves
        # its dependencies on the new command's indexes, and those may be what the
        # recorded index was there for.
        location = tmp_path / "cao_decider-0.1.0-py3-none-any.whl"
        if key == "path":
            location.write_bytes(b"")
        else:
            location.mkdir()
        receipt = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {{ name = "cao-decider", {key} = "{location}" }},
]

[tool.options]
no-index = true
find-links = ["file:///opt/wheels"]
""",
        )
        receipt_path.return_value = receipt

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "the install sets find-links, no-index," in result.output
        assert str(receipt) in result.output
        assert f"--with 'cao-decider @ {location}'" in result.output
        assert "'<the [tool.options] settings as uv flags>'" in result.output
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_index_settings_with_nothing_carried_unpin_as_before(
        self, _which, receipt_path, mock_run, tmp_path
    ):
        # CAO and its extras have public names, so with nothing else to carry the
        # recorded index changes nothing.
        receipt_path.return_value = _write(
            tmp_path,
            """
[tool]
requirements = [{ name = "cli-agent-orchestrator", extras = ["agui"], specifier = "==1.0.0" }]

[tool.options]
index-url = "https://packages.example.com/simple"
""",
        )
        mock_run.return_value = _completed(0)

        result = CliRunner().invoke(update, [])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "install", f"{_PACKAGE}[agui]@latest", "--upgrade"]
        )

    @pytest.mark.parametrize(
        "entry,shown",
        [
            ('"cao-decider-demo"', "an unnamed requirement"),
            ('{ specifier = ">=1.0" }', "an unnamed requirement"),
            ('{ name = "d", path = "/w/d.whl", marker = "sys_platform == \'linux\'" }', "'d'"),
            ('{ name = "d", extras = "fast" }', "'d'"),
            ('{ name = "d", extras = [""] }', "'d'"),
            ('{ name = "d", specifier = 1 }', "'d'"),
            ('{ name = "d", editable = "/home/me/d" }', "'d'"),
            ('{ name = "d", marker = "python_version >= \'3.10\'" }', "'d'"),
        ],
    )
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_constrained_registry_refuses_each_shape_it_cannot_rebuild(
        self, _which, receipt_path, mock_run, tmp_path, entry, shown
    ):
        checkout = tmp_path / "d"
        checkout.mkdir()
        entry = entry.replace("/home/me/d", str(checkout))
        receipt_path.return_value = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {entry},
]
""",
        )

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert f"the extra package {shown} is from a source it can't rebuild" in result.output
        assert "None" not in result.output
        mock_run.assert_not_called()

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

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._classify_source", return_value=(_REGISTRY, None))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_nonzero_exit_hint_says_nothing_about_a_changed_source(
        self, _which, _rp, _cls, mock_run
    ):
        mock_run.return_value = _completed(2)
        result = CliRunner().invoke(update, [])
        assert "update it with the package manager you used instead." in result.output
        assert "source changed" not in result.output

    @pytest.mark.parametrize("key", ["path", "directory"])
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_nonzero_exit_after_carrying_a_named_path_points_at_the_source(
        self, _which, receipt_path, mock_run, tmp_path, key
    ):
        # uv checks `<name> @ <path>` against the project at that path and refuses
        # a path that now holds another project. The "other package manager" hint
        # alone would point away from that.
        location = tmp_path / "decider"
        location.mkdir()
        receipt_path.return_value = _write(
            tmp_path,
            f"""
[tool]
requirements = [
    {{ name = "cli-agent-orchestrator", specifier = "==1.0.0" }},
    {{ name = "cao-decider-demo", {key} = "{location}" }},
]
""",
        )
        mock_run.return_value = _completed(1)

        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert mock_run.call_args.args[0][-2:] == ["--with", f"cao-decider-demo @ {location}"]
        assert (
            "uv exited with code 1. If CAO was not installed via `uv tool install`, update "
            "it with the package manager you used instead, or a recorded package's source "
            "changed (see uv's message above)."
        ) in result.output

    @patch(f"{_MOD}.subprocess.run", side_effect=OSError("cannot exec"))
    @patch(f"{_MOD}._classify_source", return_value=(_REGISTRY, None))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_oserror_is_wrapped(self, _which, _rp, _cls, _run):
        result = CliRunner().invoke(update, [])
        assert result.exit_code != 0
        assert "Failed to run uv" in result.output
