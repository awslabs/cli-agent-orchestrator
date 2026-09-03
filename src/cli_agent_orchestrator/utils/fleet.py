"""Client for a CAO fleet's worker broker.

`cao fleet` and `cao worker` talk to a broker over HTTP. They do not talk to
Kubernetes, and that is the point of this module living in core: the contract is
four routes and one token, so the same commands work against the EKS broker in
`examples/cao-clusters/kubernetes/eks`, against any other broker that implements
them, and against a stub in a test — with no kubernetes import anywhere in `src/`.

The contract:

    GET    /workers                     lease ledger: state, reason, age
    DELETE /workers/{id}                release one worker
    GET    /workers/{id}/logs           the worker container's log
    GET|POST /workers/{id}/api/{path}   an allowlisted cao-server call, proxied

The last one is what makes `cao worker` possible at all. A worker's cao-server is
not reachable from outside its cluster — on EKS its Service is ClusterIP and
exists only for the length of one task — so the broker forwards a fixed set of
read and send routes to whichever worker the caller names. It is an allowlist on
the broker's side, so a route this client does not use is a route it cannot reach.

One port-forward to the broker is therefore the whole setup:

    kubectl -n cao-cluster port-forward svc/cao-worker-broker 9890:9890
    export CAO_ELASTIC_BROKER_URL=http://127.0.0.1:9890
    export CAO_ELASTIC_BROKER_TOKEN=...
"""

import json
import os
from typing import Any, Iterator, Optional
from urllib.parse import quote

import click
import requests

from cli_agent_orchestrator.constants import (
    ELASTIC_BROKER_TOKEN_ENV,
    ELASTIC_BROKER_TOKEN_HEADER,
    ELASTIC_BROKER_URL_ENV,
)

# Connect fast, read patiently. A broker is normally one port-forward away, so a
# slow connect means it is not there; a slow read means a worker is thinking.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 60.0

# States in which a worker still exists. The broker's own reaper settles a lease
# into anything else — released, completed, failed, terminated, expired — and its
# `reason` is the only record of why a delegation that claimed success produced
# nothing. Kept in sync with `_LIVE_LEASE_STATES` in the broker.
LIVE_STATES = frozenset({"creating", "leased"})


class FleetClient:
    """One fleet, addressed through its broker."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self._headers = {ELASTIC_BROKER_TOKEN_HEADER: token}

    @classmethod
    def from_env(cls) -> "FleetClient":
        url = os.environ.get(ELASTIC_BROKER_URL_ENV, "").strip()
        token = os.environ.get(ELASTIC_BROKER_TOKEN_ENV, "").strip()
        if not url or not token:
            raise click.ClickException(
                f"No fleet configured. Set {ELASTIC_BROKER_URL_ENV} and "
                f"{ELASTIC_BROKER_TOKEN_ENV} to point at your fleet's worker broker "
                "(port-forward it first if you are outside the cluster)."
            )
        return cls(url, token)

    # -- broker's own routes -------------------------------------------------

    def workers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/workers").json()

    def release(self, worker_id: str) -> bool:
        """Release one worker. True if it is now gone, including if it already was."""
        response = self._request("DELETE", f"/workers/{worker_id}", raise_for_status=False)
        if response.status_code == 404:
            return True
        self._check(response)
        return True

    def logs(self, worker_id: str, *, tail_lines: int = 200) -> str:
        return self._request(
            "GET", f"/workers/{worker_id}/logs", params={"tail_lines": tail_lines}
        ).text

    def follow_logs(self, worker_id: str, *, tail_lines: int = 200) -> Iterator[str]:
        """Stream a worker's log. No read timeout: a quiet log is not a stalled one."""
        try:
            response = requests.get(
                f"{self.url}/workers/{worker_id}/logs",
                params={"tail_lines": tail_lines, "follow": "true"},
                headers=self._headers,
                stream=True,
                timeout=(_CONNECT_TIMEOUT, None),
            )
        except requests.RequestException as exc:
            raise click.ClickException(f"Failed to reach the fleet broker: {exc}") from exc
        self._check(response)
        return response.iter_lines(decode_unicode=True)

    # -- the worker's own cao-server, proxied --------------------------------

    def node_get(self, worker_id: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        return self._request("GET", self._node_path(worker_id, path), params=params).json()

    def node_post(self, worker_id: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        response = self._request("POST", self._node_path(worker_id, path), params=params)
        try:
            return response.json()
        except ValueError:
            return None

    def _node_path(self, worker_id: str, path: str) -> str:
        return f"/workers/{worker_id}/api/{path.lstrip('/')}"

    # -- worker-scoped conveniences, matching `cao session`'s routes ---------

    def sessions(self, worker_id: str) -> list[dict[str, Any]]:
        return self.node_get(worker_id, "sessions")

    def terminals(self, worker_id: str, session_name: str) -> list[dict[str, Any]]:
        return self.node_get(worker_id, f"sessions/{quote(session_name, safe='')}/terminals")

    def terminal(self, worker_id: str, terminal_id: str) -> dict[str, Any]:
        return self.node_get(worker_id, f"terminals/{terminal_id}")

    def terminal_status(self, worker_id: str, terminal_id: str) -> Optional[str]:
        return self.terminal(worker_id, terminal_id).get("status")

    def terminal_output(self, worker_id: str, terminal_id: str) -> Optional[str]:
        payload = self.node_get(worker_id, f"terminals/{terminal_id}/output", {"mode": "last"})
        return payload.get("output") if isinstance(payload, dict) else None

    def send_input(self, worker_id: str, terminal_id: str, message: str) -> None:
        self.node_post(worker_id, f"terminals/{terminal_id}/input", {"message": message})

    def sole_terminal(self, worker_id: str) -> dict[str, Any]:
        """The worker's one terminal.

        A worker is minted with ``CAO_MAX_TERMINALS=1``, so naming a terminal is
        an argument nobody should have to supply — and a worker with two would be
        a topology bug worth surfacing rather than picking a winner for.
        """
        sessions = self.sessions(worker_id)
        terminals = [t for s in sessions for t in self.terminals(worker_id, s["name"])]
        if not terminals:
            raise click.ClickException(
                f"Worker {worker_id} has no terminal yet. It may still be booting; "
                f"`cao worker logs {worker_id}` shows how far it got."
            )
        return terminals[0]

    # -- transport -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        raise_for_status: bool = True,
    ) -> requests.Response:
        try:
            response = requests.request(
                method,
                f"{self.url}{path}",
                params=params,
                headers=self._headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise click.ClickException(
                f"Failed to reach the fleet broker at {self.url}: {exc}"
            ) from exc
        if raise_for_status:
            self._check(response)
        return response

    def _check(self, response: requests.Response) -> None:
        """Turn a broker error into the sentence the broker actually wrote.

        Worth the code: the broker's 409 says which lease settled and why, and its
        404 on a proxied path says the route is not allowlisted rather than that
        the worker is missing. A bare `raise_for_status` would replace both with a
        status code.
        """
        if response.status_code < 400:
            return
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))
            else:
                detail = json.dumps(body)
        except ValueError:
            detail = (response.text or "").strip()
        if response.status_code == 401:
            detail = detail or f"broker rejected the token in {ELASTIC_BROKER_TOKEN_ENV}"
        raise click.ClickException(detail or f"broker returned HTTP {response.status_code}")
