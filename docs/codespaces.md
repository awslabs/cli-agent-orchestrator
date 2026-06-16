# Working in GitHub Codespaces

This project runs end-to-end inside a GitHub Codespace. The server, the Web UI,
and the dev workflow are all pre-wired — you just need to start the server and
forward the port.

## Prerequisites

A GitHub account with Codespaces enabled. The default Codespace machine type
(2 vCPU / 8 GB RAM) is enough for development and tests.

The repository does not yet ship a custom `.devcontainer/devcontainer.json`, so
Codespaces uses the default `universal` image. The first build installs:

- Python 3.x and `uv` (via the default image)
- tmux and a few CLI tools

No additional setup steps are required.

## Start the server

From the codespace terminal, in the repository root:

```bash
pkill -9 -f "cao-server" || true

cd /workspaces/cli-agent-orchestrator
CAO_API_HOST=0.0.0.0 \
CAO_API_PORT=9889 \
CAO_ALLOWED_HOSTS="*" \
CAO_WS_ALLOWED_CLIENTS="*" \
  uv run cao-server --host 0.0.0.0 --port 9889
```

What each variable does:

| Variable                  | Why we set it                                                                                                          |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `CAO_API_HOST=0.0.0.0`    | Bind on all interfaces so the GitHub port-forward proxy can reach the server. `127.0.0.1` will not work through a forwarded URL. |
| `CAO_API_PORT=9889`       | The port the forwarded URL maps to.                                                                                    |
| `CAO_ALLOWED_HOSTS="*"`   | FastAPI's `TrustedHostMiddleware` accepts the `*.app.github.dev` forwarded hostname.                                   |
| `CAO_WS_ALLOWED_CLIENTS="*"` | WebSocket connections from the forwarded origin are accepted.                                                       |

You should see:

```
INFO:     Uvicorn running on http://0.0.0.0:9889 (Press CTRL+C to quit)
```

## Forward the port

1. Open the **Ports** tab in the codespace.
2. If port `9889` is not listed, add it (local port `9889`).
3. Right-click the row and set visibility to **Public** if you want to open the
   UI from a browser not signed into GitHub. **Private** works only inside
   GitHub.com tabs authenticated to the codespace.
4. Click the forwarded address — the URL has the form
   `https://<codespace-name>-9889.app.github.dev/`.

Open the bare URL with no path. `cao-server` serves the Web UI at `/`, and
hitting any unknown path (including the codespace chrome's trailing
`<environment_details>`) returns **HTTP 404** because the SPA only has a
catch-all route for client-side navigation.

## Verify

From inside the codespace terminal:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9889/health   # 200
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9889/         # 200
```

If `/health` returns 200 inside the codespace but the forwarded URL returns
404, the issue is the port forward, not the server. Re-check the **Ports** tab.

## Troubleshooting

| Symptom                                                                                  | Cause / Fix                                                                                                                                |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Forwarded URL returns 404 immediately                                                    | Port `9889` is not forwarded, the codespace is suspended, or visibility is **Private** in an incognito tab. Open the **Ports** tab and wake / re-add the port. |
| `curl http://127.0.0.1:9889/` works but the forwarded URL 404s                            | The codespace auto-suspended. Open the codespace in a GitHub.com tab to wake it, then retry.                                               |
| `curl http://127.0.0.1:9889/health` fails with connection refused                         | The `cao-server` process is not running. Restart it using the command in [Start the server](#start-the-server).                            |
| `curl http://127.0.0.1:9889/some/path` 404s while `/` works                              | Expected — only `/`, `/health`, `/docs`, and the OpenAPI routes are registered. The Web UI handles other paths client-side.               |
| WebSocket connection fails with 400/403                                                  | `CAO_WS_ALLOWED_CLIENTS` is too restrictive. Set it to `"*"` for local dev.                                                                |
| Browser shows "No webpage was found" on the forwarded URL                                | Either the codespace is stopped or the port is not forwarded. See above.                                                                   |

## Stopping the server

`Ctrl+C` in the terminal where `cao-server` is running. The codespace will
auto-suspend after the default idle timeout (30 minutes); the forwarded URL
will start returning 404 once the codespace is suspended.
