"""The dark exact physical executor for same-native-session reincarnation
(cond-0378 B3).

B3 is the one place the accepted M3-B physical sequence actually happens.
It consumes the exact B1 restore contract and the B2 winning operation /
effect-intent / barrier seam, and performs — for one dormant stable
agent — the single physical reincarnation:

1. claim/adopt the winning B2 operation, then durably reserve exactly one
   successor terminal id and fresh generation BEFORE any physical I/O;
2. ``fence_prior``: establish that the retired prior incarnation is the
   exact fenced source (retired, current, dormant);
3. ``reap_prior``: tear down only that exact prior terminal generation/
   pane/process when present, through the exact-generation teardown seam
   with the attachment release and roster retirement HELD for B3's own
   authorized steps — a reused terminal id or a replacement generation
   preserves the replacement and refuses;
4. ``release_attachment``: reconcile/release the prior native attachment
   only after the authoritative no-survivor proof; a positively live or
   unobservable competing owner refuses rather than double-attaches;
5. ``acquire_native``/``create_pane``/``launch_resume``/``verify_identity``:
   acquire the SAME harness-scoped native session id for the reserved
   successor and launch the same harness through
   ``native_tui_launch.start(..., launch_kind="resume")`` — never a fresh
   launch, never ``managed_launch_v2.attempt_resume``, never a shell
   send-keys path, never an alternative provider binder.  The launch's
   optional authorize callback is the pre-effect linearization point for
   each of those ordered intents; because pane creation atomically starts
   the resume argv, the ``create_pane`` and ``launch_resume`` intents are
   authorized back-to-back immediately before the single atomic transport
   call — a barrier landing between them creates no pane;
6. append/bind the fresh incarnation to the EXISTING stable agent and
   native lineage with disposition ``bound`` in the same database
   transaction that rechecks the operation phase, lifecycle epoch,
   barrier, and exact dormant source — then record the durable accepted
   result.

The successor is never task-admitted and zero original task/input bytes
are sent: B3 stops at ``verify_identity``; ``admit_input`` belongs to
M3-D/M3-F.  Exact restoration preserves the actual harness and native
session; a model/effort/route/mode/provider-version variation — or launch
material selecting profile/provider-home material other than the
contract's recorded mapping — is allowed only when the operation names the
exact bounded compatibility-cell ref/digest for this harness and the
launch material agrees with it; B3 records/consumes that fact, it never
infers that a cell passes (B4 owns installed canaries, M3-F owns
activation).  An absent/unproven/mismatched required cell or a missing
required B1 launch fact is a typed disabled/refused outcome BEFORE any
physical effect.  The effective requested-or-stored model/effort is
computed once and pinned provider-aware on the resume argv/environment
itself AND the managed terminal metadata; a pin-requiring harness with an
unknown or unpinnable effective model disables exact restore rather than
falling back to the provider's ambient default — except that a
cell-covered route the harness's own validator cannot attest (a
DeepSeek/GLM-style route through the claude_code harness) may pin the
effective model through the material's explicit, precedence-checked route
args/environment.  The identity-proof version selector is the contract's
recorded executable version unless the material's hint enters the
variation gate.  The selected profile/provider-home mappings are verified
live (paired ``_path``/``_sha256`` files re-hashed) and then APPLIED to
the launch through provider-appropriate explicit lanes (claude_code
``--settings <path>``; ``provider_home_path`` → the harness's home carrier
env) or the material's sealed profile args/environment — validated to
reference exactly the selected paths — so the resumed harness loads the
verified bytes, never an ambient HOME/default profile; a selected
reference with no safe lane and no sealed application is typed-disabled
before effects.  The stored trusted project root is validated before
effects (canonical real directory, Codex only) and passed exactly to
managed terminal creation.

Outcomes are durable and bounded: response loss and restart return the
same successor terminal id/generation and the recorded outcome —
``accepted`` and ``reconciliation-required`` are write-once final, so an
ambiguous physical result is never hidden by a later call.  Stop remains
a hard boundary: an effect intent that committed just before the barrier
stays recorded for M3-C drain/force-reap; a barrier that won first
admits no later effect; a barrier that lands while launch/verification
is already in flight records the observed resource, rechecks the gate
transactionally before the roster bind, and returns
reconciliation-required without binding or admitting anything.  Only the
operator resumes a stopped campaign.

No lock — Python, file, or database — is ever held over provider or tmux
I/O: every journal/reservation/result call commits a short transaction
and returns before the physical act it authorized.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.providers.codex import CodexRoute, codex_route_suffix
from cli_agent_orchestrator.services import (
    claude_native_launch,
)
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import (
    native_attachment,
    native_attachment_recovery,
    native_tui_launch,
    operation_journal,
    provider_contracts,
    restore_contract,
)
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import (
    terminal_service,
)
from cli_agent_orchestrator.utils.terminal import generate_terminal_id, managed_window_name

#: Versioned identity of the executor's result records.
SCHEMA_VERSION = "cao-m3-exact-executor-v1"

#: The durable bounded outcomes, re-exported from the journal so callers
#: of the executor read one coherent vocabulary.
OUTCOME_PENDING = operation_journal.RESULT_PENDING
OUTCOME_ACCEPTED = operation_journal.RESULT_ACCEPTED
OUTCOME_REFUSED = operation_journal.RESULT_REFUSED
OUTCOME_RECONCILIATION_REQUIRED = operation_journal.RESULT_RECONCILIATION_REQUIRED

#: Deterministic effect ids: one namespace, derived from the exact
#: (operation, step) pair so a response-loss retry replays the SAME id and
#: the journal's exact-replay adoption path takes over — never a second
#: effect id for one logical step.
_EFFECT_NAMESPACE = uuid.UUID("03780000-b3ef-4acc-a7e3-000000000003")

MAX_EXTRA_ARGS = 32
MAX_EXTRA_ARG_LEN = 512
MAX_EXTRA_ARGS_BYTES = 8192
MAX_ENV_KEYS = 32
MAX_ENV_VALUE_LEN = 4096
MAX_ENV_BYTES = 16384
MAX_PROVIDER_VERSION_LEN = 128

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Terminal-id surfaces that can own an 8-hex id.  The successor
#: allocation checks every one of them plus the operation store's unique
#: successor index.
_SUCCESSOR_ID_ALLOCATION_ATTEMPTS = 128

#: Per-operation in-process execution locks.  A concurrent duplicate
#: executor run for the SAME operation (two supervisor tasks racing one
#: resurrection) serializes here so exactly one performs the physical
#: sequence and the other adopts the durable outcome — the same
#: at-most-once contract the managed launch's ``claim_launch`` state CAS
#: provides.  Keyed by operation id; the number of live operations on
#: one process is bounded by the roster.
_execute_locks: dict[str, asyncio.Lock] = {}
_execute_locks_guard = threading.Lock()


def _operation_lock(operation_id: str) -> asyncio.Lock:
    with _execute_locks_guard:
        lock = _execute_locks.get(operation_id)
        if lock is None:
            lock = asyncio.Lock()
            _execute_locks[operation_id] = lock
        return lock


class ExactExecutorError(RuntimeError):
    """Base error for the exact executor."""

    code = "exact-executor-error"


class ExactExecutorInvalid(ExactExecutorError):
    """A supplied launch-material value is malformed or unbounded."""

    code = "exact-executor-invalid"


class ExactExecutorRefused(ExactExecutorError):
    """A typed disabled/refused outcome with zero physical effect.

    Refusals are observations about the current world (a live competing
    owner, a claimed barrier, an unproven required fact); a later attempt
    re-evaluates them.  The durable ``refused`` result is recorded so a
    response-loss caller can read the same outcome back.
    """

    code = "exact-executor-refused"


class ExactExecutorConflict(ExactExecutorError):
    """The request conflicts with durable identity (wrong source, contract,
    or a replacement incarnation the teardown must preserve)."""

    code = "exact-executor-conflict"


class ExactExecutorReconciliation(ExactExecutorError):
    """An ambiguous physical outcome; the operation is durably
    reconciliation-required and is never silently retried by B3.

    M3-C adopts/drains or force-reaps the preserved in-flight intent; the
    frozen attachment stays frozen until an operator adjudicates it.
    """

    code = "exact-executor-reconciliation-required"


class ExactExecutorUnavailable(ExactExecutorError):
    """A store the executor depends on could not be read or written."""

    code = "exact-executor-unavailable"


class _AdoptedAccepted(RuntimeError):
    """Internal control flow carrying a concurrently finalized acceptance.

    Result recording is a DB CAS and may adopt another process's write-once
    final result.  Call sites are otherwise about to raise their local
    refusal/reconciliation observation, so acceptance crosses those frames as
    this private signal and the public executor rebuilds the durable response.
    """

    def __init__(self, operation: dict[str, Any]) -> None:
        super().__init__("the operation was concurrently finalized as accepted")
        self.operation = operation


@dataclass(frozen=True)
class LaunchMaterial:
    """The explicit, bounded, ephemeral launch material of one restore.

    Everything here is caller-supplied and validated: B3 never silently
    rebuilds material from ambient ``PATH``, ``HOME``, mutable profile
    files, or an unverified route.  ``environment`` / ``route_environment``
    values may carry provider-carrier material and are therefore NEVER
    stored — only key names and a digest of the mapping reach the durable
    evidence.

    ``profile_material`` / ``provider_home`` are the SELECTED
    reference/digest mappings (the same no-secret shape as the B1 contract
    facts): absent means "the contract's recorded material".  A supplied
    mapping is schema-validated, digest-compared against the contract's
    recorded fact, and its paired ``_path``/``_sha256`` files verified live;
    a digest difference is a variation only the operation's exact
    compatibility cell may cover.  The selected mapping is then APPLIED to
    the launch: known keys ride provider-appropriate explicit lanes (e.g.
    claude_code ``--settings <path>``, ``provider_home_path`` → the
    harness's home env), and any key without a defined lane must be carried
    by the sealed ``profile_args``/``profile_environment`` — whose
    referenced paths are validated to be exactly the selected mapping's —
    or the launch is typed-disabled before any physical effect.

    ``route_args`` / ``route_environment`` are the cell-bound route
    material: lawful only for a cell-covered route variation the harness's
    normal validator cannot attest (a non-Anthropic model routed through
    the claude_code harness); never accepted on an exact restore.
    """

    extra_args: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    provider_version: Optional[str] = None
    profile_material: Optional[Mapping[str, str]] = None
    provider_home: Optional[Mapping[str, str]] = None
    profile_args: tuple[str, ...] = ()
    profile_environment: Mapping[str, str] = field(default_factory=dict)
    route_args: tuple[str, ...] = ()
    route_environment: Mapping[str, str] = field(default_factory=dict)


def _bounded_args(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        args: tuple[str, ...] = ()
    elif isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExactExecutorInvalid(f"{field} must be a sequence of argument strings")
    else:
        args = tuple(value)
    if len(args) > MAX_EXTRA_ARGS:
        raise ExactExecutorInvalid(
            f"{field} may carry at most {MAX_EXTRA_ARGS} entries; got {len(args)}"
        )
    for arg in args:
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            raise ExactExecutorInvalid(
                f"{field} entries must be non-empty NUL-free strings; got {arg!r}"
            )
        if len(arg) > MAX_EXTRA_ARG_LEN:
            raise ExactExecutorInvalid(
                f"{field} entries must be at most {MAX_EXTRA_ARG_LEN} characters"
            )
    if sum(len(arg) + 1 for arg in args) > MAX_EXTRA_ARGS_BYTES:
        raise ExactExecutorInvalid(
            f"{field} must serialize to at most {MAX_EXTRA_ARGS_BYTES} bytes"
        )
    return args


def _bounded_env(value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        environment: dict[str, str] = {}
    elif not isinstance(value, Mapping):
        raise ExactExecutorInvalid(f"{field} must be a mapping of environment strings")
    else:
        environment = dict(value)
    if len(environment) > MAX_ENV_KEYS:
        raise ExactExecutorInvalid(
            f"{field} may carry at most {MAX_ENV_KEYS} entries; got {len(environment)}"
        )
    total = 0
    for key, item in environment.items():
        if not isinstance(key, str) or _ENV_KEY_RE.fullmatch(key) is None:
            raise ExactExecutorInvalid(f"{field} key {key!r} must match {_ENV_KEY_RE.pattern}")
        if not isinstance(item, str) or "\x00" in item:
            raise ExactExecutorInvalid(f"{field}.{key} must be a NUL-free string")
        if len(item) > MAX_ENV_VALUE_LEN:
            raise ExactExecutorInvalid(
                f"{field}.{key} must be at most {MAX_ENV_VALUE_LEN} characters"
            )
        total += len(key) + len(item)
    if total > MAX_ENV_BYTES:
        raise ExactExecutorInvalid(f"{field} must serialize to at most {MAX_ENV_BYTES} bytes")
    return environment


def _reference_mapping(value: Any, *, field: str) -> Optional[dict[str, str]]:
    """The supplied profile/provider-home selection: the same closed
    no-secret reference shape the B1 contract validates."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ExactExecutorInvalid(f"{field} must be a mapping of references/digests")
    try:
        return restore_contract._validate_reference_dict(dict(value), field=field)
    except (restore_contract.RestoreContractError, TypeError, ValueError) as exc:
        raise ExactExecutorInvalid(f"{field}: {exc}") from exc


def _validate_launch_material(material: LaunchMaterial) -> LaunchMaterial:
    extra = _bounded_args(material.extra_args, field="extra_args")
    profile_args = _bounded_args(material.profile_args, field="profile_args")
    route_args = _bounded_args(material.route_args, field="route_args")
    environment = _bounded_env(material.environment, field="environment")
    profile_environment = _bounded_env(material.profile_environment, field="profile_environment")
    route_environment = _bounded_env(material.route_environment, field="route_environment")
    profile_material = _reference_mapping(material.profile_material, field="profile_material")
    provider_home = _reference_mapping(material.provider_home, field="provider_home")
    provider_version = material.provider_version
    if provider_version is not None:
        if not isinstance(provider_version, str) or not provider_version:
            raise ExactExecutorInvalid("provider_version must be a non-empty string or None")
        if len(provider_version) > MAX_PROVIDER_VERSION_LEN:
            raise ExactExecutorInvalid(
                f"provider_version must be at most {MAX_PROVIDER_VERSION_LEN} characters"
            )
    return LaunchMaterial(
        extra_args=extra,
        environment=environment,
        provider_version=provider_version,
        profile_material=profile_material,
        provider_home=provider_home,
        profile_args=profile_args,
        profile_environment=profile_environment,
        route_args=route_args,
        route_environment=route_environment,
    )


def _environment_evidence(environment: Mapping[str, str]) -> dict[str, str]:
    """Bounded, value-free evidence for a forwarded environment.

    Key names are structural facts; values could carry provider-carrier
    secrets, so only a digest of the full mapping is recorded.
    """
    if not environment:
        return {}
    canonical = "\x00".join(f"{key}={environment[key]}" for key in sorted(environment))
    keys = ",".join(sorted(environment))
    evidence = {
        "environment_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    if len(keys) <= operation_journal.MAX_EFFECT_PAYLOAD_VALUE_LEN:
        evidence["environment_keys"] = keys
    else:
        evidence["environment_keys_digest"] = hashlib.sha256(keys.encode("utf-8")).hexdigest()
    return evidence


def _launch_material_digest(
    argv: Sequence[str], environment: Mapping[str, str], provider_version: Optional[str]
) -> str:
    """Value-free identity of the exact ephemeral launch material.

    Carrier values may contain provider secrets, so effect intents persist
    only this canonical digest.  Binding it to the acquire/launch intents
    makes response-loss adoption reject a later retry whose argv, environment,
    or identity-proof version differs from the already-authorized attempt.
    """
    canonical = json.dumps(
        {
            "argv": list(argv),
            "environment": dict(environment),
            "provider_version": provider_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _effect_id(operation_id: str, step: str) -> str:
    return str(uuid.uuid5(_EFFECT_NAMESPACE, f"{operation_id}:{step}"))


def _previous_step(step: str) -> str:
    order = operation_journal._EFFECT_STEP_ORDER
    return order[order.index(step) - 1]


# ---------------------------------------------------------------------------
# required B1 launch facts (typed decoder/refusal gate)
# ---------------------------------------------------------------------------


def _fact_refusal(contract: restore_contract.RestoreContract) -> Optional[str]:
    """Why this stored contract cannot authorize an exact resume launch.

    Everything checked here is a BEFORE-effect gate: a required fact that
    is ``unavailable``/``missing``, a canonical working directory that no
    longer exists, or an executable whose recorded path or digest cannot
    be matched is a typed disabled outcome with zero physical effects.
    """
    if contract.native_session_id is None:
        return (
            "the restore contract records no native session id (identity_missing "
            "lineage); an exact same-native-session restore is impossible without it"
        )
    executable = contract.executable
    if executable.state != restore_contract.FACT_PRESENT:
        return (
            f"the restore contract's executable identity is {executable.state!r}"
            + (f" ({executable.reason})" if executable.reason else "")
            + "; an exact restore requires the present, digest-matched executable"
        )
    value = executable.value or {}
    path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        return "the restore contract's executable fact carries no path/digest pair"
    if not os.path.isfile(path):
        return f"the recorded executable no longer exists at {path}"
    observed = native_tui_launch._sha256_file(path)
    if observed != digest:
        return (
            f"the recorded executable digest does not match the bytes at {path}; "
            "refusing to launch a provider whose image is not the one that was admitted"
        )
    working_directory = contract.working_directory
    if not os.path.isdir(working_directory):
        return f"the recorded canonical working directory no longer exists: " f"{working_directory}"
    return None


def _stored_executable_version(contract: restore_contract.RestoreContract) -> Optional[str]:
    """The recorded executable version fact, when the contract carries one."""
    if contract.executable.state != restore_contract.FACT_PRESENT:
        return None
    value = contract.executable.value or {}
    version = value.get("version")
    return version if isinstance(version, str) else None


def _variations(
    request: operation_journal.OperationRequest,
    contract: restore_contract.RestoreContract,
    material: LaunchMaterial,
) -> list[str]:
    """Every way the requested launch varies from the recorded contract.

    Exact restoration — requested facts equal the contract's recorded facts
    and the material selects the contract's recorded profile/home/version —
    produces an empty list.  Anything else is a variation that only the
    operation's exact compatibility cell may cover.
    """
    variations: list[str] = []
    if request.model_requested is not None and request.model_requested != _present_value(
        contract.model
    ):
        variations.append(f"model {request.model_requested!r}")
    if request.effort_requested is not None and request.effort_requested != _present_value(
        contract.effort
    ):
        variations.append(f"effort {request.effort_requested!r}")
    requested_mode = request.execution_mode_requested or contract.execution_mode
    if requested_mode != contract.execution_mode:
        variations.append(f"execution mode {requested_mode!r}")
    if request.route_provider is not None and request.route_provider != contract.provider:
        variations.append(f"route {request.route_provider!r}")
    if material.profile_material is not None and _reference_dict_digest(
        material.profile_material
    ) != _present_fact_digest(contract.profile_material):
        variations.append("profile material")
    if material.provider_home is not None and _reference_dict_digest(
        material.provider_home
    ) != _present_fact_digest(contract.provider_home_facts):
        variations.append("provider-home material")
    if (
        material.provider_version is not None
        and material.provider_version != _stored_executable_version(contract)
    ):
        variations.append("provider version")
    return variations


def _carrier_cell_reasons(material: LaunchMaterial) -> list[str]:
    """Explicit carrier material that requires an exact compatibility cell.

    B1 cannot persist secret-bearing argv/environment values.  B3 therefore
    accepts them ephemerally, but only under the operation's harness-named
    compatibility receipt rather than treating arbitrary caller material as
    an exact-contract fact.
    """
    reasons: list[str] = []
    if material.extra_args or material.environment:
        reasons.append("explicit launch carrier material")
    if material.profile_args or material.profile_environment:
        reasons.append("explicit profile carrier material")
    if material.route_args or material.route_environment:
        reasons.append("explicit route carrier material")
    return reasons


def _cell_refusal(
    request: operation_journal.OperationRequest,
    variations: list[str],
) -> Optional[str]:
    """Why the named variations are not authorized.

    Any variation requires the operation to name the exact bounded cell
    ref/digest, and the ref must at least name the harness the launch
    material is for.  B3 records the cell facts; it never infers that a
    cell passes (B4 owns canaries).
    """
    if not variations:
        return None
    if request.compatibility_cell_ref is None or request.compatibility_cell_digest is None:
        return (
            "the requested launch varies from the recorded contract ("
            + ", ".join(variations)
            + ") without naming an exact compatibility cell; an unproven "
            "route/model/effort/mode/profile/home/version variation is typed-disabled, "
            "not inferred"
        )
    if request.harness not in request.compatibility_cell_ref:
        return (
            f"compatibility cell {request.compatibility_cell_ref!r} does not name the "
            f"launch harness {request.harness!r}; the launch material does not agree "
            "with the recorded cell"
        )
    return None


def _trust_refusal(
    request: operation_journal.OperationRequest, contract: restore_contract.RestoreContract
) -> Optional[str]:
    """Why the stored trusted project root cannot authorize this launch.

    A trusted project root applies ONLY to the Codex harness; a contract
    carrying one for any other harness is a fact this launch cannot honor
    and refuses rather than drops.  For Codex the root must still be the
    canonical real directory the contract recorded — a root that drifted
    (removed, or now resolving through a new symlink) is changed launch
    material, refused before any physical effect.
    """
    root = contract.trusted_project_root
    if root is None:
        return None
    if request.harness != "codex":
        return (
            f"the restore contract records trusted project root {root!r}, but trusted "
            f"project roots apply only to the codex harness, not {request.harness!r}; "
            "an exact restore never silently drops a recorded trust fact"
        )
    if not os.path.isdir(root):
        return (
            f"the recorded trusted project root no longer exists as a directory: {root}; "
            "the Codex launch would trust a root that is not the one that was admitted"
        )
    if os.path.realpath(root) != root:
        return (
            f"the recorded trusted project root is no longer canonical: {root!r} now "
            f"resolves to {os.path.realpath(root)!r}"
        )
    return None


def _reference_dict_digest(value: Mapping[str, str]) -> str:
    """The canonical digest of one reference mapping (references/digests in,
    one hash out — never values beyond what the mapping already holds)."""
    canonical = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _present_fact_digest(fact: restore_contract.ContractFact) -> Optional[str]:
    """The canonical digest of a present reference-dict fact, else None."""
    if fact.state != restore_contract.FACT_PRESENT:
        return None
    value = fact.value
    if not isinstance(value, Mapping):
        return None
    return _reference_dict_digest(value)


def _reference_live_drift(field_name: str, mapping: Optional[Mapping[str, str]]) -> Optional[str]:
    """Live drift of one selected reference mapping's paired path/digest
    files.

    Every ``<stem>_path`` with a paired ``<stem>_sha256`` names a regular
    file whose bytes the harness will read at launch; the file must still
    exist and re-hash to the recorded digest.  An edited profile or home
    file is changed launch material: B3 refuses rather than launching it.
    Unpaired paths are checked for existence only when the launch applies
    them (see ``_apply_reference_material``).
    """
    for key, item in (mapping or {}).items():
        if not key.endswith("_path"):
            continue
        recorded = (mapping or {}).get(f"{key[: -len('_path')]}_sha256")
        if recorded is None:
            continue
        if not os.path.isfile(item):
            return (
                f"the selected {field_name} reference {key} no longer exists "
                f"at {item}; the launch would read ambient state instead of the selected file"
            )
        if native_tui_launch._sha256_file(item) != recorded:
            return (
                f"the selected {field_name} reference {key} drifted: the bytes "
                f"at {item} no longer match the recorded digest; refusing to launch "
                "changed material (M3-F fallback preserves forward motion)"
            )
    return None


def _explicit_profile_refusal(
    selected_profile: Optional[Mapping[str, str]],
    selected_home: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Refuse a process-start restore with no explicit profile/home path.

    Native session identity preserves the conversation, not the configuration
    bytes a CLI reads when its replacement process starts.  At least one B1
    selected path must therefore reach an explicit provider lane (or a sealed
    path carrier); otherwise the only possible source is ambient HOME/default
    configuration.
    """
    if any(
        key.endswith("_path")
        for mapping in (selected_profile, selected_home)
        for key in (mapping or {})
    ):
        return None
    return (
        "the restore contract supplies no selected profile/provider-home path; "
        "a replacement CLI would read ambient HOME/default profile material, so "
        "exact restore is typed-disabled before physical effects"
    )


#: Harnesses whose exact resume may not pick a provider-default model
#: silently: the effective model must be present and pinned on the argv.
#: Codex is the documented exception — an omitted model is the provider
#: default, and its bootstrap records the actual (:class:`CodexRoute`).
_MODEL_PIN_REQUIRED = frozenset({"claude_code", "kimi_cli", "muse_cli"})

#: Route flags/env keys the executor derives from the bound request/contract
#: facts.  Launch material restating any of them can never be shown to agree
#: with the effective route (two spellings of one knob is precedence, not
#: agreement), so it refuses before any physical effect.
_ROUTE_ARGV_OWNERS = {
    "claude_code": ("--model", "--effort"),
    "kimi_cli": ("--model",),
    "muse_cli": ("--model", "--reasoning-effort"),
    "codex": ("--model",),
}
_ROUTE_ENV_OWNERS = {"kimi_cli": ("KIMI_MODEL_THINKING_EFFORT",)}

#: The reference keys the executor can apply to the resumed harness itself,
#: per harness — the provider-appropriate explicit lane for that exact
#: file/directory.  A selected ``_path`` reference outside this vocabulary
#: has no safe generic mapping: it must reach the launch through the
#: material's sealed profile args/environment (validated against the
#: selected mapping), or the launch is typed-disabled before effects.
#: claude_code reads ``--settings <file>`` for its profile config (the same
#: lane claude_native_readiness uses) and ``CLAUDE_CONFIG_DIR`` for its
#: home; kimi/codex take their documented home carriers.
_PROFILE_PATH_ARGV_LANES = {
    "claude_code": {"profile_config_path": "--settings"},
}
_HOME_PATH_ENV_LANES = {
    "claude_code": {"provider_home_path": "CLAUDE_CONFIG_DIR"},
    "kimi_cli": {"provider_home_path": "KIMI_CODE_HOME"},
    "codex": {"provider_home_path": "CODEX_HOME"},
}


def _apply_reference_material(
    harness: str,
    selected_profile: Optional[Mapping[str, str]],
    selected_home: Optional[Mapping[str, str]],
    material: LaunchMaterial,
) -> tuple[list[str], dict[str, str]]:
    """Apply the selected profile/home reference mappings to the launch.

    Returns ``(profile_argv, profile_env)``.  Every selected ``_path``
    either rides its provider-appropriate explicit lane or must be
    referenced by the material's sealed profile args/environment; a
    selected path that reaches neither is a typed pre-effect refusal (the
    resumed harness would otherwise read ambient state), and a sealed path
    outside the selected mapping is never launched.  Raises
    :class:`ExactExecutorRefused` on any disagreement.
    """
    profile_argv: list[str] = []
    profile_env: dict[str, str] = {}
    unapplied: dict[str, str] = {}
    for mapping, lanes, kind in (
        (selected_profile, _PROFILE_PATH_ARGV_LANES.get(harness, {}), "profile_material"),
        (selected_home, _HOME_PATH_ENV_LANES.get(harness, {}), "provider_home"),
    ):
        for key, path in (mapping or {}).items():
            if not key.endswith("_path"):
                continue
            lane = lanes.get(key)
            if lane is not None and kind == "profile_material":
                if not os.path.isfile(path):
                    raise ExactExecutorRefused(
                        f"the selected profile_material reference {key} no longer exists "
                        f"at {path}; the explicit {lane} lane would point at nothing"
                    )
                profile_argv += [lane, path]
            elif lane is not None:
                if not os.path.isdir(path):
                    raise ExactExecutorRefused(
                        f"the selected provider_home reference {key} no longer exists as "
                        f"a directory at {path}; the explicit {lane} carrier would point "
                        "at nothing"
                    )
                profile_env[lane] = path
            else:
                unapplied[f"{kind}.{key}"] = path
    sealed_paths: set[str] = set()
    for arg in material.profile_args:
        if os.path.isabs(arg):
            sealed_paths.add(os.path.realpath(arg))
    for value in material.profile_environment.values():
        if os.path.isabs(value):
            sealed_paths.add(os.path.realpath(value))
    missing = sorted(label for label, path in unapplied.items() if path not in sealed_paths)
    if missing:
        raise ExactExecutorRefused(
            f"the selected reference(s) {missing} have no provider-appropriate lane for "
            f"{harness!r} and are not carried by the material's sealed profile "
            "args/environment; an ambient HOME/default profile can never satisfy the "
            "explicit contract, so this cell is typed-disabled before any physical effect"
        )
    selected_paths = {
        path
        for mapping in (selected_profile, selected_home)
        for key, path in (mapping or {}).items()
        if key.endswith("_path")
    }
    foreign = sorted(sealed_paths - selected_paths)
    if foreign:
        raise ExactExecutorRefused(
            f"the sealed profile material references path(s) {foreign} outside the "
            "selected mapping; only the verified selected references may reach the launch"
        )
    return profile_argv, profile_env


def _route_pin(
    harness: str,
    model: Optional[str],
    effort: Optional[str],
    *,
    route_args: Sequence[str] = (),
    route_environment: Optional[Mapping[str, str]] = None,
    cell_covered: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """The provider-aware argv/env pinning of the effective route.

    The model/effort are the requested-or-stored effective facts; each
    harness pins them in its own native pattern (the same patterns the
    managed launcher uses), so the resumed pane cannot come up on an
    unpinned or different configuration.  A pin-requiring harness with no
    effective model — or an unpinnable one — is a typed pre-effect refusal,
    never an ambient launch.

    ``route_args``/``route_environment`` are the cell-bound route material:
    lawful only for claude_code when the variation is covered by the exact
    compatibility cell AND the Anthropic validator cannot attest the
    effective model (a DeepSeek/GLM-style route through the claude harness).
    The material must pin the effective model explicitly — exactly one
    ``--model <effective>`` — carry no executor-owned effort flag, no
    identity option, and no repeated flag (precedence is not agreement).
    Returns ``(argv_prefix, env_overlay)``.
    """
    selected_effort = effort if provider_contracts.route_selects_effort(effort) else None
    route_args = tuple(route_args or ())
    route_environment = dict(route_environment or {})
    if harness == "claude_code":
        if model is None:
            raise ExactExecutorRefused(
                "the effective model is unknown (no stored fact, none requested) and "
                "claude_code requires an explicit pinned model; exact restore is disabled "
                "rather than launching the provider's ambient default"
            )
        try:
            pinned = claude_native_launch.validate_requested_model(model)
        except claude_native_launch.ClaudeNativeLaunchError:
            pinned = None
        if pinned is not None:
            if route_args or route_environment:
                raise ExactExecutorRefused(
                    f"the effective model {model!r} is attestable by the harness's own "
                    "validator; explicit route material is accepted only for a "
                    "cell-covered route it cannot attest — never as a shadow pin"
                )
            args = [claude_native_launch.MODEL_OPTION, pinned]
            if selected_effort is not None:
                args += ["--effort", str(selected_effort)]
            return args, {}
        # The normal validator cannot attest this route: only an exact
        # cell-covered variation with explicit route material may proceed.
        if not cell_covered:
            raise ExactExecutorRefused(
                f"the effective model {model!r} is not a pinnable claude_code route and "
                "no exact compatibility cell covers the variation"
            )
        if not route_args:
            raise ExactExecutorRefused(
                f"the effective model {model!r} is not attestable by the harness's own "
                "validator; the cell-covered route requires explicit, bounded route "
                "args that pin it — an ambient or default route is never launched"
            )
        seen: dict[str, int] = {}
        index = 0
        args = list(route_args)
        while index < len(args):
            flag = args[index]
            if not flag.startswith("--"):
                raise ExactExecutorRefused(
                    f"route_args[{index}]={flag!r} is a positional value; route material "
                    "must be (flag, value) pairs so no precedence ambiguity is possible"
                )
            if flag in claude_native_launch.FORBIDDEN_OPTIONS or flag in (
                claude_native_launch.LAUNCH_OPTION,
                *claude_native_launch.RESUME_OPTION_ALIASES,
            ):
                raise ExactExecutorRefused(
                    f"route_args[{index}]={flag!r} is an identity/recency option; the "
                    "resumed identity is owned by the launch seam, never by route material"
                )
            if flag == "--effort":
                raise ExactExecutorRefused(
                    "route_args restate the executor-owned --effort pin; effort is "
                    "harness-level and stays derived from the bound facts"
                )
            seen[flag] = seen.get(flag, 0) + 1
            if seen[flag] > 1:
                raise ExactExecutorRefused(
                    f"route_args repeat {flag!r}; a repeated flag is option precedence, "
                    "not agreement"
                )
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                raise ExactExecutorRefused(
                    f"route_args[{index}]={flag!r} requires exactly one value"
                )
            index += 2
        if seen.get("--model") != 1 or args[args.index("--model") + 1] != model:
            raise ExactExecutorRefused(
                f"route_args must pin the effective model exactly once "
                f"(--model {model!r}); the cell covers that route and nothing else"
            )
        if selected_effort is not None:
            args += ["--effort", str(selected_effort)]
        return args, dict(route_environment)
    if route_args or route_environment:
        raise ExactExecutorRefused(
            f"explicit route material is mapped only for the claude_code harness; "
            f"{harness!r} has no safe mapping for it, so the launch is typed-disabled"
        )
    if harness == "kimi_cli":
        if model is None:
            raise ExactExecutorRefused(
                "the effective model is unknown (no stored fact, none requested) and "
                "kimi_cli requires an explicit pinned model; exact restore is disabled "
                "rather than launching the provider's ambient default"
            )
        args = ["--model", model]
        return args, provider_contracts.kimi_effort_env(selected_effort)
    if harness == "muse_cli":
        if model is None:
            raise ExactExecutorRefused(
                "the effective model is unknown (no stored fact, none requested) and "
                "muse_cli requires an explicit pinned model; exact restore is disabled "
                "rather than launching the provider's ambient default"
            )
        args = ["--model", model]
        if selected_effort is not None:
            args += ["--reasoning-effort", str(selected_effort)]
        return args, {}
    if harness == "codex":
        return (
            codex_route_suffix(CodexRoute(model=model or None, effort=selected_effort)),
            {},
        )
    raise ExactExecutorRefused(
        f"no exact-resume route pinning is implemented for harness {harness!r}; "
        f"implemented: {sorted(_MODEL_PIN_REQUIRED | {'codex'})}"
    )


def _material_agreement_refusal(
    request: operation_journal.OperationRequest,
    material: LaunchMaterial,
    *,
    profile_argv: Sequence[str],
    profile_env: Mapping[str, str],
    route_env: Mapping[str, str],
) -> Optional[str]:
    """Why the supplied launch material cannot agree with the bound launch.

    The route flags/env and the applied profile lanes are owned by the
    executor's derivation from the bound facts; material restating any of
    them introduces a second spelling whose effective value would depend on
    provider option precedence.  Caller env layers must also be disjoint —
    a silent later-wins merge is precedence, not agreement.
    """
    harness = request.harness
    route_owner_flags = set(_ROUTE_ARGV_OWNERS.get(harness, ()))
    applied_profile_flags = {arg for arg in profile_argv if arg.startswith("-")}
    route_material_flags = {arg for arg in material.route_args if arg.startswith("-")}
    profile_material_flags = {arg for arg in material.profile_args if arg.startswith("-")}
    route_profile_overlap = route_material_flags & applied_profile_flags
    if route_profile_overlap:
        return (
            f"launch material route_args restate applied profile flag(s) "
            f"{sorted(route_profile_overlap)}; route/profile precedence is not agreement"
        )
    for label, args, owned_argv in (
        (
            "extra_args",
            material.extra_args,
            route_owner_flags
            | applied_profile_flags
            | route_material_flags
            | profile_material_flags,
        ),
        (
            "profile_args",
            material.profile_args,
            route_owner_flags | applied_profile_flags | route_material_flags,
        ),
    ):
        extra = list(args)
        for index, arg in enumerate(extra):
            if arg in owned_argv:
                return (
                    f"launch material {label} restate the executor-owned flag {arg!r}; "
                    "the effective route and profile lanes are pinned from the bound "
                    "request/contract facts and material carrying them cannot be shown "
                    "to agree"
                )
            if harness == "codex" and arg == "-c":
                override = extra[index + 1] if index + 1 < len(extra) else ""
                if override.startswith("model=") or override.startswith("model_reasoning_effort="):
                    return (
                        f"launch material {label} override the executor-owned codex route "
                        f"via {override!r}; the effective route is pinned from the bound facts"
                    )
    overlap = set(profile_env) & set(route_env)
    if overlap:
        return (
            f"launch material route_environment overlaps the selected provider-home "
            f"lane(s) {sorted(overlap)}; silently overwriting either value would make "
            "accepted material differ from the process environment"
        )
    owned_env = set(_ROUTE_ENV_OWNERS.get(harness, ()))
    owned_env |= set(profile_env) | set(route_env)
    for key in material.environment:
        if key in owned_env:
            return (
                f"launch material environment sets the executor-owned launch key {key!r}; "
                "the effective route/home lanes are pinned from the bound facts"
            )
    overlap = set(material.environment) & set(material.profile_environment)
    if overlap:
        return (
            f"launch material sets {sorted(overlap)} in both environment and "
            "profile_environment; which value the launch sees would depend on merge order"
        )
    for key in material.profile_environment:
        if key in owned_env:
            return (
                f"launch material profile_environment sets the executor-owned launch key "
                f"{key!r}; the effective route/home lanes are pinned from the bound facts"
            )
    return None


def _present_value(fact: restore_contract.ContractFact) -> Optional[str]:
    if fact.state != restore_contract.FACT_PRESENT:
        return None
    value = fact.value
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# successor terminal-id allocation (every surface that can own the 8-hex id)
# ---------------------------------------------------------------------------


def _successor_id_taken(db: Any, candidate: str) -> bool:
    """Whether ANY current terminal/reservation surface owns the id.

    Checked cooperatively and fail-closed: a surface that cannot be read
    counts as taken, so allocation moves to the next candidate rather
    than guessing.  The operation store's unique successor index is the
    durable backstop for races this read cannot see.
    """

    def _query(model: Any, column: Any) -> bool:
        try:
            return db.query(model).filter(column == candidate).first() is not None
        except Exception:  # noqa: BLE001 - an unreadable surface fails closed
            return True

    return (
        _query(database.TerminalModel, database.TerminalModel.id)
        or _query(
            database.ManagedLaunchReservationModel,
            database.ManagedLaunchReservationModel.terminal_id,
        )
        or _query(
            database.ManagedLaunchV2ReservationModel,
            database.ManagedLaunchV2ReservationModel.terminal_id,
        )
        or _query(
            database.ManagedLaunchV2TerminalModel,
            database.ManagedLaunchV2TerminalModel.id,
        )
        or _query(
            database.ReincarnationOperationModel,
            database.ReincarnationOperationModel.successor_terminal_id,
        )
    )


def _allocate_successor(db: Any, prior_generation: Optional[str]) -> tuple[str, str]:
    """One fresh 8-hex successor terminal id and one fresh canonical UUID.

    The generation is fresh and must never equal the prior generation: a
    replacement incarnation is always a new generation.
    """
    for _attempt in range(_SUCCESSOR_ID_ALLOCATION_ATTEMPTS):
        candidate = generate_terminal_id()
        if _successor_id_taken(db, candidate):
            continue
        generation = str(uuid.uuid4())
        if generation != prior_generation:
            return candidate, generation
    raise ExactExecutorUnavailable("could not allocate a unique successor terminal id")


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------


class _SuccessorPaneTransport:
    """The default managed native pane transport for the successor pane.

    Mirrors the managed-launch pattern: ``create_pane`` creates the
    successor terminal through ``terminal_service.create_terminal`` with
    the reserved successor id/generation and the resume argv as the
    pane's OWN process (``managed_native_command``) — atomic window
    creation, no shell, no keystrokes — and observation/capture delegate
    to the real ``TmuxNativePane`` bound to the deterministic managed
    window name.
    """

    def __init__(
        self,
        *,
        session_name: str,
        terminal_id: str,
        generation: str,
        provider: str,
        agent_profile: str,
        working_directory: str,
        trusted_project_root: Optional[str],
        expected_model: Optional[str],
        expected_effort: Optional[str],
        environment: Mapping[str, str],
        registry: Any,
        loop: Any,
    ) -> None:
        self._session_name = session_name
        self._terminal_id = terminal_id
        self._generation = generation
        self._provider = provider
        self._agent_profile = agent_profile
        self._working_directory = working_directory
        self._trusted_project_root = trusted_project_root
        self._expected_model = expected_model
        self._expected_effort = expected_effort
        self._environment = dict(environment)
        self._registry = registry
        self._loop = loop
        self.window = managed_window_name(terminal_id, generation)

    def create_pane(self, *, argv: Sequence[str]) -> str:
        return asyncio.run_coroutine_threadsafe(self._create(list(argv)), self._loop).result()

    async def _create(self, argv: list[str]) -> str:
        await terminal_service.create_terminal(
            provider=self._provider,
            agent_profile=self._agent_profile,
            session_name=self._session_name,
            new_session=False,
            working_directory=self._working_directory,
            registry=self._registry,
            defer_init=False,
            initial_message=None,
            reserved_terminal_id=self._terminal_id,
            terminal_generation=self._generation,
            trusted_project_root=self._trusted_project_root,
            expected_model=self._expected_model,
            expected_effort=self._expected_effort,
            # A managed no-task launch preserves a generation whose launch
            # failed: the reservation is queryable and reconciliation owns
            # the cleanup decision.
            preserve_on_init_failure=True,
            # The TUI is the pane's OWN argv. Nothing is typed into a shell.
            managed_native_command=argv,
            env_vars=dict(self._environment),
            # A reserved managed generation persists ONLY on the isolated
            # managed-v2 surface (journal-first resource declaration, no
            # legacy terminal row) — never the legacy v1 plane.
            protocol_vintage="v2",
            # The pane runs the provider's own full-screen TUI; its status
            # comes from the native observer and the FIFO monitor is never
            # scheduled for it.
            native_status_source=True,
        )
        return self.window

    def _pane(self) -> Any:
        return native_tui_launch.TmuxNativePane(
            terminal_service.get_backend(),
            session_name=self._session_name,
            window_name=self.window,
            terminal_id=self._terminal_id,
        )

    def observe(self) -> Any:
        return self._pane().observe()

    def capture_render(self, pane_id: str) -> Any:
        return self._pane().capture_render(pane_id)


class _Execution:
    """One executor run's shared, bounded state."""

    def __init__(
        self,
        request: operation_journal.OperationRequest,
        material: LaunchMaterial,
        transport_factory: Optional[Callable[[], Any]],
        registry: Any,
    ) -> None:
        self.request = request
        self.material = material
        self.transport_factory = transport_factory
        self.registry = registry
        self.evidence: dict[str, str] = {}
        #: Whether the successor pane may physically exist from here on:
        #: set the moment the create_pane/launch_resume intent pair is
        #: authorized, because the atomic transport call follows at once.
        self.successor_physical = False
        #: The provider-aware route pinning and the applied profile/home
        #: lanes, derived from the bound facts (set by the pre-effect
        #: gates; consumed by the launch and the managed terminal).
        self.route_argv: list[str] = []
        self.route_env: dict[str, str] = {}
        self.profile_argv: list[str] = []
        self.profile_env: dict[str, str] = {}
        self.effective_model: Optional[str] = None
        self.effective_effort: Optional[str] = None
        self.effective_provider_version: Optional[str] = None

    # -- journal steps ----------------------------------------------------

    def step(self, step: str, payload: dict[str, str]) -> None:
        """Authorize (or adopt) the exact ordered intent for ``step``."""
        operation_journal.authorize_effect_intent(
            self.request.operation_id,
            effect_id=_effect_id(self.request.operation_id, step),
            effect_step=step,
            effect_payload=payload,
            expected_phase=_previous_step(step),
        )

    @staticmethod
    def _bounded_detail(detail: str) -> str:
        """The journal bounds result detail; an over-long exception message
        is truncated, never masked by a validation error that would skip the
        durable record entirely."""
        limit = operation_journal.MAX_RESULT_DETAIL_LEN
        if len(detail) > limit:
            return detail[: limit - len("...(truncated)")] + "...(truncated)"
        return detail

    def record_refused(self, detail: str) -> None:
        self._record_result(
            operation_journal.RESULT_REFUSED,
            detail,
        )

    def record_reconciliation(self, detail: str) -> None:
        self._record_result(
            operation_journal.RESULT_RECONCILIATION_REQUIRED,
            detail,
        )

    def _record_result(self, state: str, detail: str) -> None:
        """Record ``state`` or surface a concurrently won final outcome.

        ``record_result`` returns the effective stored operation.  Ignoring
        its ``adopted`` result would let this process raise a retryable local
        refusal (or a local ambiguity) after another process had already
        finalized acceptance/reconciliation.  The durable write-once winner
        is the response in that race.
        """
        recorded = operation_journal.record_result(
            self.request.operation_id,
            state,
            detail=self._bounded_detail(detail),
            evidence=dict(self.evidence),
        )
        if not recorded.get("adopted"):
            return
        operation = recorded["operation"]
        if operation["result_state"] == operation_journal.RESULT_ACCEPTED:
            raise _AdoptedAccepted(operation)
        if operation["result_state"] == operation_journal.RESULT_RECONCILIATION_REQUIRED:
            raise ExactExecutorReconciliation(
                operation["result_detail"] or "the operation is durably reconciliation-required"
            )


def _classify_journal_conflict(
    execution: _Execution, exc: operation_journal.OperationJournalError
) -> ExactExecutorError:
    """A seam refusal at a pre-pane boundary is a retryable typed refusal;
    after the successor pane may exist it is a durable reconciliation."""
    if execution.successor_physical:
        return ExactExecutorReconciliation(
            f"the Stop barrier/lifecycle gate refused an in-flight successor "
            f"effect: {exc}; the successor pane may exist and must be reconciled "
            "(M3-C drain/force-reap), never silently retried"
        )
    return ExactExecutorRefused(str(exc))


async def execute(
    request: operation_journal.OperationRequest,
    *,
    material: Optional[LaunchMaterial] = None,
    transport_factory: Optional[Callable[[], Any]] = None,
    registry: Any = None,
) -> dict[str, Any]:
    """Perform (or adopt) the one exact physical reincarnation.

    Returns the durable accepted outcome.  Raises a typed
    ``ExactExecutorError`` for every refused/reconciliation outcome; the
    durable ``result_state`` on the operation row carries the same truth
    for a response-loss caller.  Idempotent: an operation whose result is
    final returns/raises the recorded outcome without any new effect, and
    concurrent duplicate runs serialize on one per-operation lock so
    exactly one performs the physical sequence.
    """
    if not isinstance(request, operation_journal.OperationRequest):
        raise ExactExecutorInvalid(f"request must be an OperationRequest; got {request!r}")
    material = _validate_launch_material(material or LaunchMaterial())
    try:
        async with _operation_lock(request.operation_id):
            return await _execute_locked(request, material, transport_factory, registry)
    except _AdoptedAccepted as adopted:
        return _accepted_outcome(adopted.operation, request)


async def _execute_locked(
    request: operation_journal.OperationRequest,
    material: LaunchMaterial,
    transport_factory: Optional[Callable[[], Any]],
    registry: Any,
) -> dict[str, Any]:
    """The executor body, already serialized per operation."""

    # 1. Claim/adopt the winning operation.  The journal's own typed
    #    conflicts (wrong source/revision/contract, a stopped session, a
    #    claimed barrier) propagate unchanged: they are already the
    #    response-loss-safe refusal surface B2 defines.
    claim = operation_journal.claim_operation(request)
    operation = claim["operation"]

    # 2. A final durable outcome is the answer; never re-run it.
    if operation["result_state"] == operation_journal.RESULT_RECONCILIATION_REQUIRED:
        raise ExactExecutorReconciliation(
            operation["result_detail"] or "the operation is durably reconciliation-required"
        )
    if operation["result_state"] == operation_journal.RESULT_ACCEPTED:
        return _accepted_outcome(operation, request)

    # 3. The stored B1 contract, through its typed decoder/refusal gate.
    stored_contract = restore_contract.get_contract_by_incarnation(
        terminal_id=request.prior_terminal_id, generation=request.prior_generation
    )
    if stored_contract is None:
        raise ExactExecutorConflict(
            f"no immutable restore contract is recorded for source "
            f"{request.prior_terminal_id}/{request.prior_generation}"
        )
    contract = restore_contract.decode_stored_contract(stored_contract["contract"])
    if contract is None:
        execution = _Execution(request, material, transport_factory, registry)
        detail = (
            "the stored restore contract does not decode into a complete validated "
            "contract; it cannot authorize an exact resume"
        )
        execution.record_refused(detail)
        raise ExactExecutorRefused(detail)
    execution = _Execution(request, material, transport_factory, registry)
    execution.evidence["restore_contract_id"] = request.restore_contract_id
    execution.evidence["restore_contract_digest"] = request.restore_contract_digest

    # Every gate in this step is a BEFORE-effect boundary: a refusal here
    # leaves zero successor reservation, zero effect intents, zero panes.
    #
    # The SELECTED profile/home mappings: the material's supplied mapping
    # when present, else the contract's recorded fact.  The selected mapping
    # is what gets verified live AND what the launch itself will load.
    selected_profile = (
        material.profile_material
        if material.profile_material is not None
        else (
            dict(contract.profile_material.value)
            if contract.profile_material.state == restore_contract.FACT_PRESENT
            and isinstance(contract.profile_material.value, Mapping)
            else None
        )
    )
    selected_home = (
        material.provider_home
        if material.provider_home is not None
        else (
            dict(contract.provider_home_facts.value)
            if contract.provider_home_facts.state == restore_contract.FACT_PRESENT
            and isinstance(contract.provider_home_facts.value, Mapping)
            else None
        )
    )
    variations = _variations(request, contract, material)
    cell_reasons = variations + _carrier_cell_reasons(material)
    pre_effect_gates: list[Callable[[], Optional[str]]] = [
        lambda: _fact_refusal(contract),
        lambda: _trust_refusal(request, contract),
        lambda: _reference_live_drift("profile_material", selected_profile),
        lambda: _reference_live_drift("provider_home_facts", selected_home),
        lambda: _explicit_profile_refusal(selected_profile, selected_home),
        lambda: _cell_refusal(request, cell_reasons),
    ]
    for gate in pre_effect_gates:
        gate_refusal = gate()
        if gate_refusal is not None:
            execution.record_refused(gate_refusal)
            raise ExactExecutorRefused(gate_refusal)
    # The effective requested-or-stored route + identity-proof version,
    # computed ONCE and consumed by both the resume argv/env and the
    # managed terminal metadata.  A pin-requiring harness with an
    # unknown/unpinnable effective model is a typed refusal here — never
    # an unpinned ambient launch.
    effective_model = (
        request.model_requested
        if request.model_requested is not None
        else _present_value(contract.model)
    )
    effective_effort = (
        request.effort_requested
        if request.effort_requested is not None
        else _present_value(contract.effort)
    )
    try:
        route_argv, route_env = _route_pin(
            request.harness,
            effective_model,
            effective_effort,
            route_args=material.route_args,
            route_environment=material.route_environment,
            cell_covered=bool(cell_reasons),
        )
        profile_argv, profile_env = _apply_reference_material(
            request.harness, selected_profile, selected_home, material
        )
    except ExactExecutorRefused as exc:
        execution.record_refused(str(exc))
        raise
    agreement_refusal = _material_agreement_refusal(
        request,
        material,
        profile_argv=profile_argv,
        profile_env=profile_env,
        route_env=route_env,
    )
    if agreement_refusal is not None:
        execution.record_refused(agreement_refusal)
        raise ExactExecutorRefused(agreement_refusal)
    execution.route_argv = route_argv
    execution.route_env = route_env
    execution.profile_argv = profile_argv
    execution.profile_env = profile_env
    execution.effective_model = effective_model
    execution.effective_effort = effective_effort
    execution.effective_provider_version = (
        material.provider_version
        if material.provider_version is not None
        else _stored_executable_version(contract)
    )
    # Durable evidence carries digests/references only — never
    # profile/home/carrier values.
    profile_digest = _present_fact_digest(contract.profile_material)
    if profile_digest is not None:
        execution.evidence["profile_material_digest"] = profile_digest
    home_digest = _present_fact_digest(contract.provider_home_facts)
    if home_digest is not None:
        execution.evidence["provider_home_digest"] = home_digest
    if material.profile_material is not None:
        declared_profile = _reference_dict_digest(material.profile_material)
        if declared_profile != profile_digest:
            execution.evidence["declared_profile_material_digest"] = declared_profile
    if material.provider_home is not None:
        declared_home = _reference_dict_digest(material.provider_home)
        if declared_home != home_digest:
            execution.evidence["declared_provider_home_digest"] = declared_home
    if execution.effective_provider_version is not None:
        execution.evidence["provider_version"] = execution.effective_provider_version
    if request.compatibility_cell_ref is not None:
        execution.evidence["compatibility_cell_ref"] = request.compatibility_cell_ref
        cell_digest = request.compatibility_cell_digest
        if cell_digest is None:
            # Unreachable: the request schema refuses partial compatibility
            # evidence at construction; fail closed rather than record half.
            raise ExactExecutorInvalid("compatibility cell ref without its digest")
        execution.evidence["compatibility_cell_digest"] = cell_digest

    # 4. Reserve exactly one successor terminal id + fresh generation
    #    before any physical I/O; a replay adopts the durable reservation
    #    rather than allocating a second successor.  Every race resolves by
    #    re-reading the durable record: a concurrent (cross-process) run that
    #    reserved or finished between our claim and this reservation is
    #    adopted, never fought with a second candidate.
    reservation: Optional[dict[str, Any]] = None
    for _reservation_attempt in range(3):
        current = operation_journal.get_result(request.operation_id)
        # A concurrent run that reached a final outcome between our claim
        # and this reservation IS the answer (the same truth step 2 reads).
        if current["result_state"] == operation_journal.RESULT_ACCEPTED:
            return _accepted_outcome(operation_journal.get_operation(request.operation_id), request)
        if current["result_state"] == operation_journal.RESULT_RECONCILIATION_REQUIRED:
            raise ExactExecutorReconciliation(
                current["result_detail"] or "the operation is durably reconciliation-required"
            )
        if current["successor_terminal_id"] is not None:
            if current["successor_generation"] is None:
                raise ExactExecutorUnavailable(
                    f"operation {request.operation_id} records a successor terminal id "
                    "without its generation; the reservation pair is written atomically, "
                    "so the store is contradictory — refusing to guess"
                )
            reservation = operation_journal.reserve_successor(
                request.operation_id,
                current["successor_terminal_id"],
                current["successor_generation"],
            )
            break
        with database.SessionLocal() as session:
            candidate_id, candidate_generation = _allocate_successor(
                session, request.prior_generation
            )
        try:
            reservation = operation_journal.reserve_successor(
                request.operation_id, candidate_id, candidate_generation
            )
            break
        except operation_journal.OperationJournalUnavailable:
            # The candidate lost a unique-index race for a DIFFERENT
            # operation's successor slot; allocate a fresh one.
            continue
        except operation_journal.OperationJournalConflict:
            # A concurrent run reserved or finished first; loop back,
            # re-read the durable record, and adopt it.
            continue
    if reservation is None:
        raise ExactExecutorUnavailable("could not reserve a unique successor terminal id")
    operation = reservation["operation"]
    successor_terminal_id = operation["successor_terminal_id"]
    successor_generation = operation["successor_generation"]
    window = managed_window_name(successor_terminal_id, successor_generation)
    execution.evidence["successor_window"] = window

    # 5. fence_prior — establish the retired prior incarnation is the exact
    #    fenced source (the seam re-verifies it on every intent).
    try:
        execution.step(
            operation_journal.EFFECT_STEP_FENCE_PRIOR,
            {
                "prior_terminal_id": request.prior_terminal_id,
                "prior_generation": request.prior_generation or "legacy",
            },
        )
        _verify_fenced_source(request)
    except operation_journal.OperationJournalError as exc:
        refusal = _classify_journal_conflict(execution, exc)
        execution.record_refused(str(refusal))
        raise refusal from exc
    except ExactExecutorError as exc:
        execution.record_refused(str(exc))
        raise

    # 6. reap_prior — tear down only the exact prior terminal generation.
    try:
        execution.step(
            operation_journal.EFFECT_STEP_REAP_PRIOR,
            {"prior_terminal_id": request.prior_terminal_id, "window": "prior"},
        )
        await _reap_prior(request)
        execution.evidence["prior_reaped"] = "yes"
    except operation_journal.OperationJournalError as exc:
        refusal = _classify_journal_conflict(execution, exc)
        execution.record_refused(str(refusal))
        raise refusal from exc
    except terminal_service.TerminalGenerationMismatchError as exc:
        # A reused terminal id / replacement generation: the teardown seam
        # already guaranteed zero destructive action; preserve and refuse.
        detail = (
            f"the prior terminal id now names a replacement incarnation; the "
            f"exact-generation teardown refused with zero destructive action: {exc}"
        )
        execution.record_refused(detail)
        raise ExactExecutorConflict(detail) from exc
    except ExactExecutorError as exc:
        execution.record_refused(str(exc))
        raise

    # 7. release_attachment — only after the authoritative no-survivor proof.
    try:
        execution.step(
            operation_journal.EFFECT_STEP_RELEASE_ATTACHMENT,
            {
                "harness": request.harness,
                "native_session_id": request.native_session_id,
            },
        )
        _release_prior_attachment(request)
    except operation_journal.OperationJournalError as exc:
        refusal = _classify_journal_conflict(execution, exc)
        execution.record_refused(str(refusal))
        raise refusal from exc
    except ExactExecutorRefused as exc:
        execution.record_refused(str(exc))
        raise

    # 8. acquire + create + launch + verify, through the exact launch seam.
    launch_outcome = await _launch_successor(
        execution,
        contract,
        successor_terminal_id,
        successor_generation,
        window,
    )

    # 9. Final roster bind: one transaction that rechecks the gate AND binds
    #    the successor AND records the accepted result.  Only a GATE refusal
    #    (barrier/epoch/phase/source drift) is a durable reconciliation — the
    #    successor pane exists and nothing but M3-C/operator reconciliation
    #    may touch it.  Any other failure leaves the truthful pending state
    #    (pane published, identity verified, not yet bound): a retry adopts
    #    the same intents, attachment, and pane, and binds.
    bind: Optional[dict[str, Any]] = None
    for bind_attempt in range(5):
        try:
            bind = await asyncio.to_thread(
                _bind_successor,
                execution,
                request,
                successor_terminal_id,
                successor_generation,
                launch_outcome,
            )
            break
        except _BindDrift as exc:
            # A concurrent duplicate may have completed the whole restore
            # between this run's claim and its bind: the durable accepted
            # result IS the answer for both callers, never a spurious
            # reconciliation.
            settled = operation_journal.get_result(request.operation_id)
            if settled["result_state"] == operation_journal.RESULT_ACCEPTED:
                return _accepted_outcome(
                    operation_journal.get_operation(request.operation_id), request
                )
            detail = (
                f"the gate refused the final roster bind: {exc}; the successor pane "
                "exists, is not bound, and is not admitted — reconciliation owns it"
            )
            execution.evidence["successor_pane"] = window
            execution.record_reconciliation(detail)
            raise ExactExecutorReconciliation(detail) from exc
        except (
            IntegrityError,
            OperationalError,
            roster.StableAgentUnavailable,
            operation_journal.OperationJournalUnavailable,
        ) as exc:
            # Two processes can attempt the same final transaction after both
            # adopted the exact pane.  SQLite may reject the stale reader's
            # write upgrade while the winner commits.  Re-read the durable
            # outcome first; while still pending, retry the SAME bind in a
            # fresh short transaction rather than converting ordinary writer
            # contention into an ambiguous physical result.
            settled = operation_journal.get_result(request.operation_id)
            if settled["result_state"] == operation_journal.RESULT_ACCEPTED:
                return _accepted_outcome(
                    operation_journal.get_operation(request.operation_id), request
                )
            if settled["result_state"] == operation_journal.RESULT_RECONCILIATION_REQUIRED:
                raise ExactExecutorReconciliation(
                    settled["result_detail"] or "the operation is durably reconciliation-required"
                ) from exc
            if bind_attempt == 4:
                raise ExactExecutorUnavailable(
                    "the exact successor bind remained contended after five fresh "
                    f"transactions: {exc}"
                ) from exc
            await asyncio.sleep(0.05)
    if bind is None:  # pragma: no cover - the loop returns, raises, or binds
        raise ExactExecutorUnavailable("the exact successor bind produced no result")

    stored = operation_journal.get_result(request.operation_id)
    return {
        "schema": SCHEMA_VERSION,
        "operation_id": request.operation_id,
        "outcome": operation_journal.RESULT_ACCEPTED,
        "successor_terminal_id": successor_terminal_id,
        "successor_generation": successor_generation,
        "successor_incarnation_id": bind["incarnation"]["incarnation_id"],
        "native_session_id": request.native_session_id,
        "admitted": False,
        "launch": launch_outcome["summary"],
        "roster": {
            "agent_id": bind["agent"]["agent_id"],
            "lineage_id": bind["lineage"]["lineage_id"],
            "revision": bind["agent"]["revision"],
            "incarnation_disposition": bind["incarnation"]["disposition"],
        },
        "result_evidence": stored["result_evidence"],
    }


def _accepted_outcome(
    operation: dict[str, Any], request: operation_journal.OperationRequest
) -> dict[str, Any]:
    """Rebuild the accepted result payload from the durable record."""
    evidence = operation.get("result_evidence") or {}
    return {
        "schema": SCHEMA_VERSION,
        "operation_id": operation["operation_id"],
        "outcome": operation_journal.RESULT_ACCEPTED,
        "successor_terminal_id": operation["successor_terminal_id"],
        "successor_generation": operation["successor_generation"],
        "successor_incarnation_id": operation["successor_incarnation_id"],
        "native_session_id": request.native_session_id,
        "admitted": False,
        "launch": {"summary_source": "durable-result"},
        "roster": {
            "agent_id": operation["agent_id"],
            "lineage_id": operation["lineage_id"],
        },
        "result_evidence": evidence,
    }


# ---------------------------------------------------------------------------
# the physical steps
# ---------------------------------------------------------------------------


def _verify_fenced_source(request: operation_journal.OperationRequest) -> None:
    """The fence step's own verification: the exact retired source.

    The seam re-verifies these facts on every intent; this step records
    the fence as an explicit ordered boundary so the retired generation's
    no-further-bytes state is established before any pane is touched.
    """
    with database.SessionLocal() as session:
        incarnation = (
            session.query(database.StableAgentIncarnationModel)
            .filter(
                database.StableAgentIncarnationModel.incarnation_id == request.prior_incarnation_id
            )
            .one_or_none()
        )
        if incarnation is None or incarnation.disposition != roster.INCARNATION_RETIRED:
            raise ExactExecutorConflict(
                f"prior incarnation {request.prior_incarnation_id} is not the retired "
                "source; the exact old generation is not fenced"
            )
        agent = (
            session.query(database.StableAgentModel)
            .filter(database.StableAgentModel.agent_id == request.agent_id)
            .one_or_none()
        )
        if (
            agent is None
            or agent.disposition != roster.DISPOSITION_DORMANT
            or agent.current_incarnation_id != request.prior_incarnation_id
        ):
            raise ExactExecutorConflict(
                f"stable agent {request.agent_id} is not dormant on the exact prior "
                "source; the exact old generation is not fenced"
            )


def _delete_prior_terminal(request: operation_journal.OperationRequest) -> bool:
    """The exact-generation teardown of the prior incarnation, with the
    attachment release and roster retirement held for B3's own authorized
    steps (typed explicitly: the seam's kwargs never go through an
    unchecked ``**`` unpack)."""
    if request.prior_generation is not None:
        return terminal_service.delete_terminal(
            request.prior_terminal_id,
            expected_generation=request.prior_generation,
            expected_session=request.session_name,
            release_native_attachments=False,
            retire_roster=False,
        )
    return terminal_service.delete_terminal(
        request.prior_terminal_id,
        release_native_attachments=False,
        retire_roster=False,
    )


async def _reap_prior(request: operation_journal.OperationRequest) -> None:
    """Reap only the exact prior terminal generation/pane/process.

    Uses the exact-generation teardown seam with the attachment release
    and roster retirement held: B3 performs those under its own
    authorized steps.  A managed prior (generation present) is torn down
    under its exact generation + session; a legacy prior (no generation)
    uses the seam's bare legacy form — a session identity without a
    generation never degrades to ID-only destruction.  Replays over an
    absent old terminal/window are idempotent (the seam's row-absent path
    kills only the deterministic managed window name).
    """
    await asyncio.to_thread(_delete_prior_terminal, request)


def _release_prior_attachment(request: operation_journal.OperationRequest) -> None:
    """Release the prior native attachment on the no-survivor proof.

    A positively live or unobservable owner — or a release the store
    refused — refuses the whole restore with no successor pane: a second
    attacher on one provider session is the one harm this step exists to
    prevent.
    """
    outcomes = native_attachment_recovery.release_owned_by_terminal(
        request.prior_terminal_id, generation=request.prior_generation
    )
    for outcome in outcomes:
        action = outcome.get("action")
        if action == "released":
            continue
        if action == "skipped" and outcome.get("reason") == "not-live":
            # Detached (already released) rows hold nothing.
            continue
        raise ExactExecutorRefused(
            f"the prior native attachment could not be released on an "
            f"authoritative no-survivor proof ({action}/{outcome.get('reason')}): "
            f"{outcome.get('detail')}; a competing owner is never double-attached"
        )


async def _await_concurrent_launch_owner(
    execution: _Execution,
    successor_terminal_id: str,
    successor_generation: str,
) -> bool:
    """Wait for the exact same-owner launcher that won the start CAS.

    Distinct executor processes do not share ``_operation_lock``.  They may
    therefore adopt the same launch intents and both reach the attachment's
    DECLARED -> STARTING transition.  That epoch CAS selects one physical pane
    creator; a loser waits for only that exact owner to publish, then re-enters
    the ordinary launch seam and adopts the attachment.  No lock spans the
    wait.  A final operation result always wins, while a changed/frozen owner or
    a bounded no-progress timeout falls back to reconciliation.
    """
    deadline = (
        asyncio.get_running_loop().time() + native_tui_launch.NATIVE_COLD_START_RUNWAY_SECONDS
    )
    while True:
        settled = operation_journal.get_result(execution.request.operation_id)
        if settled["result_state"] == operation_journal.RESULT_ACCEPTED:
            raise _AdoptedAccepted(operation_journal.get_operation(execution.request.operation_id))
        if settled["result_state"] == operation_journal.RESULT_RECONCILIATION_REQUIRED:
            raise ExactExecutorReconciliation(
                settled["result_detail"] or "the operation is durably reconciliation-required"
            )
        try:
            attachment = native_attachment.get(
                execution.request.harness, execution.request.native_session_id
            )
        except native_attachment.NativeAttachmentError as exc:
            raise ExactExecutorUnavailable(
                f"the concurrent exact launch owner could not be read: {exc}"
            ) from exc
        owner = (attachment or {}).get("owner") or {}
        if (
            attachment is None
            or owner.get("terminal_id") != successor_terminal_id
            or owner.get("generation") != successor_generation
            or owner.get("execution_mode") != em.NATIVE_TUI
        ):
            return False
        if attachment.get("state") == native_attachment.ATTACHED:
            return True
        if attachment.get("state") != native_attachment.STARTING:
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(native_tui_launch.INNER_EXEC_CONVERGENCE_POLL_SECONDS, remaining))


async def _launch_successor(
    execution: _Execution,
    contract: restore_contract.RestoreContract,
    successor_terminal_id: str,
    successor_generation: str,
    window: str,
    *,
    concurrent_retry: bool = False,
) -> dict[str, Any]:
    """Acquire, create, launch, and verify — through ``start(resume)``.

    The launch's authorize callback is the pre-effect linearization
    point: ``declare`` authorizes ``acquire_native``; ``create_pane``
    authorizes ``create_pane`` + ``launch_resume`` back-to-back
    immediately before the single atomic transport call; ``publish``
    authorizes ``verify_identity``.
    """

    # The launch argv/env composition: the executor-derived route pins lead
    # (they own the route), the applied profile lanes and the sealed profile
    # material follow (they own the selected references), and the caller's
    # extra args come last — already proven to restate none of the owned
    # flags.  Env layers merge in ascending authority order, each proven
    # disjoint from the keys a later layer owns.
    launch_args = (
        list(execution.route_argv)
        + list(execution.profile_argv)
        + list(execution.material.profile_args)
        + list(execution.material.extra_args)
    )
    environment = {
        **execution.material.environment,
        **execution.material.profile_environment,
        **execution.route_env,
        **execution.profile_env,
    }
    launch_material_digest = _launch_material_digest(
        launch_args, environment, execution.effective_provider_version
    )
    execution.evidence["launch_material_digest"] = launch_material_digest

    def _authorize(boundary: str) -> None:
        if boundary == native_tui_launch.AUTHORIZE_BOUNDARY_DECLARE:
            execution.step(
                operation_journal.EFFECT_STEP_ACQUIRE_NATIVE,
                {
                    "harness": execution.request.harness,
                    "native_session_id": execution.request.native_session_id,
                    "launch_material_digest": launch_material_digest,
                },
            )
        elif boundary == native_tui_launch.AUTHORIZE_BOUNDARY_CREATE_PANE:
            execution.step(
                operation_journal.EFFECT_STEP_CREATE_PANE,
                {
                    "successor_terminal_id": successor_terminal_id,
                    "window": window,
                },
            )
            execution.step(
                operation_journal.EFFECT_STEP_LAUNCH_RESUME,
                {
                    "successor_terminal_id": successor_terminal_id,
                    "binary_sha256": _executable_digest(contract),
                    "launch_material_digest": launch_material_digest,
                },
            )
            # The atomic pane creation follows at once: from here the
            # successor pane may physically exist.
            execution.successor_physical = True
        elif boundary == native_tui_launch.AUTHORIZE_BOUNDARY_PUBLISH:
            execution.step(
                operation_journal.EFFECT_STEP_VERIFY_IDENTITY,
                {
                    "harness": execution.request.harness,
                    "native_session_id": execution.request.native_session_id,
                },
            )

    intent = native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_RESUME,
        acquisition_receipt={
            "kind": "m3-exact-restore",
            "operation_id": execution.request.operation_id,
            "restore_contract_id": execution.request.restore_contract_id,
        },
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        note=f"cond-0378 B3 exact restore of {execution.request.prior_terminal_id}",
    )
    try:
        attachment = native_attachment.get(
            execution.request.harness, execution.request.native_session_id
        )
    except native_attachment.NativeAttachmentError as exc:
        raise ExactExecutorUnavailable(
            f"the exact successor attachment could not be read before launch: {exc}"
        ) from exc
    owner = (attachment or {}).get("owner") or {}
    if (
        attachment is not None
        and attachment.get("state") == native_attachment.STARTING
        and owner.get("terminal_id") == successor_terminal_id
        and owner.get("generation") == successor_generation
        and owner.get("execution_mode") == em.NATIVE_TUI
    ):
        # A different executor process already crossed the pane-start CAS for
        # this exact successor.  Let that healthy owner publish before calling
        # the generic launch seam: its STARTING re-entry branch is reserved for
        # genuine crash recovery and would otherwise freeze an in-flight pane.
        await _await_concurrent_launch_owner(execution, successor_terminal_id, successor_generation)
    if execution.transport_factory is not None:
        transport = execution.transport_factory()
    else:
        transport = _SuccessorPaneTransport(
            session_name=execution.request.session_name,
            terminal_id=successor_terminal_id,
            generation=successor_generation,
            provider=execution.request.harness,
            agent_profile=execution.request.profile_family,
            working_directory=contract.working_directory,
            trusted_project_root=(
                contract.trusted_project_root if execution.request.harness == "codex" else None
            ),
            # The SAME effective values that pinned the resume argv/env —
            # the stored contract facts when the request omitted them.
            expected_model=execution.effective_model,
            expected_effort=execution.effective_effort,
            environment=environment,
            registry=execution.registry,
            loop=asyncio.get_running_loop(),
        )
    execution.evidence.update(_environment_evidence(environment))
    executable = contract.executable.value or {}
    try:
        # The blocking launch seam runs on a worker thread; the authorize
        # callback it invokes performs its own short committed journal
        # transactions there, and the default transport's pane creation
        # round-trips back onto this loop — which is free while we await.
        outcome = await asyncio.to_thread(
            native_tui_launch.start,
            provider=execution.request.harness,
            native_session_id=execution.request.native_session_id,
            terminal_id=successor_terminal_id,
            generation=successor_generation,
            execution_mode=em.NATIVE_TUI,
            intent=intent,
            binary=str(executable["path"]),
            binary_sha256=str(executable["sha256"]),
            working_directory=contract.working_directory,
            transport=transport,
            extra_args=launch_args or None,
            launch_kind=native_tui_launch.LAUNCH_KIND_RESUME,
            provider_version=execution.effective_provider_version,
            authorize=_authorize,
        )
    except native_tui_launch.NativeLaunchAmbiguous as exc:
        detail = (
            f"the successor launch froze the attachment ({exc.reason}: {exc.detail}); "
            "an ambiguous pane/process outcome never binds a successor and stays "
            "durably reconciliation-required"
        )
        execution.evidence["successor_pane"] = window
        execution.record_reconciliation(detail)
        raise ExactExecutorReconciliation(detail) from exc
    except native_tui_launch.NativeLaunchConflict as exc:
        if execution.successor_physical:
            if not concurrent_retry and await _await_concurrent_launch_owner(
                execution, successor_terminal_id, successor_generation
            ):
                return await _launch_successor(
                    execution,
                    contract,
                    successor_terminal_id,
                    successor_generation,
                    window,
                    concurrent_retry=True,
                )
            detail = (
                f"the successor launch conflicted after the pane boundary: {exc}; "
                "reconciliation owns the successor pane"
            )
            execution.evidence["successor_pane"] = window
            execution.record_reconciliation(detail)
            raise ExactExecutorReconciliation(detail) from exc
        execution.record_refused(str(exc))
        raise ExactExecutorRefused(str(exc)) from exc
    except native_tui_launch.NativeLaunchInvalid as exc:
        execution.record_refused(str(exc))
        raise ExactExecutorRefused(str(exc)) from exc
    except native_tui_launch.NativeLaunchUnavailable as exc:
        # The local dependency failure may race another process's write-once
        # final result.  Surface that durable winner now instead of returning
        # a transient error for an operation that is already settled.
        settled = operation_journal.get_result(execution.request.operation_id)
        if settled["result_state"] == operation_journal.RESULT_ACCEPTED:
            raise _AdoptedAccepted(operation_journal.get_operation(execution.request.operation_id))
        if settled["result_state"] == operation_journal.RESULT_RECONCILIATION_REQUIRED:
            raise ExactExecutorReconciliation(
                settled["result_detail"] or "the operation is durably reconciliation-required"
            ) from exc
        raise ExactExecutorUnavailable(str(exc)) from exc
    except operation_journal.OperationJournalError as exc:
        refusal = _classify_journal_conflict(execution, exc)
        if isinstance(refusal, ExactExecutorReconciliation):
            execution.evidence["successor_pane"] = window
            execution.record_reconciliation(str(refusal))
        else:
            execution.record_refused(str(refusal))
        raise refusal from exc

    observation = outcome.get("pane_observation") or {}
    execution.evidence["session_proof"] = str(outcome.get("session_proof") or "already_attached")
    if observation.get("pane_id"):
        execution.evidence["successor_pane"] = str(observation["pane_id"])
    return {
        "outcome": outcome,
        "summary": {
            "launch_outcome": outcome["outcome"],
            "session_proof": outcome.get("session_proof"),
            "pane_id": observation.get("pane_id"),
        },
    }


def _executable_digest(contract: restore_contract.RestoreContract) -> str:
    value = contract.executable.value or {}
    return str(value.get("sha256", ""))


class _BindDrift(ExactExecutorError):
    """The final-bind gate refused inside the binding transaction."""


def _bind_successor(
    execution: _Execution,
    request: operation_journal.OperationRequest,
    successor_terminal_id: str,
    successor_generation: str,
    launch_outcome: dict[str, Any],
) -> dict[str, Any]:
    """Bind the successor on the same lineage/stable agent, atomically.

    One database transaction rechecks the operation phase, lifecycle
    epoch, barrier, and exact dormant source, then binds through the
    roster transaction seam and records the accepted result — so a
    concurrent Stop either wins first (no bind) or observes a fully bound
    successor.
    """
    observation = (launch_outcome["outcome"].get("pane_observation")) or {}
    attachment = launch_outcome["outcome"].get("attachment") or {}
    owner = attachment.get("owner") or {}
    process_identity = owner.get("process_identity")
    if process_identity is None and observation.get("pid"):
        process_identity = native_attachment.process_identity(
            pid=int(observation["pid"]),
            start_marker=str(observation.get("start_marker") or ""),
        )
    with database.SessionLocal() as session:
        row = (
            session.query(database.ReincarnationOperationModel)
            .filter(database.ReincarnationOperationModel.operation_id == request.operation_id)
            .one_or_none()
        )
        if row is None:
            raise _BindDrift(f"operation {request.operation_id} is no longer recorded")
        if row.phase != operation_journal.EFFECT_STEP_VERIFY_IDENTITY:
            raise _BindDrift(
                f"operation {request.operation_id} is in journal phase {row.phase!r}, "
                f"not {operation_journal.EFFECT_STEP_VERIFY_IDENTITY!r}"
            )
        if row.result_state in operation_journal.RESULT_FINAL_STATES:
            raise _BindDrift(
                f"operation {request.operation_id} already has the final result "
                f"{row.result_state!r}"
            )
        lifecycle, epoch = operation_journal._lifecycle_in_session(session, request.session_name)
        if lifecycle == sl.STOPPED:
            raise _BindDrift(f"session {request.session_name} is stopped")
        if epoch != request.lifecycle_epoch:
            raise _BindDrift(f"session {request.session_name} moved to lifecycle epoch {epoch}")
        barrier = operation_journal._barrier_claimed(session, request.session_name)
        if barrier:
            raise _BindDrift(f"session {request.session_name} has a claimed Stop barrier")
        agent = (
            session.query(database.StableAgentModel)
            .filter(database.StableAgentModel.agent_id == request.agent_id)
            .one_or_none()
        )
        if (
            agent is None
            or agent.disposition != roster.DISPOSITION_DORMANT
            or agent.current_incarnation_id != request.prior_incarnation_id
            or int(agent.revision or 0) != request.roster_revision
        ):
            raise _BindDrift(
                f"stable agent {request.agent_id} no longer agrees with the "
                "operation's bound dormant source"
            )
        incarnation = (
            session.query(database.StableAgentIncarnationModel)
            .filter(
                database.StableAgentIncarnationModel.incarnation_id == request.prior_incarnation_id
            )
            .one_or_none()
        )
        if incarnation is None or incarnation.disposition != roster.INCARNATION_RETIRED:
            raise _BindDrift(
                f"prior incarnation {request.prior_incarnation_id} is no longer the "
                "retired source"
            )
        bind = roster.bind_generation(
            roster.BindingContract(
                agent_id=request.agent_id,
                session_name=request.session_name,
                role=request.role,
                profile_family=request.profile_family,
                harness=request.harness,
                native_session_id=request.native_session_id,
                acquisition_method=native_attachment.ACQUISITION_RESUME,
                terminal_id=successor_terminal_id,
                generation=successor_generation,
                pane_id=owner.get("pane_id") or observation.get("pane_id"),
                pane_pid=(int(observation["pid"]) if observation.get("pid") else None),
                process_identity=process_identity,
                execution_mode=em.NATIVE_TUI,
                lineage_origin=roster.LINEAGE_ORIGIN_RESUME,
            ),
            db=session,
        )
        if bind["incarnation"]["disposition"] != roster.INCARNATION_BOUND:
            raise _BindDrift(
                f"the successor incarnation is {bind['incarnation']['disposition']!r}, not "
                f"{roster.INCARNATION_BOUND!r}; B3 binds and never admits — a drifted "
                "disposition is reconciliation, not an accepted result"
            )
        operation_journal.record_result(
            request.operation_id,
            operation_journal.RESULT_ACCEPTED,
            detail="exact restore bound; not task-admitted",
            evidence=dict(execution.evidence),
            successor_incarnation_id=bind["incarnation"]["incarnation_id"],
            db=session,
        )
        session.commit()
        return bind


def get_result(operation_id: str) -> dict[str, Any]:
    """The successor reservation and durable bounded outcome (read surface)."""
    return operation_journal.get_result(operation_id)
