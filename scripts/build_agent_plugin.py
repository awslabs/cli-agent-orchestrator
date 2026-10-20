#!/usr/bin/env python3
"""Build CAO's own Agent Plugins packages, and guard them against drift.

CAO ships **two** packages, split by audience rather than cosmetically:

===================  ==========================================================
``cao``              **Operator** — someone driving CAO from a foreign
                     Agent-Plugins-compatible client. "Install profiles, launch
                     sessions, message a running fleet."
``cao-contributor``  **Contributor** — someone extending CAO itself. Authoring a
                     provider, authoring an event plugin.
===================  ==========================================================

Shipping repo-development skills inside the operator package would enlarge the
prompt surface of every foreign agent that installs it with instructions it will
never act on, so each package's skills all serve one story.

Both packages are **generated and committed**, following the canonical-source +
generated-mirror + ``--check`` pattern ``scripts/sync_skills.py`` already uses.
Committing them is what makes ``cao plugin add ./agent-plugin/cao`` work from a
clone with no build step, and what lets a foreign client point at the
subdirectory of this repository directly — which is how the ``git`` resolver's
``--subdir`` support gets exercised by CAO's own packages rather than only
tested.

Usage::

    python scripts/build_agent_plugin.py            # regenerate both packages
    python scripts/build_agent_plugin.py --check     # CI guard: exit 1 on drift

Naming caveats, both deliberate and both recorded rather than silently resolved:

* The contributor package's name (``cao-contributor``) and the packaged name of
  the event-plugin authoring skill depend on **maintainer decision M4**. Both are
  provisional. Because each package's allowlist is *data*, a rename is a
  one-line edit here plus a rebuild — no restructuring.
* ``cao-contributing`` is **conditional and not present**. It depends on PR #448,
  which is open and still a draft, so this build does not claim it. Adding it
  when #448 lands is likewise one allowlist line.
"""

from __future__ import annotations

import argparse
import ast
import filecmp
import json
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
CANONICAL_SKILLS_DIR = ROOT / "skills"
PACKAGES_DIR = ROOT / "agent-plugin"
PYPROJECT = ROOT / "pyproject.toml"

SPEC_VERSION = "1.0.0"
PLUGIN_SCHEMA_ID = f"https://agent-plugins.org/schemas/{SPEC_VERSION}/plugin.schema.json"
MCP_SCHEMA_ID = f"https://agent-plugins.org/schemas/{SPEC_VERSION}/mcp.schema.json"

# The distribution the packaged MCP server is launched from, and the console
# script that server exposes.
PYPI_DISTRIBUTION = "cli-agent-orchestrator"
OPS_CONSOLE_SCRIPT = "cao-ops-mcp-server"
OPS_SERVER_KEY = "cao-ops"
PYPI_TIMEOUT_S = 30

# Files copied into every package alongside its generated manifest.
SHARED_FILES = ["LICENSE"]


@dataclass(frozen=True)
class PackageConfig:
    """Everything that distinguishes one package from the other.

    A dataclass rather than branching code, so adding, removing, or renaming a
    package's skills is a data edit that ``--check`` then enforces.
    """

    name: str
    description: str
    keywords: List[str]
    skills: List[str]
    """Allowlist of repo-root ``skills/<name>/`` directories to copy in."""

    excluded_note: str = ""
    """Why specific skills are deliberately absent, rendered into CHANGELOG.md."""

    ships_mcp: bool = False
    """Whether this package ships an ``mcp.json`` (Increment 2).

    Operator package only. The contributor package ships none in either
    increment: authoring skills read and write repo files through the host
    agent's own tools and need no CAO runtime, so the ``uv``/``cao-server``
    prerequisites do not apply to it at all.
    """

    extra_manifest: Dict[str, object] = field(default_factory=dict)


OPERATOR = PackageConfig(
    name="cao",
    description=(
        "Drive CLI Agent Orchestrator multi-agent sessions from any "
        "Agent-Plugins-compatible client. Prerequisites: the `uv` toolchain must be "
        "on PATH (the MCP server is launched via `uvx`), and a CAO API server must be "
        "running locally at http://127.0.0.1:9889 (`cao-server`). All communication "
        "is localhost-only."
    ),
    keywords=["orchestration", "multi-agent", "cao", "tmux"],
    ships_mcp=True,
    skills=[
        # The core capability: launch a session, check status, send instructions,
        # unblock, shut down. Without it the package does nothing.
        "cao-session-management",
        # Choosing the right profile before delegating.
        "cao-agent-routing",
        # MAINTAINER-TUNABLE, currently included. A foreign client that launches a
        # supervisor and then sends it work is acting as a supervisor's peer: it
        # needs the assign/handoff/idle-inbox semantics to phrase instructions the
        # fleet will act on. The counter-argument — that CAO already injects these
        # into its own terminals, so shipping them again couples the portable
        # package to an internal contract that can change without a plugin version
        # bump — is recorded in design.md. Reversing this is one line.
        "cao-supervisor-protocols",
        "cao-worker-protocols",
    ],
    excluded_note=(
        "Excluded: `cao-provider` and the event-plugin authoring skill (both moved to "
        "cao-contributor, where they serve the contributor story); `cao-memory`, "
        "`cao-learning`, `cao-workflow` (useful, but they expand the surface before the "
        "delivery path is proven); `skills/vendor/ext-apps/*` (Apache-2.0 vendored content "
        "with its own NOTICE attribution obligations, which redistributing in a second "
        "package would multiply for no benefit); `agui-author`, `cao-mcp-apps`, "
        "`mcp-apps-builder` (adjacent features with their own consumers)."
    ),
)

CONTRIBUTOR = PackageConfig(
    name="cao-contributor",
    description=(
        "Skills for extending CLI Agent Orchestrator: authoring providers and event "
        "plugins. Development-facing; install the `cao` plugin instead to drive sessions."
    ),
    keywords=["cao", "development", "provider", "extension"],
    skills=[
        # Authoring a new provider: the skill encodes the provider contract that is
        # otherwise only discoverable by reading providers/.
        "cao-provider",
        # Authoring an *event* plugin. Packaged under whichever name M4 settles on,
        # since the packaged folder name must equal the frontmatter `name`.
        "cao-plugin",
        # `cao-contributing` joins here when PR #448 lands. Deliberately absent:
        # the skill does not exist in the tree yet and this build does not claim it.
    ],
    excluded_note=(
        "Excluded: every operator-facing skill (they belong in the `cao` package), and "
        "the adjacent-feature and vendored skills excluded there for the same reasons. "
        "`cao-contributing` is conditional on PR #448 (open, draft) and is not present; "
        "adding it is a one-line allowlist edit in scripts/build_agent_plugin.py."
    ),
)

PACKAGES: List[PackageConfig] = [OPERATOR, CONTRIBUTOR]


class BuildError(RuntimeError):
    """Raised when a package cannot be built."""


class ArtifactInspectionError(BuildError):
    """The published artifact could not be INSPECTED.

    Distinct from "the artifact is unbounded" on purpose (R5.3). A network failure,
    a missing sdist, an unreadable archive and a renamed module all mean the
    question was not answered; reporting any of them as an unbounded release would
    invent a finding, and reporting an unbounded release as a fetch problem would
    hide one.
    """


def package_version() -> str:
    """Read CAO's own version from ``pyproject.toml``.

    Single source of truth: syncing from package metadata is what makes it
    impossible for ``scripts/bump_version.py`` and a package manifest to drift
    without ``--check`` failing.
    """
    match = re.search(r'(?m)^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        raise BuildError("Could not read `version` from pyproject.toml")
    return match.group(1)


def render_claude_code_manifest(config: PackageConfig, version: str) -> str:
    """Render the Claude Code compatibility overlay (``.claude-plugin/plugin.json``).

    Claude Code (verified against 2.1.226) discovers ``skills/`` from an Agent
    Plugins package unchanged, but reads the package's identity only from
    ``.claude-plugin/plugin.json`` and its MCP servers only from a root
    ``.mcp.json`` — never from the standard's ``plugin.json``/``mcp.json``. The
    overlay closes exactly that gap; other clients ignore dot-prefixed entries,
    and the pinned validator discovers fixed locations only, so it adds no
    finding and no spec surface. Identity fields only, rendered from the same
    ``PackageConfig`` as the root manifest so the two cannot disagree.
    """
    manifest = {
        "name": config.name,
        "version": version,
        "description": config.description,
    }
    return json.dumps(manifest, indent=2) + "\n"


def render_manifest(config: PackageConfig, version: str) -> str:
    """Render one package's ``plugin.json``, deterministically."""
    manifest: Dict[str, object] = {
        "$schema": PLUGIN_SCHEMA_ID,
        "name": config.name,
        "version": version,
        "description": config.description,
        "repository": "https://github.com/awslabs/cli-agent-orchestrator",
        "license": "Apache-2.0",
        "keywords": list(config.keywords),
    }
    manifest.update(config.extra_manifest)
    return json.dumps(manifest, indent=2) + "\n"


def render_mcp(version: str) -> str:
    """Render the operator package's ``mcp.json``.

    **The packaged server is the *ops* server, not `cao-mcp-server`.** That is
    not a preference: `cao-mcp-server` is the *in-session* surface and derives
    its identity from `CAO_TERMINAL_ID`, which it validates and, on the
    orchestration paths, raises without. A foreign client installing this
    package has no terminal identity — it was not launched by CAO into a
    CAO-managed terminal — so packaging `cao-mcp-server` would ship a manifest
    whose tools fail on first call. `cao-ops-mcp-server` is the outside-a-session
    surface, and its tool set is exactly the operator story.

    **`command` is the single token `uvx`** (§7.2.1 permits nothing richer), with
    every other detail in `args`. CAO's own `resolve_cao_mcp_command` produces
    either a console-script path or `<python> -m ...`; neither is portable across
    foreign clients, and neither can be bundled without shipping CAO itself. So
    `uvx` on PATH is a documented prerequisite rather than something the manifest
    can guarantee.

    **The version is pinned exactly.** An unpinned `--from cli-agent-orchestrator`
    lets `uvx` resolve the latest PyPI release at first run, so the plugin's
    declared version and the server it actually launches can skew. The pin is
    written by the same pass that syncs `plugin.json`'s `version`, so the two
    cannot diverge.

    No `env`, no `headers`, no credentials (§7.2.1, §9.2).
    """
    mcp = {
        "$schema": MCP_SCHEMA_ID,
        "mcpServers": {
            OPS_SERVER_KEY: {
                "type": "stdio",
                "command": "uvx",
                "args": [
                    "--from",
                    f"{PYPI_DISTRIBUTION}=={version}",
                    OPS_CONSOLE_SCRIPT,
                ],
            }
        },
    }
    return json.dumps(mcp, indent=2) + "\n"


def pin_changed(version: str) -> bool:
    """Whether ``version`` differs from the pin in the COMMITTED package bytes.

    Read from disk rather than recomputed: the point is to detect that this build
    would change the pin, which is only answerable against what is already
    committed. A missing or unreadable package counts as changed -- there are no
    bytes for "unchanged" to be true of.
    """
    mcp_path = PACKAGES_DIR / "cao" / "mcp.json"
    try:
        committed = json.loads(mcp_path.read_text(encoding="utf-8"))
        args = committed["mcpServers"][OPS_SERVER_KEY]["args"]
    except (OSError, ValueError, KeyError):
        return True
    prefix = f"{PYPI_DISTRIBUTION}=="
    for arg in args:
        if isinstance(arg, str) and arg.startswith(prefix):
            return arg[len(prefix) :] != version
    return True


def committed_package_version() -> Optional[str]:
    """The version the COMMITTED packages were generated at, or ``None``.

    ``--check`` must compare the committed bytes against *the version they were
    built at*, not against whatever ``pyproject.toml`` currently says. Otherwise
    ``release.yml`` bumping the version on ``main`` turns every rendered file into
    a ``CONTENT DIFFERS`` finding and reddens ``main`` and every open PR, before
    the job that exists to repin has had a chance to run (defect G1c).

    Both packages carry the version, and they are written by the same pass, so
    disagreement between them is itself drift. ``None`` means "cannot tell" -- a
    missing, unreadable, or internally inconsistent set of packages -- and the
    caller then falls back to the ``pyproject`` version so the existing
    "package missing" / ``CONTENT DIFFERS`` diagnostics are what the operator
    sees, rather than an error about version discovery.
    """
    versions = set()
    for config in PACKAGES:
        manifest_path = PACKAGES_DIR / config.name / "plugin.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = manifest.get("version")
        if not isinstance(version, str) or not version:
            return None
        versions.add(version)
    if len(versions) != 1:
        return None
    return versions.pop()


def pin_ordering_problems(committed: str, current: str) -> List[str]:
    """Assert the committed pin is not AHEAD of ``pyproject.toml``.

    This is the check that survives R4's loosening. Tolerating ``committed <
    current`` is what stops a release bump reddening ``main``; tolerating
    ``committed > current`` would let the packages pin a release this tree is not,
    which is the failure ``--check`` exists to catch. Equality is the steady
    state, and ``--require-current-pin`` is what demands it.
    """
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging is a transitive dependency
        return []
    try:
        if Version(committed) > Version(current):
            return [
                f"the committed packages pin {PYPI_DISTRIBUTION}=={committed}, which is "
                f"AHEAD of pyproject.toml's {current}. The packages name a release this "
                f"tree is not. Run `make agent-plugin` (then commit the result)."
            ]
    except InvalidVersion as exc:
        return [f"could not compare the committed pin {committed!r} with {current!r}: {exc}"]
    return []


def should_check_boundedness(version: str, *, require: bool) -> bool:
    """Whether this build must answer for the pinned release's HTTP boundedness.

    Deliberately NOT "always". A contributor editing ``skills/`` and rerunning the
    build has expressed no opinion about the pinned release, and forcing them to
    answer for it -- or requiring the network to say so -- would break a workflow
    that has nothing to do with the finding. The question is asked exactly when
    somebody is choosing a release: a pin CHANGE, or the explicit flag the release
    job passes.
    """
    return require or pin_changed(version)


#: Where the ops server's module sits inside a published sdist. The sdist is a
#: src-layout tree under a ``<dist>-<version>/`` prefix, so the suffix is matched
#: rather than the whole path.
OPS_SERVER_SDIST_SUFFIX = "src/cli_agent_orchestrator/ops_mcp_server/server.py"

#: The keyword whose presence at every ``requests.*`` call site IS boundedness.
BOUNDED_REQUEST_KWARG = "timeout"

#: The sdist carries the whole repo (tens of MB), so it gets a larger budget than
#: the JSON metadata probe.
SDIST_TIMEOUT_S = 300


def request_call_sites_missing_kwargs(source: str, required: Iterable[str]) -> List[str]:
    """Every ``requests.*`` call in ``source`` that omits one of ``required``.

    Hoisted out of ``test_packaged_mcp_server.py``, which owns the same scan for
    the working tree's module and now imports it from here: one predicate, two
    consumers, so the definition of "bounded" cannot drift between the test that
    asserts this tree is bounded and the build that asks whether a *published*
    release was.

    Parsed rather than grepped, for the reason that test records: the module's
    prose legitimately discusses timeouts and requests, so a substring match would
    be satisfied by the comments explaining the property instead of the property.

    Returns human-readable offenders, empty when every call site complies.
    """
    required_set = set(required)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ArtifactInspectionError(
            f"the ops-server module could not be parsed ({exc}), so its request "
            f"boundedness could not be determined"
        ) from exc

    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if not (isinstance(owner, ast.Name) and owner.id == "requests"):
            continue
        passed = {kw.arg for kw in node.keywords if kw.arg}
        missing = required_set - passed
        if missing:
            offenders.append(
                f"line {node.lineno}: requests.{func.attr}(...) missing {sorted(missing)}"
            )
    return offenders


def published_sdist_url(version: str) -> str:
    """Locate the sdist for ``version`` through PyPI's JSON metadata."""
    url = f"https://pypi.org/pypi/{PYPI_DISTRIBUTION}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=PYPI_TIMEOUT_S) as response:  # noqa: S310
            metadata = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ArtifactInspectionError(
            f"could not read PyPI metadata for {PYPI_DISTRIBUTION}=={version} ({exc})"
        ) from exc

    for entry in metadata.get("urls", []):
        if entry.get("packagetype") == "sdist" and entry.get("url"):
            return str(entry["url"])
    raise ArtifactInspectionError(
        f"{PYPI_DISTRIBUTION}=={version} publishes no sdist, so its ops-server source "
        f"is not available to inspect"
    )


def fetch_published_ops_server_source(version: str) -> str:
    """Return the ops-server module source as PUBLISHED at ``version``.

    Downloads the sdist and reads one member out of it. Members are read, never
    extracted to disk, so a hostile archive has no path to traverse.
    """
    import tempfile

    url = published_sdist_url(version)
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
            with urllib.request.urlopen(url, timeout=SDIST_TIMEOUT_S) as response:  # noqa: S310
                shutil.copyfileobj(response, archive)
            archive.flush()
            archive.seek(0)
            with tarfile.open(archive.name, mode="r:gz") as tar:
                member = next(
                    (m for m in tar.getmembers() if m.name.endswith(OPS_SERVER_SDIST_SUFFIX)),
                    None,
                )
                if member is None:
                    raise ArtifactInspectionError(
                        f"the {PYPI_DISTRIBUTION}=={version} sdist contains no "
                        f"{OPS_SERVER_SDIST_SUFFIX}, so its request boundedness could not "
                        f"be determined"
                    )
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ArtifactInspectionError(
                        f"{OPS_SERVER_SDIST_SUFFIX} in the {PYPI_DISTRIBUTION}=={version} "
                        f"sdist is not a readable file"
                    )
                return extracted.read().decode("utf-8")
    except ArtifactInspectionError:
        raise
    except (urllib.error.URLError, OSError, tarfile.TarError, UnicodeDecodeError) as exc:
        raise ArtifactInspectionError(
            f"could not download or read the {PYPI_DISTRIBUTION}=={version} sdist ({exc})"
        ) from exc


def release_bounds_its_requests(version: str) -> bool:
    """Whether the PUBLISHED ``version`` bounds its ops-server HTTP requests.

    Reported as review 5222539218 item 8 on #584: the packaged ``mcp.json`` runs
    ``uvx --from cli-agent-orchestrator==<version> cao-ops-mcp-server``, so the
    boundedness that matters is the RELEASE's, not this working tree's. Asking the
    tree would answer the wrong question and pass for the wrong reason.

    Decided by INSPECTING the published artifact. The predecessor compared against
    a hand-maintained version floor, which encodes "which release contains the
    fix" as a constant somebody has to remember to move on every release — and
    which, set to a version ahead of everything published, refused legitimate patch
    releases outright (defect G1b). The artifact answers for itself.

    Raises ``ArtifactInspectionError`` when the question could not be answered,
    which is never the same as ``False``.
    """
    source = fetch_published_ops_server_source(version)
    offenders = request_call_sites_missing_kwargs(source, {BOUNDED_REQUEST_KWARG})
    if offenders:
        print(
            f"  {PYPI_DISTRIBUTION}=={version} has unbounded request call sites: "
            f"{'; '.join(offenders)}"
        )
        return False
    return True


def verify_bounded_client(version: str) -> None:
    """Fail unless the pinned release bounds its own HTTP requests.

    Raises ``BuildError`` so the caller's existing handler reports it like every
    other build failure. ``ArtifactInspectionError`` is a ``BuildError`` too and
    propagates with its own message, which says the artifact could not be
    inspected rather than implying the release is unbounded (R5.3).
    """
    if release_bounds_its_requests(version):
        print(f"  verified {PYPI_DISTRIBUTION}=={version} bounds its HTTP requests")
        return
    raise BuildError(
        f"{PYPI_DISTRIBUTION}=={version} does not bound its ops-server HTTP requests: its "
        f"published {OPS_SERVER_SDIST_SUFFIX} makes `requests.*` calls with no "
        f"`{BOUNDED_REQUEST_KWARG}=`, so the committed operator package would hand a "
        f"foreign MCP client a server that can hang indefinitely on an unreachable CAO. "
        f"Pin a release whose published artifact carries the timeout. The release "
        f"workflow's `regenerate-agent-plugin` job is what advances the pin: it rebuilds "
        f"the packages AFTER publishing, so the pin moves to the release that contains "
        f"the fix."
    )


def verify_published(version: str) -> None:
    """Fail unless ``version`` is already published on PyPI.

    Pinning a version PyPI does not yet have produces a package that fails on
    first launch with a resolution error, which the installing operator sees as
    "the plugin is broken" rather than "the release has not shipped yet". So the
    build refuses to write the pin rather than emitting one that cannot resolve.

    A network failure is also a refusal, deliberately: "could not check" is not
    "verified", and writing an unverified pin would defeat the check. Re-run the
    build where PyPI is reachable.
    """
    url = f"https://pypi.org/pypi/{PYPI_DISTRIBUTION}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=PYPI_TIMEOUT_S) as response:  # noqa: S310
            if response.status == 200:
                return
            raise BuildError(
                f"PyPI returned HTTP {response.status} for {PYPI_DISTRIBUTION}=={version}"
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BuildError(
                f"{PYPI_DISTRIBUTION}=={version} is not published on PyPI. The operator "
                f"package's mcp.json would pin a version `uvx` cannot resolve, so the "
                f"packaged server would fail on first launch. Publish the release first, "
                f"or build at a version that is already published."
            ) from exc
        raise BuildError(f"Could not verify {version} on PyPI: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BuildError(
            f"Could not reach PyPI to verify {PYPI_DISTRIBUTION}=={version} ({exc}). "
            f"The pin is not written unverified — re-run where PyPI is reachable."
        ) from exc


def render_changelog(config: PackageConfig, version: str) -> str:
    """Render a package CHANGELOG recording what it ships and what it omits."""
    skill_lines = "\n".join(f"- `{skill}`" for skill in config.skills) or "- (none)"
    return f"""\
# Changelog — `{config.name}`

This file is **generated** by `scripts/build_agent_plugin.py`. Edit the package
configuration in that script, not this file; `make check-agent-plugin` fails on
any hand edit.

## {version}

Targets the [Agent Plugins 1.0.0](https://agent-plugins.org/specification)
specification. Version is synced from CAO's own package metadata, so the plugin
and the CAO release it corresponds to cannot diverge.

Skills:

{skill_lines}

{config.excluded_note}
"""


def render_readme(config: PackageConfig, version: str) -> str:
    """Render the package README a foreign client's user reads first."""
    install_path = f"agent-plugin/{config.name}"
    return f"""\
# `{config.name}` — a CAO Agent Plugin

{config.description}

Generated by `scripts/build_agent_plugin.py` from the canonical skills in
`skills/`. Do not edit by hand — `make check-agent-plugin` fails on drift.

## Install

Installing an agent plugin runs untrusted code and content from its source. This
package comes from the CAO repository itself, but the same statement applies to
every plugin you install from anywhere.

From a clone:

```bash
cao plugin add ./{install_path}
```

From GitHub, without cloning:

```bash
cao plugin add https://github.com/awslabs/cli-agent-orchestrator --subdir {install_path}
```

Any Agent-Plugins-compatible client can install this directory the same way.

## Contents

Version `{version}`, synced from CAO's package metadata.

{chr(10).join(f"- `{skill}`" for skill in config.skills) or "- (none)"}

See [docs/agent-plugins.md](../../docs/agent-plugins.md) for the prerequisites
and the localhost-only posture.
"""


def source_skill_dir(skill: str) -> Path:
    """Locate a skill in the canonical repo-root tree, validating it exists."""
    source = CANONICAL_SKILLS_DIR / skill
    if not (source / "SKILL.md").is_file():
        raise BuildError(
            f"Allowlisted skill {skill!r} has no skills/{skill}/SKILL.md. "
            f"Fix the allowlist in scripts/build_agent_plugin.py or add the skill."
        )
    return source


def build_package(config: PackageConfig, version: str, dest_root: Path) -> Path:
    """Generate one package tree under ``dest_root``.

    Skills are **copied**, never symlinked. §4.1 permits a symlink resolving
    inside the plugin root, but a link into ``../../skills/`` escapes it and must
    be rejected — so a copy plus a drift guard is the only conformant option.
    """
    package_dir = dest_root / config.name
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)

    (package_dir / "plugin.json").write_text(render_manifest(config, version), encoding="utf-8")
    (package_dir / "CHANGELOG.md").write_text(render_changelog(config, version), encoding="utf-8")
    (package_dir / "README.md").write_text(render_readme(config, version), encoding="utf-8")

    if config.ships_mcp:
        (package_dir / "mcp.json").write_text(render_mcp(version), encoding="utf-8")

    # Claude Code compatibility overlay (see render_claude_code_manifest).
    # ``.mcp.json`` must stay byte-identical to ``mcp.json`` — one server list,
    # two filenames — which the drift guard enforces like every other file here.
    claude_dir = package_dir / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        render_claude_code_manifest(config, version), encoding="utf-8"
    )
    if config.ships_mcp:
        shutil.copy2(package_dir / "mcp.json", package_dir / ".mcp.json")

    for filename in SHARED_FILES:
        source = ROOT / filename
        if source.is_file():
            shutil.copy2(source, package_dir / filename)

    if config.skills:
        skills_dir = package_dir / "skills"
        skills_dir.mkdir()
        for skill in config.skills:
            shutil.copytree(source_skill_dir(skill), skills_dir / skill)

    return package_dir


def _relative_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def check_package(config: PackageConfig, version: str, scratch: Path) -> List[str]:
    """Diff the committed package against a freshly generated one.

    Returns a list of human-readable problems; empty means in sync. Building into
    a scratch directory rather than comparing field by field means the check
    covers *everything* the build writes, including files a future change adds.
    """
    problems: List[str] = []
    committed = PACKAGES_DIR / config.name

    if not committed.is_dir():
        return [f"package missing: agent-plugin/{config.name}/ (run `make agent-plugin`)"]

    try:
        expected = build_package(config, version, scratch)
    except BuildError as exc:
        return [str(exc)]

    expected_files = set(_relative_files(expected))
    committed_files = set(_relative_files(committed))

    for rel in sorted(expected_files - committed_files):
        problems.append(f"MISSING in agent-plugin/{config.name}/: {rel}")
    for rel in sorted(committed_files - expected_files):
        problems.append(f"UNEXPECTED in agent-plugin/{config.name}/: {rel}")
    for rel in sorted(expected_files & committed_files):
        if not filecmp.cmp(expected / rel, committed / rel, shallow=False):
            problems.append(f"CONTENT DIFFERS in agent-plugin/{config.name}/: {rel}")

    problems.extend(validate_package(config, version, committed))
    return problems


def validate_package(config: PackageConfig, version: str, package_dir: Path) -> List[str]:
    """Run CAO's own validator over the committed package.

    This is the conformance half of the guard, and the reason the CI step also
    functions as the on-every-PR validation of CAO's manifests against the
    pinned schemas that #573's AC1 requires.
    """
    problems: List[str] = []
    try:
        from cli_agent_orchestrator.agent_plugins.validation import validate_plugin
    except ImportError as exc:  # pragma: no cover - only outside the venv
        return [f"could not import the CAO validator ({exc}); run under `uv run`"]

    report = validate_plugin(package_dir)

    for finding in report.findings:
        if finding.severity.value == "fatal":
            problems.append(
                f"FATAL in agent-plugin/{config.name}/: "
                f"{finding.code} ({finding.spec_ref}) {finding.message}"
            )

    if not report.loadable:
        problems.append(f"agent-plugin/{config.name}/ is not loadable")

    discovered = set(report.skill_names)
    allowlisted = set(config.skills)
    for missing in sorted(allowlisted - discovered):
        problems.append(f"agent-plugin/{config.name}/: allowlisted skill not discovered: {missing}")
    for extra in sorted(discovered - allowlisted):
        problems.append(f"agent-plugin/{config.name}/: skill present but not allowlisted: {extra}")

    if report.manifest and report.manifest.name != config.name:
        problems.append(f"agent-plugin/{config.name}/: manifest name is {report.manifest.name!r}")

    problems.extend(_check_mcp(config, version, package_dir, report))

    return problems


def _check_mcp(config: PackageConfig, version: str, package_dir: Path, report) -> List[str]:
    """Conformance checks specific to a package's ``mcp.json``."""
    problems: List[str] = []
    mcp_path = package_dir / "mcp.json"

    if not config.ships_mcp:
        if mcp_path.exists():
            problems.append(f"agent-plugin/{config.name}/: ships an mcp.json but must not")
        return problems

    if not mcp_path.is_file():
        problems.append(f"agent-plugin/{config.name}/: mcp.json missing")
        return problems

    try:
        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"agent-plugin/{config.name}/mcp.json is not valid JSON: {exc}"]

    # §7.2.2.2: the two documents must target the same specification version, or
    # the MCP configuration is invalid. They are different schema *files*, so it
    # is the version segment that has to match, not the whole URL.
    manifest_version = _schema_version(report.manifest.schema_id if report.manifest else "")
    if _schema_version(mcp.get("$schema", "")) != manifest_version:
        problems.append(
            f"agent-plugin/{config.name}/: mcp.json and plugin.json target different "
            f"specification versions"
        )

    servers = mcp.get("mcpServers", {})
    if set(servers) != {OPS_SERVER_KEY}:
        problems.append(
            f"agent-plugin/{config.name}/: mcp.json must declare exactly one server "
            f"named {OPS_SERVER_KEY!r}, found {sorted(servers)}"
        )
        return problems

    entry = servers[OPS_SERVER_KEY]
    args = entry.get("args", [])

    if entry.get("command") != "uvx":
        problems.append(f"agent-plugin/{config.name}/: `command` must be the single token 'uvx'")
    if OPS_CONSOLE_SCRIPT not in args:
        problems.append(f"agent-plugin/{config.name}/: mcp.json must invoke {OPS_CONSOLE_SCRIPT}")
    if any("cao-mcp-server" == a for a in args):
        problems.append(
            f"agent-plugin/{config.name}/: mcp.json must not invoke the in-session "
            f"cao-mcp-server, which requires a CAO_TERMINAL_ID this package cannot provide"
        )
    if f"{PYPI_DISTRIBUTION}=={version}" not in args:
        problems.append(
            f"agent-plugin/{config.name}/: mcp.json must pin "
            f"{PYPI_DISTRIBUTION}=={version} exactly"
        )
    for forbidden in ("env", "headers"):
        if forbidden in entry:
            problems.append(
                f"agent-plugin/{config.name}/: the {OPS_SERVER_KEY} entry must declare no "
                f"`{forbidden}` (§7.2.1, §9.2)"
            )

    return problems


def _schema_version(schema_id: str) -> str:
    match = re.search(r"/schemas/([^/]+)/", schema_id or "")
    return match.group(1) if match else ""


def run_build(*, verify_pypi: bool = True, require_bounded_client: bool = False) -> int:
    version = package_version()
    ships_mcp = any(config.ships_mcp for config in PACKAGES)
    if verify_pypi and ships_mcp:
        verify_published(version)
        print(f"  verified {PYPI_DISTRIBUTION}=={version} is published on PyPI")
    # Beside `verify_published`, and for the same reason: both are facts about the
    # release the manifest points a foreign client at, and both must be settled
    # before bytes are written. `run_check` stays offline and does not ask.
    #
    # Both are also gated on `verify_pypi`, i.e. suppressed by
    # `--skip-publish-check` (R5.2). Boundedness is now decided by DOWNLOADING the
    # published sdist, so leaving it enabled would make `--skip-publish-check`
    # advertise an offline rebuild while still requiring the network — and requiring
    # it to fetch an artifact whose publication the same flag just declined to
    # check. One flag, one meaning: do not touch PyPI.
    if (
        verify_pypi
        and ships_mcp
        and should_check_boundedness(version, require=require_bounded_client)
    ):
        verify_bounded_client(version)
    elif not verify_pypi and require_bounded_client:
        print(
            "  skipping the boundedness inspection: --skip-publish-check was passed, "
            "which suppresses every PyPI fetch. The pin's boundedness is UNVERIFIED."
        )
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    for config in PACKAGES:
        build_package(config, version, PACKAGES_DIR)
        print(f"  built agent-plugin/{config.name}/ ({len(config.skills)} skill(s))")
    print(f"Built {len(PACKAGES)} agent plugin package(s) at version {version}")
    return 0


def run_check(*, require_current_pin: bool = False) -> int:
    """Evaluate **both** packages independently and report both outcomes.

    A failure in one package must not suppress reporting a failure in the other —
    otherwise the contributor package can quietly rot behind a green check on the
    operator package.

    By default the committed packages are checked **at the version they were built
    at**, and the only version constraint is ``committed <= pyproject``. This is
    deliberate loosening (defect G1c): ``release.yml`` bumps ``pyproject.toml`` on
    ``main`` and the job that repins the packages runs afterwards, so between those
    two moments strict equality would fail `ci.yml` on ``main`` and on every open
    PR. Everything else the check covers — drift in any generated byte, validator
    findings, allowlist agreement, the mcp.json conformance rules — is unchanged
    and still evaluated.

    ``require_current_pin`` restores strict equality: the packages must pin exactly
    the version in ``pyproject.toml``. The regeneration job passes it, which is
    what bounds the loosening — a forgotten repin is caught there rather than
    nowhere.
    """
    import tempfile

    current_version = package_version()
    if require_current_pin:
        version = current_version
    else:
        version = committed_package_version() or current_version

    all_problems: Dict[str, List[str]] = {}

    with tempfile.TemporaryDirectory(prefix="cao-agent-plugin-check-") as scratch_root:
        for config in PACKAGES:
            scratch = Path(scratch_root) / config.name
            scratch.mkdir()
            try:
                # Offline: --check compares committed bytes and runs the
                # validator. Publication is verified when the pin is *written*,
                # which is where an unpublished version could still be prevented.
                all_problems[config.name] = check_package(config, version, scratch)
            except Exception as exc:  # keep going: the other package still gets checked
                all_problems[config.name] = [f"check raised: {exc}"]

    ordering = pin_ordering_problems(version, current_version)
    if ordering:
        all_problems.setdefault(PACKAGES[0].name, []).extend(ordering)

    failed = {name: problems for name, problems in all_problems.items() if problems}
    if failed:
        print("Agent plugin packages are OUT OF SYNC or non-conformant:", file=sys.stderr)
        for name, problems in failed.items():
            print(f"\n  {name}:", file=sys.stderr)
            for problem in problems:
                print(f"    - {problem}", file=sys.stderr)
        print(
            "\nRun: python scripts/build_agent_plugin.py  (then commit the result)",
            file=sys.stderr,
        )
        return 1

    names = ", ".join(config.name for config in PACKAGES)
    if version == current_version:
        print(f"OK: agent plugin packages in sync and loadable at version {version} ({names})")
    else:
        print(
            f"OK: agent plugin packages in sync and loadable at the version they were "
            f"built at, {version} ({names}). pyproject.toml is now at {current_version}; "
            f"the release workflow's `regenerate-agent-plugin` job advances the pin."
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed packages instead of regenerating them.",
    )
    parser.add_argument(
        "--skip-publish-check",
        action="store_true",
        help=(
            "Build without confirming the pinned version is on PyPI. For local "
            "iteration only: a pin written this way can fail on first launch, and "
            "`--check` will still compare the bytes it produced."
        ),
    )
    parser.add_argument(
        "--require-bounded-client",
        action="store_true",
        help=(
            "Fail unless the pinned release bounds its ops-server HTTP requests. "
            "Passed by the release workflow. Without it the check runs only when "
            "this build would CHANGE the committed pin, so a plain rebuild after a "
            "skills/ edit still works."
        ),
    )
    parser.add_argument(
        "--require-current-pin",
        action="store_true",
        help=(
            "With --check: require the committed packages to pin exactly the version "
            "in pyproject.toml, instead of merely not being ahead of it. Passed by the "
            "release workflow's regeneration job after it repins. Not used by CI's "
            "`make check-agent-plugin`, because a release bump lands on `main` before "
            "the repin does and strict equality there would redden `main`."
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            return run_check(require_current_pin=args.require_current_pin)
        return run_build(
            verify_pypi=not args.skip_publish_check,
            require_bounded_client=args.require_bounded_client,
        )
    except BuildError as exc:
        print(f"Agent plugin build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
