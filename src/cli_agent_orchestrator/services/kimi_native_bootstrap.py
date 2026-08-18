"""Mint a Kimi provider session over ACP, send no turn, and then leave.

A native TUI cannot mint its own session id.  The id has to exist, and be
known to CAO, *before* the TUI starts — otherwise the durable attachment
record would be written after the process it describes, and a crash in
between would leave a running provider nobody owns.  Kimi's only pre-turn
identity source is the ACP ``session/new`` ``sessionId``, so a short ACP
conversation mints the id and nothing else.

Two properties make that safe, and both are enforced rather than
described:

**No turn is ever sent.**  This module contains no ``session/prompt``
call at all.  The guarantee is the absence of the code path, not a flag
someone remembered to leave false — a bootstrap that submitted work would
put a turn into a session whose transcript will later be read as the
native worker's own history.

**The bootstrap is gone before the TUI starts.**  A provider session is a
single-writer resource.  If the minting process were still attached when
the TUI resumed the same session, the two would interleave turns and
neither could tell.  So the id is only returned alongside proof that the
minting process exited.

That proof is deliberately narrow.  A liveness check on the recorded pid
proves nothing: pids are recycled, so an "it is gone" reading can be a
recycled stranger and an "it is alive" reading can be too.  The only
sound proof available here is that *this* process was the direct parent,
called ``wait()``, and reaped a real exit status.  Anything short of that
— a timeout, a transport that declines to say — is treated as a live
process still holding the session, and the session id is refused rather
than handed on.  A refused bootstrap costs one wasted session; an
unproven one costs a silent double-attach.

Termination is also ordered: stdin is closed first so the provider exits
on EOF and finishes writing its own session store, and signals escalate
only after that.  A session id naming a record the provider never
finished flushing is not resumable, and discovering that at resume time —
after ownership has been claimed — is much worse than failing here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence

from cli_agent_orchestrator.services import (
    kimi_native_launch,
    native_attachment,
    provider_contracts,
)
from cli_agent_orchestrator.services.kimi_route import _current_option
from cli_agent_orchestrator.services.provider_contracts import (
    PROVIDER_KIMI,
    ProviderContractError,
    check_pinned_version,
    native_id_source,
    normalized_version,
)

BOOTSTRAP_SCHEMA = "cao-kimi-native-bootstrap-v1"
EXIT_PROOF_SCHEMA = "cao-kimi-native-bootstrap-exit-v1"

#: The ACP protocol version and client identity used for the mint.  The
#: client advertises no filesystem or terminal capability: a bootstrap
#: that could be asked to read or write on the provider's behalf is a
#: bootstrap that can do work, and this one exists to do none.
ACP_PROTOCOL_VERSION = 1
CLIENT_NAME = "cao-native-bootstrap"

#: Termination steps, in the only order they may appear.
STEP_STDIN_CLOSED = "stdin_closed"
STEP_SIGTERM = "sigterm"
STEP_SIGKILL = "sigkill"
EXIT_STEPS: tuple[str, ...] = (STEP_STDIN_CLOSED, STEP_SIGTERM, STEP_SIGKILL)

#: Seconds allowed for each termination step.  The EOF budget is the
#: largest because it is the one that lets the provider flush its own
#: session store; the signal budgets only need to cover process teardown.
EOF_GRACE_SECONDS = 20.0
SIGNAL_GRACE_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 120.0


class KimiBootstrapError(RuntimeError):
    """A Kimi native bootstrap could not be completed safely."""

    code = "kimi-native-bootstrap-error"


class KimiBootstrapInvalid(KimiBootstrapError):
    """The bootstrap inputs were not a usable pinned identity."""


class KimiBootstrapProtocol(KimiBootstrapError):
    """The ACP exchange did not yield exactly one usable session id."""


class KimiBootstrapNotDetached(KimiBootstrapError):
    """The minting process could not be proven to have exited.

    Raised in preference to any error that was already in flight: an
    unproven process may still be attached to the session, which is a
    worse condition than whatever failed the exchange.
    """


class AcpBootstrapTransport(Protocol):
    """One ACP conversation that can be ended and proven ended.

    ``terminate`` returns its exit evidence rather than raising on an
    unproven exit, so the fail-closed decision lives in one place in this
    module instead of being re-derived by every transport.
    """

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def terminate(self) -> Mapping[str, Any]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KimiBootstrapInvalid(f"{field} must be a non-empty string; got {value!r}")
    return value


def _validate_binary(binary: str, binary_sha256: str, version_output: str) -> str:
    """Validate the exact pinned binary this bootstrap is allowed to run.

    The launch path validates the same binary again on its own, from its
    own pin, rather than trusting a receipt this module produced.  That
    repetition is deliberate: the two checks bracket the window in which
    a binary could be swapped between minting a session and resuming it.
    """
    path = _require_text(binary, field="kimi_binary")
    if not os.path.isabs(path) or os.path.realpath(path) != path:
        raise KimiBootstrapInvalid(
            f"kimi binary must be a canonical absolute path; got {path!r} "
            f"(realpath {os.path.realpath(path)!r})"
        )
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise KimiBootstrapInvalid(f"kimi binary {path!r} is not an executable file")
    expected = _require_text(binary_sha256, field="binary_sha256").lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise KimiBootstrapInvalid(
            f"binary_sha256 must be a 64-character hex digest; got {expected!r}"
        )
    with open(path, "rb") as handle:
        observed = hashlib.sha256(handle.read()).hexdigest()
    if observed != expected:
        raise KimiBootstrapInvalid(
            f"kimi binary {path!r} has digest {observed} but the pin says {expected}; "
            "refusing to mint a session against an unpinned binary"
        )
    # Open mode permits a semver-shaped update to reach the launch boundary,
    # while strict mode remains an explicit operator rollback after a
    # reproduced regression.  The native identity proof below needs no exact
    # build listing: the ACP exchange mints and re-loads the session against
    # the installed binary itself, so an unlisted build proves — or loudly
    # fails — the identity contract at runtime.
    try:
        check_pinned_version(PROVIDER_KIMI, version_output)
    except ProviderContractError as exc:
        raise KimiBootstrapInvalid(str(exc)) from exc
    return observed


def _validate_working_directory(working_directory: Any) -> str:
    """Accept only a canonical working directory to mint a session in.

    Kimi buckets a session by the ``cwd`` *string* this call passes it,
    and resolves ``--session`` against the recorded string.  The TUI that
    later resumes it is a node process, whose reported working directory
    is always the realpath.  Mint under ``/tmp/w`` and the resume of that
    exact directory is refused with "created under a different
    directory", because the two names index different buckets.

    Refused rather than canonicalised here, for the same reason the
    binary above is: this module is handed a path that a reservation
    already recorded and that the pane will be started in, and quietly
    minting under a different string than the one on record is precisely
    the divergence being guarded against.  The caller's own boundary
    rejects this first; the repetition is deliberate, and brackets the
    window in which the recorded path and the minted path could drift
    apart.
    """
    path = _require_text(working_directory, field="working_directory")
    if not os.path.isabs(path) or os.path.realpath(path) != path:
        raise KimiBootstrapInvalid(
            f"working_directory must be a canonical absolute path; got {path!r} "
            f"(realpath {os.path.realpath(path)!r}) — Kimi files the session under the "
            "string given here and the resuming TUI reports only the realpath, so the "
            "two would never match"
        )
    if not os.path.isdir(path):
        raise KimiBootstrapInvalid(f"working_directory {path!r} is not an existing directory")
    return path


def _validated_exit_proof(raw: Any) -> dict[str, Any]:
    """Accept only evidence that the minting process was actually reaped."""
    if not isinstance(raw, Mapping):
        raise KimiBootstrapNotDetached(
            f"termination returned {type(raw).__name__}, not exit evidence; the minting "
            "process must be treated as still holding the session"
        )
    steps = raw.get("escalation")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        raise KimiBootstrapNotDetached("exit evidence carries no escalation sequence")
    sequence = list(steps)
    if [step for step in sequence if step not in EXIT_STEPS] or sequence != [
        step for step in EXIT_STEPS if step in sequence
    ]:
        raise KimiBootstrapNotDetached(
            f"escalation {sequence!r} is not an ordered prefix-respecting subsequence of "
            f"{list(EXIT_STEPS)}"
        )
    if not sequence or sequence[0] != STEP_STDIN_CLOSED:
        raise KimiBootstrapNotDetached(
            "termination must close stdin first so the provider exits on EOF and finishes "
            f"writing its own session store; escalation was {sequence!r}"
        )
    if raw.get("reaped") is not True:
        raise KimiBootstrapNotDetached(
            f"the minting process was not reaped after {sequence!r}; it may still be "
            "attached to the session, so the session id is refused rather than handed on"
        )
    pid = raw.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise KimiBootstrapNotDetached(f"exit evidence has no owning pid; got {pid!r}")
    status = raw.get("exit_status")
    if not isinstance(status, int) or isinstance(status, bool):
        raise KimiBootstrapNotDetached(
            f"exit evidence has no reaped exit status; got {status!r}. A reaped process "
            "always has one, so its absence means the wait never returned"
        )
    return {
        "schema": EXIT_PROOF_SCHEMA,
        "pid": pid,
        "exit_status": status,
        "escalation": sequence,
        "reaped": True,
    }


def mint_session(
    *,
    kimi_binary: str,
    binary_sha256: str,
    version_output: str,
    working_directory: str,
    model: str,
    effort: str,
    transport: AcpBootstrapTransport,
    mcp_servers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Mint one Kimi session id with no turn, prove the minter exited.

    Returns the receipt that a native launch presents as its acquisition
    evidence.  The returned id is already validated against the resume
    argv rules, because an id that cannot be safely placed on a
    ``--session`` command line is not a usable bootstrap — and finding
    that out at launch time, after ownership has been claimed, is far
    worse than finding it out here.

    The route is written into the session record here, for the same
    reason the id is: the resume command line carries no model or effort
    option, so the only chance to bind the route to this session is while
    an ACP conversation still owns it.  It is set *and read back*, and
    the receipt reports the read-back values — a set that silently did
    not take would otherwise become a worker quietly running the wrong
    model under a receipt that says otherwise.
    """
    # Everything below runs under a finally that terminates the injected
    # transport.  The minting process already exists — the caller spawned
    # it before handing the transport here — so a refusal from the binary,
    # working-directory, version, or route validation must not return
    # while that process is still attached to a session.  Pulling those
    # validations inside the try is the whole point: an ESC-less exit that
    # sends no ACP request at all still passes through ``_terminate``
    # exactly once, so no refusal ever leaks a running provider behind a
    # ``preflight_blocked`` reservation.  ``_validated_exit_proof`` raising
    # from the finally deliberately displaces an error already
    # propagating: a live unowned process outranks whatever failed the
    # mint, and the original remains attached as the exception's context.
    try:
        digest = _validate_binary(kimi_binary, binary_sha256, version_output)
        cwd = _validate_working_directory(working_directory)
        wanted_model = _require_text(model, field="model")
        wanted_effort = _require_text(effort, field="effort")
        servers = [dict(server) for server in mcp_servers]

        initialize_params: dict[str, Any] = {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": CLIENT_NAME, "version": BOOTSTRAP_SCHEMA},
        }
        session_params: dict[str, Any] = {"cwd": cwd, "mcpServers": servers}

        initialized = transport.request("initialize", initialize_params)
        session = transport.request("session/new", session_params)
        if not isinstance(session, Mapping):
            raise KimiBootstrapProtocol(
                f"session/new returned {type(session).__name__}, not a result object"
            )
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise KimiBootstrapProtocol(
                "Kimi session/new omitted the provider session id; there is no other "
                "pre-turn identity source for this provider"
            )
        try:
            kimi_native_launch.validate_session_id(session_id)
        except kimi_native_launch.KimiNativeLaunchError as exc:
            raise KimiBootstrapProtocol(
                f"Kimi minted session id {session_id!r} cannot be safely resumed: {exc}"
            ) from exc
        options = _apply_route(
            transport,
            session_id=session_id,
            options=session.get("configOptions"),
            model=wanted_model,
            effort=wanted_effort,
        )
    finally:
        exit_proof = _validated_exit_proof(_terminate(transport))

    return {
        "schema": BOOTSTRAP_SCHEMA,
        "provider": PROVIDER_KIMI,
        "native_session_id": session_id,
        "id_source": native_id_source(PROVIDER_KIMI),
        # The exact version the pinned binary reports (0.29.0, 0.29.1, or
        # 0.29.2),
        # validated by _validate_binary above — not a pin constant that
        # could name the wrong one of several accepted builds.
        "provider_version": normalized_version(version_output),
        "binary_path": kimi_binary,
        "binary_sha256": digest,
        "working_directory": cwd,
        # The read-back route, not the requested one.  They are equal
        # here only because ``_apply_route`` refuses to return otherwise.
        #
        # ``effort`` is null for a route that selected none: this session
        # was never told an effort and never checked one, so whatever the
        # thought_level option happens to read is an artifact of the
        # session rather than a route fact, and reporting it would turn a
        # value nobody asked for into a value this receipt vouches for.
        "model": _current_option(options, category="model", option_id="model"),
        "effort": (
            _current_option(options, category="thought_level", option_id="thinking")
            if provider_contracts.route_selects_effort(effort)
            else None
        ),
        # Both are facts about this module's shape, not caller claims:
        # there is no prompt call here, and the exit proof above is the
        # only way past this line.
        "sent_no_turn": True,
        "detached_before_launch": True,
        "exit_proof": exit_proof,
        # The exchange is digested rather than stored.  It is enough to
        # detect a receipt describing a different conversation, and it
        # keeps provider output — which can carry credentials — out of a
        # durable record.
        "acp_exchange_sha256": _digest(
            {
                "initialize": initialize_params,
                "initialized": dict(initialized) if isinstance(initialized, Mapping) else None,
                "session_new": session_params,
                "session_result": dict(session),
            }
        ),
        "minted_at": _now(),
    }


def _apply_route(
    transport: AcpBootstrapTransport,
    *,
    session_id: str,
    options: Any,
    model: str,
    effort: str,
) -> Any:
    """Write the exact route into the session record and read it back.

    Each option is only set when it is not already the wanted value, so a
    session that already carries the route costs no extra provider calls.
    The final check covers every option this call actually set: a ``set``
    that returned success while leaving the record unchanged is exactly the
    failure this exists to catch, and it is indistinguishable from success
    unless the values are re-read.

    A route that selects no effort sets no thinking option and checks none.
    The model half is unchanged and still exact — what such a route
    declines to pin is the effort, not the model — and the option is
    *omitted* rather than set to some stand-in value, so the provider
    applies its own default.
    """
    wanted = [("model", "model", model)]
    if provider_contracts.route_selects_effort(effort):
        wanted.append(("thinking", "thought_level", effort))
    for config_id, category, value in wanted:
        if _current_option(options, category=category, option_id=config_id) != value:
            changed = transport.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": value},
            )
            if not isinstance(changed, Mapping):
                raise KimiBootstrapProtocol(
                    f"session/set_config_option for {config_id!r} returned no result object"
                )
            options = changed.get("configOptions")

    observed_model = _current_option(options, category="model", option_id="model")
    observed_effort = _current_option(options, category="thought_level", option_id="thinking")
    if observed_model != model or (
        provider_contracts.route_selects_effort(effort) and observed_effort != effort
    ):
        raise KimiBootstrapProtocol(
            f"session {session_id} settled on model={observed_model!r} effort="
            f"{observed_effort!r} rather than the requested model={model!r} effort={effort!r}; "
            "the resume command line carries no route option, so a wrong route here is "
            "permanent for this session"
        )
    return options


def _terminate(transport: AcpBootstrapTransport) -> Any:
    try:
        return transport.terminate()
    except Exception as exc:  # noqa: BLE001 - an unraisable teardown is an unproven exit
        raise KimiBootstrapNotDetached(
            f"terminating the minting process itself failed ({exc}); the process cannot "
            "be assumed gone"
        ) from exc


def bootstrap_intent(receipt: Mapping[str, Any], *, note: Optional[str] = None) -> dict[str, Any]:
    """Build the attachment intent a bootstrapped native launch may declare.

    The attachment store requires a zero-prompt bootstrap to assert that
    it sent no turn and fully detached.  Routing those assertions through
    this function means the only way to make them is to hold a receipt in
    which they are true — a caller cannot assert them from nothing.
    """
    if not isinstance(receipt, Mapping) or receipt.get("schema") != BOOTSTRAP_SCHEMA:
        raise KimiBootstrapInvalid(f"expected a {BOOTSTRAP_SCHEMA} receipt; got {receipt!r}")
    if receipt.get("sent_no_turn") is not True or receipt.get("detached_before_launch") is not True:
        raise KimiBootstrapInvalid(
            "receipt does not assert a turn-free, detached bootstrap; it cannot license "
            "a native attachment"
        )
    # Re-validated rather than trusted: the receipt may have crossed a
    # durable boundary since it was built.
    _validated_exit_proof(receipt.get("exit_proof"))
    return native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt=dict(receipt),
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
        note=note,
    )


class StdioAcpBootstrap:
    """The production transport: ``kimi acp`` over its own stdio pipes.

    Spawned directly rather than through the managed rendezvous launcher.
    That launcher exists to broker a UDS rendezvous and companion prompts
    for a long-lived bridge; a bootstrap that sends no turn raises no
    prompts and outlives nothing, so routing it through the launcher
    would add a second process to prove dead for no gain.

    ``env`` is required and never defaults to the ambient environment: a
    provider child inheriting an operator's shell environment is how
    unrelated credentials and routing end up inside a session record.
    """

    def __init__(
        self,
        *,
        kimi_binary: str,
        env: Mapping[str, str],
        working_directory: str,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout = float(request_timeout)
        self._next_id = 1
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._write_lock = threading.Lock()
        self._proc = subprocess.Popen(
            [kimi_binary, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=dict(env),
            cwd=working_directory,
        )
        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            self._proc.kill()
            self._proc.wait()
            raise KimiBootstrapProtocol("the bootstrap process pipes were not created")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    @property
    def pid(self) -> int:
        return self._proc.pid

    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        with contextlib.suppress(Exception):
            for line in self._proc.stdout:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    self._inbox.put(item)
        self._inbox.put(None)

    def _drain_stderr(self) -> None:
        # Drained so a chatty provider cannot deadlock on a full pipe, and
        # discarded rather than journaled: provider stderr can carry
        # credentials, and this transport feeds a durable receipt.
        assert self._proc.stderr is not None
        with contextlib.suppress(Exception):
            for _ in self._proc.stderr:
                pass

    def _send(self, value: dict[str, Any]) -> None:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self._write_lock:
            if self._proc.stdin is None:
                raise KimiBootstrapProtocol("the bootstrap process stdin is closed")
            self._proc.stdin.write(raw + "\n")
            self._proc.stdin.flush()

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KimiBootstrapProtocol(f"{method} did not answer within {self._timeout:g}s")
            try:
                item = self._inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if item is None:
                raise KimiBootstrapProtocol(
                    f"the bootstrap process closed its output before answering {method}"
                )
            if "method" in item and "id" in item:
                # A reverse request during a turn-free bootstrap is
                # refused, never granted: this conversation has no
                # authority to permit anything on the worker's behalf.
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": item["id"],
                        "error": {
                            "code": -32601,
                            "message": f"unsupported client method {item.get('method')!r}",
                        },
                    }
                )
                continue
            if item.get("id") != request_id:
                continue
            if "error" in item:
                raise KimiBootstrapProtocol(f"{method} failed: {item['error']!r}")
            result = item.get("result")
            if not isinstance(result, dict):
                raise KimiBootstrapProtocol(f"{method} returned no result object")
            return result

    def _wait(self, seconds: float) -> Optional[int]:
        try:
            return self._proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> dict[str, Any]:
        escalation = [STEP_STDIN_CLOSED]
        with contextlib.suppress(Exception):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        status = self._wait(EOF_GRACE_SECONDS)
        if status is None:
            escalation.append(STEP_SIGTERM)
            with contextlib.suppress(Exception):
                self._proc.terminate()
            status = self._wait(SIGNAL_GRACE_SECONDS)
        if status is None:
            escalation.append(STEP_SIGKILL)
            with contextlib.suppress(Exception):
                self._proc.kill()
            status = self._wait(SIGNAL_GRACE_SECONDS)
        if status is None:
            # Reported, not raised: the caller decides what an unproven
            # exit means, and it always means refusal.
            return {"pid": self._proc.pid, "escalation": escalation, "reaped": False}
        return {
            "pid": self._proc.pid,
            "exit_status": status,
            "escalation": escalation,
            "reaped": True,
        }
