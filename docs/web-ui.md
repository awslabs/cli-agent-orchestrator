# Web UI

CAO includes a web dashboard for managing agents, terminals, and flows from the browser.

![CAO Web UI](https://github.com/user-attachments/assets/e7db9261-62b1-4422-b9f5-6fe5f65bdea4)

## Additional requirements

- **Node.js 18+** — required for the frontend dev server and Codex CLI.

```bash
# macOS (Homebrew)
brew install node

# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs

# Amazon Linux 2023 / Fedora
sudo dnf install nodejs20

# Verify
node --version   # Should be 18 or higher
```

## Starting the Web UI

### Option A: Development mode (hot-reload, two terminals)

```bash
# Terminal 1 — start the backend server
cao-server

# Terminal 2 — start the frontend dev server
cd web/
npm install        # First time only
npm run dev        # Starts on http://localhost:5173
```

Open http://localhost:5173 in your browser.

### Option B: Production mode (single server, no Vite needed)

The built Web UI is bundled into the CAO wheel, so a plain `uv tool install` ships everything you need. Just start the server:

```bash
cao-server
```

Open http://localhost:9889 in your browser.

To rebuild the frontend from source:

```bash
cd web/
npm install && npm run build   # Outputs to src/cli_agent_orchestrator/web_ui/
uv tool install . --reinstall
```

> **Custom host/port:** `cao-server --host 0.0.0.0 --port 9889` exposes the server to the network — see [Security](../README.md#security) in the root README before doing this.

## Remote machine access

If you are running CAO on a remote host (e.g. a dev desktop), forward the ports over SSH:

```bash
# Dev mode (proxy both frontend and backend)
ssh -L 5173:localhost:5173 -L 9889:localhost:9889 your-remote-host

# Production mode (backend serves UI directly)
ssh -L 9889:localhost:9889 your-remote-host
```

Then open the same URLs (localhost:5173 or localhost:9889) in your local browser.

## Features

Manage sessions, spawn agents, create scheduled flows, configure agent directories, and interact with live terminals — all from the browser. Includes live status badges, an inbox for agent-to-agent messaging, output viewer, and provider auto-detection.

## Related

- [web/README.md](../web/README.md) — frontend architecture and component details
- [docs/settings.md](settings.md) — agent directory configuration
- [docs/control-planes.md](control-planes.md) — where the Web UI fits alongside `cao session` and `cao-ops-mcp`
