"""Update command for CLI Agent Orchestrator (issue #26)."""

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import click

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomli is a declared dependency there
    import tomli as tomllib  # type: ignore[no-redef]

# The uv tool package name CAO is installed under.
_PACKAGE = "cli-agent-orchestrator"

# Source kinds returned by _classify_source. The install method decides how (or
# whether) `cao update` can advance CAO to a newer version.
_GIT = "git"  # uv upgrades from the recorded git source
_REGISTRY = "registry"  # unpinned PyPI — `uv tool upgrade` advances it
_REGISTRY_CONSTRAINED = "registry_constrained"  # any version constraint — @latest to unpin
_DIRECTORY = "directory"  # local source tree — user must update + reinstall
_PATH = "path"  # local wheel/artifact — user must rebuild + reinstall
_EDITABLE = "editable"  # local editable clone — user must reinstall --editable

# Local source kinds have no remote to advance; `cao update` prints guidance.
_LOCAL_KINDS = (_DIRECTORY, _PATH, _EDITABLE)

# Receipt ``[tool.options]`` keys that say where packages are resolved from.
_INDEX_OPTIONS = ("index", "index-url", "extra-index-url", "find-links", "no-index")

# The runbook table that turns each kind of receipt entry into a uv argument.
# A URL, not a repository path: an install from PyPI has no ``docs/`` directory.
_RUNBOOK = (
    "https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/"
    "ephemeral-agents-agent-setup.md#3-install-the-decider-into-the-servers-python"
)

# A git ref that is safe to put after ``@`` in ``git+<url>@<ref>``. uv records
# the ``/`` of a branch such as ``feature/x`` as ``%2F`` and accepts it back in
# that form, so that one escape is allowed and the ref is printed as recorded.
_GIT_REF = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]|%2[Ff])*")


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
    for req in _requirements(receipt) or []:
        if isinstance(req, dict) and req.get("name") == _PACKAGE:
            return req
    return None


def _tool_table(receipt: Path) -> Optional[dict]:
    """Return the receipt's ``[tool]`` table, or None if it is missing or malformed."""
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, ValueError):
        return None
    # tomllib always returns a dict at the root, so `data` is a mapping here.
    tool = data.get("tool")
    return tool if isinstance(tool, dict) else None


def _requirements(receipt: Path) -> Optional[list]:
    """Return the receipt's requirement list, or None if it is missing or malformed."""
    tool = _tool_table(receipt)
    requirements = tool.get("requirements") if tool else None
    return requirements if isinstance(requirements, list) else None


def _index_options(receipt: Path) -> List[str]:
    """The ``_INDEX_OPTIONS`` keys the receipt records under ``[tool.options]``."""
    tool = _tool_table(receipt)
    options = tool.get("options") if tool else None
    if not isinstance(options, dict):
        return []
    return [key for key in _INDEX_OPTIONS if key in options]


def _str_field(req: dict, key: str) -> Optional[str]:
    """Return req[key] only if it's a non-empty string (else None).

    Guards against non-string source fields (e.g. a boolean ``directory``) that
    would otherwise flow into command construction or ``shlex.quote``.
    """
    value = req.get(key)
    return value if isinstance(value, str) and value else None


def _classify_source(receipt: Optional[Path]) -> Tuple[str, Optional[str]]:
    """Classify how CAO was installed into ``(kind, detail)``.

    - ``(_GIT, "git+<url>[@rev]")`` — a git source string for status output.
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


def _extras(req: dict) -> Optional[List[str]]:
    """``req["extras"]`` if it is a list of non-empty strings (``[]`` when absent), else None."""
    extras = req.get("extras", [])
    if not isinstance(extras, list) or not all(isinstance(e, str) and e for e in extras):
        return None
    return extras


def _with_requirement(req: object) -> Optional[str]:
    """The ``--with`` value that reinstalls one extra receipt requirement, or None.

    Covers the shapes uv records for a ``<name>[extras]<specifier>`` requirement, a
    wheel ``path`` and a ``directory`` (both as ``<name> @ <location>``). Any other
    shape, such as a git or URL source, an editable install or a marker, returns None.
    """
    if not isinstance(req, dict):
        return None
    name = _str_field(req, "name")
    if name is None:
        return None
    if "path" in req or "directory" in req:
        if set(req) not in ({"name", "path"}, {"name", "directory"}):
            return None
        location = _str_field(req, "path") or _str_field(req, "directory")
        # With the name, uv refuses a wheel or directory that now holds a
        # different project. A bare path would install whatever is there.
        return f"{name} @ {location}" if location else None
    if set(req) - {"name", "extras", "specifier"}:
        return None
    extras = _extras(req)
    specifier = req.get("specifier", "")
    if extras is None or not isinstance(specifier, str):
        return None
    return name + (f"[{','.join(extras)}]" if extras else "") + specifier


def _missing_location(req: object) -> Optional[str]:
    """The ``path``, ``directory`` or ``editable`` location of an entry, if it no longer exists."""
    if not isinstance(req, dict):
        return None
    location = (
        _str_field(req, "path") or _str_field(req, "directory") or _str_field(req, "editable")
    )
    if location is None or Path(location).exists():
        return None
    return location


def _hand_finish_args(req: object) -> Optional[List[str]]:
    """The uv arguments a user needs for an entry ``cao update`` won't carry, or None.

    Only the two shapes whose receipt form is wrong to paste back: an editable
    entry needs ``--with-editable`` (``--with <path>`` makes a static copy), and
    a pinned git entry is recorded as ``<url>?rev=<ref>``, which uv ignores, and
    has to be written ``git+<url>@<ref>``. Anything else returns None and is
    left as a placeholder.
    """
    if not isinstance(req, dict):
        return None
    name = _str_field(req, "name")
    if name is None:
        return None
    if set(req) == {"name", "editable"}:
        editable = _str_field(req, "editable")
        return ["--with-editable", editable] if editable else None
    if set(req) == {"name", "git"}:
        git = _str_field(req, "git")
        if git is None:
            return None
        url, query_mark, query = git.partition("?")
        if not query_mark:
            return ["--with", f"{name} @ git+{url}"]
        ref = query[len("rev=") :] if query.startswith("rev=") else ""
        if _GIT_REF.fullmatch(ref):
            return ["--with", f"{name} @ git+{url}@{ref}"]
    return None


def _unpin_args(receipt: Optional[Path]) -> List[str]:
    """The arguments after ``uv tool install`` that unpin CAO and keep the rest of the receipt.

    ``uv tool install <pkg>@latest --upgrade`` writes a new receipt from its command
    line and does not read the old one, unlike ``uv tool upgrade``. So everything the
    receipt records has to be passed again or it is lost: CAO's own extras, every
    other requirement (as ``--with``) and the index settings under ``[tool.options]``.
    If any of that can't be rebuilt, raise before uv runs, naming the receipt and
    printing a command the user can finish by hand.
    """
    cao = f"{_PACKAGE}@latest"
    with_args: List[str] = []
    problems: List[str] = []
    for req in (_requirements(receipt) if receipt else None) or []:
        if isinstance(req, dict) and req.get("name") == _PACKAGE:
            extras = _extras(req)
            if extras is None or set(req) - {"name", "extras", "specifier"}:
                problems.append("CAO's own requirement records more than extras and a version")
                cao = f"{_PACKAGE}[<extras>]@latest"
            elif extras:
                cao = f"{_PACKAGE}[{','.join(extras)}]@latest"
            continue
        value = _with_requirement(req)
        name = _str_field(req, "name") if isinstance(req, dict) else None
        missing = _missing_location(req)
        if value is None:
            label = repr(name) if name else "an unnamed requirement"
            placeholder = ["--with", f"<{name or 'requirement'} source>"]
            hand_finish = _hand_finish_args(req)
            if hand_finish and hand_finish[0] == "--with-editable" and missing is not None:
                # An editable checkout that was moved: the ready-made
                # ``--with-editable <path>`` would only fail in uv.
                problems.append(
                    f"the extra package {label} is recorded at {missing}, which is gone"
                )
                with_args += placeholder
                continue
            problems.append(f"the extra package {label} is from a source it can't rebuild")
            with_args += hand_finish or placeholder
            continue
        if missing is not None:
            # uv would fail on the path, and the hint after a uv failure points
            # at the wrong cause. Stop here and name the entry instead.
            problems.append(f"the extra package {name!r} is recorded at {missing}, which is gone")
            with_args += ["--with", f"<{name} source>"]
            continue
        with_args += ["--with", value]

    index_keys = _index_options(receipt) if receipt else []
    if index_keys and with_args:
        # Without them uv resolves the extra packages and their dependencies on the
        # default index, which can install a different package that has the same name.
        # A local wheel or directory needs no index itself, but what it requires does.
        problems.append(
            f"the install sets {', '.join(index_keys)}, which uv would not reuse for the "
            "extra packages"
        )
        with_args.append("<the [tool.options] settings as uv flags>")

    args = [cao, "--upgrade", *with_args]
    if problems:
        if any(arg.startswith("<") for arg in args) or "[<extras>]" in cao:
            # Pasting a recorded git ``<url>?rev=<ref>`` back makes uv ignore the
            # ref, so the user is sent to the table and not told to copy the file.
            how = (
                f"yourself, replacing each <...>. {_RUNBOOK} gives the form for each "
                "kind of entry in that file"
            )
        else:
            how = "yourself"
        raise click.ClickException(
            "`cao update` can't unpin CAO without losing part of the install: "
            f"{'; '.join(problems)}. The install is recorded in {receipt}. Reinstall it "
            f"{how}:\n  {shlex.join(['uv', 'tool', 'install', *args])}"
        )
    return args


def _build_command(
    kind: str, detail: Optional[str], unpin_args: Optional[Sequence[str]] = None
) -> List[str]:
    """The uv invocation that advances CAO for a remote-backed install kind.

    - git: ``uv tool upgrade <pkg> --reinstall`` lets uv reuse the receipt,
      including the git revision and extra requirements. Reinstalling
      also refreshes moving refs such as ``@main``.
    - registry (unconstrained): ``uv tool upgrade`` re-resolves to the latest.
    - registry (constrained): ``uv tool upgrade`` honours the constraint and can
      report "Nothing to upgrade"; ``uv tool install <pkg>@latest --upgrade``
      unpins to the latest published release. That replaces the receipt, so
      ``unpin_args`` (from ``_unpin_args``) carries what the receipt recorded.

    Local (directory/path/editable) kinds have no remote to advance and are
    handled by the command before reaching here.
    """
    if kind == _GIT and detail:
        return ["uv", "tool", "upgrade", _PACKAGE, "--reinstall"]
    if kind == _REGISTRY_CONSTRAINED:
        if unpin_args is None:
            unpin_args = [f"{_PACKAGE}@latest", "--upgrade"]
        return ["uv", "tool", "install", *unpin_args]
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
    matching uv command: a git install is reinstalled using its recorded ref
    and dependencies; an unconstrained registry install is upgraded to
    the latest published release; a version-constrained registry install is
    unpinned to the latest. A local directory/path/editable install can't be
    advanced remotely, so the command prints the exact steps instead. Requires
    that CAO was installed as a uv tool.

    Acts immediately; there is no confirmation prompt.
    """
    if shutil.which("uv") is None:
        raise click.ClickException(
            "uv is not on PATH. `cao update` upgrades the uv tool install; "
            "install uv (https://docs.astral.sh/uv/) or update CAO with the "
            "package manager you installed it with."
        )

    receipt = _receipt_path()
    kind, detail = _classify_source(receipt)

    if kind in _LOCAL_KINDS:
        # A local install has no remote to advance from; `uv tool upgrade` would
        # be a silent no-op that then reports success. Tell the user the exact
        # steps instead of pretending to update.
        assert detail is not None  # local kinds always carry a location
        raise click.ClickException(
            f"CAO was installed from a local {kind} ({detail}). "
            f"`cao update` can't advance a local install; {_local_fix_hint(kind, detail)}"
        )

    unpin_args = _unpin_args(receipt) if kind == _REGISTRY_CONSTRAINED else None
    command = _build_command(kind, detail, unpin_args)

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
        hint = (
            "If CAO was not installed via `uv tool install`, update it with the "
            "package manager you used instead"
        )
        if any(" @ " in arg for arg in unpin_args or []):
            # uv checks a carried wheel or directory against its recorded name,
            # so a path that now holds another project fails here.
            hint += ", or a recorded package's source changed (see uv's message above)"
        raise click.ClickException(f"uv exited with code {result.returncode}. {hint}.")

    click.echo("✓ CAO is up to date. Restart any running cao-server to pick up the new version.")
