"""Update command for CLI Agent Orchestrator (issue #26)."""

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import click

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomli is a declared dependency there
    import tomli as tomllib  # type: ignore[no-redef]

# The uv tool package name CAO is installed under.
_PACKAGE = "cli-agent-orchestrator"


def _receipt_path() -> Optional[Path]:
    """Path to uv's install receipt for CAO, or None if it can't be located.

    ``uv tool dir`` prints the tools root; each tool has a
    ``<name>/uv-receipt.toml`` recording how it was installed.
    """
    if shutil.which("uv") is None:
        return None
    try:
        out = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    tools_dir = out.stdout.strip()
    if not tools_dir:
        return None
    receipt = Path(tools_dir) / _PACKAGE / "uv-receipt.toml"
    return receipt if receipt.is_file() else None


def _git_source_from_receipt(receipt: Path) -> Optional[str]:
    """Return a ``git+<url>[@rev]`` source string if CAO was installed from git.

    Returns None for registry/PyPI installs (or any receipt shape we don't
    recognise as git), so the caller falls back to ``uv tool upgrade``.
    """
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, ValueError):
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if not isinstance(req, dict) or req.get("name") != _PACKAGE:
            continue
        git_url = req.get("git")
        if not git_url:
            return None  # registry / directory / path install — not git
        # A git ref (commit/tag/branch) may be recorded separately; ``git``
        # can also already carry a ``...@rev`` suffix. Preserve an explicit
        # rev when present, otherwise let uv resolve the default branch.
        rev = req.get("rev") or req.get("tag") or req.get("branch")
        if rev and "@" not in git_url.rsplit("/", 1)[-1]:
            return f"git+{git_url}@{rev}"
        return f"git+{git_url}"
    return None


def _local_source_from_receipt(receipt: Path) -> Optional[Tuple[str, str]]:
    """Return ``(kind, location)`` if CAO was installed from a local source.

    ``uv tool install .`` / ``/path/to/clone`` records a ``directory`` source;
    ``uv tool install ./dist/x.whl`` records a ``path`` source. ``uv tool
    upgrade`` cannot pull new commits/builds for either — it would be a silent
    no-op that then reports success — so the command reports the local source
    precisely instead. Returns None for registry/git/unknown shapes.
    """
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, ValueError):
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if not isinstance(req, dict) or req.get("name") != _PACKAGE:
            continue
        if req.get("directory"):
            return ("directory", req["directory"])
        if req.get("path"):
            return ("path", req["path"])
        return None
    return None


def _build_command(git_source: Optional[str]) -> List[str]:
    """Choose the right uv invocation for how CAO was installed.

    - git install: ``uv tool install <git-source> --upgrade --reinstall``. A
      ``git+...@main`` requirement pins a MOVING ref, and ``uv tool upgrade``
      can treat it as already satisfied and skip fetching newer commits;
      ``--reinstall`` forces the latest commit (matches the README's documented
      ``uv tool install git+... --upgrade`` upgrade path).
    - registry/PyPI (or unknown): ``uv tool upgrade cli-agent-orchestrator``,
      which re-resolves to the latest published version.
    """
    if git_source:
        return ["uv", "tool", "install", git_source, "--upgrade", "--reinstall"]
    return ["uv", "tool", "upgrade", _PACKAGE]


@click.command()
def update():
    """Update CAO to the latest version.

    Detects how CAO was installed (from uv's install receipt) and runs the
    matching uv command: a git install is reinstalled from its git source to
    pick up the latest commit, while a PyPI install is upgraded to the latest
    published release. Requires that CAO was installed as a uv tool.
    """
    if shutil.which("uv") is None:
        raise click.ClickException(
            "uv is not on PATH. `cao update` upgrades the uv tool install; "
            "install uv (https://docs.astral.sh/uv/) or update CAO with the "
            "package manager you installed it with."
        )

    receipt = _receipt_path()
    if receipt is not None:
        local = _local_source_from_receipt(receipt)
        if local is not None:
            # A local directory/path install has no remote to upgrade from;
            # running `uv tool upgrade` would be a silent no-op that then reports
            # success. Tell the user the exact steps instead of pretending to
            # update.
            kind, location = local
            if kind == "directory":
                fix = (
                    f"update the checkout and reinstall: "
                    f"git -C {shlex.quote(location)} pull && "
                    f"uv tool install {shlex.quote(location)} --reinstall"
                )
            else:  # path (e.g. a built wheel)
                fix = (
                    f"rebuild the artifact and reinstall: "
                    f"uv tool install {shlex.quote(location)} --reinstall"
                )
            raise click.ClickException(
                f"CAO was installed from a local {kind} ({location}). "
                f"`cao update` can't pull new versions for a local install; {fix}"
            )

    git_source = _git_source_from_receipt(receipt) if receipt is not None else None
    command = _build_command(git_source)

    source_desc = f"git ({git_source})" if git_source else "the registry"
    click.echo(f"Updating {_PACKAGE} from {source_desc}...")
    # shlex.join so the echoed line is copy-paste-safe (e.g. the `?` in a
    # git ?rev= source would otherwise be a shell glob).
    click.echo(f"$ {shlex.join(command)}")
    try:
        result = subprocess.run(command)
    except OSError as e:
        raise click.ClickException(f"Failed to run uv: {e}")

    if result.returncode != 0:
        # uv already printed the underlying reason (e.g. "is not installed" when
        # CAO was installed some other way). Surface a non-zero exit without
        # duplicating uv's message.
        raise click.ClickException(
            f"uv exited with code {result.returncode}. If CAO was not installed "
            "via `uv tool install`, update it with the package manager you used "
            "instead."
        )

    click.echo("✓ CAO is up to date. Restart any running cao-server to pick up the new version.")
