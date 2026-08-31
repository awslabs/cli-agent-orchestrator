"""Offline exercise of broker.py: object construction + lease lifecycle.

Stubs the API server so the whole request path can run on a laptop. The point is
to catch what only shows up at the first lease on a live cluster - a misspelled
kwarg on a V1* model, a Job body the serializer mangles, a reaper that never
releases. Every V1* object is pushed through the client's real serializer, which
is what actually rejects a bad field name.

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

STATE = {"jobs": {}, "services": {}, "pods": {}, "deleted_jobs": [], "deleted_svcs": []}

# What the fake API server hands back for the NEXT pod it creates. Readiness used
# to be irrelevant to the fake (the broker waited for it, so a pod that was never
# Ready just hung), but the lease is now returned before readiness and the reaper
# owns the deadline - so both "came up" and "never came up" have to be expressible.
STATE["new_pods_ready"] = True
STATE["new_pods_phase"] = "Running"
STATE["create_pods"] = True


class FakeBatch:
    def create_namespaced_job(self, ns, body):
        body.metadata.uid = "uid-" + body.metadata.name
        STATE["jobs"][body.metadata.name] = body
        wid = body.metadata.labels["cao.aws/worker-id"]
        if STATE["create_pods"]:
            conditions = ([k8s.V1PodCondition(type="Ready", status="True")]
                          if STATE["new_pods_ready"] else [])
            pod = k8s.V1Pod(
                metadata=k8s.V1ObjectMeta(name=body.metadata.name + "-abcde",
                                          labels=dict(body.metadata.labels)),
                status=k8s.V1PodStatus(
                    phase=STATE["new_pods_phase"],
                    conditions=conditions,
                ),
            )
            STATE["pods"][wid] = pod
        return body

    def read_namespaced_job(self, name, ns):
        if name not in STATE["jobs"]:
            raise k8s.rest.ApiException(status=404)
        return STATE["jobs"][name]

    def delete_namespaced_job(self, name, ns, propagation_policy=None):
        STATE["deleted_jobs"].append(name)
        STATE["jobs"].pop(name, None)


class FakeCore:
    def create_namespaced_service(self, ns, body):
        STATE["services"][body.metadata.name] = body
        return body

    def delete_namespaced_service(self, name, ns):
        STATE["deleted_svcs"].append(name)
        STATE["services"].pop(name, None)

    def list_namespaced_pod(self, ns, label_selector=None):
        wid = label_selector.split("=", 1)[1]
        pod = STATE["pods"].get(wid)
        return types.SimpleNamespace(items=[pod] if pod else [])


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker  # noqa: E402  (must follow the env setup above)

broker.batch_api = FakeBatch()
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


# --- 1. the Job body survives the real serializer -------------------------
job = broker._worker_job("deadbeef", "rt", worker_request())
wire = k8s.ApiClient().sanitize_for_serialization(job)
spec = wire["spec"]["template"]["spec"]
env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}

check("job serializes to a dict", isinstance(wire, dict))
annotations = wire["metadata"]["annotations"]
check("job persists the authorized callback receiver",
      annotations["cao.aws/callback-terminal-id"] == "abc12345")
check("job persists the authorized memory session",
      annotations["cao.aws/session-name"] == "cao-worker-deadbeef")
check("job persists the authorized memory profile",
      annotations["cao.aws/agent-profile"] == "developer")
check("default provider is claude_code", env["CAO_INSTALL_PROFILES"] == "developer:claude_code",
      env.get("CAO_INSTALL_PROFILES"))
# A credential must never be a literal in the Job body - the broker's Role has no
# `secrets`, and a value here would end up in etcd and in `kubectl get job -o yaml`.
check("no provider credential inlined in the Job", "KIRO_API_KEY" not in env)

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

# --- 2. the Service is owned by the Job ----------------------------------
try:
    broker._worker_service("deadbeef", job)
    check("unsubmitted Job is refused rather than left unowned", False, "no error raised")
except RuntimeError as exc:
    check("unsubmitted Job is refused rather than left unowned", "has no uid" in str(exc))

job = broker.batch_api.create_namespaced_job("cao-cluster", job)  # assigns a uid
svc = broker._worker_service("deadbeef", job)
swire = k8s.ApiClient().sanitize_for_serialization(svc)
owners = swire["metadata"].get("ownerReferences") or []
check("service has an ownerReference", len(owners) == 1, json.dumps(swire["metadata"]))
check("owner is the Job by uid",
      owners and owners[0]["kind"] == "Job" and owners[0]["uid"] == "uid-cao-worker-deadbeef",
      json.dumps(owners))

# --- 3. the lease returns before readiness; the reaper owns the deadline --
#
# Deliberately ABOVE the TestClient block: the reaper only runs as a thread once
# lifespan has started, so calling _reap_once() by hand here is the one place
# these transitions can be driven a tick at a time instead of waited for.
check("readiness gating is off by default", broker.GATE_ON_READY is False)

# A reaper tick can overlap Kubernetes object creation. The lease must not be
# considered active until both the Job and Service exist.
with broker._leases_lock:
    broker._leases["cafefeed"] = {
        "state": "creating",
        "reason": None,
        "leased_at": time.monotonic() - (broker.READY_TIMEOUT + 1),
        "settled_at": None,
        "ready_at": None,
        "pod_observed_at": None,
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

# A Job exists before its controller creates a Pod. Empty Pod lists are normal
# in that window and must not be called disappearance.
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
check("failed worker's job is released", f"cao-worker-{w0}" in STATE["deleted_jobs"],
      str(STATE["deleted_jobs"]))

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
    check("job created first, then service",
          f"cao-worker-{wid}" in STATE["jobs"] and f"cao-worker-{wid}" in STATE["services"])
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
            "cwd": f"/home/cao/workspace/jobs/{wid}",
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
    check("completing releases the job", f"cao-worker-{wid}" in STATE["deleted_jobs"],
          str(STATE["deleted_jobs"]))
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
        "terminal-ended signal releases the worker Job",
        f"cao-worker-{terminal_ended_id}" in STATE["deleted_jobs"],
        str(STATE["deleted_jobs"]),
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
    check("reaper released the squatting job", f"cao-worker-{wid2}" in STATE["deleted_jobs"],
          str(STATE["deleted_jobs"]))

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
    check("expired job is released", f"cao-worker-{wid3}" in STATE["deleted_jobs"])

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
               or STATE["jobs"][f"cao-worker-{r.json()['worker_id']}"]
               .spec.template.spec.containers[0].image == os.environ["CAO_ELASTIC_WORKER_IMAGE"]),
          r.text[:200])

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

print()
print("FAILURES:", FAILS if FAILS else "none")
sys.exit(1 if FAILS else 0)
