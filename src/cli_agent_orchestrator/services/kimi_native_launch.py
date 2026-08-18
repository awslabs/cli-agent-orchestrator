"""The exact argv for resuming a proven Kimi session into a native TUI.

The Kimi Code resume option is ``-S, --session [id]`` with an **optional**
argument: given an id it resumes that session, and given *no* id it opens an
interactive picker instead. The launch policy may admit a newer semver build,
but this module is used only after that build's native-session behavior has
been independently proven and added to the exact capability table.

That optionality is the hazard this module exists to remove.  A resume
that loses its session id does not fail — it launches a picker, and
whatever a human (or an errant keystroke) then selects becomes the
attached session.  CAO would hold a durable attachment record naming one
session while the pane runs a different one, and every receipt afterwards
would be confidently wrong.

So the id is validated before it is ever placed on a command line: a
missing, empty, or flag-shaped id raises rather than degrading to a bare
``--session``.  The result is argv, never a shell string, so the id
cannot be word-split or globbed on its way to the process either.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

#: The pinned resume option.  The long form is used deliberately: ``-S``
#: is a single letter away from other short flags, and a typo there fails
#: as an unknown option rather than as a wrong session.
RESUME_OPTION = "--session"

#: Provider session ids as minted by Kimi (``session_<hex>``) plus the
#: general shape of an opaque provider id.  Deliberately narrow: this
#: value is about to select which conversation a worker resumes, and a
#: permissive pattern here is how a flag-shaped or empty id reaches the
#: interactive picker.
_SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


class KimiNativeLaunchError(ValueError):
    """The native launch argv could not be built safely."""

    code = "kimi-native-launch-error"


# Kimi Code's agent-core-v2 workspace trust service persists one JSON document
# addressed by ``(scope, key)`` through its file storage backend.  The scope,
# key shape, and record fields below are the provider's observed 0.31--0.34
# contract, not a CAO-owned parallel trust database.  The managed launcher
# writes this record before the TUI process starts so the provider never
# reaches an interactive trust dialog (which is indistinguishable from a
# task-delivery interstitial at the CAO boundary).
WORKSPACE_TRUST_SCOPE = "workspace-trust"
_WORKSPACE_TRUST_KEY_PREFIX = "wd_"
_WORKSPACE_TRUST_HASH_LENGTH = 12
_WORKSPACE_TRUST_SLUG_LENGTH = 40


def _workspace_trust_key(work_dir: str) -> str:
    """Return Kimi's exact workspace-trust document key for ``work_dir``."""
    normalized = os.path.realpath(work_dir).replace("\\", "/").rstrip("/") or "/"
    name = normalized.rsplit("/", 1)[-1] or normalized
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower())
    slug = slug.strip("-")[:_WORKSPACE_TRUST_SLUG_LENGTH].strip("-")
    if slug in {"", ".", ".."}:
        slug = "workspace"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_WORKSPACE_TRUST_HASH_LENGTH]
    return f"{_WORKSPACE_TRUST_KEY_PREFIX}{slug}_{digest}"


def preauthorize_workspace(*, kimi_home: str, working_directory: str) -> str:
    """Atomically pre-authorize one canonical worktree for native Kimi.

    Kimi's v2 TUI presents a trust interstitial before it creates a session,
    including when no project MCP file exists.  CAO cannot type through that
    interstitial: doing so would make a rendered prompt look like a delivered
    task.  The provider's own ``WorkspaceTrustService.trust()`` writes the
    record reproduced here, so managed launch performs the same state change
    before process creation and then reads it back as a proof.

    The home and worktree are generation-private/canonical inputs supplied by
    the managed launch boundary.  Existing records are accepted only when
    their root and timestamp are structurally valid; a conflicting or
    malformed record fails closed rather than silently authorizing another
    directory.
    """
    home = Path(kimi_home).expanduser()
    if (
        not os.path.isabs(working_directory)
        or os.path.realpath(working_directory) != working_directory
    ):
        raise KimiNativeLaunchError(
            f"Kimi workspace trust requires a canonical worktree; got {working_directory!r}"
        )
    root = os.path.realpath(working_directory).replace("\\", "/").rstrip("/") or "/"
    if not os.path.isabs(root) or not os.path.isdir(root):
        raise KimiNativeLaunchError(
            f"Kimi workspace trust requires an existing absolute worktree; got {working_directory!r}"
        )
    trust_dir = home / WORKSPACE_TRUST_SCOPE
    trust_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = trust_dir / _workspace_trust_key(root)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KimiNativeLaunchError(
                f"Kimi workspace trust record is unreadable: {path}"
            ) from exc
        if not isinstance(existing, dict) or existing.get("root") != root:
            raise KimiNativeLaunchError(
                f"Kimi workspace trust record names a different root: {path}"
            )
        trusted_at = existing.get("trustedAt")
        if (
            isinstance(trusted_at, bool)
            or not isinstance(trusted_at, (int, float))
            or trusted_at <= 0
        ):
            raise KimiNativeLaunchError(
                f"Kimi workspace trust record has no valid timestamp: {path}"
            )
    else:
        payload = {"root": root, "trustedAt": int(time.time() * 1000)}
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=trust_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KimiNativeLaunchError(
            f"Kimi workspace trust proof could not be read: {path}"
        ) from exc
    if not isinstance(proof, dict) or proof.get("root") != root:
        raise KimiNativeLaunchError(f"Kimi workspace trust proof drifted: {path}")
    return str(path)


def validate_session_id(session_id: object) -> str:
    """Return ``session_id`` if it can never be mistaken for "no id".

    Rejects non-strings, the empty string, anything leading with ``-``
    (which the option parser would read as the next flag, leaving
    ``--session`` argument-less), and anything carrying whitespace or
    shell metacharacters.
    """
    if not isinstance(session_id, str) or not session_id:
        raise KimiNativeLaunchError(
            f"resume requires a non-empty session id; got {session_id!r} — "
            f"a missing id would make {RESUME_OPTION} open an interactive picker"
        )
    if not _SESSION_ID_PATTERN.match(session_id):
        raise KimiNativeLaunchError(
            f"session id {session_id!r} is not a plain provider id; a flag-shaped or "
            f"whitespace-bearing id can leave {RESUME_OPTION} without an argument"
        )
    return session_id


def build_resume_argv(
    *,
    session_id: str,
    kimi_binary: str = "kimi",
    extra_args: Optional[Sequence[str]] = None,
) -> list[str]:
    """Build the exact ``kimi --session <id>`` argv for a native resume.

    ``extra_args`` are placed **before** the resume option so nothing can
    be inserted between ``--session`` and its id.  Any extra argument
    that is itself a resume option is refused: two resume options on one
    command line make which session gets attached depend on the parser's
    precedence rather than on this module.

    Returned as argv for ``create_window_with_argv``, which makes the TUI
    the pane's own primary process rather than a line typed into a shell.
    """
    binary = kimi_binary if isinstance(kimi_binary, str) and kimi_binary else None
    if binary is None:
        raise KimiNativeLaunchError(f"kimi_binary must be a non-empty string; got {kimi_binary!r}")
    resume_id = validate_session_id(session_id)

    leading: list[str] = []
    for index, arg in enumerate(extra_args or ()):
        if not isinstance(arg, str):
            raise KimiNativeLaunchError(f"extra_args[{index}] must be a string; got {arg!r}")
        if arg in {RESUME_OPTION, "-S"} or arg.startswith(f"{RESUME_OPTION}="):
            raise KimiNativeLaunchError(
                f"extra_args[{index}]={arg!r} is a second resume option; the resumed session "
                "must be decided here, not by option precedence"
            )
        leading.append(arg)

    return [binary, *leading, RESUME_OPTION, resume_id]


def resumes_exactly(argv: Sequence[str], session_id: str) -> bool:
    """True when ``argv`` resumes exactly ``session_id`` and nothing else.

    Used to check an argv immediately before launch and against a durable
    attachment record afterwards, so the session CAO recorded and the
    session the pane actually runs are verified to be the same one.
    """
    resume_positions = [index for index, arg in enumerate(argv) if arg in {RESUME_OPTION, "-S"}]
    if len(resume_positions) != 1:
        return False
    position = resume_positions[0]
    if position + 1 >= len(argv):
        return False
    return argv[position + 1] == session_id


# ---------------------------------------------------------------------------
# Rendered native-header exact-session proof (COND-0312)
#
# Kimi Code 0.31.0 rewrites ``process.title`` to ``kimi-code`` *after* parsing
# its argv.  On Darwin that overwrite lands in the same buffer
# ``KERN_PROCARGS2`` exposes, so the pane observer reads back an argv of
# ``['kimi-code', '', '', ...]`` -- the resumed ``--session <id>`` is gone, and
# :func:`resumes_exactly` necessarily returns ``False`` on it.  That is the
# exact live defect that grounded p1-closure: a launch whose pane was running
# the right session, proved only by an argv the build had just erased.
#
# The TUI renders a strict native boot header instead, and the session it is
# running is named on its ``Session:`` line.  The functions below turn that
# header into an exact-session proof that is independent of the (now-unreadable)
# argv but tied to the admitted pane: the capture is read from the same pane the
# observer proved the pid/start-marker of, so a header that names the bound
# session is the admitted process's own statement of which session it holds.
#
# The proof is fail-closed and per-build.  A build appears in
# :data:`_RENDERED_SESSION_PROVEN_BUILDS` only when its title-rewrite and its
# header layout were both read and live-verified.  An unlisted build is not
# refused outright: :func:`rendered_session_proof_for` reads the same facts
# from the installed bundle (version-bound to the build being driven) and a
# successful read derives a proof whose evidence says so; a build whose
# bundle matches neither the table nor the known layout still freezes, and
# the header painted on the admitted pane remains the session proof in
# every case.
# ---------------------------------------------------------------------------

#: The rule name recorded on a rendered-header session proof.
RULE_KIMI_NATIVE_HEADER = "kimi-native-header-v1"

#: The four labels of the Kimi native boot header, read from the installed
#: 0.31.0 pane.  Each must appear exactly once with a non-empty value for the
#: header to be the proof -- a label that is missing, empty, or duplicated is
#: not the header this proof was read against, and "unproven" here is a freeze.
_NATIVE_HEADER_LABELS = ("Directory", "Session", "Model", "Version")

#: Box-drawing verticals that frame each header row when the pane is captured
#: without escape sequences.  Stripped from a row's ends before it is matched
#: as a label line, so the proof reads the header the renderer painted rather
#: than its chrome.  Deliberately only the verticals: the label text and its
#: value are the cells that carry meaning, and stripping more would risk
#: eating a value that begins or ends with a box glyph.
_NATIVE_HEADER_FRAME_CHARS = "│┃║"

_HEADER_LABEL_RE = re.compile(
    r"\A\s*(?P<label>Directory|Session|Model|Version)\s*:\s*(?P<value>.*?)\s*\Z"
)


@dataclass(frozen=True)
class RenderedSessionProof:
    """How one Kimi build's rendered native header proves its session.

    ``evidence`` records what was read for this build (the bundle digest and
    the title-rewrite fact), so review can check it without re-walking the
    tree.  A build is absent from the table unless both its rewrite and its
    header layout were read.
    """

    provider: str
    rule: str
    evidence: str


_KIMI_0310_RENDERED_EVIDENCE = (
    "live-verified on the installed Kimi Code 0.31.0 (COND-0312, 2026-08-05, "
    "run cond-0303-pr74-review-k3-r2 / pane %47): the build rewrites "
    "process.title to 'kimi-code' after parsing its argv, so Darwin "
    "KERN_PROCARGS2 returns ['kimi-code','','','',...] and the resumed "
    "--session <id> is no longer observable in the kernel argv. The TUI "
    "instead renders a strict native boot header -- exactly one each of "
    "Directory, Session, Model, and Version label lines, framed by box "
    "verticals -- and the Session line names the session the pane is "
    "running. Installed bundle dist/main.mjs sha256 "
    "689fc2a123dfc3145dab26a8e6a86c71a5dc8552b13fe0449679e065ce96774e."
)

_KIMI_0320_RENDERED_EVIDENCE = (
    "live-verified on the installed Kimi Code 0.32.0 (COND-0315, 2026-08-05, "
    "private tmux stage on a disposable socket/worktree, zero-prompt ACP-minted "
    "session_d9aea239-7b68-4a76-99b9-186e6128c5c6): the build rewrites "
    "process.title to 'kimi-code' after parsing its argv, so Darwin "
    "KERN_PROCARGS2 returns ['kimi-code','','',''] and the resumed "
    "--session <id> is no longer observable in the kernel argv. The TUI "
    "resumed the exact minted session with no picker and renders the strict "
    "native boot header -- exactly one each of Directory, Session, Model, and "
    "Version label lines, framed by box verticals -- with the Session line "
    "naming the minted session, the Version line 0.32.0, and the Directory "
    "line the bound worktree. Installed bundle dist/main.mjs sha256 "
    "b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79 "
    "(matches the npm-published digest; the process.title = PROCESS_NAME "
    "rewrite and the header infoLines read byte-identical to the "
    "npm-published 0.31.0 bundle)."
)

_KIMI_0330_RENDERED_EVIDENCE = (
    "live-verified on the installed Kimi Code 0.33.0 (COND-0315, 2026-08-05, "
    "private tmux stage on a disposable socket/worktree, zero-prompt ACP-minted "
    "session_4b587189-e4ee-45a4-a85e-bf864d45f123, KIMI_CODE_NO_AUTO_UPDATE=1): "
    "the build rewrites process.title to 'kimi-code' after parsing its argv, "
    "so Darwin KERN_PROCARGS2 returns ['kimi-code','','',''] and the resumed "
    "--session <id> is no longer observable in the kernel argv. The TUI "
    "resumed the exact minted session with no picker and renders the strict "
    "native boot header -- exactly one each of Directory, Session, Model, and "
    "Version label lines (an MCP line may follow), framed by box verticals "
    "inside the one-cell GutterContainer the parser tolerates -- with the "
    "Session line naming the minted session, the Version line 0.33.0, and the "
    "Directory line the bound worktree. Installed bundle dist/main.mjs sha256 "
    "0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2 "
    "(matches the npm-published digest; the process.title = PROCESS_NAME "
    "rewrite and the header infoLines read byte-identical to the "
    "npm-published 0.32.0 bundle; the natively reimplemented ACP surface was "
    "proven live, including the durable session/new->kill->session/load "
    "proof)."
)

_KIMI_0340_RENDERED_EVIDENCE = (
    "Kimi Code 0.34.0 dist/main.mjs sha256 "
    "d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c; "
    "source inspection found process.title='kimi-code' and the native header "
    "infoLines; a private-tmux capture after ACP session/new rendered exactly "
    "one Directory, Session, Model, and Version 0.34.0 line, with the Session "
    "matching the minted id and Directory matching the canonical worktree."
)

#: Per-build rendered-header session proofs.  A build is present only when its
#: post-parse process-title rewrite *and* its native header layout were both
#: read, so the proof never silently applies to a build nobody has examined.
#: 0.29.x/0.30.0 are deliberately absent: they preserve the resumed argv (the
#: title rewrite is new in 0.31.0), so they keep proving from the argv and the
#: rendered proof is not claimed for them.  Adding a build here never widens a
#: range -- it is a separate keyed entry, read against its own bytes.
_RENDERED_SESSION_PROVEN_BUILDS: dict[str, RenderedSessionProof] = {
    "0.31.0": RenderedSessionProof(
        provider="kimi_cli",
        rule=RULE_KIMI_NATIVE_HEADER,
        evidence=_KIMI_0310_RENDERED_EVIDENCE,
    ),
    "0.32.0": RenderedSessionProof(
        provider="kimi_cli",
        rule=RULE_KIMI_NATIVE_HEADER,
        evidence=_KIMI_0320_RENDERED_EVIDENCE,
    ),
    "0.33.0": RenderedSessionProof(
        provider="kimi_cli",
        rule=RULE_KIMI_NATIVE_HEADER,
        evidence=_KIMI_0330_RENDERED_EVIDENCE,
    ),
    "0.34.0": RenderedSessionProof(
        provider="kimi_cli",
        rule=RULE_KIMI_NATIVE_HEADER,
        evidence=_KIMI_0340_RENDERED_EVIDENCE,
    ),
}


def rendered_session_proof_for(provider_version: Optional[str]) -> Optional[RenderedSessionProof]:
    """The rendered-header proof for this exact build, or ``None``.

    For a listed build the stage-verified table entry answers.  For an
    unlisted build the same facts are read at runtime from the installed
    bundle (the process-title rewrite and the four header labels the
    strict parser requires, version-bound to the build being driven), and
    a successful read yields a proof whose evidence says it is
    bundle-derived — recorded as derived, never as stage-proven.

    ``None`` means "neither the table nor the installed bundle
    establishes this build's title-rewrite and header layout" — a caller
    that needs the proof must fail closed rather than guess at a header
    it has never seen.  A build whose bundle shows the rewrite but a
    *different* header layout gets ``None`` too: the argv proof is
    known-erased for it, so the attachment freezes loudly rather than
    proving off a stranger.  The version is normalized the way the rest
    of the per-build tables normalize theirs, so a bare ``0.31.0`` and a
    banner ``kimi 0.31.0`` name the same build.
    """
    if not isinstance(provider_version, str) or not provider_version.strip():
        return None
    from cli_agent_orchestrator.services import (
        installed_bundle_facts,
        provider_contracts,
    )

    normalized = provider_contracts.normalized_version(provider_version)
    proof = _RENDERED_SESSION_PROVEN_BUILDS.get(normalized)
    if proof is not None:
        return proof
    derived_evidence = installed_bundle_facts.kimi_rendered_header_hint(normalized)
    if derived_evidence is None:
        return None
    return RenderedSessionProof(
        provider="kimi_cli",
        rule=RULE_KIMI_NATIVE_HEADER,
        evidence=derived_evidence,
    )


def session_proof_gap_for(provider_version: Optional[str]) -> Optional[str]:
    """Why this exact build provably has no native session proof, or None.

    The preflight question behind the launch: is this build *known* to be
    unable to prove its session either way?  A build with any proof — a
    stage-verified table row, a bundle-derived rendered proof, or an
    argv-preserving build whose kernel argv still names the resumed
    session — answers ``None`` and launches.  So does a build the
    installed bundle says nothing about: unknown is not a refusal, and
    the launch's own bounded, fail-closed proofs answer it at runtime.
    A reason is returned only when the version-matched bundle of the exact
    build being driven establishes both halves of the gap — the argv-
    erasing title rewrite and the absence of the header the rendered proof
    reads — because that launch could only mint, start a pane, and freeze
    the attachment.
    """
    if rendered_session_proof_for(provider_version) is not None:
        return None
    from cli_agent_orchestrator.services import installed_bundle_facts

    return installed_bundle_facts.kimi_session_proof_gap(provider_version)


def parse_native_header(rows: object) -> Optional[dict[str, str]]:
    """The Kimi native header as one value per label, or ``None`` when unproven.

    Strict by design: every ``None`` is a freeze, never a pass.  Requires each
    of ``Directory``, ``Session``, ``Model``, and ``Version`` to appear exactly
    once as a ``Label: value`` line (after the box verticals are stripped) with
    a non-empty value.  Absence, duplication, or emptiness of any label is
    ``None`` -- a header missing its ``Session`` line is the picker hazard
    rendered, and a header with two ``Session`` lines is a session that cannot
    be picked out from here.

    Non-label rows are ignored, so a header scattered among a status bar or a
    transcript still parses -- but a *second* labelled line of any kind is a
    duplication, so a stray ``Session:`` in surrounding rendering fails closed
    rather than silently picking one.
    """
    if not isinstance(rows, (list, tuple)):
        return None
    values: dict[str, str] = {}
    for raw in rows:
        if not isinstance(raw, str):
            return None
        # Whitespace first, then the frame, then whitespace again: the TUI
        # mounts the boot header inside a one-cell ``GutterContainer`` (read
        # from the 0.31.0/0.32.0/0.33.0 bundles alike), so a painted row is
        # ``" │  Label: value ... │  "`` -- a one-cell left pad before the
        # box vertical and capture padding after it.  Stripping the frame
        # before the gutter would stop at the leading space and never match
        # the screen the proof actually reads; the label cells are what
        # carry meaning either way, and the exactly-once rule below is the
        # strictness that guards them.
        match = _HEADER_LABEL_RE.match(raw.strip().strip(_NATIVE_HEADER_FRAME_CHARS).strip())
        if match is None:
            continue
        label = match.group("label").lower()
        value = match.group("value").strip()
        if label in values:
            return None
        values[label] = value
    for label in _NATIVE_HEADER_LABELS:
        if not values.get(label.lower()):
            return None
    return dict(values)


def renders_session_exactly(
    rows: object, session_id: str, *, provider_version: Optional[str]
) -> bool:
    """True when the rendered native header proves exactly ``session_id``.

    Three independent conditions, all required, all fail-closed:

    * the build is one whose title-rewrite and header were read
      (:func:`rendered_session_proof_for` is not ``None``) -- so an unknown
      build cannot inherit the proof;
    * the header parses to exactly one of each label
      (:func:`parse_native_header` is not ``None``) -- so a missing,
      empty, or duplicated session line proves nothing;
    * the header's ``Session`` value is exactly ``session_id`` and its
      ``Version`` value is exactly the proven build -- so a pane rendering a
      different session, or a different version, is not the one claimed.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    if not isinstance(provider_version, str) or not provider_version.strip():
        return False
    proof = rendered_session_proof_for(provider_version)
    if proof is None:
        return False
    parsed = parse_native_header(rows)
    if parsed is None:
        return False
    from cli_agent_orchestrator.services import provider_contracts

    return parsed["session"] == session_id and parsed[
        "version"
    ] == provider_contracts.normalized_version(provider_version)
