"""The shipped EKS example's shared MCP endpoint is wired end to end (#745).

Five files have to agree for a supervisor agent to be able to delegate at all:
the sidecar's port, the Service port, the URL the supervisor dials, the path the
code serves, and the NetworkPolicy between them. Nothing at runtime reports a
mismatch usefully — the agent comes up with its tools listed and the first call
hangs until CAO_MCP_REQUEST_TIMEOUT, which reads as a broken tool. These are the
equalities, asserted against the manifests as shipped.

The credential boundary is asserted here too, because it is the reason the
endpoint exists: the broker URL and token belong to the server pod, and a pod
that runs an agent must not carry them.
"""

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from cli_agent_orchestrator.mcp_server.http_hosting import MCP_HTTP_PATH
from cli_agent_orchestrator.utils.mcp_resolution import SHARED_ENDPOINT_URL_ENV

EKS = Path(__file__).resolve().parents[2] / "examples/cao-clusters/kubernetes/eks"
BROKER_CREDENTIALS = ("CAO_ELASTIC_BROKER_URL", "CAO_ELASTIC_BROKER_TOKEN")


def _docs(name):
    return [d for d in yaml.safe_load_all((EKS / name).read_text()) if d]


def _one(name, kind, resource_name=None):
    return next(
        d
        for d in _docs(name)
        if d["kind"] == kind and (resource_name is None or d["metadata"]["name"] == resource_name)
    )


def _containers(sts):
    return {c["name"]: c for c in sts["spec"]["template"]["spec"]["containers"]}


def _env(container):
    return {e["name"]: e for e in container["env"]}


def _value(container, name):
    return _env(container)[name].get("value")


@pytest.fixture(scope="module")
def server():
    return _one("server.yaml", "StatefulSet")


@pytest.fixture(scope="module")
def supervisor():
    return _one("supervisor.yaml", "StatefulSet")


@pytest.fixture(scope="module")
def endpoint(supervisor):
    return urlparse(_value(_containers(supervisor)["cao-node"], SHARED_ENDPOINT_URL_ENV))


class TestTheAddressIsOneAddress:
    def test_the_supervisor_dials_the_port_the_sidecar_binds(self, server, endpoint):
        sidecar = _containers(server)["cao-mcp"]
        assert str(endpoint.port) == _value(sidecar, "CAO_MCP_HTTP_PORT")

    def test_the_sidecar_publishes_the_port_it_binds(self, server):
        sidecar = _containers(server)["cao-mcp"]
        ports = [p["containerPort"] for p in sidecar["ports"]]
        assert int(_value(sidecar, "CAO_MCP_HTTP_PORT")) in ports

    def test_the_service_publishes_it_too(self, endpoint):
        service = _one("server.yaml", "Service", "cao-server")
        published = {p["port"]: p["targetPort"] for p in service["spec"]["ports"]}
        assert published.get(endpoint.port) == endpoint.port

    def test_the_url_names_the_service_not_a_pod(self, endpoint):
        """A bare pod name does not resolve; the governing Service's FQDN does."""
        assert endpoint.hostname.startswith("cao-server.")

    def test_the_path_is_the_one_the_code_serves(self, endpoint):
        assert endpoint.path == MCP_HTTP_PATH

    def test_the_sidecar_binds_where_another_pod_can_reach_it(self, server):
        """The code default is loopback, which would be unreachable here."""
        assert _value(_containers(server)["cao-mcp"], "CAO_MCP_HTTP_HOST") == "0.0.0.0"

    def test_the_sidecar_speaks_http_not_stdio(self, server):
        assert _value(_containers(server)["cao-mcp"], "CAO_MCP_TRANSPORT") == "http"

    def test_the_endpoint_does_not_collide_with_the_broker(self, endpoint):
        """The code's default MCP port is 9890, which is the broker's port in this
        example. Two services on one number is a debugging trap, so the manifest
        pins it away — this is that pin, not an incidental value."""
        broker = _one("broker.yaml", "Service", "cao-worker-broker")
        assert endpoint.port not in {p["port"] for p in broker["spec"]["ports"]}


class TestTheNetworkAdmitsIt:
    @pytest.fixture(scope="class")
    def policies(self):
        return {d["metadata"]["name"]: d for d in _docs("networkpolicy.yaml")}

    def _peers(self, rule, direction):
        return {
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
            for peer in rule[direction]
            if "podSelector" in peer and peer["podSelector"].get("matchLabels")
        }

    def _rules_for(self, policy, key, direction, port):
        return [r for r in policy["spec"][key] if any(p["port"] == port for p in r["ports"])]

    def test_the_server_admits_the_supervisor_on_the_endpoint(self, policies, endpoint):
        rules = self._rules_for(policies["cao-server-ingress"], "ingress", "from", endpoint.port)
        assert rules, "nothing may reach the shared endpoint at all"
        assert {"cao-supervisor"} == set().union(*(self._peers(r, "from") for r in rules))

    def test_the_supervisor_may_egress_to_it(self, policies, endpoint):
        rules = self._rules_for(policies["cao-supervisor-egress"], "egress", "to", endpoint.port)
        assert rules, "the supervisor cannot reach the endpoint it is configured to use"
        assert "cao-server" in set().union(*(self._peers(r, "to") for r in rules))


class TestTheCredentialBoundaryHolds:
    def test_the_sidecar_holds_the_broker_credentials(self, server):
        env = _env(_containers(server)["cao-mcp"])
        assert all(name in env for name in BROKER_CREDENTIALS)

    def test_the_supervisor_holds_none_of_them(self, supervisor):
        """The whole point: delegation tools run where the credentials are, so
        the pod running the agent needs none. This is the assertion that fails if
        someone "fixes" a delegation problem by copying the broker env across."""
        env = _env(_containers(supervisor)["cao-node"])
        assert not [name for name in BROKER_CREDENTIALS if name in env]

    def test_a_minted_worker_is_not_forwarded(self):
        """complete_assignment reads the worker's own lease identity, so a worker
        keeps a local MCP server. Asserted against broker.py's source because the
        worker pod spec is built in code, not in a manifest."""
        source = (EKS / "broker.py").read_text()
        assert f'name="{SHARED_ENDPOINT_URL_ENV}"' not in source

    def test_the_endpoint_is_authenticated(self, server):
        """build_http_app refuses to start without it, so an endpoint that exists
        is an endpoint that demands the token."""
        env = _env(_containers(server)["cao-mcp"])
        assert env["CAO_RUNTIME_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "cao-runtime-token"

    def test_the_supervisor_can_present_that_token(self, supervisor):
        """The shim forwards it from the pod env; the pod already has it for its
        own runtime channel, so this is not a new secret in the agent's reach."""
        env = _env(_containers(supervisor)["cao-node"])
        assert env["CAO_RUNTIME_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "cao-runtime-token"


class TestTheSidecarStartsSafely:
    def test_it_waits_for_the_migrating_process(self, server):
        """Both processes open the same SQLite file and only the server migrates
        it, so binding first means crash-looping against no schema."""
        sidecar = _containers(server)["cao-mcp"]
        assert any("/health" in arg for arg in sidecar["args"])

    def test_it_shares_the_state_volume_it_reads(self, server):
        sidecar = _containers(server)["cao-mcp"]
        mounts = {m["name"]: m for m in sidecar["volumeMounts"]}
        node = {m["name"]: m for m in _containers(server)["cao-node"]["volumeMounts"]}
        assert mounts["state"]["mountPath"] == node["state"]["mountPath"]
        assert _value(sidecar, "CAO_HOME_DIR") == _value(
            _containers(server)["cao-node"], "CAO_HOME_DIR"
        )

    def test_it_is_probed_on_tcp_not_http(self, server):
        """Every MCP call must carry the token, so an HTTP probe would be 401ed
        and would restart this pod forever."""
        sidecar = _containers(server)["cao-mcp"]
        for probe in ("readinessProbe", "livenessProbe"):
            assert "tcpSocket" in sidecar[probe]
            assert "httpGet" not in sidecar[probe]

    def test_it_is_bounded(self, server):
        sidecar = _containers(server)["cao-mcp"]
        assert sidecar["resources"]["requests"] and sidecar["resources"]["limits"]


class TestGracePeriodsAreStated:
    @pytest.mark.parametrize(
        "manifest",
        ["server.yaml", "supervisor.yaml"],
    )
    def test_both_pods_state_a_grace_period(self, manifest):
        """The broker gives a minted worker 30s because SIGTERM reaches tmux and
        a provider CLI; the pods in these manifests run the same stack and must
        not be left to an implicit default."""
        spec = _one(manifest, "StatefulSet")["spec"]["template"]["spec"]
        assert spec["terminationGracePeriodSeconds"] == 30
