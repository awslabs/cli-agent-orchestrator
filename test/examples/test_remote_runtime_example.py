"""The remote-runtime example authenticates its API (#745).

The cluster-wide Service is reachable by any pod the network lets through, and
NetworkPolicy is not enforced on every cluster. So the server requires an API
token, the runtime pod (where agents run) holds none, and the README's calls
pass it.
"""

import re
from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/cao-clusters/kubernetes/remote-runtime"


def _container(manifest: str, name: str) -> dict:
    for doc in yaml.safe_load_all((EXAMPLE / manifest).read_text()):
        if doc and doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == name:
            (container,) = doc["spec"]["template"]["spec"]["containers"]
            return container
    raise AssertionError(f"no StatefulSet {name} in {manifest}")


def _env(container: dict) -> dict:
    return {e["name"]: e for e in container.get("env", [])}


def test_the_server_requires_an_api_token_from_a_secret():
    env = _env(_container("server.yaml", "cao-server"))
    assert env["CAO_AUTH_LOCAL_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "cao-api-token",
        "key": "token",
    }


def test_the_runtime_holds_no_api_credential():
    container = _container("runtime.yaml", "cao-runtime")
    assert "envFrom" not in container
    assert not {"CAO_AUTH_LOCAL_TOKEN", "AUTH0_DOMAIN", "CAO_AUTH_JWKS_URI"} & set(_env(container))


def test_every_api_call_in_the_readme_passes_the_token():
    readme = (EXAMPLE / "README.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", readme, re.S)
    calls = [line for block in blocks for line in block.splitlines() if "curl " in line]
    assert calls, "the README drives the API with curl"
    assert all('-H "$AUTH"' in line for line in calls), calls
    assert "secret generic cao-api-token" in readme
