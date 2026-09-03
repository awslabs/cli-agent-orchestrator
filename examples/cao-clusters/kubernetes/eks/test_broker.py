"""Offline exercise of broker.py: object construction + lease lifecycle.

Stubs the API server so the whole request path can run on a laptop. The point is
to catch what only shows up at the first lease on a live cluster - a misspelled
kwarg on a V1* model, a Deployment body the serializer mangles, a reaper that
never releases. Every V1* object is pushed through the client's real serializer,
which is what actually rejects a bad field name.

NOT part of the CAO test suite: broker.py lives outside the package and needs
fastapi + the Kubernetes client, neither of which is a CAO dependency. Run it in
a throwaway environment:

    uv venv /tmp/brokertest --python 3.12
    VIRTUAL_ENV=/tmp/brokertest uv pip install \\
        "fastapi>=0.104.0" "kubernetes>=30.0.0,<35.0.0" httpx
    /tmp/brokertest/bin/python examples/cao-clusters/kubernetes/eks/test_broker.py

Exits non-zero on the first failing expectation, and prints a PASS/FAIL line per
check.
"""
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ.update({
    "CAO_ELASTIC_WORKER_IMAGE": "111122223333.dkr.ecr.us-east-1.amazonaws.com/cao-server:2.4.1-cc3",
    "CAO_ELASTIC_BROKER_TOKEN": "test-token",
    "CAO_SUPERVISOR_API_URL": "http://cao-supervisor:9889",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_REGION": "us-east-1",
    "ANTHROPIC_MODEL": "global.anthropic.claude-opus-4-6-v1",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "global.anthropic.claude-opus-4-6-v1",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-opus-4-6-v1",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "CAO_PROVIDER_INIT_TIMEOUT": "180",
    "CAO_MCP_REQUEST_TIMEOUT": "240",
    "CAO_ELASTIC_REAPER_INTERVAL": "1",
    "CAO_ELASTIC_COMPLETION_TIMEOUT": "3",
})

from kubernetes import client as k8s
from kubernetes import config as k8s_config

k8s_config.load_incluster_config = lambda: None

STATE = {
    "deployments": {},
    "services": {},
    "pods": {},
    "deleted_deployments": [],
    "deleted_svcs": [],
}

# What the fake API server hands back for the NEXT pod it creates. Readiness used
# to be irrelevant to the fake (the broker waited for it, so a pod that was never
# Ready just hung), but the lease is now returned before readiness and the reaper
# owns the deadline - so both "came up" and "never came up" have to be expressible.
STATE["new_pods_ready"] = True
STATE["new_pods_phase"] = "Running"
STATE["create_pods"] = True
# Every fake pod gets a distinct uid, because the reaper now tells a REPLACEMENT
# pod from the original by uid and a fake that reused one would silently skip
# that branch.
STATE["pod_seq"] = 0
# Every read_namespaced_pod_log the broker makes, so the tail_lines cap can be
# asserted on what reached the API server rather than on what was asked for.
STATE["log_calls"] = []


def _fake_pod(name, labels):
    STATE["pod_seq"] += 1
    conditions = ([k8s.V1PodCondition(type="Ready", status="True")]
                  if STATE["new_pods_ready"] else [])
    return k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(
            name=f"{name}-{STATE['pod_seq']:05d}",
            uid=f"pod-uid-{STATE['pod_seq']}",
            labels=dict(labels),
            # The orphan sweep reads this, and a real pod always has it. Now, so
            # every pod these tests create is age 0 and no sweep fires behind the
            # lease-state sections; the sweep's own section backdates it.
            creation_timestamp=datetime.now(timezone.utc),
        ),
        status=k8s.V1PodStatus(
            phase=STATE["new_pods_phase"],
            conditions=conditions,
            # The operator plane dials this rather than the worker's Service name,
            # so a pod without one is a 502 waiting to happen. Loopback, because
            # section 10 points _WORKER_API_PORT at a stub server on 127.0.0.1 and
            # then lets the real _worker_api_target build the URL.
            pod_ip="127.0.0.1",
        ),
    )


class FakeApps:
    def create_namespaced_deployment(self, ns, body):
        body.metadata.uid = "uid-" + body.metadata.name
        STATE["deployments"][body.metadata.name] = body
        wid = body.metadata.labels["cao.aws/worker-id"]
        if STATE["create_pods"]:
            STATE["pods"][wid] = _fake_pod(body.metadata.name, body.metadata.labels)
        return body

    def read_namespaced_deployment(self, name, ns):
        if name not in STATE["deployments"]:
            raise k8s.rest.ApiException(status=404)
        return STATE["deployments"][name]

    def delete_namespaced_deployment(self, name, ns, propagation_policy=None):
        STATE["deleted_deployments"].append(name)
        STATE["deployments"].pop(name, None)


class FakeCore:
    def create_namespaced_service(self, ns, body):
        STATE["services"][body.metadata.name] = body
        return body

    def delete_namespaced_service(self, name, ns):
        STATE["deleted_svcs"].append(name)
        STATE["services"].pop(name, None)

    def list_namespaced_pod(self, ns, label_selector=None):
        # Two selectors reach here, and they mean different things. The reaper asks
        # by worker id; the orphan sweep asks by app name, because the whole point
        # of it is to find workers whose ids the broker no longer knows.
        key, value = label_selector.split("=", 1)
        if key == "app.kubernetes.io/name":
            return types.SimpleNamespace(
                items=[
                    pod
                    for pod in STATE["pods"].values()
                    if (pod.metadata.labels or {}).get(key) == value
                ]
            )
        pod = STATE["pods"].get(value)
        return types.SimpleNamespace(items=[pod] if pod else [])

    def read_namespaced_pod_log(self, name, ns, tail_lines=None, **kwargs):
        STATE["log_calls"].append({"pod": name, "tail_lines": tail_lines})
        return f"boot log of {name}\n"


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker  # noqa: E402  (must follow the env setup above)

broker.apps_api = FakeApps()
broker.core_api = FakeCore()

from fastapi.testclient import TestClient

FAILS = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def worker_request():
    return broker.WorkerRequest(
        agent_profile="developer",
        callback_terminal_id="abc12345",
    )


# --- 1. the Deployment body survives the real serializer ------------------
workload = broker._worker_deployment("deadbeef", "rt", worker_request())
wire = k8s.ApiClient().sanitize_for_serialization(workload)
spec = wire["spec"]["template"]["spec"]
env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}

check("deployment serializes to a dict", isinstance(wire, dict))
annotations = wire["metadata"]["annotations"]
check("deployment persists the authorized callback receiver",
      annotations["cao.aws/callback-terminal-id"] == "abc12345")
check("deployment persists the authorized memory session",
      annotations["cao.aws/session-name"] == "cao-worker-deadbeef")
check("deployment persists the authorized memory profile",
      annotations["cao.aws/agent-profile"] == "developer")
# The annotations are read back off the workload by _require_release_token, so
# putting them on the template instead would 401 every worker callback.
check("lease claims are on the workload, not the pod template",
      not (wire["spec"]["template"]["metadata"].get("annotations") or {}),
      json.dumps(wire["spec"]["template"]["metadata"]))

# --- 1b. the Deployment-shaped fields the Job did not have ----------------
check("exactly one replica", wire["spec"]["replicas"] == 1, str(wire["spec"].get("replicas")))
check("selector matches the worker id label",
      wire["spec"]["selector"]["matchLabels"] == {"cao.aws/worker-id": "deadbeef"},
      json.dumps(wire["spec"].get("selector")))
# RollingUpdate would briefly run two pods sharing one working directory on the
# RWX workspace volume, with the Service balancing across both.
check("update strategy is Recreate", wire["spec"]["strategy"]["type"] == "Recreate",
      json.dumps(wire["spec"].get("strategy")))
check("restartPolicy is Always, the only value a Deployment accepts",
      spec["restartPolicy"] == "Always", spec.get("restartPolicy"))
# The one Job property with no home on a Deployment at all. Setting it does not
# merely fail to work: the API server refuses the Deployment with
# `activeDeadlineSeconds in ReplicaSet is not Supported` (422), so a worker
# carrying it cannot be created. Found on a live cluster, because the fake
# apps_api below accepts any body -- which is exactly why this assertion is
# phrased as an absence and pinned here.
check("no activeDeadlineSeconds on the pod (a ReplicaSet template forbids it)",
      "activeDeadlineSeconds" not in spec,
      str(spec.get("activeDeadlineSeconds")))
check("no Job-only fields survive",
      not any(k in wire["spec"] for k in ("backoffLimit", "ttlSecondsAfterFinished")),
      json.dumps(sorted(wire["spec"])))
check("default provider is claude_code", env["CAO_INSTALL_PROFILES"] == "developer:claude_code",
      env.get("CAO_INSTALL_PROFILES"))
# A credential must never be a literal in the workload body - the broker's Role has
# no `secrets`, and a value here would end up in etcd and in
# `kubectl get deployment -o yaml`.
check("no provider credential inlined in the workload", "KIRO_API_KEY" not in env)

# The optional flag is the load-bearing half: without it the Bedrock path, which
# creates no such Secret, would hold every worker in CreateContainerConfigError.
env_from = spec["containers"][0].get("envFrom") or []
check("provider credentials come from envFrom",
      any(s.get("secretRef", {}).get("name") == "cao-provider-credentials" for s in env_from),
      env_from)
check("provider credential secret is optional",
      all(s["secretRef"].get("optional") is True for s in env_from if "secretRef" in s),
      env_from)
check("bedrock flag forwarded", env.get("CLAUDE_CODE_USE_BEDROCK") == "1")
check("region forwarded", env.get("AWS_REGION") == "us-east-1")
check("all four model tiers pinned",
      all(env.get(k) for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                               "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")))
check("both timeouts forwarded",
      env.get("CAO_PROVIDER_INIT_TIMEOUT") == "180" and env.get("CAO_MCP_REQUEST_TIMEOUT") == "240")
check("max terminals still 1", env.get("CAO_MAX_TERMINALS") == "1")
# Without this the panel can proxy every HTTP call to a worker but not its
# terminal: cao-server's WS peer-IP allowlist defaults to loopback and would
# close code 4003 on every attach from the panel pod.
check("terminal socket reachable from the panel",
      env.get("CAO_WS_ALLOWED_CLIENTS") == "*", env.get("CAO_WS_ALLOWED_CLIENTS"))
check("worker memory uses the authenticated broker gateway",
      env.get("CAO_MEMORY_API_URL") == "http://cao-worker-broker:9890",
      env.get("CAO_MEMORY_API_URL"))
check("worker warms its providers in the background",
      env.get("CAO_WARM_PROVIDER") == "background", env.get("CAO_WARM_PROVIDER"))
check("worker SA is cao-elastic-worker", spec["serviceAccountName"] == "cao-elastic-worker")
check("SA token not automounted", spec["automountServiceAccountToken"] is False)

aa = spec["affinity"]["podAntiAffinity"]
check("anti-affinity is preferred only",
      "preferredDuringSchedulingIgnoredDuringExecution" in aa
      and "requiredDuringSchedulingIgnoredDuringExecution" not in aa, json.dumps(aa)[:200])
terms = aa["preferredDuringSchedulingIgnoredDuringExecution"]
check("two anti-affinity terms", len(terms) == 2, str(len(terms)))
check("term 1 avoids the supervisor at weight 100",
      terms[0]["weight"] == 100
      and terms[0]["podAffinityTerm"]["labelSelector"]["matchLabels"]["app.kubernetes.io/name"]
      == "cao-supervisor")
check("term 2 spreads workers",
      terms[1]["podAffinityTerm"]["labelSelector"]["matchLabels"]["app.kubernetes.io/name"]
      == "cao-elastic-worker")
check("topologyKey is hostname on both",
      all(t["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname" for t in terms))
probe = spec["containers"][0]["readinessProbe"]
# The probe is the only thing standing between "the server answered" and "the
# Service has an endpoint", and every second of initialDelay is a second of every
# delegation a participant watches. /health is a constant-time dict return, so
# probing from t=0 every second costs the pod nothing it can measure.
check("readiness probe starts immediately", probe["initialDelaySeconds"] == 0,
      str(probe.get("initialDelaySeconds")))
check("readiness probe polls every second", probe["periodSeconds"] == 1,
      str(probe.get("periodSeconds")))

# --- 2. the Service is owned by the Deployment ---------------------------
try:
    broker._worker_service("deadbeef", workload)
    check("unsubmitted workload is refused rather than left unowned", False, "no error raised")
except RuntimeError as exc:
    check("unsubmitted workload is refused rather than left unowned", "has no uid" in str(exc))

workload = broker.apps_api.create_namespaced_deployment("cao-cluster", workload)  # assigns a uid
svc = broker._worker_service("deadbeef", workload)
swire = k8s.ApiClient().sanitize_for_serialization(svc)
owners = swire["metadata"].get("ownerReferences") or []
check("service has an ownerReference", len(owners) == 1, json.dumps(swire["metadata"]))
check("owner is the Deployment by uid",
      owners and owners[0]["kind"] == "Deployment"
      and owners[0]["apiVersion"] == "apps/v1"
      and owners[0]["uid"] == "uid-cao-worker-deadbeef",
      json.dumps(owners))

# The selector needs BOTH labels, and the redundant-looking one is the load-bearing
# one. worker-id alone selects the same single pod, but the VPC CNI includes a
# Service's ClusterIP in a PolicyEndpoint only when the Service selects on the
# labels the NetworkPolicy selects on. Drop the name label and the fleet panel gets
# a ConnectTimeout on every worker while its pod IP stays reachable — which is why
# this is asserted here rather than left to the manifest to imply.
sel = swire["spec"]["selector"]
check("service selects the worker by id", sel.get("cao.aws/worker-id") == "deadbeef",
      json.dumps(sel))
check("service also carries the label networkpolicy.yaml selects on",
      sel.get("app.kubernetes.io/name") == "cao-elastic-worker", json.dumps(sel))

# --- 3. the lease returns before readiness; the reaper owns the deadline --
#
# Deliberately ABOVE the TestClient block: the reaper only runs as a thread once
# lifespan has started, so calling _reap_once() by hand here is the one place
# these transitions can be driven a tick at a time instead of waited for.
check("readiness gating is off by default", broker.GATE_ON_READY is False)

# A reaper tick can overlap Kubernetes object creation. The lease must not be
# considered active until both the Deployment and Service exist.
with broker._leases_lock:
    broker._leases["cafefeed"] = {
        "state": "creating",
        "reason": None,
        "leased_at": time.monotonic() - (broker.READY_TIMEOUT + 1),
        "settled_at": None,
        "ready_at": None,
        "pod_observed_at": None,
        "pod_uid": None,
        "agent_profile": "developer",
        "provider": "claude_code",
    }
broker._reap_once()
check(
    "reaper ignores a lease while Kubernetes objects are being created",
    broker._leases["cafefeed"]["state"] == "creating",
    broker._leases["cafefeed"]["state"],
)
with broker._leases_lock:
    del broker._leases["cafefeed"]

# A Deployment exists before its ReplicaSet creates a Pod. Empty Pod lists are
# normal in that window and must not be called disappearance.
STATE["create_pods"] = False
lease_waiting_for_pod = broker.create_worker(
    worker_request(), "test-token"
)
waiting_id = lease_waiting_for_pod.worker_id
broker._reap_once()
check(
    "no Pod before first observation is left alone inside READY_TIMEOUT",
    broker._leases[waiting_id]["state"] == "leased",
    broker._leases[waiting_id]["state"],
)
with broker._leases_lock:
    broker._leases[waiting_id]["leased_at"] = time.monotonic() - (broker.READY_TIMEOUT + 1)
broker._reap_once()
check(
    "a Pod never created by the deadline is failed, not terminated",
    broker._leases[waiting_id]["state"] == "failed",
    broker._leases[waiting_id]["state"],
)
check(
    "never-created reason names Pod creation",
    "was not created" in (broker._leases[waiting_id]["reason"] or ""),
    str(broker._leases[waiting_id]["reason"]),
)
STATE["create_pods"] = True

# Once a Pod has been observed, an empty list really does mean disappearance.
observed_lease = broker.create_worker(worker_request(), "test-token")
observed_id = observed_lease.worker_id
broker._reap_once()
check(
    "reaper records the first Pod observation",
    broker._leases[observed_id]["pod_observed_at"] is not None,
)
STATE["pods"].pop(observed_id)
broker._reap_once()
check(
    "an observed Pod that disappears is terminated",
    broker._leases[observed_id]["state"] == "terminated",
    broker._leases[observed_id]["state"],
)

STATE["new_pods_ready"] = False
_t0 = time.monotonic()
lease0 = broker.create_worker(worker_request(), "test-token")
_elapsed = time.monotonic() - _t0
w0 = lease0.worker_id
# Under the old gate this call could not return at all until the pod was Ready,
# so a pod that never is would have hung here for READY_TIMEOUT.
check("create does not wait on a pod that is not Ready", _elapsed < 0.5, f"{_elapsed:.2f}s")
check("the lease is handed back regardless",
      lease0.target_host == f"cao-worker-{w0}.cao-cluster.svc.cluster.local",
      lease0.target_host)
check("readiness is unrecorded until something observes it",
      broker._leases[w0]["ready_at"] is None)

broker._reap_once()
check("a not-yet-Ready worker is left alone inside READY_TIMEOUT",
      broker._leases[w0]["state"] == "leased", json.dumps(broker._leases[w0], default=str))

with broker._leases_lock:
    broker._leases[w0]["leased_at"] = time.monotonic() - (broker.READY_TIMEOUT + 1)
broker._reap_once()
check("reaper fails a worker that never reported Ready",
      broker._leases[w0]["state"] == "failed", broker._leases[w0]["state"])
check("failed reason names never-Ready, not a completion timeout",
      "never reported Ready" in (broker._leases[w0]["reason"] or ""),
      str(broker._leases[w0]["reason"])[:200])
check("failed worker's deployment is released", f"cao-worker-{w0}" in STATE["deleted_deployments"],
      str(STATE["deleted_deployments"]))

# Once Ready has been SEEN, the readiness deadline is spent: a worker that goes
# NotReady later is a completion problem, and must expire rather than fail.
STATE["new_pods_ready"] = True
lease1 = broker.create_worker(worker_request(), "test-token")
w1 = lease1.worker_id
broker._reap_once()
check("reaper records the first Ready sighting", broker._leases[w1]["ready_at"] is not None)
STATE["pods"][w1].status.conditions = []
with broker._leases_lock:
    broker._leases[w1]["leased_at"] = time.monotonic() - (broker.READY_TIMEOUT + 1)
broker._reap_once()
check("a worker that was once Ready expires rather than fails",
      broker._leases[w1]["state"] == "expired", broker._leases[w1]["state"])

# The old behaviour is still reachable for a fleet whose workers may be the first
# caller of a model in the account.
broker.GATE_ON_READY = True
try:
    lease2 = broker.create_worker(worker_request(), "test-token")
    check("GATE_ON_READY=1 returns a lease with readiness already recorded",
          broker._leases[lease2.worker_id]["ready_at"] is not None)

    STATE["new_pods_ready"] = False
    STATE["new_pods_phase"] = "Succeeded"
    try:
        broker.create_worker(worker_request(), "test-token")
        check("GATE_ON_READY=1 surfaces a pod that dies before readiness", False,
              "no error raised")
    except RuntimeError as exc:
        check("GATE_ON_READY=1 surfaces a pod that dies before readiness",
              "ended before readiness" in str(exc), str(exc))
        _dead = [wid for wid, l in broker._leases.items()
                 if l["state"] == "failed" and "ended before readiness" in (l["reason"] or "")]
        check("...and settles that lease failed rather than leaking it", len(_dead) == 1,
              str(_dead))
finally:
    broker.GATE_ON_READY = False
    STATE["new_pods_ready"] = True
    STATE["new_pods_phase"] = "Running"

# --- 3c. the ledger is a one-hour window, and its clock is settled_at -----
#
# `_release` deletes the Deployment immediately, so this in-memory row is the
# ONLY place a settled worker's verdict survives - and the reaper drops it
# LEASE_RETENTION seconds later. Nothing pinned that, and it is read wrong in two
# directions worth catching:
#
#   * as unbounded. `cao worker list --all` looks like a full history, so an
#     operator comes back after lunch for the `expired` reason and finds a table
#     that no longer mentions the worker. Absence is not evidence of a clean run.
#   * off the wrong clock. The AGE column is `now - leased_at`, and retention
#     runs from `settled_at`. A row can show an age well past the hour and still
#     be present, which makes a count cap the tempting (and wrong) explanation.
check("settled leases are kept for an hour by default",
      broker.LEASE_RETENTION == 3600, str(broker.LEASE_RETENTION))

def _settled(worker_id, *, state, settled_ago, leased_ago=None):
    """Put one synthetic settled lease in the ledger."""
    now = time.monotonic()
    with broker._leases_lock:
        broker._leases[worker_id] = {
            "state": state,
            "reason": "released by caller",
            "leased_at": now - (leased_ago if leased_ago is not None else settled_ago),
            "settled_at": None if settled_ago is None else now - settled_ago,
            "ready_at": now,
            "pod_observed_at": now,
            "pod_uid": None,
            "agent_profile": "developer",
            "provider": "claude_code",
        }

_settled("fade0001", state="completed", settled_ago=broker.LEASE_RETENTION - 5)
_settled("fade0002", state="expired", settled_ago=broker.LEASE_RETENTION + 5)
# Settled a second ago but leased long before the window: the row that proves
# which of the two timestamps the prune reads.
_settled("fade0003", state="released", settled_ago=1,
         leased_ago=broker.LEASE_RETENTION * 2)
# A lease still open, aged past the window. What saves it is that it has no
# settle time yet, not its state: without that guard a running worker's lease
# would be deleted out from under it, leaving the pod alive with nothing left
# that knows it is owed a release.
_settled("fade0004", state="leased", settled_ago=None,
         leased_ago=broker.LEASE_RETENTION * 2)
# The same row with a settle time it could not really have. `_settle` writes
# state and settled_at together under one lock, so nothing reachable is both
# open and settled - which makes the prune's `state != "leased"` clause pure
# belt and braces. Pinned anyway, because the clause looks load-bearing and the
# next reader should not be able to delete it and see a green suite.
_settled("fade0007", state="leased", settled_ago=broker.LEASE_RETENTION + 5,
         leased_ago=broker.LEASE_RETENTION * 2)

broker._reap_once()

check("a lease settled inside the window is still readable",
      "fade0001" in broker._leases)
check("a lease settled past the window is dropped",
      "fade0002" not in broker._leases)
check("retention runs from settled_at, not from leased_at",
      "fade0003" in broker._leases)
check("an open lease has no settle time, so no age can prune it",
      "fade0004" in broker._leases, str(broker._leases.get("fade0004", {}).get("state")))
check("an open lease survives even if it somehow carries a settle time",
      "fade0007" in broker._leases)

# A `creating` lease has no settle time at all, and `now - None` in that branch
# would take the reaper thread down with it - after which nothing is reaped.
_settled("fade0005", state="creating", settled_ago=None,
         leased_ago=broker.LEASE_RETENTION * 2)
broker._reap_once()
check("a lease with no settled_at survives the prune rather than crashing it",
      "fade0005" in broker._leases)

# One tick can settle a lease and one tick can prune it, but never the same tick:
# the prune reads settled_at from before this sweep, so a verdict is always
# readable for a full retention window after it is written.
_settled("fade0006", state="leased", settled_ago=None,
         leased_ago=broker.COMPLETION_TIMEOUT + 1)
broker._reap_once()
check("a verdict written this tick is not pruned by the same tick",
      "fade0006" in broker._leases,
      str(broker._leases.get("fade0006", {}).get("state")))

# And the coupling that makes that clause redundant deserves the pin more than
# the clause does. `_settle` is the only writer of settled_at anywhere in the
# broker, and it writes state in the same critical section, so "live" and
# "settled" cannot both be true. That matters more than it reads: the prune
# exempts the literal "leased" while every other test in the broker asks
# _LIVE_LEASE_STATES, so a `creating` lease is live and NOT exempt by state. It
# survives only because nothing gives it a settle time while it is coming up.
_settled("fade0008", state="creating", settled_ago=None, leased_ago=1)
_settled_ok = broker._settle("fade0008", "completed", "done")
with broker._leases_lock:
    _row = dict(broker._leases["fade0008"])
check("_settle stamps settled_at and leaves the live set in one critical section",
      _settled_ok
      and _row["state"] not in broker._LIVE_LEASE_STATES
      and _row["settled_at"] is not None,
      f"{_row['state']} settled_at={_row['settled_at'] is not None}")
check("a settled lease cannot be settled twice, so the first verdict is the one kept",
      broker._settle("fade0008", "released", None) is False
      and broker._leases["fade0008"]["reason"] == "done",
      str(broker._leases["fade0008"]["reason"]))

with broker._leases_lock:
    for _wid in ("fade0001", "fade0003", "fade0004", "fade0005", "fade0006",
                 "fade0007", "fade0008"):
        broker._leases.pop(_wid, None)

# --- 4. lease lifecycle over HTTP ---------------------------------------
with TestClient(broker.app) as c:
    worker_payload = {
        "agent_profile": "developer",
        "callback_terminal_id": "abc12345",
    }
    r = c.post("/workers", json=worker_payload)
    check("unauthenticated create is rejected", r.status_code == 401, str(r.status_code))

    H = {"X-CAO-Broker-Token": "test-token"}
    r = c.post("/workers", json=worker_payload, headers=H)
    check("create returns a lease", r.status_code == 200, r.text[:300])
    lease = r.json()
    wid = lease["worker_id"]
    check("deployment created first, then service",
          f"cao-worker-{wid}" in STATE["deployments"] and f"cao-worker-{wid}" in STATE["services"])
    check("target_host is the per-worker service FQDN",
          lease["target_host"] == f"cao-worker-{wid}.cao-cluster.svc.cluster.local",
          lease["target_host"])
    check("lease returns its bound session name",
          lease["session_name"] == f"cao-worker-{wid}", lease["session_name"])

    r = c.get("/workers", headers=H)
    check("ledger lists the open lease",
          r.status_code == 200 and any(w["worker_id"] == wid and w["state"] == "leased"
                                       for w in r.json()), r.text[:300])

    gateway_headers = {
        "X-CAO-Worker-ID": wid,
        "X-CAO-Release-Token": lease["release_token"],
    }
    r = c.post(
        "/terminals/abc12345/inbox/messages",
        params={"sender_id": "feed0001", "message": "done"},
    )
    check("gateway rejects an unauthenticated callback", r.status_code == 401, r.text[:200])

    upstream = Mock(
        status_code=200,
        content=b'{"success":true}',
        headers={"content-type": "application/json"},
    )
    with patch.object(broker.requests, "post") as post:
        r = c.post(
            "/terminals/ffffffff/inbox/messages",
            params={"sender_id": "eeeeeeee", "message": "lateral"},
            headers=gateway_headers,
        )
    check("gateway rejects a different callback receiver", r.status_code == 403, r.text[:200])
    check("rejected callback never reaches the supervisor", not post.called, str(post.call_args))

    with patch.object(broker.requests, "post", return_value=upstream) as post:
        r = c.post(
            "/terminals/abc12345/inbox/messages",
            params={"sender_id": "eeeeeeee", "message": "done"},
            headers=gateway_headers,
        )
    check("authenticated callback is forwarded", r.status_code == 200, r.text[:200])
    check(
        "callback forwards only to the fixed supervisor inbox route",
        post.call_args.args[0] == "http://cao-supervisor:9889/terminals/abc12345/inbox/messages",
        str(post.call_args),
    )
    check(
        "callback sender is derived from the authenticated worker",
        post.call_args.kwargs["params"]["sender_id"] == wid,
        str(post.call_args),
    )

    memory_upstream = Mock(
        status_code=200,
        content=b'{"context":"shared"}',
        headers={"content-type": "application/json"},
    )
    with patch.object(broker.requests, "post", return_value=memory_upstream) as post:
        r = c.post(
            "/internal/memory/context",
            json={
                "terminal_context": {
                    "terminal_id": "ffffffff",
                    "session_name": "cao-other-session",
                    "provider": "other_provider",
                    "agent_profile": "other_profile",
                    "cwd": "/workspace/other",
                },
                "budget_chars": 3000,
            },
            headers=gateway_headers,
        )
    check("authenticated memory request is forwarded", r.status_code == 200, r.text[:200])
    check(
        "memory forwards only to the fixed supervisor route",
        post.call_args.args[0] == "http://cao-supervisor:9889/internal/memory/context",
        str(post.call_args),
    )
    check(
        "memory identity is derived from the authenticated lease",
        post.call_args.kwargs["json"]["terminal_context"]
        == {
            "terminal_id": wid,
            "session_name": f"cao-worker-{wid}",
            "provider": "claude_code",
            "agent_profile": "developer",
            "cwd": f"/home/cao/workspace/workers/{wid}",
        },
        str(post.call_args),
    )

    r = c.post("/sessions", headers=gateway_headers)
    check("gateway exposes no supervisor session route", r.status_code == 404, r.text[:200])

    r = c.post(f"/workers/{wid}/complete", headers={"X-CAO-Release-Token": "wrong"})
    check("wrong release token is rejected", r.status_code == 401, str(r.status_code))

    r = c.post(f"/workers/{wid}/complete",
               headers={"X-CAO-Release-Token": lease["release_token"]})
    check("complete accepted with the right token", r.status_code == 200, r.text[:200])
    check("completing releases the deployment", f"cao-worker-{wid}" in STATE["deleted_deployments"],
          str(STATE["deleted_deployments"]))
    r = c.get("/workers", headers=H)
    check("ledger records completion",
          any(w["worker_id"] == wid and w["state"] == "completed" for w in r.json()), r.text[:300])

    # --- 5. a one-shot terminal ends, complete never arrives --------------
    r = c.post("/workers", json=worker_payload, headers=H)
    terminal_ended_lease = r.json()
    terminal_ended_id = terminal_ended_lease["worker_id"]
    r = c.post(
        f"/workers/{terminal_ended_id}/terminal-ended",
        json={"terminal_id": "abc12345"},
        headers={"X-CAO-Release-Token": terminal_ended_lease["release_token"]},
    )
    check("terminal-ended signal is accepted", r.status_code == 200, r.text[:200])
    check(
        "terminal-ended signal settles the lease immediately",
        broker._leases[terminal_ended_id]["state"] == "terminated",
        broker._leases[terminal_ended_id]["state"],
    )
    check(
        "terminal-ended reason names missing completion",
        "without calling complete_assignment"
        in (broker._leases[terminal_ended_id]["reason"] or ""),
        str(broker._leases[terminal_ended_id]["reason"]),
    )
    check(
        "terminal-ended signal releases the worker Deployment",
        f"cao-worker-{terminal_ended_id}" in STATE["deleted_deployments"],
        str(STATE["deleted_deployments"]),
    )

    # --- 6. pod terminal phase fallback, complete never arrives -----------
    r = c.post("/workers", json=worker_payload, headers=H)
    wid2 = r.json()["worker_id"]
    STATE["pods"][wid2].status.phase = "Succeeded"
    STATE["pods"][wid2].status.conditions = []
    deadline = time.time() + 12
    while time.time() < deadline:
        st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == wid2]
        if st and st[0]["state"] != "leased":
            break
        time.sleep(0.3)
    st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == wid2][0]
    check("reaper marks an early-terminated worker `terminated`", st["state"] == "terminated",
          json.dumps(st))
    check("reaper reason names the truth, not a success",
          st["reason"] and "NOT necessarily done" in st["reason"], str(st.get("reason"))[:200])
    check("reaper released the squatting deployment", f"cao-worker-{wid2}" in STATE["deleted_deployments"],
          str(STATE["deleted_deployments"]))

    # --- 7. completion deadline on a still-healthy pod --------------------
    r = c.post("/workers", json=worker_payload, headers=H)
    wid3 = r.json()["worker_id"]
    deadline = time.time() + 12
    while time.time() < deadline:
        st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == wid3]
        if st and st[0]["state"] != "leased":
            break
        time.sleep(0.3)
    st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == wid3][0]
    check("a healthy pod that never completes expires", st["state"] == "expired", json.dumps(st))
    check("expired deployment is released", f"cao-worker-{wid3}" in STATE["deleted_deployments"])

    # --- 7b. the two failures the Deployment introduced -------------------
    #
    # Under a Job these could not happen: restartPolicy Never plus backoffLimit 0
    # meant a dead worker stayed dead, and the "pod gone" / "pod Failed" branches
    # above caught it. A Deployment brings the worker back, Ready and useless, so
    # the reaper has to notice by identity rather than by phase.
    r = c.post("/workers", json=worker_payload, headers=H)
    restarted_id = r.json()["worker_id"]
    STATE["pods"][restarted_id].status.container_statuses = [
        k8s.V1ContainerStatus(
            name="cao-node", image="x", image_id="x", ready=True, restart_count=1
        )
    ]
    deadline = time.time() + 12
    while time.time() < deadline:
        st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == restarted_id]
        if st and st[0]["state"] != "leased":
            break
        time.sleep(0.3)
    st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == restarted_id][0]
    check("a restarted container settles the lease as terminated",
          st["state"] == "terminated", json.dumps(st))
    check("restart reason says the agent is gone, not that the task finished",
          st["reason"] and "restarted" in st["reason"], str(st.get("reason"))[:200])

    r = c.post("/workers", json=worker_payload, headers=H)
    replaced_id = r.json()["worker_id"]
    # Wait for the reaper to record the ORIGINAL pod's uid before swapping it. A
    # pod replaced before the broker ever saw the first one is indistinguishable
    # from a slow start, and COMPLETION_TIMEOUT owns that case instead.
    observed = time.time() + 6
    while time.time() < observed and broker._leases[replaced_id].get("pod_uid") is None:
        time.sleep(0.1)
    check("reaper records the first pod's uid",
          broker._leases[replaced_id].get("pod_uid") is not None)
    # A ReplicaSet replacing the pod: same labels, same Service, new uid, and a
    # brand new emptyDir with no profile store and no session in it.
    STATE["pods"][replaced_id] = _fake_pod(f"cao-worker-{replaced_id}",
                                           broker._labels(replaced_id))
    deadline = time.time() + 12
    while time.time() < deadline:
        st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == replaced_id]
        if st and st[0]["state"] != "leased":
            break
        time.sleep(0.3)
    st = [w for w in c.get("/workers", headers=H).json() if w["worker_id"] == replaced_id][0]
    check("a replacement pod settles the lease as terminated",
          st["state"] == "terminated", json.dumps(st))
    check("replacement reason names the empty state volume",
          st["reason"] and "empty state volume" in st["reason"], str(st.get("reason"))[:200])
    check("replaced worker's deployment is released",
          f"cao-worker-{replaced_id}" in STATE["deleted_deployments"],
          str(STATE["deleted_deployments"]))

    # --- 8. input validation still bounded -------------------------------
    r = c.post("/workers",
               json={**worker_payload, "agent_profile": "../../etc/passwd"}, headers=H)
    check("path-ish profile rejected", r.status_code == 422, str(r.status_code))
    r = c.post("/workers", json={**worker_payload, "provider": "a b"}, headers=H)
    check("provider with a space rejected", r.status_code == 422, str(r.status_code))
    r = c.post("/workers", json={**worker_payload, "image": "evil:latest"},
               headers=H)
    check("caller cannot inject an image",
          r.status_code in (200, 422)
          and (r.status_code == 422
               or STATE["deployments"][f"cao-worker-{r.json()['worker_id']}"]
               .spec.template.spec.containers[0].image == os.environ["CAO_ELASTIC_WORKER_IMAGE"]),
          r.text[:200])

    # --- 10. the operator plane: what `cao worker` can and cannot reach ----
    #
    # The allowlist is the whole security argument for this route, so the checks
    # that matter are the refusals. A stub stands in for the worker's cao-server;
    # the real one is unreachable offline, and what is being tested here is the
    # broker's decision to forward, not the node's answer.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    NODE_CALLS = []

    class _NodeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self):
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            NODE_CALLS.append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
            body = json.dumps({"ok": True, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("x-node-hint", "kept")
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond

        def log_message(self, *args):
            pass

    _node = HTTPServer(("127.0.0.1", 0), _NodeHandler)
    threading.Thread(target=_node.serve_forever, daemon=True).start()
    # Only the port is stubbed. _worker_api_target itself runs for real, so the
    # fake pod's IP is what gets dialled and the Service name it puts in the Host
    # header is checked below on what the stub received - the two halves of the
    # arrangement that makes a podSelector NetworkPolicy rule cover this hop.
    _saved_port = broker._WORKER_API_PORT
    broker._WORKER_API_PORT = _node.server_address[1]

    # The reaper's 3s completion deadline would settle this worker mid-section and
    # every proxied call would then correctly 409. Lift it for this section only.
    _saved_completion = broker.COMPLETION_TIMEOUT
    broker.COMPLETION_TIMEOUT = 3600
    try:
        r = c.post("/workers", json=worker_payload, headers=H)
        pid = r.json()["worker_id"]

        r = c.get(f"/workers/{pid}/api/sessions", headers=H)
        check("allowlisted GET reaches the worker", r.status_code == 200, r.text[:200])
        check("proxied path arrives unchanged at the worker",
              NODE_CALLS[-1]["path"] == "/sessions", NODE_CALLS[-1]["path"])
        # The request went to the pod's IP - the Service name does not resolve
        # here, and on EKS it is not covered by the broker's egress rule - but the
        # worker only trusts Host names it was given in CAO_ALLOWED_HOSTS.
        check("the Host header carries the Service name, not the pod IP",
              NODE_CALLS[-1]["headers"].get("host")
              == f"cao-worker-{pid}.cao-cluster.svc.cluster.local",
              str(NODE_CALLS[-1]["headers"].get("host")))
        # The one header that must not travel. It is the broker's credential for
        # the broker's own API, and a worker is the pod running an agent.
        check("broker token is not forwarded to the worker",
              "x-cao-broker-token" not in NODE_CALLS[-1]["headers"],
              json.dumps(sorted(NODE_CALLS[-1]["headers"])))
        check("worker response headers survive the hop",
              r.headers.get("x-node-hint") == "kept", json.dumps(dict(r.headers)))
        # requests decompressed the body already, so both of these would describe
        # bytes that no longer exist.
        check("content-encoding is not passed through",
              "content-encoding" not in {k.lower() for k in r.headers}, json.dumps(dict(r.headers)))

        r = c.get(f"/workers/{pid}/api/sessions/my-session/terminals", headers=H)
        check("a session's terminals are allowlisted", r.status_code == 200, r.text[:200])
        r = c.get(f"/workers/{pid}/api/terminals/abc12345/output?mode=last", headers=H)
        check("terminal output is allowlisted", r.status_code == 200, r.text[:200])
        check("the query string is forwarded",
              "mode=last" in NODE_CALLS[-1]["path"], NODE_CALLS[-1]["path"])
        # Deliberately on the list: `cao worker send` is the verb this plane
        # exists for, and the same token already deletes workers outright.
        r = c.post(f"/workers/{pid}/api/terminals/abc12345/input?message=hi", headers=H)
        check("sending input to a worker is allowlisted", r.status_code == 200, r.text[:200])

        before = len(NODE_CALLS)
        r = c.get(f"/workers/{pid}/api/settings", headers=H)
        check("an unlisted path is refused", r.status_code == 404, r.text[:200])
        r = c.get(f"/workers/{pid}/api/terminals/abc12345/websocket", headers=H)
        check("the pty socket route is refused", r.status_code == 404, r.text[:200])
        r = c.post(f"/workers/{pid}/api/sessions", headers=H)
        check("a listed path on an unlisted method is refused",
              r.status_code == 404, r.text[:200])
        # Percent-encoded, because Starlette decodes it back to `..` and the
        # segment pattern would otherwise match it as an ordinary terminal id.
        r = c.get(f"/workers/{pid}/api/terminals/%2e%2e/output", headers=H)
        check("an encoded dot segment is refused", r.status_code == 400, r.text[:200])
        check("nothing refused ever reached the worker",
              len(NODE_CALLS) == before, str(len(NODE_CALLS) - before))

        r = c.get(f"/workers/{pid}/api/sessions")
        check("unauthenticated proxy call is rejected", r.status_code == 401, r.text[:200])
        r = c.get("/workers/not-a-worker-id/api/sessions", headers=H)
        check("a malformed worker id is refused", r.status_code == 404, r.text[:200])

        # --- 10b. logs -----------------------------------------------------
        r = c.get(f"/workers/{pid}/logs", headers=H)
        check("logs return the container output", r.status_code == 200
              and "boot log of" in r.text, r.text[:200])
        check("logs are served as text", r.headers["content-type"].startswith("text/plain"),
              r.headers.get("content-type"))
        r = c.get(f"/workers/{pid}/logs", params={"tail_lines": 999999}, headers=H)
        check("tail_lines is capped at the broker's ceiling",
              STATE["log_calls"][-1]["tail_lines"] == broker._LOG_TAIL_MAX,
              str(STATE["log_calls"][-1]))
        r = c.get(f"/workers/{pid}/logs")
        check("unauthenticated log read is rejected", r.status_code == 401, r.text[:200])

        # --- 10c. a settled lease answers with WHY, not with a timeout ------
        broker._leases[pid]["state"] = "expired"
        broker._leases[pid]["reason"] = "no completion within 900s"
        r = c.get(f"/workers/{pid}/api/sessions", headers=H)
        check("a settled worker is refused with its lease state",
              r.status_code == 409 and "expired" in r.text, r.text[:200])
        check("the refusal carries the reaper's reason",
              "no completion within 900s" in r.text, r.text[:200])
        r = c.get(f"/workers/{pid}/logs", headers=H)
        check("logs are refused for a settled worker too",
              r.status_code == 409, r.text[:200])
        # An unknown worker_id is NOT refused: after a broker restart every
        # surviving worker is unknown here and all of them are still reachable.
        del broker._leases[pid]
        r = c.get(f"/workers/{pid}/api/sessions", headers=H)
        check("a worker with no lease row is still reachable",
              r.status_code == 200, r.text[:200])

        # A pod that has not been assigned an IP cannot be dialled at all. Saying
        # so beats a five-second connect timeout to nowhere.
        _ip = STATE["pods"][pid].status.pod_ip
        STATE["pods"][pid].status.pod_ip = None
        try:
            r = c.get(f"/workers/{pid}/api/sessions", headers=H)
            check("a worker with no pod IP is refused, not dialled",
                  r.status_code == 503 and "pod IP" in r.text, r.text[:200])
        finally:
            STATE["pods"][pid].status.pod_ip = _ip
    finally:
        broker.COMPLETION_TIMEOUT = _saved_completion
        broker._WORKER_API_PORT = _saved_port
        _node.shutdown()

    # --- 10d. the allowlist itself, without a transport ---------------------
    check("health is readable", broker._worker_api_allowed("GET", "health"))
    check("the sessions list is readable", broker._worker_api_allowed("GET", "sessions"))
    check("a terminal is readable", broker._worker_api_allowed("GET", "terminals/abc12345"))
    check("an inbox is readable",
          broker._worker_api_allowed("GET", "terminals/abc12345/inbox/messages"))
    check("input is writable", broker._worker_api_allowed("POST", "terminals/abc12345/input"))
    for method, path in [
        ("GET", "internal/memory/recall"),
        ("GET", "settings"),
        ("GET", "workflows"),
        ("POST", "sessions"),
        ("POST", "terminals/abc12345/inbox/messages"),
        ("DELETE", "sessions/foo"),
        ("GET", "sessions/foo/terminals/extra"),
        ("GET", ""),
    ]:
        check(f"not proxied: {method} /{path}", not broker._worker_api_allowed(method, path))

# --- 9. a missing model pin must stop the broker, not the first task -----
import subprocess

_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_DEFAULT_HAIKU_MODEL"}
_probe = subprocess.run(
    [sys.executable, "-c",
     "import kubernetes.config as c; c.load_incluster_config=lambda: None;"
     "import sys; sys.path.insert(0, %r); import broker"
     % os.path.dirname(os.path.abspath(__file__))],
    capture_output=True, text=True, env=_env,
)
check("broker refuses to start with a passthrough var unset",
      _probe.returncode != 0 and "ANTHROPIC_DEFAULT_HAIKU_MODEL" in _probe.stderr,
      (_probe.stderr or _probe.stdout)[-300:])

# --- 11. the orphan sweep, which replaced activeDeadlineSeconds -----------
#
# The case it exists for cannot be reached through the API: leases live in the
# broker's memory, so an orphan is a worker whose lease this process never had.
# That is what a broker restart leaves behind, and it is why the sweep queries
# the cluster by app label instead of walking `_leases`.
STATE["deleted_deployments"].clear()
STATE["deleted_svcs"].clear()


def _plant_worker(worker_id, age_seconds):
    """A worker workload with no lease, as a restarted broker would find it."""
    name = f"cao-worker-{worker_id}"
    STATE["deployments"][name] = types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name)
    )
    pod = _fake_pod(name, broker._labels(worker_id))
    pod.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=age_seconds
    )
    STATE["pods"][worker_id] = pod


with patch.object(broker, "_update_fleet_config"):
    # Old enough: this is the pod activeDeadlineSeconds used to kill.
    _plant_worker("aaaaaaaa", broker.WORKER_TIMEOUT + 60)
    # Not old enough. A broker that restarts mid-task must not kill the task -
    # under Jobs it did not, and the worker could still call /complete.
    _plant_worker("bbbbbbbb", broker.WORKER_TIMEOUT - 60)
    broker._sweep_orphan_workers()

check("orphan sweep deletes a leaseless worker past WORKER_TIMEOUT",
      "cao-worker-aaaaaaaa" in STATE["deleted_deployments"],
      json.dumps(STATE["deleted_deployments"]))
check("orphan sweep takes the worker's Service with it",
      "cao-worker-aaaaaaaa" in STATE["deleted_svcs"],
      json.dumps(STATE["deleted_svcs"]))
check("orphan sweep spares a leaseless worker inside WORKER_TIMEOUT",
      "cao-worker-bbbbbbbb" not in STATE["deleted_deployments"],
      json.dumps(STATE["deleted_deployments"]))

# The assertion that stops the sweep being a fleet-wide kill switch. A worker with
# a LIVE lease belongs to the reaper, which can say WHY it released it; the sweep
# can only delete. Age alone must never be enough.
STATE["deleted_deployments"].clear()
with patch.object(broker, "_update_fleet_config"):
    _plant_worker("cccccccc", broker.WORKER_TIMEOUT * 10)
    with broker._leases_lock:
        broker._leases["cccccccc"] = {
            "state": "leased",
            "reason": None,
            "leased_at": time.monotonic(),
            "settled_at": None,
            "ready_at": time.monotonic(),
            "pod_observed_at": time.monotonic(),
            "pod_uid": None,
            "agent_profile": "developer",
            "provider": "claude_code",
            "release_token": "rt",
        }
    broker._sweep_orphan_workers()
check("orphan sweep never touches a worker with a live lease, at any age",
      "cao-worker-cccccccc" not in STATE["deleted_deployments"],
      json.dumps(STATE["deleted_deployments"]))

print()
print("FAILURES:", FAILS if FAILS else "none")
sys.exit(1 if FAILS else 0)
