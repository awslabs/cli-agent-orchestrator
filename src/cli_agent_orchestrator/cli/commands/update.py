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

# Source kinds returned by _classify_source. The install method decides how (or
# whether) `cao update` can advance CAO to a newer version.
_GIT = "git"  # git+<url>[?rev=...] — reinstall to fetch newer commits
_REGISTRY = "registry"  # unpinned PyPI — `uv tool upgrade` advances it
_REGISTRY_CONSTRAINED = "registry_constrained"  # any version constraint — @latest to unpin
_DIRECTORY = "directory"  # local source tree — user must update + reinstall
_PATH = "path"  # local wheel/artifact — user must rebuild + reinstall
_EDITABLE = "editable"  # local editable clone — user must reinstall --editable

# Local source kinds have no remote to advance; `cao update` prints guidance.
_LOCAL_KINDS = (_DIRECTORY, _PATH, _EDITABLE)


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


def _cao_requirement(receipt: Path) -> Optional[dict]:
    """Return CAO's requirement mapping from the receipt, or None.

    Defensive against a damaged or future-schema receipt: any parse error or
    unexpected shape (``tool`` not a table, ``requirements`` not a list, a
    requirement that isn't a mapping) yields None so the caller degrades to the
    documented fallback rather than raising on a malformed file.
    """
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, ValueError):
        return None
    # tomllib always returns a dict at the root, so `data` is a mapping here.
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return None
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        return None
    for req in requirements:
        if isinstance(req, dict) and req.get("name") == _PACKAGE:
            return req
    return None


def _str_field(req: dict, key: str) -> Optional[str]:
    """Return req[key] only if it's a non-empty string (else None).

    Guards against non-string source fields (e.g. a boolean ``directory``) that
    would otherwise flow into command construction or ``shlex.quote``.
    """
    value = req.get(key)
    return value if isinstance(value, str) and value else None


def _classify_source(receipt: Optional[Path]) -> Tuple[str, Optional[str]]:
    """Classify how CAO was installed into ``(kind, detail)``.

    - ``(_GIT, "git+<url>[@rev]")`` — a git source string ready to reinstall.
    - ``(_DIRECTORY, path)`` / ``(_PATH, path)`` / ``(_EDITABLE, path)`` — a
      local install location.
    - ``(_REGISTRY_CONSTRAINED, "<spec>")`` — a registry install carrying ANY
      version constraint (``==``, ``<``, ``~=``, …) that can hold it below the
      latest release; needs the ``@latest`` unpin path.
    - ``(_REGISTRY, None)`` — an unconstrained/unknown registry install (the
      default and the fallback for a missing/unparseable/wrong-shape receipt).
    """
    if receipt is None:
        return (_REGISTRY, None)
    req = _cao_requirement(receipt)
    if req is None:
        return (_REGISTRY, None)

    git_url = _str_field(req, "git")
    if git_url:
        # A ref may be recorded separately or already carried as a ``...@rev``
        # (or ``?rev=``) suffix in the URL. Preserve an explicit separate rev,
        # otherwise let uv resolve whatever the URL encodes / the default branch.
        rev = _str_field(req, "rev") or _str_field(req, "tag") or _str_field(req, "branch")
        if rev and "@" not in git_url.rsplit("/", 1)[-1] and "?" not in git_url:
            return (_GIT, f"git+{git_url}@{rev}")
        return (_GIT, f"git+{git_url}")

    directory = _str_field(req, "directory")
    if directory:
        return (_DIRECTORY, directory)
    path = _str_field(req, "path")
    if path:
        return (_PATH, path)
    editable = _str_field(req, "editable")
    if editable:
        return (_EDITABLE, editable)

    # ANY specifier (not just ``==``) can hold the install below the latest
    # release — ``<2.4``, ``~=2.3``, combined constraints — and `uv tool upgrade`
    # honours it and reports "Nothing to upgrade". Route every constraint through
    # the ``@latest`` unpin path so `cao update` reaches the latest release.
    specifier = _str_field(req, "specifier")
    if specifier:
        return (_REGISTRY_CONSTRAINED, specifier)
    return (_REGISTRY, None)


def _build_command(kind: str, detail: Optional[str]) -> List[str]:
    """The uv invocation that advances CAO for a remote-backed install kind.

    - git: ``uv tool install <git-source> --upgrade --reinstall``. ``@main`` is
      a MOVING ref that ``uv tool upgrade`` treats as already satisfied, so
      ``--reinstall`` is required to fetch the latest commit.
    - registry (unconstrained): ``uv tool upgrade`` re-resolves to the latest.
    - registry (constrained): ``uv tool upgrade`` honours the constraint and can
      report "Nothing to upgrade"; ``uv tool install <pkg>@latest --upgrade``
      unpins to the latest published release.

    Local (directory/path/editable) kinds have no remote to advance and are
    handled by the command before reaching here.
    """
    if kind == _GIT and detail:
        return ["uv", "tool", "install", detail, "--upgrade", "--reinstall"]
    if kind == _REGISTRY_CONSTRAINED:
        return ["uv", "tool", "install", f"{_PACKAGE}@latest", "--upgrade"]
    return ["uv", "tool", "upgrade", _PACKAGE]


def _local_fix_hint(kind: str, location: str) -> str:
    """User-facing remediation for a local (directory/path/editable) install."""
    quoted = shlex.quote(location)
    if kind == _EDITABLE:
        # Preserve the editable install rather than converting it to a regular one.
        return (
            f"update the local source, then reinstall: "
            f"uv tool install --editable {quoted} --reinstall "
            f"(for a git checkout, first run: git -C {quoted} pull)"
        )
    if kind == _DIRECTORY:
        # A directory source is not necessarily a git checkout, so show the
        # reinstall as the definite step and `git pull` only as the common case.
        return (
            f"update the local source, then reinstall: uv tool install {quoted} --reinstall "
            f"(for a git checkout, first run: git -C {quoted} pull)"
        )
    return f"rebuild the artifact, then reinstall: uv tool install {quoted} --reinstall"


@click.command()
def update():
    """Update CAO to the latest version.

    Detects how CAO was installed (from uv's install receipt) and runs the
    matching uv command: a git install is reinstalled from its git source to
    pick up the latest commit; an unconstrained registry install is upgraded to
    the latest published release; a version-constrained registry install is
    unpinned to the latest. A local directory/path/editable install can't be
    advanced remotely, so the command prints the exact steps instead. Requires
    that CAO was installed as a uv tool.
    """
    if shutil.which("uv") is None:
        raise click.ClickException(
            "uv is not on PATH. `cao update` upgrades the uv tool install; "
            "install uv (https://docs.astral.sh/uv/) or update CAO with the "
            "package manager you installed it with."
        )

    kind, detail = _classify_source(_receipt_path())

    if kind in _LOCAL_KINDS:
        # A local install has no remote to advance from; `uv tool upgrade` would
        # be a silent no-op that then reports success. Tell the user the exact
        # steps instead of pretending to update.
        assert detail is not None  # local kinds always carry a location
        raise click.ClickException(
            f"CAO was installed from a local {kind} ({detail}). "
            f"`cao update` can't advance a local install; {_local_fix_hint(kind, detail)}"
        )

    command = _build_command(kind, detail)

    if kind == _GIT:
        source_desc = f"git ({detail})"
    elif kind == _REGISTRY_CONSTRAINED:
        source_desc = f"the registry (unpinning from {detail})"
    else:
        source_desc = "the registry"
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
