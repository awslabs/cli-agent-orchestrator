"""Offline exercise of k8s/broker.py: object construction + lease lifecycle.

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
    /tmp/brokertest/bin/python k8s/test_broker.py

Exits non-zero on the first failing expectation, and prints a PASS/FAIL line per
check.
"""
import json
import os
import sys
import time
import types

os.environ.update({
    "CAO_ELASTIC_WORKER_IMAGE": "111122223333.dkr.ecr.us-east-1.amazonaws.com/cao-server:2.4.1-cc3",
    "CAO_ELASTIC_BROKER_TOKEN": "test-token",
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


class FakeBatch:
    def create_namespaced_job(self, ns, body):
        body.metadata.uid = "uid-" + body.metadata.name
        STATE["jobs"][body.metadata.name] = body
        wid = body.metadata.labels["cao.aws/worker-id"]
        pod = k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(name=body.metadata.name + "-abcde",
                                      labels=dict(body.metadata.labels)),
            status=k8s.V1PodStatus(
                phase="Running",
                conditions=[k8s.V1PodCondition(type="Ready", status="True")],
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


# --- 1. the Job body survives the real serializer -------------------------
job = broker._worker_job("deadbeef", "rt", broker.WorkerRequest(agent_profile="developer"))
wire = k8s.ApiClient().sanitize_for_serialization(job)
spec = wire["spec"]["template"]["spec"]
env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}

check("job serializes to a dict", isinstance(wire, dict))
check("default provider is claude_code", env["CAO_INSTALL_PROFILES"] == "developer:claude_code",
      env.get("CAO_INSTALL_PROFILES"))
check("no KIRO_API_KEY on worker", "KIRO_API_KEY" not in env)
check("bedrock flag forwarded", env.get("CLAUDE_CODE_USE_BEDROCK") == "1")
check("region forwarded", env.get("AWS_REGION") == "us-east-1")
check("all four model tiers pinned",
      all(env.get(k) for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                               "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")))
check("both timeouts forwarded",
      env.get("CAO_PROVIDER_INIT_TIMEOUT") == "180" and env.get("CAO_MCP_REQUEST_TIMEOUT") == "240")
check("max terminals still 1", env.get("CAO_MAX_TERMINALS") == "1")
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
check("readiness initialDelay lowered to 5", probe["initialDelaySeconds"] == 5,
      str(probe.get("initialDelaySeconds")))

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

# --- 3. lease lifecycle over HTTP ---------------------------------------
with TestClient(broker.app) as c:
    r = c.post("/workers", json={"agent_profile": "developer"})
    check("unauthenticated create is rejected", r.status_code == 401, str(r.status_code))

    H = {"X-CAO-Broker-Token": "test-token"}
    r = c.post("/workers", json={"agent_profile": "developer"}, headers=H)
    check("create returns a lease", r.status_code == 200, r.text[:300])
    lease = r.json()
    wid = lease["worker_id"]
    check("job created first, then service",
          f"cao-worker-{wid}" in STATE["jobs"] and f"cao-worker-{wid}" in STATE["services"])
    check("target_host is the per-worker service FQDN",
          lease["target_host"] == f"cao-worker-{wid}.cao-cluster.svc.cluster.local",
          lease["target_host"])

    r = c.get("/workers", headers=H)
    check("ledger lists the open lease",
          r.status_code == 200 and any(w["worker_id"] == wid and w["state"] == "leased"
                                       for w in r.json()), r.text[:300])

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

    # --- 4. the false-success race: pod dies, complete never arrives ------
    r = c.post("/workers", json={"agent_profile": "developer"}, headers=H)
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

    # --- 5. completion deadline on a still-healthy pod --------------------
    r = c.post("/workers", json={"agent_profile": "developer"}, headers=H)
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

    # --- 6. input validation still bounded -------------------------------
    r = c.post("/workers", json={"agent_profile": "../../etc/passwd"}, headers=H)
    check("path-ish profile rejected", r.status_code == 422, str(r.status_code))
    r = c.post("/workers", json={"agent_profile": "developer", "provider": "a b"}, headers=H)
    check("provider with a space rejected", r.status_code == 422, str(r.status_code))
    r = c.post("/workers", json={"agent_profile": "developer", "image": "evil:latest"},
               headers=H)
    check("caller cannot inject an image",
          r.status_code in (200, 422)
          and (r.status_code == 422
               or STATE["jobs"][f"cao-worker-{r.json()['worker_id']}"]
               .spec.template.spec.containers[0].image == os.environ["CAO_ELASTIC_WORKER_IMAGE"]),
          r.text[:200])

# --- 7. a missing model pin must stop the broker, not the first task -----
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
