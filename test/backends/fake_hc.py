"""In-memory stand-in for HcRpcClient: terminals, documents, and a call log."""

import copy
import itertools
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.clients.hc_rpc import REQUIRED_METHODS, HcRpcError


class FakeHc:
    def __init__(self, generation: int = 1) -> None:
        self.session = "cao"
        self.generation = generation
        self.terminals: Dict[str, Dict[str, Any]] = {}
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self.conflicts_to_inject = 0
        self.fail_methods: set = set()
        # Addresses whose terminal outlives terminal.kill and terminal.rm (a
        # kill the core accepted but that did not take, as seen by a re-list).
        self.unkillable: set = set()
        self.closed = False
        self._addr = itertools.count(1)

    # helpers for tests
    def add_terminal(self, labels: Dict[str, str], **fields: Any) -> Dict[str, Any]:
        address = f"t{next(self._addr)}"
        record = {
            "address": address,
            "name": fields.pop("name", None),
            "state": "live",
            "labels": dict(labels),
            "status": None,
            "cwd": "/tmp",
            "parent": None,
            "foreground": {"name": "bash", "cwd": "/tmp", "argv": ["bash"]},
        }
        record.update(fields)
        self.terminals[address] = record
        return record

    def set_doc(self, name: str, value: Any, version: int = 1) -> None:
        self.docs[name] = {"value": copy.deepcopy(value), "version": version}

    def calls_to(self, method: str) -> List[Dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def _find(self, ref: str) -> Dict[str, Any]:
        for t in self.terminals.values():
            if ref in (t["address"], t["name"]):
                return t
        raise HcRpcError("not_found", f'no terminal "{ref}"')

    # HcRpcClient surface
    def start_server(self) -> None:
        self.calls.append(("server.start", {}))

    def close(self) -> None:
        self.closed = True

    def hello(self) -> Dict[str, Any]:
        from cli_agent_orchestrator.clients.hc_rpc import HcProtocolError

        if self.generation != 1:
            raise HcProtocolError(f"herdr-core speaks protocol generation {self.generation}")
        return {"generation": 1, "methods": sorted(REQUIRED_METHODS)}

    def call(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0
    ) -> Any:
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method in self.fail_methods:
            raise HcRpcError("unavailable", f"{method} failed")
        if method == "terminal.run":
            labels = params.get("labels", {})
            return copy.deepcopy(
                self.add_terminal(labels, name=params.get("name"), cwd=params.get("cwd"))
            )
        if method == "terminal.list":
            want = params.get("labels", {})
            return {
                "terminals": [
                    copy.deepcopy(t)
                    for t in self.terminals.values()
                    if (params.get("all") or t["state"] == "live")
                    and all(t["labels"].get(k) == v for k, v in want.items())
                ]
            }
        if method == "terminal.get":
            return copy.deepcopy(self._find(params["terminal"]))
        if method == "terminal.kill":
            t = self._find(params["terminal"])
            if t["address"] not in self.unkillable:
                t["state"] = "finished"
            return copy.deepcopy(t)
        if method == "terminal.label":
            t = self._find(params["terminal"])
            for key, value in params["labels"].items():
                if value == "":  # `key=` removes the label
                    t["labels"].pop(key, None)
                else:
                    t["labels"][key] = value
            return copy.deepcopy(t)
        if method == "terminal.rm":
            t = self._find(params["terminal"])
            if t["address"] not in self.unkillable:
                del self.terminals[t["address"]]
            return {"removed": t["address"]}
        if method == "terminal.read":
            return {"text": self._find(params["terminal"]).get("text", ""), "complete": True}
        if method in ("terminal.send", "terminal.keys"):
            self._find(params["terminal"])
            return {"status": None, "turn": 0}
        if method == "doc.get":
            d = self.docs.get(params["name"], {"value": None, "version": 0})
            return copy.deepcopy(d)
        if method == "doc.set":
            d = self.docs.get(params["name"], {"value": None, "version": 0})
            if self.conflicts_to_inject or params["version"] != d["version"]:
                self.conflicts_to_inject = max(0, self.conflicts_to_inject - 1)
                d["version"] += 1  # someone else wrote meanwhile
                self.docs[params["name"]] = d
                raise HcRpcError("conflict", f"document is at version {d['version']}")
            self.docs[params["name"]] = {
                "value": copy.deepcopy(params["value"]),
                "version": d["version"] + 1,
            }
            return {"version": d["version"] + 1}
        raise AssertionError(f"FakeHc: unexpected method {method}")


def layout_with(space_id: str, tabs: List[tuple], space_name: str = "") -> Dict[str, Any]:
    """A herdr-app document with one space; tabs are (tab_id, tab_name, address)."""
    return {
        "spaces": [
            {
                "id": space_id,
                "name": space_name,
                "folder": "/tmp",
                "tabs": [
                    {
                        "id": tb,
                        "name": name,
                        "root": {
                            "id": tb,
                            "type": "row",
                            "children": [
                                {"id": f"p{tb}", "type": "block", "content": {"terminal": addr}}
                            ],
                        },
                    }
                    for tb, name, addr in tabs
                ],
            }
        ]
    }
