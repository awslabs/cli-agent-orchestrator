"""The remote-runner Kubernetes example keeps the #745 deployment rules.

These read the manifests, so an edit cannot quietly expose the hub, mount an
API token into runner pods, or turn on cleartext outside the local overlay.
Where ``kubectl`` is installed, the base and the overlays are also rendered
with ``kubectl kustomize`` and checked as applied. The EKS overlay's
CloudFormation template (``overlays/eks/iac``) is checked here too.
"""

import base64
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/cao-clusters/kubernetes/remote-runners"
BASE = EXAMPLE / "base"
KIND = EXAMPLE / "overlays/kind"
EKS = EXAMPLE / "overlays/eks"
HUB_PORT = 9891
API_PORT = 9889


def _docs(paths) -> List[Dict]:
    out = []
    for path in paths:
        out.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
    return out


def _manifest_paths() -> List[Path]:
    """Every Kubernetes manifest; ``iac/`` holds CloudFormation, not manifests."""
    return sorted(p for p in EXAMPLE.rglob("*.yaml") if "iac" not in p.relative_to(EXAMPLE).parts)


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
    docs = _docs(_manifest_paths())
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
    # Public addresses only: never the cluster's own (private and carrier-grade
    # NAT ranges, which some clusters give pods) or the metadata service.
    assert sorted(public[0]["to"][0]["ipBlock"]["except"]) == sorted(
        ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]
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


def test_every_container_drops_privileges():
    """No container can gain privileges or keeps a Linux capability."""
    containers = [
        c for w in _workloads(_base()) for c in w["spec"]["template"]["spec"]["containers"]
    ]
    assert containers
    for container in containers:
        context = container.get("securityContext") or {}
        assert context.get("allowPrivilegeEscalation") is False, container["name"]
        assert context.get("capabilities", {}).get("drop") == ["ALL"], container["name"]


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
        for p in _manifest_paths()
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


# --- the EKS Auto Mode overlay ------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    """Reads CloudFormation's short-form tags (``!Ref``, ``!Sub``, ...) as plain values."""


_CfnLoader.add_multi_constructor(
    "!",
    lambda loader, suffix, node: (
        loader.construct_scalar(node)
        if isinstance(node, yaml.ScalarNode)
        else (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
        )
    ),
)


def _cfn() -> Dict:
    return yaml.load((EKS / "iac/cfn-auto-mode.yaml").read_text(), Loader=_CfnLoader)


def _resources(kind) -> Dict[str, Dict]:
    return {n: r for n, r in _cfn()["Resources"].items() if r["Type"] == kind}


def test_the_eks_cluster_api_is_reachable_only_from_listed_sources():
    template = _cfn()
    (cluster,) = _resources("AWS::EKS::Cluster").values()
    vpc = cluster["Properties"]["ResourcesVpcConfig"]
    assert vpc["PublicAccessCidrs"] == "PublicAccessCidrs"
    assert vpc["EndpointPrivateAccess"] is True
    # No default: the operator names the sources, and 0.0.0.0/0 is never implied.
    assert "Default" not in template["Parameters"]["PublicAccessCidrs"]
    assert cluster["Properties"]["AccessConfig"]["AuthenticationMode"] == "API"
    assert cluster["Properties"]["EncryptionConfig"][0]["Resources"] == ["secrets"]


def test_the_eks_cluster_is_auto_mode_with_a_pinned_service_range():
    (cluster,) = _resources("AWS::EKS::Cluster").values()
    props = cluster["Properties"]
    assert props["ComputeConfig"]["Enabled"] is True
    assert props["StorageConfig"]["BlockStorage"]["Enabled"] is True
    assert props["KubernetesNetworkConfig"]["ElasticLoadBalancing"]["Enabled"] is True
    assert props["BootstrapSelfManagedAddons"] is False
    # deploy.sh renders the node-local DNS address from this range.
    assert props["KubernetesNetworkConfig"]["ServiceIpv4Cidr"] == "ServiceIpv4Cidr"


def test_only_the_runner_service_account_gets_bedrock_and_only_for_the_allowed_models():
    (association,) = _resources("AWS::EKS::PodIdentityAssociation").values()
    # Fixed, not parameters: the manifests name them.
    assert association["Properties"]["Namespace"] == "cao-remote"
    assert association["Properties"]["ServiceAccount"] == "cao-runner"
    assert _one(_docs([EKS / "serviceaccount.yaml"]), "ServiceAccount", "cao-runner")
    (policy,) = _resources("AWS::IAM::ManagedPolicy").values()
    statements = {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}
    assert set(statements) == {"InvokeAllowedModels", "ReadModelCatalogue"}
    invoke = statements["InvokeAllowedModels"]
    assert invoke["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    assert invoke["Resource"] != "*"
    assert all(r.endswith("${BedrockModelPattern}") for r in invoke["Resource"])
    catalogue = statements["ReadModelCatalogue"]["Action"]
    assert all(a.startswith(("bedrock:List", "bedrock:Get")) for a in catalogue)
    # The only other Pod Identity role is the EFS driver's, for EFS only.
    (addon,) = _resources("AWS::EKS::Addon").values()
    assert addon["Properties"]["AddonName"] == "aws-efs-csi-driver"
    (efs_identity,) = addon["Properties"]["PodIdentityAssociations"]
    efs_role = _cfn()["Resources"][efs_identity["RoleArn"].split(".")[0]]
    policies = [p.rsplit("/", 1)[-1] for p in efs_role["Properties"]["ManagedPolicyArns"]]
    assert policies == ["AmazonEFSCSIDriverPolicy"]


def test_the_stack_refuses_an_open_api_endpoint_and_names_that_do_not_fit():
    import re

    template = _cfn()
    (assertion,) = template["Rules"]["NoOpenApiEndpoint"]["Assertions"]
    assert "0.0.0.0/0" in str(assertion["Assert"])
    name = re.compile(template["Parameters"]["ClusterName"]["AllowedPattern"])
    assert name.fullmatch("cao-remote") and name.fullmatch("a" * 40)
    for bad in ("Cao-remote", "cao-remote-", "a" * 41):
        assert not name.fullmatch(bad), bad
    vpc = re.compile(template["Parameters"]["VpcCidr"]["AllowedPattern"])
    assert vpc.fullmatch("10.42.0.0/18") and not vpc.fullmatch("10.42.0.0/19")


def test_the_eks_workspace_and_registry_are_private():
    (group,) = _resources("AWS::EC2::SecurityGroup").values()
    (rule,) = group["Properties"]["SecurityGroupIngress"]
    assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", 2049, 2049)
    assert "CidrIp" not in rule and rule["SourceSecurityGroupId"]
    (filesystem,) = _resources("AWS::EFS::FileSystem").values()
    assert filesystem["Properties"]["Encrypted"] is True
    (repository,) = _resources("AWS::ECR::Repository").values()
    props = repository["Properties"]
    assert props["ImageTagMutability"] == "IMMUTABLE"
    assert props["ImageScanningConfiguration"] == {"ScanOnPush": True}
    assert "RepositoryPolicyText" not in props


def test_the_eks_deploy_script_turns_on_network_policy_first_and_keeps_secrets_out():
    script = EKS / "deploy.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert "secrets.token_hex" in text and '"${#token}" -eq 48' in text
    # In Auto Mode NetworkPolicy objects are ignored until this exists.
    enable = text.index("enable-network-policy-controller")
    assert enable < text.index('k apply -f "${HERE}/../../base/namespace.yaml"')
    assert 'DOCKER_CONFIG="${CTX}/docker" docker login' in text
    # Placeholders the script renders, and the check that none is left.
    for placeholder in (
        "<server-repository-uri>",
        "<workspace-volume-handle>",
        "<cluster-dns-ip>",
        "<provider-env-hash>",
        "<hub-tls-hash>",
    ):
        assert placeholder in text
    assert "unrendered placeholder" in text
    assert "CAO_AUTH" not in text


@needs_kubectl
def test_the_rendered_eks_overlay_serves_wss_and_changes_only_what_eks_needs():
    docs = _render(EKS)
    base = _render(BASE)
    runner, server = _runner(docs), _server(docs)
    env = _env(_container(runner, "runner"))
    assert env["CAO_RUNNER_SERVER_URL"]["value"] == f"wss://cao-server.cao-remote.svc:{HUB_PORT}"
    assert env["CAO_RUNNER_ALLOW_INSECURE"]["value"] == "0"
    assert env["CAO_RUNNER_CA_FILE"]["value"] == "/var/run/cao-hub-ca/ca.crt"
    base_env = _env(_container(_runner(base), "runner"))
    assert set(env) - set(base_env) == {"CAO_RUNNER_CA_FILE"}
    assert {n for n in base_env if env[n] != base_env[n]} == {"CAO_RUNNER_SERVER_URL"}
    # Runners only see the CA certificate; the key stays with the server.
    runner_volumes = {v["name"]: v for v in runner["spec"]["template"]["spec"]["volumes"]}
    assert runner_volumes["hub-ca"] == {"name": "hub-ca", "configMap": {"name": "cao-hub-ca"}}
    assert runner["spec"]["template"]["spec"]["serviceAccountName"] == "cao-runner"
    assert "serviceAccountName" not in server["spec"]["template"]["spec"]
    server_env = _env(_container(server, "cao-server"))
    assert server_env["CAO_RUNNER_TLS_KEY"]["value"] == "/var/run/cao-hub-tls/tls.key"
    server_volumes = {v["name"]: v for v in server["spec"]["template"]["spec"]["volumes"]}
    assert server_volumes["hub-tls"]["secret"]["secretName"] == "cao-hub-tls"
    assert not [n for n in server_env if n.startswith(("AWS_", "ANTHROPIC_", "CLAUDE_"))]
    (state,) = server["spec"]["volumeClaimTemplates"]
    assert state["spec"]["storageClassName"] == "cao-gp3"
    volume = _one(docs, "PersistentVolume", "cao-remote-workspace")
    assert volume["spec"]["csi"]["driver"] == "efs.csi.aws.com"
    assert volume["spec"]["claimRef"] == {"namespace": "cao-remote", "name": "cao-remote-workspace"}
    account = _one(docs, "ServiceAccount", "cao-runner")
    assert account["automountServiceAccountToken"] is False


@needs_kubectl
def test_the_rendered_eks_overlay_opens_only_dns_and_the_pod_identity_agent():
    docs = _render(EKS)
    base = {d["metadata"]["name"]: d["spec"] for d in _render(BASE) if d["kind"] == "NetworkPolicy"}
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    assert set(policies) == set(base)
    changed = {n for n in policies if policies[n] != base[n]}
    assert changed == {"allow-dns", "runner-egress"}
    # The base rules stay; each policy gains exactly one rule.
    for name in changed:
        assert policies[name]["egress"][:-1] == base[name]["egress"]
    assert policies["allow-dns"]["egress"][-1] == {
        "to": [{"ipBlock": {"cidr": "<cluster-dns-ip>/32"}}],
        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
    }
    assert policies["runner-egress"]["egress"][-1] == {
        "to": [{"ipBlock": {"cidr": "169.254.170.23/32"}}],
        "ports": [{"port": 80, "protocol": "TCP"}],
    }


# --- deploy.sh against stub aws, kubectl and docker ---------------------------------

STUBS = Path(__file__).parent / "fixtures/eks_deploy_stubs"


def _working_openssl():
    for candidate in ("/usr/bin/openssl", shutil.which("openssl")):
        if (
            candidate
            and subprocess.run([candidate, "version"], capture_output=True).returncode == 0
        ):
            return candidate
    return None


@pytest.fixture
def deploy(tmp_path):
    real_kubectl = shutil.which("kubectl")
    openssl = _working_openssl()
    if real_kubectl is None or openssl is None:
        pytest.skip("needs kubectl and a working openssl")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("aws", "kubectl", "docker"):
        (bin_dir / name).symlink_to(STUBS / name)
    (bin_dir / "openssl").symlink_to(openssl)
    state = tmp_path / "state"
    state.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    def run(**extra):
        return subprocess.run(
            ["bash", str(EKS / "deploy.sh"), "cao-test"],
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(home),
                "STATE": str(state),
                "REAL_KUBECTL": real_kubectl,
                "STUB_PYTHON": sys.executable,
                "AWS_REGION": "ap-southeast-2",
                "CAO_IMAGE_TAG": "abc1234",
                **extra,
            },
            capture_output=True,
            text=True,
        )

    run.state = state
    run.home = home
    run.openssl = openssl
    return run


def _argv(state, name):
    path = state / f"{name}.argv"
    return path.read_text() if path.exists() else ""


def _rendered_hashes(state):
    """The hub-tls hash on the server and on the runners as last applied."""
    hashes = {}
    for doc in yaml.safe_load_all(state.joinpath("applied.yaml").read_text()):
        if doc and doc["kind"] in ("StatefulSet", "Deployment"):
            annotations = doc["spec"]["template"]["metadata"]["annotations"]
            hashes[doc["metadata"]["name"]] = annotations["cao.awslabs.com/hub-tls"]
    return hashes


def _live_tls_hash(state):
    secret = yaml.safe_load((state / "obj-cao-hub-tls").read_text())
    ca = yaml.safe_load((state / "obj-cao-hub-ca").read_text())["data"]["ca.crt"]
    material = base64.b64decode(secret["data"]["tls.crt"]) + ca.rstrip("\n").encode() + b"\n"
    return hashlib.sha256(material).hexdigest()[:16]


def _issued(state):
    kubectl = _argv(state, "kubectl")
    return "create secret tls cao-hub-tls" in kubectl and "create configmap cao-hub-ca" in kubectl


def _fresh_argv(state):
    for name in ("kubectl", "docker", "aws"):
        (state / f"{name}.argv").unlink(missing_ok=True)


def test_deploy_renders_everything_and_keeps_secrets_off_command_lines(deploy):
    done = deploy()
    assert done.returncode == 0, done.stderr
    state = deploy.state
    applied = (state / "applied.yaml").read_text()
    # NetworkPolicy enforcement is the first thing applied.
    assert "enable-network-policy-controller" in applied.split("\n---\n")[0]
    for placeholder in (
        "<server-repository-uri>",
        "<image-tag>",
        "<workspace-volume-handle>",
        "<cluster-dns-ip>",
        "<provider-env-hash>",
        "<hub-tls-hash>",
    ):
        assert placeholder not in applied, placeholder
    assert (
        "111122223333.dkr.ecr.ap-southeast-2.amazonaws.com/cao-test/cao-server:abc1234" in applied
    )
    assert "fs-0123::fsap-0456" in applied and "10.100.0.10/32" in applied
    assert "cao.awslabs.com/provider-env:" in applied
    # The token: 48 hex characters, from a file, on no command line.
    token = (state / "token-file").read_text()
    assert len(token) == 48 and int(token, 16) >= 0
    assert token not in _argv(state, "kubectl") + _argv(state, "docker")
    # The registry login went to a private config that is gone, never to $HOME.
    (login,) = [ln for ln in _argv(state, "docker").splitlines() if ln.startswith("login")]
    config_dir = login.split("DOCKER_CONFIG=")[1]
    assert config_dir and not Path(config_dir).exists()
    assert not (deploy.home / ".docker").exists()
    # TLS: the server's Secret and the runners' CA, no CA key kept, and both
    # workloads carry the hash of what is in the cluster.
    assert _issued(state)
    assert "ca.key" not in applied and "cao-hub-ca-key" not in applied
    assert _rendered_hashes(state) == {
        "cao-server": _live_tls_hash(state),
        "cao-runner": _live_tls_hash(state),
    }
    assert "rollout restart" not in _argv(state, "kubectl")


def test_a_second_deploy_reissues_nothing_and_changes_no_pod_template(deploy):
    assert deploy().returncode == 0
    before = _rendered_hashes(deploy.state)
    _fresh_argv(deploy.state)
    (deploy.state / "token-file").unlink()
    done = deploy()
    assert done.returncode == 0, done.stderr
    kubectl = _argv(deploy.state, "kubectl")
    assert "create secret generic cao-runner-token" not in kubectl
    assert not _issued(deploy.state)
    assert _rendered_hashes(deploy.state) == before
    assert _argv(deploy.state, "docker") == ""  # the image is already pushed


def _other_ca_cert(deploy, tmp_path, days=365):
    """A CA and a certificate it signed, as PEM text, not the deploy's own."""
    run = lambda *a: subprocess.run(
        [deploy.openssl, *a], cwd=tmp_path, check=True, capture_output=True
    )
    run(
        "req",
        "-x509",
        "-new",
        "-nodes",
        "-newkey",
        "rsa:2048",
        "-days",
        str(days),
        "-subj",
        "/CN=other",
        "-keyout",
        "o-ca.key",
        "-out",
        "o-ca.crt",
    )
    run(
        "req",
        "-new",
        "-nodes",
        "-newkey",
        "rsa:2048",
        "-subj",
        "/CN=hub",
        "-keyout",
        "o.key",
        "-out",
        "o.csr",
    )
    run(
        "x509",
        "-req",
        "-in",
        "o.csr",
        "-CA",
        "o-ca.crt",
        "-CAkey",
        "o-ca.key",
        "-CAcreateserial",
        "-days",
        str(days),
        "-out",
        "o.crt",
    )
    return (tmp_path / "o-ca.crt").read_text(), (tmp_path / "o.crt").read_bytes()


def _put(state, name, data):
    path = state / f"obj-{name}"
    doc = yaml.safe_load(path.read_text())
    doc["data"].update(data)
    path.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize("broken", ["ca-missing", "secret-missing", "ca-does-not-match"])
def test_tls_that_does_not_hold_together_is_issued_again_and_rolls_both(deploy, tmp_path, broken):
    assert deploy().returncode == 0
    before = _rendered_hashes(deploy.state)
    if broken == "ca-missing":
        (deploy.state / "obj-cao-hub-ca").unlink()  # a run that stopped after the Secret
    elif broken == "secret-missing":
        (deploy.state / "obj-cao-hub-tls").unlink()
    else:  # the Secret was updated, the ConfigMap update failed
        other_ca, _ = _other_ca_cert(deploy, tmp_path)
        _put(deploy.state, "cao-hub-ca", {"ca.crt": other_ca})
    _fresh_argv(deploy.state)
    done = deploy()
    assert done.returncode == 0, done.stderr
    assert _issued(deploy.state)
    after = _rendered_hashes(deploy.state)
    assert after["cao-server"] == after["cao-runner"] == _live_tls_hash(deploy.state)
    assert after != before


def test_a_certificate_close_to_expiry_is_issued_again(deploy, tmp_path):
    assert deploy().returncode == 0
    ca, cert = _other_ca_cert(deploy, tmp_path, days=10)
    _put(deploy.state, "cao-hub-ca", {"ca.crt": ca})
    _put(deploy.state, "cao-hub-tls", {"tls.crt": base64.b64encode(cert).decode()})
    _fresh_argv(deploy.state)
    assert deploy().returncode == 0
    assert _issued(deploy.state)


def test_material_issued_by_a_run_that_failed_still_rolls_the_workloads(deploy):
    assert deploy().returncode == 0
    first = _rendered_hashes(deploy.state)
    (deploy.state / "obj-cao-hub-tls").unlink()
    _fresh_argv(deploy.state)
    # New material is issued, then applying the overlay fails.
    failed = deploy(STUB_APPLY_FAILS_ON="kind: StatefulSet")
    assert failed.returncode != 0 and _issued(deploy.state)
    _fresh_argv(deploy.state)
    done = deploy()
    assert done.returncode == 0, done.stderr
    assert not _issued(deploy.state)  # what the failed run issued holds together
    after = _rendered_hashes(deploy.state)
    assert after["cao-server"] == after["cao-runner"] == _live_tls_hash(deploy.state)
    assert after != first  # so the pod templates change and both roll


def test_the_default_tag_names_the_build_content_and_is_stable(deploy):
    done = deploy(CAO_IMAGE_TAG="")
    assert done.returncode == 0, done.stderr
    assert "--no-cache" not in _argv(deploy.state, "docker")
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tag = (deploy.state / "image").read_text()
    assert re.fullmatch(rf"{commit}-[0-9a-f]{{12}}", tag), tag
    _fresh_argv(deploy.state)
    assert deploy(CAO_IMAGE_TAG="").returncode == 0
    assert _argv(deploy.state, "docker") == ""  # same content, same tag, no new build
    assert f"imageTag={tag}" in _argv(deploy.state, "aws")


def test_a_rebuild_pushes_a_new_image_for_the_same_content(deploy):
    assert deploy(CAO_IMAGE_TAG="").returncode == 0
    content_tag = (deploy.state / "image").read_text()
    _fresh_argv(deploy.state)
    done = deploy(CAO_IMAGE_TAG="", CAO_IMAGE_REBUILD="1")
    assert done.returncode == 0, done.stderr
    tag = (deploy.state / "image").read_text()
    assert re.fullmatch(rf"{re.escape(content_tag)}-r[0-9]{{14}}", tag), tag
    (build,) = [ln for ln in _argv(deploy.state, "docker").splitlines() if ln.startswith("build")]
    assert "--pull" in build.split() and "--no-cache" in build.split()
    assert f"cao-server:{tag}" in (deploy.state / "applied.yaml").read_text()


def test_an_unknown_rebuild_value_is_refused(deploy):
    done = deploy(CAO_IMAGE_REBUILD="yes")
    assert done.returncode == 2
    assert "CAO_IMAGE_REBUILD" in done.stderr
    assert not (deploy.state / "kubectl.argv").exists()


def test_the_context_digest_follows_content_and_skips_bytecode(tmp_path):
    script = EKS / "context-digest.py"
    root = tmp_path / "repo"
    for rel in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "NOTICE",
        "scripts/hatch_build_tui_tag.py",
        "src/pkg/mod.py",
        "examples/cao-clusters/kubernetes/eks/Dockerfile",
        "test/providers/fixtures/bin/mock_cli",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    digest = lambda: subprocess.run(
        [sys.executable, str(script), str(root)], capture_output=True, text=True, check=True
    ).stdout.strip()
    first = digest()
    assert re.fullmatch(r"[0-9a-f]{12}", first) and digest() == first
    (root / "src/pkg/__pycache__").mkdir()
    (root / "src/pkg/__pycache__/mod.cpython-312.pyc").write_bytes(b"x")
    (root / "src/pkg/stale.pyc").write_bytes(b"x")
    (root / "unrelated.txt").write_text("not in the image")
    assert digest() == first
    (root / "src/pkg/new.py").write_text("untracked, but copied into the image")
    second = digest()
    assert second != first
    (root / "src/pkg/new.py").chmod(0o755)
    assert digest() != second
    (root / "LICENSE").unlink()
    assert (
        subprocess.run([sys.executable, str(script), str(root)], capture_output=True).returncode
        != 0
    )


def test_an_image_lookup_error_stops_the_deploy_before_any_build(deploy):
    done = deploy(STUB_ECR_FAILS="1")
    assert done.returncode == 1
    assert "could not look up image" in done.stderr and "AccessDeniedException" in done.stderr
    assert _argv(deploy.state, "docker") == ""


def test_deploy_checks_the_endpoint_with_the_exact_query(deploy):
    assert deploy().returncode == 0
    (call,) = [ln for ln in _argv(deploy.state, "aws").splitlines() if "describe-cluster" in ln]
    assert call == (
        "eks describe-cluster --name cao-test --query "
        "contains(cluster.resourcesVpcConfig.publicAccessCidrs, '0.0.0.0/0') --output text"
    )


def test_deploy_refuses_a_cluster_whose_api_endpoint_is_open(deploy):
    done = deploy(STUB_OPEN_ENDPOINT="1")
    assert done.returncode == 1
    assert "0.0.0.0/0" in done.stderr
    assert not (deploy.state / "kubectl.argv").exists()  # nothing was applied


def test_deploy_stops_when_the_endpoint_cannot_be_read(deploy):
    done = deploy(STUB_DESCRIBE_CLUSTER_FAILS="1")
    assert done.returncode == 1
    assert "could not read cluster cao-test" in done.stderr
    assert not (deploy.state / "kubectl.argv").exists()
