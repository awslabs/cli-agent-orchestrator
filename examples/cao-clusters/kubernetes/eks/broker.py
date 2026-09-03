"""Narrow worker broker for the CAO elastic Kubernetes topology.

The broker is the only component in the fleet that can create a pod, and it is
deliberately the smallest surface that can: a client names an agent profile and
a provider, and gets back a lease. Image, command, volumes, service account and
resource limits are all broker-controlled, so a compromised supervisor cannot
turn `assign_elastic` into arbitrary pod creation.

It is also the fleet's only supervision point. CAO decides a turn is over by
watching the agent's TUI, which means an agent that speaks before its first tool
call gets its terminal killed and its task reported as a success (measured: 3.1s
on a task needing 36s). The supervisor cannot tell that apart from real success,
so it never releases the lease. The broker can: it holds the lease, it knows
`complete_assignment` never arrived, and it reaps on a deadline and records WHY.
`GET /workers` is where that truth is legible - see the reaper below.

The broker is also the workers' narrow gateway to supervisor-owned state.
NetworkPolicy denies workers any direct route to the supervisor control API;
the broker authenticates each worker's release token and forwards only inbox
delivery and the four explicit memory operations.

Finally it is the operator's way in. Worker Services are ClusterIP and last only
as long as a task, so `cao worker` cannot port-forward to each one; instead it
holds the broker token and the broker forwards an allowlisted set of read and
send routes to the worker it names. See the operator plane at the end of this
file for what is on that list and why.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("CAO_ELASTIC_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cao.broker")

NAMESPACE = os.environ.get("CAO_ELASTIC_NAMESPACE", "cao-cluster")
WORKER_IMAGE = os.environ["CAO_ELASTIC_WORKER_IMAGE"]
WORKSPACE_PVC = os.environ.get("CAO_ELASTIC_WORKSPACE_PVC", "cao-elastic-workspace")
SUPERVISOR_API_URL = os.environ.get("CAO_SUPERVISOR_API_URL", "http://cao-supervisor:9889").rstrip(
    "/"
)
BROKER_PUBLIC_URL = os.environ.get("CAO_ELASTIC_BROKER_URL", "http://cao-worker-broker:9890")
BROKER_TOKEN = os.environ["CAO_ELASTIC_BROKER_TOKEN"]
WORKSPACE_ROOT = os.environ.get("CAO_ELASTIC_WORKSPACE_ROOT", "/home/cao/workspace/jobs")
PROJECT_ID = os.environ.get("CAO_ELASTIC_PROJECT_ID", "cao-cluster")
WORKER_SERVICE_ACCOUNT = os.environ.get("CAO_ELASTIC_WORKER_SERVICE_ACCOUNT", "cao-elastic-worker")
# Outer bound on a worker's life, as a backstop for a broker that forgot it.
# This used to be activeDeadlineSeconds on the worker's Job; a Deployment cannot
# carry it at all (see the pod spec), so it is now enforced by
# _sweep_orphan_workers, which deletes worker workloads this broker holds no live
# lease for once they are older than this. Same number, same effect, one
# REAPER_INTERVAL of slack.
WORKER_TIMEOUT = int(os.environ.get("CAO_ELASTIC_WORKER_TIMEOUT", "3600"))
READY_TIMEOUT = int(os.environ.get("CAO_ELASTIC_READY_TIMEOUT", "300"))
# Does POST /workers block until the worker pod reports Ready?
#
# It used to, unconditionally, and that single `await` was the largest term in
# placement latency: 22s of a 22s call, of which 16s was the worker's own boot.
# Worse, readiness was never the condition the caller actually needed. The caller
# reaches the worker through its SERVICE, and a pod passing its readiness probe
# is not yet a Service with a published endpoint and every node's rules
# programmed - so the gate was simultaneously slow AND too weak, and a
# five-way fan-out reliably lost a worker to `connect timeout=10.0` on a pod that
# `kubectl` showed as 1/1 Running.
#
# So the lease is now returned as soon as the Deployment and Service exist, and the
# CALLER waits - on the thing it actually depends on, by polling the worker's
# /health through the Service until it answers (see _wait_remote_ready in
# mcp_server/server.py). One wait instead of two, on the correct predicate.
#
# What the synchronous gate did usefully was fail a worker that never came up at
# all, inside READY_TIMEOUT. That has not been dropped - it moved into the
# reaper, which now settles a lease whose pod has not reported Ready in
# READY_TIMEOUT as `failed`. Same deadline, same terminal state, detected within
# one REAPER_INTERVAL of it instead of exactly at it.
#
# Set to 1/true to restore the old blocking behaviour.
GATE_ON_READY = os.environ.get("CAO_ELASTIC_GATE_ON_READY", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
# The real deadline. A leased worker that has not called /complete within this
# many seconds is reaped and marked `expired`. Set well above the slowest task a
# participant will delegate and well below WORKER_TIMEOUT, so the broker reaps
# first and the Kubernetes deadline never has to.
COMPLETION_TIMEOUT = int(os.environ.get("CAO_ELASTIC_COMPLETION_TIMEOUT", "900"))
REAPER_INTERVAL = int(os.environ.get("CAO_ELASTIC_REAPER_INTERVAL", "15"))
# How long a finished lease stays queryable through GET /workers. This is the
# audit trail for the false-success race, and since _release deletes the
# Deployment immediately, this in-memory record is the ONLY place a settled
# worker's verdict survives at all.
LEASE_RETENTION = int(os.environ.get("CAO_ELASTIC_LEASE_RETENTION", "3600"))

# Names the broker copies from its OWN environment into every worker pod. The
# Bedrock block lives in broker.yaml rather than here so that deploy.sh renders
# the region in one place and no model id is baked into this image.
WORKER_ENV_PASSTHROUGH = [
    name.strip()
    for name in os.environ.get(
        "CAO_ELASTIC_WORKER_ENV_PASSTHROUGH",
        "CLAUDE_CODE_USE_BEDROCK,"
        "AWS_REGION,"
        "ANTHROPIC_MODEL,"
        "ANTHROPIC_DEFAULT_OPUS_MODEL,"
        "ANTHROPIC_DEFAULT_SONNET_MODEL,"
        "ANTHROPIC_DEFAULT_HAIKU_MODEL,"
        "CAO_PROVIDER_INIT_TIMEOUT,"
        "CAO_MCP_REQUEST_TIMEOUT",
    ).split(",")
    if name.strip()
]
# Fail at startup, not at the first model call. A missing model pin does not
# break scheduling - the worker comes up Ready and then 401s the moment
# something reaches for an unentitled default, which reads as a model problem.
_absent = [name for name in WORKER_ENV_PASSTHROUGH if name not in os.environ]
if _absent:
    raise RuntimeError(
        "CAO_ELASTIC_WORKER_ENV_PASSTHROUGH names variables that are not set on "
        f"the broker: {', '.join(_absent)}. Set them in broker.yaml, or "
        "shorten the passthrough list."
    )

config.load_incluster_config()
apps_api = client.AppsV1Api()
core_api = client.CoreV1Api()

# worker_id -> lease lifecycle and placement observations
_leases: dict[str, dict] = {}
_leases_lock = threading.Lock()


# The provider a worker can actually run is a property of the DEPLOYMENT - it is
# whichever CLI the worker image ships - so the deployment sets it rather than
# this file hard-coding one. It must match the image: a delegation resolves the
# provider from the TARGET's profile store, so a mismatch fails with
# "<cli> was not found", naming a CLI the caller never asked for.
#
# Defaults to claude_code because that is what Dockerfile builds by
# default. Set CAO_ELASTIC_WORKER_PROVIDER=kiro_cli on the broker Deployment when
# the worker image carries kiro-cli instead.
DEFAULT_WORKER_PROVIDER = os.environ.get("CAO_ELASTIC_WORKER_PROVIDER", "claude_code")

# Optional Secret carrying whatever credential the worker's provider needs, for a
# provider that authenticates with a key rather than with Pod Identity. Mounted
# with envFrom and optional=True, so the Bedrock path - where no such Secret
# exists - is unaffected rather than stuck in CreateContainerConfigError.
#
# Deliberately not a named variable: the broker should not have to learn a new
# environment variable each time a provider is added. Whatever keys the Secret
# holds become the worker's environment.
PROVIDER_CREDENTIALS_SECRET = "cao-provider-credentials"
# ConfigMap the fleet panel mounts. The broker keeps its worker entries current;
# see the "fleet view" section below.
FLEET_CONFIG_MAP = os.environ.get("CAO_ELASTIC_FLEET_CONFIG_MAP", "cao-fleet-config")
FLEET_CONFIG_KEY = os.environ.get("CAO_ELASTIC_FLEET_CONFIG_KEY", "fleet.json")
FLEET_CONFIG_RETRIES = 3


class WorkerRequest(BaseModel):
    agent_profile: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    callback_terminal_id: str = Field(pattern=r"^[a-f0-9]{8}$")
    provider: str = Field(
        default_factory=lambda: DEFAULT_WORKER_PROVIDER,
        pattern=r"^[a-zA-Z0-9_-]{1,64}$",
    )


class WorkerLease(BaseModel):
    worker_id: str
    target_host: str
    working_directory: str
    session_name: str
    release_token: str


class WorkerStatus(BaseModel):
    worker_id: str
    state: str
    reason: Optional[str] = None
    agent_profile: Optional[str] = None
    provider: Optional[str] = None
    age_seconds: int


class TerminalEndedRequest(BaseModel):
    terminal_id: str = Field(pattern=r"^[a-f0-9]{8}$")


class WorkerAuthorization(BaseModel):
    worker_id: str
    callback_terminal_id: str
    session_name: str
    agent_profile: str
    provider: str
    working_directory: str


_RELEASE_TOKEN_ANNOTATION = "cao.aws/release-token"
_CALLBACK_TERMINAL_ANNOTATION = "cao.aws/callback-terminal-id"
_SESSION_NAME_ANNOTATION = "cao.aws/session-name"
_AGENT_PROFILE_ANNOTATION = "cao.aws/agent-profile"
_PROVIDER_ANNOTATION = "cao.aws/provider"
_WORKING_DIRECTORY_ANNOTATION = "cao.aws/working-directory"


def _require_broker_token(value: Optional[str]) -> None:
    if not value or not hmac.compare_digest(value, BROKER_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


def _workload_name(worker_id: str) -> str:
    """Name of the worker's Deployment, and of the Service in front of it.

    The two share a name deliberately: the Service is what everything else
    dials, so the string is also the worker's DNS label. It has not changed
    since workers were Jobs, which is why moving to Deployments moved no host
    name, no session name and no NetworkPolicy selector.
    """
    return f"cao-worker-{worker_id}"


def _session_name(worker_id: str) -> str:
    return f"cao-worker-{worker_id}"


def _working_directory(worker_id: str) -> str:
    return f"{WORKSPACE_ROOT}/{worker_id}"


def _labels(worker_id: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "cao-elastic-worker",
        "app.kubernetes.io/part-of": "cao-elastic-fleet",
        "cao.aws/worker-id": worker_id,
    }


def _worker_deployment(
    worker_id: str,
    release_token: str,
    request: WorkerRequest,
) -> client.V1Deployment:
    """One Deployment per worker, replicas=1.

    A worker used to be a Job, and the reason it no longer is comes down to what
    the pod actually does: `entrypoint.sh` ends in `exec cao-server`, so the
    container never exits and the Job never completed. Its lifecycle was doing no
    work - nothing read `.status.succeeded`, and the reaper watches Pods, not
    Jobs. What a Job did contribute was a batch shape that made `cao worker` and
    `cao fleet` awkward to explain and impossible to restart.

    Not a StatefulSet, despite the per-pod identity looking like a fit: release is
    out of order. `_release` deletes whichever single worker just finished, and a
    StatefulSet only scales down from the highest ordinal, so releasing worker 3
    of 10 would mean keeping or killing 4-9 with it. Identity is already solved by
    the per-worker Service below, which is what callers dial.
    """
    name = _workload_name(worker_id)
    labels = _labels(worker_id)
    working_directory = _working_directory(worker_id)
    env = [
        client.V1EnvVar(name="CAO_BIND_HOST", value="0.0.0.0"),
        client.V1EnvVar(name="CAO_API_PORT", value="9889"),
        client.V1EnvVar(
            name="CAO_ALLOWED_HOSTS",
            value=f"{name},{name}.{NAMESPACE}.svc.cluster.local,localhost",
        ),
        # One terminal per worker, and one worker per task. The isolation is the
        # point of the topology: a wedged agent cannot starve a sibling of a
        # terminal slot, because it has no siblings.
        client.V1EnvVar(name="CAO_MAX_TERMINALS", value="1"),
        client.V1EnvVar(name="CAO_HOME_DIR", value="/home/cao/.cao/state"),
        # The provider MUST be pinned, and here it always is: the store is a
        # fresh emptyDir per worker, so the profile the task needs is installed
        # with its provider at pod start and cannot drift.
        client.V1EnvVar(
            name="CAO_INSTALL_PROFILES",
            value=f"{request.agent_profile}:{request.provider}",
        ),
        # Warm Bedrock AFTER the server is listening, not before.
        #
        # The two throwaway model calls the entrypoint makes measured 8.3s of an
        # 18s pod readiness, and on a worker they buy very little: marketplace
        # activation is per ACCOUNT and the supervisor already paid it at
        # provisioning, warming the same pinned model ids. The supervisor keeps
        # the blocking default, so the account is still activated by something
        # whose startup nobody is timing.
        client.V1EnvVar(name="CAO_WARM_PROVIDER", value="background"),
        # Workers reach supervisor-owned memory only through the authenticated
        # broker gateway; they have no NetworkPolicy path to port 9889.
        client.V1EnvVar(name="CAO_MEMORY_API_URL", value=BROKER_PUBLIC_URL),
        client.V1EnvVar(name="CAO_PROJECT_ID", value=PROJECT_ID),
        client.V1EnvVar(name="CAO_ELASTIC_WORKER_ID", value=worker_id),
        client.V1EnvVar(name="CAO_ELASTIC_BROKER_URL", value=BROKER_PUBLIC_URL),
        client.V1EnvVar(name="CAO_ELASTIC_RELEASE_TOKEN", value=release_token),
        client.V1EnvVar(name="CAO_ELASTIC_WORKING_DIRECTORY", value=working_directory),
    ]
    # Bedrock credentials come from Pod Identity on WORKER_SERVICE_ACCOUNT, so on
    # that path there is no provider secret to read - only configuration to
    # forward. A key-authenticated provider gets its credential from
    # PROVIDER_CREDENTIALS_SECRET via env_from below.
    env.extend(
        client.V1EnvVar(name=name_, value=os.environ[name_]) for name_ in WORKER_ENV_PASSTHROUGH
    )
    mounts = [
        client.V1VolumeMount(name="state", mount_path="/home/cao/.cao"),
        client.V1VolumeMount(
            name="workspace",
            mount_path="/home/cao/workspace",
        ),
    ]
    init = client.V1Container(
        name="prepare-workspace",
        image="public.ecr.aws/docker/library/busybox:1.36",
        command=["sh", "-c", f"mkdir -p {working_directory}"],
        volume_mounts=[mounts[1]],
        security_context=client.V1SecurityContext(
            run_as_user=1000,
            run_as_group=1000,
        ),
    )
    container = client.V1Container(
        name="cao-node",
        image=WORKER_IMAGE,
        env=env,
        env_from=[
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name=PROVIDER_CREDENTIALS_SECRET,
                    optional=True,
                )
            )
        ],
        ports=[client.V1ContainerPort(name="http", container_port=9889)],
        volume_mounts=mounts,
        resources=client.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "1Gi"},
            limits={"cpu": "1", "memory": "3Gi"},
        ),
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(
                path="/health",
                port=9889,
                http_headers=[client.V1HTTPHeader(name="Host", value="localhost")],
            ),
            # Both values are a floor on how fast this pod can be USED, and both
            # are paid on every task in this topology rather than once at startup.
            #
            # They were 5 and 3, chosen when CAO's boot was 12-14s: at that scale
            # an initial delay of 5s was free and a 3s period cost at most 3s.
            # Moving the Bedrock warm-up off this path and seeding the state
            # directory takes boot to roughly 2-3s, at which point the OLD numbers
            # dominate what is left - a 5s delay against a 2.5s boot is 2.5s of
            # pure idle, and then up to 3s more waiting for a probe to come round.
            # A saving elsewhere turns into a floor here, so they move together.
            #
            # 0 rather than 1 on the delay: the first probe then fires immediately
            # and simply fails, which costs one connection refused in the events
            # and never costs a second of latency. `period_seconds=1` makes the
            # worst-case wait after the server binds 1s instead of 3s.
            initial_delay_seconds=0,
            period_seconds=1,
        ),
    )
    pod_spec = client.V1PodSpec(
        # A Deployment accepts no other value. It also means a worker whose
        # cao-server dies comes back instead of staying dead - see the restart
        # check in _reap_once, which releases the lease, because the pod that
        # comes back has lost the agent and (on a replacement pod) its emptyDir.
        restart_policy="Always",
        # NO activeDeadlineSeconds here. It is the one Job property with no
        # equivalent on a Deployment, and not merely in the sense of being
        # ineffective: Kubernetes REFUSES the Deployment outright, with
        # `spec.template.spec.activeDeadlineSeconds: Forbidden:
        # activeDeadlineSeconds in ReplicaSet is not Supported` (422). Setting it
        # does not weaken the cap, it stops every worker from being created. What
        # the field bought is now _sweep_orphan_workers; see there for why age
        # rather than existence is the trigger.
        # Pod Identity injects its own projected token volume via the webhook,
        # so the default SA mount stays off: nothing in a worker should be able
        # to talk to the API server.
        automount_service_account_token=False,
        service_account_name=WORKER_SERVICE_ACCOUNT,
        security_context=client.V1PodSecurityContext(
            fs_group=1000,
            fs_group_change_policy="OnRootMismatch",
        ),
        # PREFERRED, never required. A required rule would leave a worker
        # Pending on a two-node cluster the moment both nodes hold something,
        # which turns a scheduling preference into a failed delegation. This
        # only asks the scheduler to keep work off the supervisor's node, and to
        # spread concurrent workers, when it can.
        affinity=client.V1Affinity(
            pod_anti_affinity=client.V1PodAntiAffinity(
                preferred_during_scheduling_ignored_during_execution=[
                    client.V1WeightedPodAffinityTerm(
                        weight=100,
                        pod_affinity_term=client.V1PodAffinityTerm(
                            topology_key="kubernetes.io/hostname",
                            label_selector=client.V1LabelSelector(
                                match_labels={"app.kubernetes.io/name": "cao-supervisor"}
                            ),
                        ),
                    ),
                    client.V1WeightedPodAffinityTerm(
                        weight=50,
                        pod_affinity_term=client.V1PodAffinityTerm(
                            topology_key="kubernetes.io/hostname",
                            label_selector=client.V1LabelSelector(
                                match_labels={"app.kubernetes.io/name": "cao-elastic-worker"}
                            ),
                        ),
                    ),
                ]
            )
        ),
        init_containers=[init],
        containers=[container],
        volumes=[
            client.V1Volume(
                name="state",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="workspace",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=WORKSPACE_PVC
                ),
            ),
        ],
        termination_grace_period_seconds=30,
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=pod_spec,
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name,
            labels=labels,
            # Read back by _require_release_token off the Deployment, so these
            # must stay on the workload's own metadata and not on the template.
            annotations={
                _RELEASE_TOKEN_ANNOTATION: release_token,
                _CALLBACK_TERMINAL_ANNOTATION: request.callback_terminal_id,
                _SESSION_NAME_ANNOTATION: _session_name(worker_id),
                _AGENT_PROFILE_ANNOTATION: request.agent_profile,
                _PROVIDER_ANNOTATION: request.provider,
                _WORKING_DIRECTORY_ANNOTATION: working_directory,
            },
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"cao.aws/worker-id": worker_id}),
            template=template,
            # Recreate, not the default RollingUpdate. Nothing updates a worker
            # Deployment today, but if anything ever did, RollingUpdate would
            # briefly run two pods that share one working directory on the RWX
            # workspace volume, and the Service would balance across both.
            strategy=client.V1DeploymentStrategy(type="Recreate"),
        ),
    )


def _worker_service(worker_id: str, workload: client.V1Deployment) -> client.V1Service:
    """Per-worker ClusterIP, owned by the Deployment so it cannot outlive it.

    Without the ownerReference a Service leaks whenever the Deployment is removed
    by anything other than _release - a `kubectl delete deployment`, a namespace
    cleanup that catches one and not the other. Garbage collection then leaves a
    Service whose selector matches nothing, and the next lease looks healthy
    while resolving to a black hole.

    This Service is also what makes a Deployment enough: callers dial the name,
    never the pod, so a worker keeps one stable address across a restart even
    though its pod name changes.
    """
    name = _workload_name(worker_id)
    if not (workload.metadata and workload.metadata.uid):
        # Only reachable if this is called with an unsubmitted Deployment; the
        # API server always assigns a uid on create. Refuse rather than fall back
        # to an unowned Service, which would leak silently.
        raise RuntimeError(f"cannot own worker Service {name}: workload has no uid (not created?)")
    return client.V1Service(
        metadata=client.V1ObjectMeta(
            name=name,
            labels=_labels(worker_id),
            owner_references=[
                client.V1OwnerReference(
                    api_version="apps/v1",
                    kind="Deployment",
                    name=workload.metadata.name,
                    uid=workload.metadata.uid,
                    controller=True,
                    block_owner_deletion=False,
                )
            ],
        ),
        spec=client.V1ServiceSpec(
            selector={"cao.aws/worker-id": worker_id},
            ports=[client.V1ServicePort(name="http", port=9889, target_port=9889)],
        ),
    )


def _pod_ready(pod: client.V1Pod) -> bool:
    conditions = (pod.status.conditions if pod.status else None) or []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


# Poll interval while waiting for readiness, and how long the fast interval
# lasts. A worker becomes Ready in single-digit seconds, so a flat 1s poll spent
# up to a second of every lease waiting on its own timer - but a flat 0.25s poll
# would issue 1200 list calls against the API server for a worker that is never
# coming up, times however many are stuck. Fast while the answer is plausibly
# imminent, then back off.
_READY_POLL_FAST = 0.25
_READY_POLL_SLOW = 1.0
_READY_POLL_FAST_WINDOW = 10.0


def _wait_ready(worker_id: str) -> None:
    started = time.monotonic()
    deadline = started + READY_TIMEOUT
    selector = f"cao.aws/worker-id={worker_id}"
    while time.monotonic() < deadline:
        pods = core_api.list_namespaced_pod(NAMESPACE, label_selector=selector).items
        for pod in pods:
            if _pod_ready(pod):
                return
            if pod.status.phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"worker pod ended before readiness: {pod.status.phase}")
        elapsed = time.monotonic() - started
        time.sleep(_READY_POLL_FAST if elapsed < _READY_POLL_FAST_WINDOW else _READY_POLL_SLOW)
    raise TimeoutError(f"worker {worker_id} did not become ready in {READY_TIMEOUT}s")


# --- fleet view -------------------------------------------------------------
#
# The panel renders whatever fleet.json lists and cannot discover an elastic
# worker, because one is created on demand with a generated id. The broker owns that
# lifecycle, so it publishes each leased worker into the ConfigMap the panel
# mounts and withdraws it on release.
#
# The panel re-reads the file on every /api/fleet request, so no restart is
# needed. A mounted ConfigMap is refreshed on the kubelet's sync period, so the
# view can lag a lease.


def _worker_machine(worker_id: str) -> dict[str, str]:
    """The panel's fleet entry for one worker."""
    return {
        "name": f"worker-{worker_id}",
        "host": f"{_workload_name(worker_id)}.{NAMESPACE}.svc.cluster.local",
        "label": f"Worker {worker_id}",
        "role": "worker",
    }


def _fleet_with(doc: dict, worker_id: str) -> dict:
    """Return `doc` with this worker present exactly once.

    Pure so the merge rules can be read on their own: entries the broker does not
    own -- the supervisor, anything an operator added -- are carried through
    untouched, and re-publishing replaces rather than duplicates.
    """
    machines = [m for m in doc.get("machines", []) if m.get("name") != f"worker-{worker_id}"]
    machines.append(_worker_machine(worker_id))
    return {**doc, "machines": machines}


def _fleet_without(doc: dict, worker_id: str) -> dict:
    """Return `doc` with this worker absent. Idempotent."""
    machines = [m for m in doc.get("machines", []) if m.get("name") != f"worker-{worker_id}"]
    return {**doc, "machines": machines}


def _update_fleet_config(worker_id: str, publish: bool) -> None:
    """Add or remove this worker in the panel's ConfigMap.

    Never raises. The fleet view is an operator convenience; a worker that runs
    but is missing from the panel is a cosmetic fault, while a lease that fails
    because a ConfigMap write did is a real one.

    Retries on 409: two concurrent leases read-modify-write the same object, so
    the loser must re-read rather than clobber the winner's entry.
    """
    for attempt in range(FLEET_CONFIG_RETRIES):
        try:
            cm = core_api.read_namespaced_config_map(FLEET_CONFIG_MAP, NAMESPACE)
            raw = (cm.data or {}).get(FLEET_CONFIG_KEY)
            if raw is None:
                # Nothing to merge into. Publishing a fleet from scratch would
                # invent a supervisor entry this code cannot know.
                log.warning(
                    "fleet config %s/%s has no %s key; skipping fleet view update",
                    NAMESPACE,
                    FLEET_CONFIG_MAP,
                    FLEET_CONFIG_KEY,
                )
                return
            doc = json.loads(raw)
            updated = _fleet_with(doc, worker_id) if publish else _fleet_without(doc, worker_id)
            cm.data[FLEET_CONFIG_KEY] = json.dumps(updated, indent=2) + "\n"
            core_api.replace_namespaced_config_map(FLEET_CONFIG_MAP, NAMESPACE, cm)
            return
        except ApiException as exc:
            if exc.status == 409 and attempt < FLEET_CONFIG_RETRIES - 1:
                continue
            log.warning("could not update fleet view for worker %s: %s", worker_id, exc)
            return
        except Exception as exc:  # malformed JSON, missing ConfigMap, anything
            log.warning("could not update fleet view for worker %s: %s", worker_id, exc)
            return


def _release(worker_id: str) -> None:
    name = _workload_name(worker_id)
    # First, so the panel stops probing a host that is about to disappear. This is
    # the one funnel for every removal path -- delete, complete, and every reaper
    # verdict via _release_and_settle -- so withdrawing here covers all of them.
    _update_fleet_config(worker_id, publish=False)
    try:
        # Foreground, so this returns only once the ReplicaSet and the pod are
        # gone too. Background would let the lease settle while a worker pod is
        # still holding a node's worth of memory.
        apps_api.delete_namespaced_deployment(
            name,
            NAMESPACE,
            propagation_policy="Foreground",
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
    # Belt and braces alongside the ownerReference: an explicit release should
    # not wait on garbage collection.
    try:
        core_api.delete_namespaced_service(name, NAMESPACE)
    except ApiException as exc:
        if exc.status != 404:
            raise


# The two states in which a worker still exists. Everything else -- released,
# completed, failed, terminated, expired -- is terminal, which is why _settle
# refuses to move a lease twice and the operator plane refuses to dial one.
_LIVE_LEASE_STATES = frozenset({"creating", "leased"})


def _settle(worker_id: str, state: str, reason: Optional[str] = None) -> bool:
    with _leases_lock:
        lease = _leases.get(worker_id)
        if lease is None:
            return False
        if lease["state"] not in _LIVE_LEASE_STATES:
            return False
        lease["state"] = state
        lease["reason"] = reason
        lease["settled_at"] = time.monotonic()
        return True


def _release_and_settle(worker_id: str, state: str, reason: Optional[str] = None) -> None:
    _settle(worker_id, state, reason)
    try:
        _release(worker_id)
    except Exception as exc:  # pragma: no cover - reaper must not die
        log.warning("release of worker %s failed: %s", worker_id, exc)


def _sweep_orphan_workers() -> None:
    """Delete worker workloads this broker holds no live lease for.

    This is what replaced `activeDeadlineSeconds`, and the replacement was forced
    rather than chosen: a Deployment's pod template may not carry that field, so
    the Kubernetes-side cap that used to hold when the broker was not reaping had
    to move into the broker. Which sounds circular, and is not quite: what breaks
    a lease is not the process dying, it is the process forgetting.

    Leases live in this process's memory. A broker restart therefore forgets every
    one of them, and the ordinary reaper walks `_leases` -- so a worker minted by
    the previous incarnation can never be settled by the next one, no matter how
    long it runs. Under Jobs those orphans died on the Kubernetes deadline about an
    hour later. Nothing else would collect them: their Service is ClusterIP, their
    pod is Ready, and the supervisor terminal holding the release token is gone
    too.

    **Age, not absence of a lease, is the trigger.** A broker that restarts while
    five workers are mid-task must not kill five running tasks -- under Jobs it
    did not, and a task that finishes can still reach `/complete`. So an orphan
    gets the same WORKER_TIMEOUT it always had, measured from pod creation, and
    only then is it swept.

    A worker with a LIVE lease is never touched here: the reaper owns those, with
    reasons this function cannot supply. A worker whose lease has already settled
    is fair game, because settling calls `_release` -- so if the workload is still
    standing, that release failed, and this is the retry.
    """
    try:
        pods = core_api.list_namespaced_pod(
            NAMESPACE,
            label_selector="app.kubernetes.io/name=cao-elastic-worker",
        ).items
    except ApiException as exc:  # pragma: no cover - transient API errors
        log.warning("orphan sweep could not list worker pods: %s", exc)
        return

    now = datetime.now(timezone.utc)
    for pod in pods:
        meta = pod.metadata
        worker_id = (meta.labels or {}).get("cao.aws/worker-id") if meta else None
        if not worker_id:
            continue
        with _leases_lock:
            lease = _leases.get(worker_id)
            if lease is not None and lease["state"] in _LIVE_LEASE_STATES:
                continue
        created = meta.creation_timestamp if meta else None
        if created is None:
            continue
        age = (now - created).total_seconds()
        if age <= WORKER_TIMEOUT:
            continue
        log.warning(
            "orphan sweep: worker %s has no live lease and is %ss old, deleting",
            worker_id,
            int(age),
        )
        try:
            _release(worker_id)
        except Exception as exc:  # pragma: no cover - sweep must not kill the reaper
            log.warning("orphan sweep could not delete worker %s: %s", worker_id, exc)


def _reap_once() -> None:
    """Release leases that will never be completed, and say why.

    Four distinct failures land here, and none of them is visible to the caller:

    - `terminated`: the one-shot terminal ended without `complete_assignment`,
      or a pod that had already been observed disappeared/ended while leased,
      or the pod restarted or was replaced under the lease. The terminal-ended
      signal catches the TUI turn-detection race directly; Pod phase cannot see a
      dead tmux window while cao-server remains running.
    - `failed`: the pod never reported Ready within READY_TIMEOUT - unschedulable,
      ImagePullBackOff, a crash-looping entrypoint. This case used to be caught
      synchronously inside POST /workers, which is why the lease could be handed
      back only after a wait nobody wanted; see GATE_ON_READY. Moving it here
      keeps the deadline and gives up only exactness about when it is noticed.
    - `expired`: the pod is still Ready but /complete never arrived within
      COMPLETION_TIMEOUT. Without this the pod squats a whole node's worth of
      memory until the orphan sweep collects it, an hour later.

    The restart case is the one thing the Deployment made the reaper responsible
    for. Under a Job, `restartPolicy: Never` and `backoffLimit: 0` meant a worker
    that died stayed dead, and the existing "pod gone" and "pod Failed" branches
    caught it. A Deployment brings it back, so without the check below the reaper
    would see a Ready pod and keep the lease open for COMPLETION_TIMEOUT while the
    agent that lease refers to no longer exists.
    """
    now = time.monotonic()
    with _leases_lock:
        open_ids = [wid for wid, l in _leases.items() if l["state"] == "leased"]
        stale = [
            wid
            for wid, l in _leases.items()
            if l["state"] != "leased"
            and l["settled_at"] is not None
            and now - l["settled_at"] > LEASE_RETENTION
        ]
        for wid in stale:
            del _leases[wid]

    for worker_id in open_ids:
        with _leases_lock:
            lease = _leases.get(worker_id)
            if lease is None or lease["state"] != "leased":
                continue
            age = now - lease["leased_at"]
            ever_ready = lease.get("ready_at") is not None
            pod_observed = lease.get("pod_observed_at") is not None
            known_pod_uid = lease.get("pod_uid")

        selector = f"cao.aws/worker-id={worker_id}"
        try:
            pods = core_api.list_namespaced_pod(NAMESPACE, label_selector=selector).items
        except ApiException as exc:  # pragma: no cover - transient API errors
            log.warning("reaper could not list pods for %s: %s", worker_id, exc)
            continue

        if not pods:
            if pod_observed:
                _release_and_settle(worker_id, "terminated", "worker pod disappeared while leased")
                log.warning("worker %s: pod gone while leased, released", worker_id)
            elif age > READY_TIMEOUT:
                _release_and_settle(
                    worker_id,
                    "failed",
                    f"worker pod was not created within {READY_TIMEOUT}s - check "
                    "the Deployment and its ReplicaSet, scheduling, and pod events",
                )
                log.warning(
                    "worker %s: no pod created after %ss, released",
                    worker_id,
                    int(age),
                )
            continue

        if not pod_observed:
            with _leases_lock:
                lease = _leases.get(worker_id)
                if lease is not None and lease["pod_observed_at"] is None:
                    lease["pod_observed_at"] = now
                    # Remembered so a REPLACEMENT pod can be told from the
                    # original. A replacement has a fresh emptyDir, so its
                    # cao-server has no profile store, no session and no agent.
                    lease["pod_uid"] = pods[0].metadata.uid

        # A ReplicaSet replacing the pod, or the kubelet restarting the container
        # in place. Either way the tmux window and the agent inside it are gone,
        # so the lease can never be completed and holding it open only delays the
        # caller's error by COMPLETION_TIMEOUT.
        #
        # Membership rather than pods[0]: during a replacement the selector
        # matches both the terminating pod and the new one, in no fixed order.
        if known_pod_uid is not None:
            live_uids = {p.metadata.uid for p in pods if p.metadata and p.metadata.uid}
            if known_pod_uid not in live_uids:
                _release_and_settle(
                    worker_id,
                    "terminated",
                    f"worker pod was replaced after {int(age)}s - the new pod has "
                    "an empty state volume, so the leased agent is gone",
                )
                log.warning("worker %s: pod replaced while leased, released", worker_id)
                continue

        restarts = sum(
            (cs.restart_count or 0) for cs in (pods[0].status.container_statuses or [])
        )
        if restarts:
            _release_and_settle(
                worker_id,
                "terminated",
                f"worker container restarted {restarts}x after {int(age)}s - "
                "cao-server came back without the agent it was hosting",
            )
            log.warning("worker %s: container restarted while leased, released", worker_id)
            continue

        phase = pods[0].status.phase
        if phase in {"Failed", "Succeeded"}:
            _release_and_settle(
                worker_id,
                "terminated",
                f"worker pod reached {phase} without calling complete_assignment "
                f"after {int(age)}s - the task was NOT necessarily done, see the "
                f"turn-detection race in the workshop notes",
            )
            log.warning("worker %s: pod %s while leased, released", worker_id, phase)
            continue

        # Readiness is observed here rather than waited on, so record the first
        # sighting. It is what separates "never came up" from "came up and then
        # stopped talking", which are the same age but different bugs - and it
        # keeps the deadline one-way: a worker that goes NotReady later is a
        # completion problem, and COMPLETION_TIMEOUT owns it.
        if not ever_ready:
            if _pod_ready(pods[0]):
                with _leases_lock:
                    lease = _leases.get(worker_id)
                    if lease is not None and lease["ready_at"] is None:
                        lease["ready_at"] = now
            elif age > READY_TIMEOUT:
                _release_and_settle(
                    worker_id,
                    "failed",
                    f"worker pod never reported Ready within {READY_TIMEOUT}s "
                    f"(phase {phase}) - it was leased but never usable; check "
                    f"scheduling, the image pull, and the pod's events",
                )
                log.warning(
                    "worker %s: not ready after %ss (phase %s), released",
                    worker_id,
                    int(age),
                    phase,
                )
                continue

        if age > COMPLETION_TIMEOUT:
            _release_and_settle(
                worker_id,
                "expired",
                f"no completion within {COMPLETION_TIMEOUT}s",
            )
            log.warning("worker %s: lease expired after %ss, released", worker_id, int(age))

    # Last, and outside the per-lease loop: this one walks the CLUSTER rather than
    # the ledger, which is the only way to see a worker the ledger has forgotten.
    _sweep_orphan_workers()


def _reaper() -> None:  # pragma: no cover - background loop
    while True:
        try:
            _reap_once()
        except Exception as exc:
            log.exception("reaper iteration failed: %s", exc)
        time.sleep(REAPER_INTERVAL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    thread = threading.Thread(target=_reaper, name="cao-lease-reaper", daemon=True)
    thread.start()
    log.info(
        "broker up: namespace=%s image=%s completion_timeout=%ss " "lease_returns=%s forwarding=%s",
        NAMESPACE,
        WORKER_IMAGE,
        COMPLETION_TIMEOUT,
        # Worth one field in the startup line: it is the difference between a
        # 22-second POST /workers and a 1-second one, and the symptom of having
        # it wrong is "the broker got slow again" with nothing else to look at.
        "after-ready" if GATE_ON_READY else "on-create",
        ",".join(WORKER_ENV_PASSTHROUGH),
    )
    yield


app = FastAPI(title="CAO Elastic Worker Broker", lifespan=lifespan)


def _require_release_token(worker_id: str, value: Optional[str]) -> client.V1Deployment:
    """The worker's own Deployment is the record of what it was leased for.

    Kept on the workload rather than in `_leases` on purpose: it survives a broker
    restart, so a worker that calls /complete after the broker was rescheduled is
    still recognised instead of being told its identity is invalid.
    """
    try:
        workload = apps_api.read_namespaced_deployment(_workload_name(worker_id), NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail="worker not found") from exc
        raise
    expected = (workload.metadata.annotations or {}).get(_RELEASE_TOKEN_ANNOTATION, "")
    if not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="invalid release token")
    return workload


def _require_worker_gateway(
    worker_id: Optional[str],
    release_token: Optional[str],
) -> WorkerAuthorization:
    if not worker_id or not re.fullmatch(r"[a-f0-9]{8}", worker_id):
        raise HTTPException(status_code=401, detail="invalid worker identity")
    workload = _require_release_token(worker_id, release_token)
    annotations = workload.metadata.annotations or {}
    try:
        return WorkerAuthorization(
            worker_id=worker_id,
            callback_terminal_id=annotations[_CALLBACK_TERMINAL_ANNOTATION],
            session_name=annotations[_SESSION_NAME_ANNOTATION],
            agent_profile=annotations[_AGENT_PROFILE_ANNOTATION],
            provider=annotations[_PROVIDER_ANNOTATION],
            working_directory=annotations[_WORKING_DIRECTORY_ANNOTATION],
        )
    except (KeyError, ValueError) as exc:
        log.error("worker %s has incomplete gateway authorization annotations", worker_id)
        raise HTTPException(
            status_code=403,
            detail="worker gateway authorization is incomplete",
        ) from exc


def _proxy_to_supervisor(
    path: str,
    *,
    body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, str]] = None,
) -> Response:
    try:
        upstream = requests.post(
            f"{SUPERVISOR_API_URL}{path}",
            json=body,
            params=params,
            allow_redirects=False,
            timeout=(5.0, 30.0),
        )
    except requests.RequestException as exc:
        log.warning("supervisor gateway request failed for %s: %s", path, exc)
        raise HTTPException(status_code=502, detail="supervisor gateway unavailable") from exc
    headers = {}
    content_type = upstream.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/terminals/{receiver_id}/inbox/messages")
def gateway_inbox_message(
    receiver_id: str,
    sender_id: str,
    message: str,
    x_cao_worker_id: Optional[str] = Header(default=None),
    x_cao_release_token: Optional[str] = Header(default=None),
) -> Response:
    """Authenticated worker callback; only this inbox route is forwarded."""
    authorization = _require_worker_gateway(x_cao_worker_id, x_cao_release_token)
    if receiver_id != authorization.callback_terminal_id:
        raise HTTPException(status_code=403, detail="callback receiver is not authorized")
    return _proxy_to_supervisor(
        f"/terminals/{receiver_id}/inbox/messages",
        # Never trust the caller's sender_id. The authenticated lease identity
        # is the only identity this worker may assert on the supervisor.
        params={"sender_id": authorization.worker_id, "message": message},
    )


def _gateway_memory(
    path: str,
    body: dict[str, Any],
    worker_id: Optional[str],
    release_token: Optional[str],
) -> Response:
    authorization = _require_worker_gateway(worker_id, release_token)
    bound_body = dict(body)
    # The worker controls its request body, including terminal_context. Replace
    # every identity-bearing field with the immutable lease claims persisted on
    # the Deployment so session/agent scopes cannot be redirected laterally.
    bound_body["terminal_context"] = {
        "terminal_id": authorization.worker_id,
        "session_name": authorization.session_name,
        "provider": authorization.provider,
        "agent_profile": authorization.agent_profile,
        "cwd": authorization.working_directory,
    }
    return _proxy_to_supervisor(path, body=bound_body)


@app.post("/internal/memory/store")
def gateway_memory_store(
    body: dict[str, Any],
    x_cao_worker_id: Optional[str] = Header(default=None),
    x_cao_release_token: Optional[str] = Header(default=None),
) -> Response:
    return _gateway_memory("/internal/memory/store", body, x_cao_worker_id, x_cao_release_token)


@app.post("/internal/memory/recall")
def gateway_memory_recall(
    body: dict[str, Any],
    x_cao_worker_id: Optional[str] = Header(default=None),
    x_cao_release_token: Optional[str] = Header(default=None),
) -> Response:
    return _gateway_memory("/internal/memory/recall", body, x_cao_worker_id, x_cao_release_token)


@app.post("/internal/memory/forget")
def gateway_memory_forget(
    body: dict[str, Any],
    x_cao_worker_id: Optional[str] = Header(default=None),
    x_cao_release_token: Optional[str] = Header(default=None),
) -> Response:
    return _gateway_memory("/internal/memory/forget", body, x_cao_worker_id, x_cao_release_token)


@app.post("/internal/memory/context")
def gateway_memory_context(
    body: dict[str, Any],
    x_cao_worker_id: Optional[str] = Header(default=None),
    x_cao_release_token: Optional[str] = Header(default=None),
) -> Response:
    return _gateway_memory("/internal/memory/context", body, x_cao_worker_id, x_cao_release_token)


@app.get("/workers", response_model=list[WorkerStatus])
def list_workers(
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> list[WorkerStatus]:
    """Lease ledger, including settled leases and why they settled.

    This is the endpoint to read after a delegation that claimed success and
    produced nothing: a `terminated` entry names the turn-detection race, where
    the supervisor's own transcript shows only a clean success.
    """
    _require_broker_token(x_cao_broker_token)
    now = time.monotonic()
    with _leases_lock:
        snapshot = [(wid, dict(lease)) for wid, lease in _leases.items()]
    return [
        WorkerStatus(
            worker_id=wid,
            state=lease["state"],
            reason=lease["reason"],
            agent_profile=lease["agent_profile"],
            provider=lease["provider"],
            age_seconds=int(now - lease["leased_at"]),
        )
        for wid, lease in sorted(snapshot, key=lambda item: item[1]["leased_at"])
    ]


@app.post("/workers", response_model=WorkerLease)
def create_worker(
    request: WorkerRequest,
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> WorkerLease:
    _require_broker_token(x_cao_broker_token)
    worker_id = secrets.token_hex(4)
    release_token = secrets.token_urlsafe(32)
    name = _workload_name(worker_id)
    with _leases_lock:
        _leases[worker_id] = {
            "state": "creating",
            "reason": None,
            "leased_at": time.monotonic(),
            "settled_at": None,
            "ready_at": None,
            "pod_observed_at": None,
            # Set once, at the first sighting, and then compared on every sweep;
            # see the replacement check in _reap_once.
            "pod_uid": None,
            "agent_profile": request.agent_profile,
            "provider": request.provider,
            "callback_terminal_id": request.callback_terminal_id,
        }
    try:
        # Deployment first, so the Service can be created owned by it. The pod
        # does not need its own DNS name to boot - the readiness probe goes
        # straight to the pod IP, and the supervisor only resolves the Service
        # after this call returns a lease.
        workload = apps_api.create_namespaced_deployment(
            NAMESPACE,
            _worker_deployment(worker_id, release_token, request),
        )
        core_api.create_namespaced_service(NAMESPACE, _worker_service(worker_id, workload))
        # The reaper ignores `creating` leases. Deployment-to-Pod creation is
        # asynchronous, so `leased` still does not imply a Pod exists; the
        # separate pod_observed_at marker distinguishes "not created yet" from
        # "disappeared after creation".
        with _leases_lock:
            lease = _leases.get(worker_id)
            if lease is not None and lease["state"] == "creating":
                lease["state"] = "leased"
        # Published here rather than after readiness, because this call no longer
        # waits for it: a lease asserts PLACED, not ready. The panel therefore
        # shows a booting worker as unreachable for the seconds it takes to come
        # up, which is the honest reading of its probe -- and the alternative,
        # gating the fleet view on GATE_ON_READY, would hide every worker on the
        # default non-blocking path.
        _update_fleet_config(worker_id, publish=True)
        # Both objects now exist, which is the whole of what a lease asserts:
        # this worker_id is yours, here is where it will answer, here is the
        # token that releases it. Whether it is answering YET is the caller's
        # question to ask, of the Service it will actually use. See
        # GATE_ON_READY above for why waiting here was both slow and wrong.
        if GATE_ON_READY:
            _wait_ready(worker_id)
            with _leases_lock:
                lease = _leases.get(worker_id)
                if lease is not None:
                    lease["ready_at"] = time.monotonic()
    except Exception as exc:
        _release_and_settle(worker_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    return WorkerLease(
        worker_id=worker_id,
        target_host=f"{name}.{NAMESPACE}.svc.cluster.local",
        working_directory=_working_directory(worker_id),
        session_name=_session_name(worker_id),
        release_token=release_token,
    )


@app.delete("/workers/{worker_id}")
def delete_worker(
    worker_id: str,
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    _require_broker_token(x_cao_broker_token)
    _settle(worker_id, "released", "released by caller")
    _release(worker_id)
    return {"released": True}


@app.post("/workers/{worker_id}/complete")
def complete_worker(
    worker_id: str,
    background_tasks: BackgroundTasks,
    x_cao_release_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    _require_release_token(worker_id, x_cao_release_token)
    _settle(worker_id, "completed", None)
    background_tasks.add_task(_release, worker_id)
    return {"release_scheduled": True}


@app.post("/workers/{worker_id}/terminal-ended")
def terminal_ended(
    worker_id: str,
    body: TerminalEndedRequest,
    background_tasks: BackgroundTasks,
    x_cao_release_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    """Settle a one-shot worker whose terminal ended without completion."""
    _require_release_token(worker_id, x_cao_release_token)
    settled = _settle(
        worker_id,
        "terminated",
        f"terminal {body.terminal_id} ended without calling complete_assignment; "
        "the task was not confirmed complete",
    )
    if settled:
        background_tasks.add_task(_release, worker_id)
    return {"release_scheduled": settled}


# ---------------------------------------------------------------------------
# Operator plane: `cao worker` reaching a leased worker from outside the cluster.
#
# Everything above this line is the fleet talking to itself. This is for a human,
# or the CLI acting for one, holding the broker token: one port-forward to the
# broker reaches every worker. Port-forwarding each worker instead is not a
# workflow - their Services are ClusterIP and exist only for the length of a
# task, so the name to forward to does not exist until a lease is taken and is
# gone before anyone types it.
#
# It is an allowlist of (method, path) and not a pass-through, for a reason worth
# stating plainly: cao-server is an unauthenticated command-execution surface, so
# forwarding arbitrary paths would publish the whole of it, on every worker, to
# anyone holding one token. The list below is what `cao worker list/status/send`
# and `cao worker sessions/attach` actually call, and nothing else.
#
# `POST /terminals/{id}/input` IS on it. That types into a live agent, which
# sounds like the line not to cross, but the same token already creates and
# deletes workers outright - being able to talk to a worker you can delete is not
# an escalation, and `cao worker send` is the verb the whole plane exists for.
#
# Adding a route is a one-line change here. Deleting the allowlist is not a
# simplification.
_WORKER_API_PORT = 9889

# One path segment. Deliberately narrow enough to exclude `/`, so segment counts
# in the patterns below are load-bearing, and wide enough for a percent-encoded
# session name - `cao session` quotes them with safe='' before they get here.
_SEGMENT = r"[A-Za-z0-9._~%-]+"

_WORKER_API_ALLOWLIST: dict[str, tuple[re.Pattern[str], ...]] = {
    "GET": (
        re.compile(r"health"),
        re.compile(r"sessions"),
        re.compile(rf"sessions/{_SEGMENT}/terminals"),
        re.compile(rf"terminals/{_SEGMENT}"),
        re.compile(rf"terminals/{_SEGMENT}/output"),
        re.compile(rf"terminals/{_SEGMENT}/inbox/messages"),
    ),
    "POST": (re.compile(rf"terminals/{_SEGMENT}/input"),),
}

# Request headers forwarded to the worker. An allowlist, because the header that
# must NOT travel is `x-cao-broker-token`: it is the broker's credential for the
# broker's own API, a worker has no use for it, and a worker is the one pod here
# running an agent that could be talked into repeating what it was handed.
_FORWARD_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-type",
    }
)

# Response headers dropped on the way back. The hop-by-hop set describes the
# broker<->worker connection rather than this one, and uvicorn writes its own
# `date` and `server`.
#
# `content-length` and `content-encoding` are in here for a different reason, and
# it is the one that bites: this proxy buffers, and `requests` has already
# decompressed the body by the time it is read, so both headers now describe
# bytes that no longer exist. Passing them through serves a gzip header over
# plain text. The panel's streaming proxy keeps them precisely because it never
# touches the bytes.
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "date",
        "server",
        "content-length",
        "content-encoding",
    }
)

# Log tail ceiling. A whole worker boot log is a few hundred lines, and the point
# of a cap is that `cao worker logs` cannot be turned into a way to pull an
# arbitrary amount of an agent's transcript through the broker in one call.
_LOG_TAIL_MAX = 2000


def _worker_api_target(worker_id: str) -> tuple[str, str]:
    """Where a worker answers: (url base, Host header). The pod's IP, not the Service.

    The Service name is the obvious address and it does not work from here. On EKS
    with the VPC CNI's NetworkPolicy agent, a pod's egress is matched against the
    address the pod DIALLED, before kube-proxy rewrites it - so a rule written as
    `to: podSelector: cao-elastic-worker` never matches traffic aimed at a worker's
    ClusterIP, and the connection hangs until it times out. Proven on a live
    cluster: from this pod, the worker's pod IP connects in 0.00s and its ClusterIP
    times out, and adding that one ClusterIP to the policy as an ipBlock makes it
    connect. The supervisor reaches workers by Service name only because its own
    egress rule is `podSelector: {}` with no ports - an allow-all the agent does not
    have to resolve.

    The alternative was an ipBlock for the whole Service CIDR on 9889, which would
    let the broker reach every ClusterIP in the cluster to buy back an address it
    does not need. Dialling the pod directly stays inside the existing podSelector
    rule, and the pod is looked up per request, so a ReplicaSet replacement is
    picked up on the next call rather than cached into a 502.

    The Host header still carries the Service name: it is what the worker lists in
    CAO_ALLOWED_HOSTS, and a bare pod IP there would fail its DNS-rebinding check.
    """
    name = _workload_name(worker_id)
    host = f"{name}.{NAMESPACE}.svc.cluster.local"
    pod = _worker_pod(worker_id)
    ip = pod.status.pod_ip if pod.status else None
    if not ip:
        raise HTTPException(
            status_code=503,
            detail=f"worker {worker_id} has no pod IP yet",
        )
    return f"http://{ip}:{_WORKER_API_PORT}", host


def _require_leased_worker(worker_id: str) -> None:
    """Refuse to dial a worker the ledger knows is gone, and say why it is gone.

    Without this, a request to a released worker fails on DNS and surfaces as a
    502 - which reads as a broken cluster rather than a finished task. The lease
    ledger already knows better, and its `reason` is the useful half of the
    answer.

    An unrecognised worker_id is deliberately NOT refused. Leases live in this
    process's memory while workloads live in the API server, so after a broker
    restart every surviving worker is unknown here and every one of them is still
    perfectly reachable.
    """
    if not re.fullmatch(r"[a-f0-9]{8}", worker_id):
        raise HTTPException(status_code=404, detail="worker not found")
    with _leases_lock:
        lease = _leases.get(worker_id)
        state = lease["state"] if lease else None
        reason = lease["reason"] if lease else None
    if state is not None and state not in _LIVE_LEASE_STATES:
        detail = f"worker {worker_id} is no longer leased ({state})"
        if reason:
            detail = f"{detail}: {reason}"
        raise HTTPException(status_code=409, detail=detail)


def _worker_api_allowed(method: str, path: str) -> bool:
    return any(
        pattern.fullmatch(path)
        for pattern in _WORKER_API_ALLOWLIST.get(method.upper(), ())
    )


def _forward_to_worker(
    worker_id: str,
    method: str,
    path: str,
    *,
    query: str,
    body: bytes,
    headers: dict[str, str],
) -> Response:
    base, host = _worker_api_target(worker_id)
    try:
        upstream = requests.request(
            method,
            f"{base}/{path}",
            params=query,
            data=body or None,
            headers={**headers, "host": host},
            allow_redirects=False,
            timeout=(5.0, 60.0),
        )
    except requests.RequestException as exc:
        log.warning("worker %s api request failed for %s %s: %s", worker_id, method, path, exc)
        raise HTTPException(
            status_code=502,
            detail=f"worker {worker_id} did not answer: {type(exc).__name__}",
        ) from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _DROP_RESPONSE_HEADERS
        },
    )


@app.api_route("/workers/{worker_id}/api/{path:path}", methods=["GET", "POST"])
async def worker_api(
    worker_id: str,
    path: str,
    request: Request,
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> Response:
    """Forward one allowlisted cao-server call to a leased worker.

    Buffered rather than streamed, unlike the panel's node proxy: nothing on the
    allowlist is a stream, and buffering is what lets the 502 above name the
    worker that failed instead of tearing down a response already in flight.
    """
    _require_broker_token(x_cao_broker_token)
    _require_leased_worker(worker_id)
    if ".." in path.split("/"):
        # requests leaves dot segments in the path and the worker's own server may
        # resolve them, which would step outside the route just matched.
        raise HTTPException(status_code=400, detail="invalid path")
    if not _worker_api_allowed(request.method, path):
        raise HTTPException(
            status_code=404,
            detail=f"'{request.method} /{path}' is not proxied to workers",
        )
    return await run_in_threadpool(
        _forward_to_worker,
        worker_id,
        request.method,
        path,
        query=request.url.query,
        body=await request.body(),
        headers={
            key: value
            for key, value in request.headers.items()
            if key.lower() in _FORWARD_REQUEST_HEADERS
        },
    )


def _worker_pod(worker_id: str) -> client.V1Pod:
    """The one pod behind a worker id, for callers that must name it or dial it."""
    pods = core_api.list_namespaced_pod(
        NAMESPACE,
        label_selector=f"cao.aws/worker-id={worker_id}",
    ).items
    if not pods:
        raise HTTPException(status_code=404, detail=f"worker {worker_id} has no pod")
    # A Deployment can show two pods at once - the replacement and the pod being
    # replaced - and the selector matches both in no fixed order. Prefer the one
    # that is running; on a boot failure there is no such pod and the first one's
    # log is exactly what the caller came for.
    running = [pod for pod in pods if (pod.status.phase if pod.status else None) == "Running"]
    return (running or pods)[0]


def _worker_pod_name(worker_id: str) -> str:
    return _worker_pod(worker_id).metadata.name


@app.get("/workers/{worker_id}/logs")
def worker_logs(
    worker_id: str,
    tail_lines: int = 200,
    follow: bool = False,
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> Response:
    """The worker container's log, which is where a boot failure is legible.

    `GET /workers` can say a lease `failed`; it cannot say the image pull was
    denied, or that the profile install died on a bad provider pin. Answering that
    needs `pods/log` `get` added to the broker Role - a real widening, and the
    narrowest one that makes a failed worker diagnosable without putting kubectl
    in the caller's hands. It is still not `pods/exec`: the broker can read what a
    worker printed and never run anything inside it.
    """
    _require_broker_token(x_cao_broker_token)
    _require_leased_worker(worker_id)
    tail = max(1, min(tail_lines, _LOG_TAIL_MAX))
    pod_name = _worker_pod_name(worker_id)
    if not follow:
        try:
            body = core_api.read_namespaced_pod_log(pod_name, NAMESPACE, tail_lines=tail)
        except ApiException as exc:
            if exc.status == 404:
                raise HTTPException(status_code=404, detail="worker pod not found") from exc
            raise HTTPException(
                status_code=502,
                detail=f"could not read worker log: {exc.reason}",
            ) from exc
        return Response(content=body, media_type="text/plain; charset=utf-8")

    def _stream():
        # Watch, not read_namespaced_pod_log(follow=True, _preload_content=False):
        # the client library sets `follow` for any function documenting it and
        # yields whole lines, so a live log arrives line by line instead of
        # stalling inside a fixed-size socket read.
        watcher = watch.Watch()
        try:
            for line in watcher.stream(
                core_api.read_namespaced_pod_log,
                name=pod_name,
                namespace=NAMESPACE,
                tail_lines=tail,
            ):
                yield f"{line}\n"
        except ApiException as exc:  # pragma: no cover - live cluster only
            log.warning("log follow for worker %s ended: %s", worker_id, exc)
        finally:
            watcher.stop()

    return StreamingResponse(_stream(), media_type="text/plain; charset=utf-8")
