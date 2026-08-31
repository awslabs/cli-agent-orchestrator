---
sidebar_position: 5
---

# Web UI

CAO ships a browser-based dashboard for managing agents, terminals, and flows. The pre-built UI is packaged inside the Python wheel, so **no Node.js or `npm install` is required** to use it.

## Starting the Web UI

### Production mode (recommended)

```bash
cao-server
```

Open http://localhost:9889 in your browser. The server serves both the API and the pre-built frontend from a single process.

### Development mode (hot-reload)

For contributors working on the frontend:

```bash
# Terminal 1 -- backend
cao-server

# Terminal 2 -- frontend dev server (requires Node.js 18+)
cd web/
npm install        # first time only
npm run dev        # starts on http://localhost:5173
```

Open http://localhost:5173. The Vite dev server proxies API requests to the backend on port 9889.

## Features

- **Session dashboard** -- view all active sessions and their terminals at a glance
- **Terminal status** -- real-time IDLE / PROCESSING / COMPLETED / ERROR indicators
- **Terminal output** -- live-streaming scrollback from each agent
- **WebSocket PTY** -- full interactive terminal access from the browser (`/terminals/{id}/ws`)
- **Inbox monitoring** -- see pending and delivered messages per terminal
- **Flow management** -- view scheduled flows, next run times, enable/disable
- **Settings** -- configure agent directories, skill directories, memory, and backend from the UI
- **Session control** -- launch, send messages, and shut down sessions from the browser

## Remote Access over SSH

If CAO runs on a remote host (cloud dev desktop, EC2, etc.), forward the port over SSH:

```bash
# Production mode -- single port
ssh -L 9889:localhost:9889 your-remote-host

# Dev mode -- forward both frontend and backend
ssh -L 5173:localhost:5173 -L 9889:localhost:9889 your-remote-host
```

Then open http://localhost:9889 (or :5173 for dev mode) on your local machine.

## Custom Host and Port

```bash
cao-server --host 0.0.0.0 --port 9889
```

:::caution
Binding to `0.0.0.0` exposes the server to the network. The WebSocket PTY endpoint provides full terminal access and is unauthenticated by default. Only do this on trusted networks or behind a reverse proxy with authentication.
:::

## Rebuilding the Frontend

Only needed if you modify files under `web/`:

```bash
cd web/
npm install && npm run build   # outputs to src/cli_agent_orchestrator/web_ui/
uv tool install . --reinstall  # re-package the wheel with the new build
```

## Architecture

The Web UI is a React/TypeScript application built with Vite. In production, the compiled static assets are bundled into the CAO Python package at `src/cli_agent_orchestrator/web_ui/` and served directly by the FastAPI backend. No separate frontend process is needed.
