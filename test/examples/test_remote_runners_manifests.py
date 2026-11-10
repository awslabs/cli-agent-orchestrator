"""The remote-runner Kubernetes example keeps the #745 deployment rules.

These read the manifests, so an edit cannot quietly expose the hub, mount an
API token into runner pods, or turn on cleartext outside the local overlay.
Where ``kubectl`` is installed, the base and the kind overlay are also rendered
with ``kubectl kustomize`` and checked as applied.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/cao-clusters/kubernetes/remote-runners"
BASE = EXAMPLE / "base"
KIND = EXAMPLE / "overlays/kind"
HUB_PORT = 9891
API_PORT = 9889


def _docs(paths) -> List[Dict]:
    out = []
    for path in paths:
        out.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
    return out


def _base() -> List[Dict]:
    return _docs(sorted(BASE.glob("*.yaml")))


def _workloads(docs) -> List[Dict]:
    return [d for d in docs if d["kind"] in ("Deployment", "StatefulSet")]


def _one(docs, kind, name) -> Dict:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)}"
    return found[0]


def _container(workload, name) -> Dict:
    return next(c for c in workload["spec"]["template"]["spec"]["containers"] if c["name"] == name)


def _env(container) -> Dict[str, Dict]:
    return {e["name"]: e for e in container.get("env", [])}


def _runner(docs) -> Dict:
    return _one(docs, "Deployment", "cao-runner")


def _server(docs) -> Dict:
    return _one(docs, "StatefulSet", "cao-server")


# --- the hub port stays in the cluster ------------------------------------------


def test_nothing_in_the_example_exposes_a_port_outside_the_cluster():
    docs = _docs(sorted(EXAMPLE.rglob("*.yaml")))
    assert not [d for d in docs if d.get("kind") in ("Ingress", "Gateway", "HTTPRoute")]
    services = [d for d in docs if d.get("kind") == "Service"]
    assert services, "the example has no Service"
    for service in services:
        assert service["spec"].get("type") == "ClusterIP", service["metadata"]["name"]
        assert not service["spec"].get("externalIPs"), service["metadata"]["name"]
        assert not any("nodePort" in p for p in service["spec"]["ports"])


def test_only_runner_pods_reach_the_hub():
    docs = _base()
    policies = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    deny = policies["default-deny"]["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    to_server = [
        p
        for p in policies.values()
        if p["spec"]["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/name")
        == "cao-server"
        and "Ingress" in p["spec"]["policyTypes"]
    ]
    assert len(to_server) == 1
    rules = to_server[0]["spec"]["ingress"]
    assert len(rules) == 1, "runners must be admitted by exactly one rule"
    for rule in rules:
        assert rule["from"] == [
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "runner"}}}
        ]
        assert {p["port"] for p in rule["ports"]} == {HUB_PORT, API_PORT}
    labels = _runner(docs)["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "runner"
    assert (
        _server(docs)["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        != "runner"
    )


def test_runners_and_the_server_keep_the_paths_they_need():
    """Under default-deny, a missing egress rule leaves the deployment unusable."""
    docs = _base()
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    # Exactly these policies, so no extra one can widen what default-deny closed.
    assert set(policies) == {"default-deny", "cao-server-ingress", "allow-dns", "runner-egress"}
    assert "egress" not in policies["default-deny"] and "ingress" not in policies["default-deny"]
    assert policies["cao-server-ingress"]["policyTypes"] == ["Ingress"]
    assert policies["allow-dns"]["policyTypes"] == ["Egress"]
    egress = policies["runner-egress"]
    assert len(egress["egress"]) == 2, "runner egress is the hub/API rule and the public 443 rule"
    assert egress["podSelector"] == {"matchLabels": {"app.kubernetes.io/component": "runner"}}
    assert egress["policyTypes"] == ["Egress"]
    to_server = [
        r
        for r in egress["egress"]
        if r["to"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "cao-server"}}}]
    ]
    assert len(to_server) == 1
    assert {p["port"] for p in to_server[0]["ports"]} == {HUB_PORT, API_PORT}
    public = [r for r in egress["egress"] if "ipBlock" in r["to"][0]]
    assert len(public) == 1 and public[0]["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"
    assert [p["port"] for p in public[0]["ports"]] == [443]
    # Public addresses only: never the cluster's own or the metadata service.
    assert sorted(public[0]["to"][0]["ipBlock"]["except"]) == sorted(
        ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
    )
    dns = policies["allow-dns"]
    assert dns["podSelector"] == {"matchLabels": {"app.kubernetes.io/part-of": "cao-remote"}}
    (rule,) = dns["egress"]
    assert rule["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ]
    assert {(p["port"], p["protocol"]) for p in rule["ports"]} == {(53, "UDP"), (53, "TCP")}
    for workload in _workloads(docs):
        labels = workload["spec"]["template"]["metadata"]["labels"]
        assert labels["app.kubernetes.io/part-of"] == "cao-remote", workload["metadata"]["name"]


def test_service_names_do_not_assume_a_cluster_dns_domain():
    for path in EXAMPLE.rglob("*"):
        if path.is_file() and path.suffix in (".yaml", ".sh"):
            code = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]
            assert not [ln for ln in code if "cluster.local" in ln], path
    env = _env(_container(_runner(_base()), "runner"))
    assert env["CAO_RUNNER_SERVER_URL"]["value"] == f"ws://cao-server.cao-remote.svc:{HUB_PORT}"


# --- one pool of identical runners ------------------------------------------------


def test_one_pool_of_identical_runners_one_per_node():
    docs = _base()
    runners = [
        w
        for w in _workloads(docs)
        if w["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        == "runner"
    ]
    assert [w["metadata"]["name"] for w in runners] == ["cao-runner"]
    spec = runners[0]["spec"]["template"]["spec"]
    (term,) = spec["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert term["topologyKey"] == "kubernetes.io/hostname"
    assert term["labelSelector"]["matchLabels"] == {"app.kubernetes.io/name": "cao-runner"}
    # A surge pod could not schedule beside a runner on every node.
    assert runners[0]["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }
    env = _env(_container(runners[0], "runner"))
    assert env["CAO_RUNNER_MAX_TERMINALS"]["value"] == "1"
    assert "CAO_RUNNER_CLAIMS" not in env
    assert "CAO_RUNNER_RESERVED_SESSIONS" not in _env(_container(_server(docs), "cao-server"))


def test_the_example_has_no_supervisor_role():
    for path in EXAMPLE.rglob("*"):
        if path.is_file():
            assert "supervisor" not in path.read_text().lower(), path


def test_the_server_is_one_replica_with_its_own_claim():
    server = _server(_base())
    assert server["spec"]["replicas"] == 1
    (claim,) = server["spec"]["volumeClaimTemplates"]
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    env = _env(_container(server, "cao-server"))
    assert env["CAO_TERMINAL_BACKEND"]["value"] == "remote"


def test_runner_pods_reap_orphans_and_need_no_kubernetes_api():
    for workload in _workloads(_base()):
        spec = workload["spec"]["template"]["spec"]
        assert spec["automountServiceAccountToken"] is False, workload["metadata"]["name"]
    assert _runner(_base())["spec"]["template"]["spec"]["shareProcessNamespace"] is True


# --- credentials ---------------------------------------------------------------------


def test_the_runner_token_is_a_mounted_file_never_an_env_value():
    for workload in _workloads(_base()):
        spec = workload["spec"]["template"]["spec"]
        (container,) = spec["containers"]
        env = _env(container)
        assert "CAO_RUNNER_TOKEN" not in env
        assert env["CAO_RUNNER_TOKEN_FILE"]["value"] == "/var/run/cao-runner/token"
        mount = next(m for m in container["volumeMounts"] if m["name"] == "runner-token")
        assert mount["mountPath"] == "/var/run/cao-runner" and mount["readOnly"] is True
        volume = next(v for v in spec["volumes"] if v["name"] == "runner-token")
        assert volume["secret"]["secretName"] == "cao-runner-token"


def test_no_api_credential_anywhere_in_the_example():
    """With terminal.backend=remote the server refuses to start with API auth on."""
    for path in EXAMPLE.rglob("*"):
        if path.is_file() and path.suffix in (".yaml", ".sh"):
            text = path.read_text()
            assert "CAO_AUTH" not in text, path
    for workload in _workloads(_base()):
        (container,) = workload["spec"]["template"]["spec"]["containers"]
        refs = [e for e in container.get("envFrom", [])]
        names = {(r.get("secretRef") or r.get("configMapRef"))["name"] for r in refs}
        assert names <= {"cao-provider-env", "cao-provider-credentials"}


def test_provider_credentials_reach_only_runner_pods():
    docs = _base()
    assert "envFrom" not in _container(_server(docs), "cao-server")
    refs = _container(_runner(docs), "runner")["envFrom"]
    assert all((r.get("secretRef") or r.get("configMapRef"))["optional"] is True for r in refs)


# --- transport -----------------------------------------------------------------------


def test_cleartext_is_opted_into_only_by_the_kind_overlay():
    env = _env(_container(_runner(_base()), "runner"))
    # Explicit, so an envFrom source (cao-provider-env) cannot override it.
    assert env["CAO_RUNNER_ALLOW_INSECURE"] == {"name": "CAO_RUNNER_ALLOW_INSECURE", "value": "0"}
    for workload in _workloads(_base()):
        for container in workload["spec"]["template"]["spec"]["containers"]:
            assert _env(container).get("CAO_RUNNER_ALLOW_INSECURE", {}).get("value") in (
                None,
                "0",
            )
    holders = sorted(
        p.relative_to(EXAMPLE).as_posix()
        for p in EXAMPLE.rglob("*.yaml")
        if any(
            "CAO_RUNNER_ALLOW_INSECURE" in line and '"1"' in line
            for line in p.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
    )
    assert holders == ["overlays/kind/kustomization.yaml"]


def test_the_base_names_no_storage_class_registry_or_provider():
    docs = _base()
    claim = _one(docs, "PersistentVolumeClaim", "cao-remote-workspace")
    assert claim["spec"]["accessModes"] == ["ReadWriteMany"]
    assert "storageClassName" not in claim["spec"]
    for workload in _workloads(docs):
        for container in workload["spec"]["template"]["spec"]["containers"]:
            assert container["image"] == "cao-server"
            env = _env(container)
            assert not [n for n in env if n.startswith(("AWS_", "ANTHROPIC_", "CLAUDE_"))]


def test_the_kind_deploy_script_is_valid_and_mints_a_non_empty_token():
    script = KIND / "deploy.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert "secrets.token_hex" in text and '"${#token}" -eq 48' in text
    assert "CAO_AUTH" not in text


# --- rendered, where kubectl is available --------------------------------------------


def _render(path) -> List[Dict]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(path)], check=True, capture_output=True, text=True
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")


@needs_kubectl
def test_the_rendered_base_is_secure_by_default():
    docs = _render(BASE)
    assert {d["metadata"].get("namespace") for d in docs if d["kind"] != "Namespace"} == {
        "cao-remote"
    }
    env = _env(_container(_runner(docs), "runner"))
    assert env["CAO_RUNNER_ALLOW_INSECURE"]["value"] == "0"
    assert env["CAO_RUNNER_SERVER_URL"]["value"].startswith("ws://")  # refused without opt-in


@needs_kubectl
def test_the_rendered_kind_overlay_binds_the_workspace_and_opts_into_cleartext():
    docs = _render(KIND)
    env = _env(_container(_runner(docs), "runner"))
    assert env["CAO_RUNNER_ALLOW_INSECURE"]["value"] == "1"
    claim = _one(docs, "PersistentVolumeClaim", "cao-remote-workspace")
    volume = _one(docs, "PersistentVolume", "cao-remote-workspace")
    assert (
        claim["spec"]["storageClassName"] == ""
        and claim["spec"]["volumeName"] == "cao-remote-workspace"
    )
    assert volume["spec"]["claimRef"] == {"namespace": "cao-remote", "name": "cao-remote-workspace"}
    assert "ReadWriteMany" in volume["spec"]["accessModes"]
    # The overlay changes nothing else about the runner.
    base_env = _env(_container(_runner(_render(BASE)), "runner"))
    assert set(env) == set(base_env)
    assert {n for n in env if env[n] != base_env[n]} == {"CAO_RUNNER_ALLOW_INSECURE"}
