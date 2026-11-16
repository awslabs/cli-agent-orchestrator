#!/usr/bin/env python3
"""Fake `hc connect` for hc_rpc tests. Behavior comes from FAKE_HC_MODE."""

import json
import os
import sys

mode = os.environ.get("FAKE_HC_MODE", "echo")
log = os.environ.get("FAKE_HC_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")

for line in sys.stdin:
    req = json.loads(line)
    method, rid = req["method"], req.get("id")
    if mode == "eof_after_request":
        sys.exit(0)
    if method == "hello":
        result = {
            "generation": int(os.environ.get("FAKE_HC_GEN", "1")),
            "methods": os.environ.get("FAKE_HC_METHODS", "").split(","),
        }
        out = {"jsonrpc": "2.0", "id": rid, "result": result}
    elif method == "fail":
        out = {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": -32000, "message": "no terminal", "data": {"kind": "not_found"}},
        }
    elif method == "events.subscribe":
        reply = {"jsonrpc": "2.0", "id": rid, "result": {"seq": 0, "terminals": []}}
        print(json.dumps(reply), flush=True)
        for n in (
            {
                "method": "event",
                "params": {"seq": 1, "type": "title", "terminal": "t1", "data": {}},
            },
            {"method": "heartbeat", "params": {"seq": 1}},
        ):
            print(json.dumps({"jsonrpc": "2.0", **n}), flush=True)
        continue
    else:
        out = {"jsonrpc": "2.0", "id": rid, "result": {"echo": req.get("params")}}
    print(json.dumps(out), flush=True)
