# PR #533 Review

PR: https://github.com/awslabs/cli-agent-orchestrator/pull/533  
Reviewed head: `381e3fb`  
Verdict: request changes before approval

## Confirmed Finding

[P2] Legitimate same-origin terminal viewers are rejected outside the
`cao-server` entry point's locally derived origins.

Files:

- `src/cli_agent_orchestrator/constants.py:461`
- `src/cli_agent_orchestrator/api/main.py:3240`
- `src/cli_agent_orchestrator/api/main.py:3993`
- `docs/codespaces.md:27`

The Origin guard correctly rejects arbitrary cross-site origins, but it treats a
browser connection as trusted only when its exact Origin appears in
`CORS_ORIGINS` or `CAO_WS_ALLOWED_ORIGINS`. The server's own origins are added
only when `main()` calls `add_local_cors_origins(host, port)`.

This breaks two legitimate same-origin paths:

1. The documented Codespaces setup binds `0.0.0.0:9889` and serves the UI at
   `https://<codespace-name>-9889.app.github.dev`. The current command sets
   `CAO_ALLOWED_HOSTS="*"` and `CAO_WS_ALLOWED_CLIENTS="*"`, but not an allowed
   WebSocket Origin. `add_local_cors_origins("0.0.0.0", 9889)` adds only
   `http://localhost:9889`, `http://127.0.0.1:9889`, and
   `http://[::1]:9889`. The UI and REST calls load through the proxy, but every
   terminal socket is closed with 4403.
2. The explicitly supported imported-app path
   `uvicorn cli_agent_orchestrator.api.main:app` does not execute `main()`.
   Even its default same-origin viewer at `http://localhost:9889` is absent
   from the static CORS defaults and is rejected with 4403.

I reproduced the imported-app case against the real ASGI stack:

```text
foreign:            disconnect code=4403 reason='WebSocket Origin not allowed'
opaque/null:        disconnect code=4403 reason='WebSocket Origin not allowed'
same-origin-default disconnect code=4403 reason='WebSocket Origin not allowed'
```

This is P2 rather than P1 because the normal `cao-server` launch is protected
and continues to work, and affected deployments have a configuration
workaround. It is still merge-blocking because it disables the terminal viewer
in a documented deployment and makes the PR's "no breaking change for the
supported deployment" statement inaccurate.

## Suggested Fix

Prefer recognizing the request's actual same origin: strictly parse `Origin`
and allow it when its authority matches the trusted WebSocket request `Host`
(with the expected HTTP/HTTPS scheme), while retaining the explicit allowlists
for genuinely cross-origin viewers. This naturally supports dynamic reverse
proxy/Codespaces hostnames and imported ASGI deployments without broadening
trust to arbitrary sites.

If the implementation remains entirely allowlist-based:

- Initialize origins for `SERVER_HOST` / `SERVER_PORT` outside `main()` so
  imported-app deployments work.
- Add the exact Codespaces forwarding origin to the documented launch command.
- Document and register `CAO_WS_ALLOWED_ORIGINS` in `NetworkConfig`,
  `ENV_REGISTRY`, and `docs/configuration.md`.
- Add integration coverage for the bundled same-origin viewer and a proxied
  HTTPS origin.

## Suggested PR Comment

```md
The Origin check is in the right place and blocks the reported CSWSH path, but
I found one P2 compatibility regression before this is ready to approve.

The new guard only trusts exact entries in `CORS_ORIGINS` /
`CAO_WS_ALLOWED_ORIGINS`, while the server's own origins are populated only by
`main()` calling `add_local_cors_origins()`.

This breaks the documented Codespaces flow. It serves the bundled UI from
`https://<codespace>-9889.app.github.dev`, but the documented command only sets
`CAO_ALLOWED_HOSTS="*"` and `CAO_WS_ALLOWED_CLIENTS="*"`.
`add_local_cors_origins("0.0.0.0", 9889)` adds only local HTTP origins, so the
UI and REST API load but terminal WebSockets close with 4403.

It also breaks `uvicorn cli_agent_orchestrator.api.main:app`: that imported-app
path never calls `main()`, so even `Origin: http://localhost:9889` is rejected.
I reproduced that result against the real ASGI stack.

Please make legitimate same-origin requests work independently of the CLI
entry point. The most robust approach is to strictly parse `Origin` and accept
it when its authority matches the trusted request `Host`, retaining the
allowlists for genuinely cross-origin viewers. If you keep the current model,
initialize the configured server origin outside `main()` and update the
Codespaces/config docs and `ENV_REGISTRY` for
`CAO_WS_ALLOWED_ORIGINS`. Please add regression coverage for both paths.
```

## Nonblocking Notes

- Copilot's suggestion to make `CAO_CORS_ORIGINS="*"` automatically disable
  WebSocket Origin validation should not be adopted. PTY access is more
  sensitive than ordinary CORS access; only the explicit
  `CAO_WS_ALLOWED_ORIGINS="*"` escape hatch should disable this guard. Clarify
  that intentional difference in the helper documentation.
- Log `terminal_id` with `%r`, as Copilot suggested, so encoded control
  characters cannot create misleading multiline warning entries.
- Add `CAO_WS_ALLOWED_ORIGINS` to the environment-cleanup lists in
  `test/test_constants.py` so the constants tests remain isolated when that
  variable is present in the test runner's environment.

## What Is Correct

- Validation runs before `websocket.accept()` and before terminal lookup, so a
  rejected cross-site caller cannot use valid/invalid IDs as an oracle.
- Exact comparison rejects foreign origins, malformed values, and the literal
  `null` origin.
- Missing Origin remains compatible with non-browser clients; browser
  JavaScript cannot suppress the Origin header on a WebSocket handshake.
- The existing client-IP restriction remains in force.

## Validation

```text
uv run pytest test/api/test_terminals.py test/test_constants.py -q
102 passed

uv run black --check <four changed files>
4 files would be left unchanged

uv run isort --check-only <four changed files>
passed

git diff --check origin/main...HEAD
passed
```

All GitHub checks were green at the end of review.
