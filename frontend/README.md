# CAO Frontend — Next.js Console

A Next.js web console for the **CLI Agent Orchestrator** (CAO) API.

## Architecture

```
Browser (React UI)
    │
    │  HTTP /api/cao/*
    ▼
Next.js App (port 3000)          ← this directory
    │  Next.js API Routes
    │  (middleware layer)
    │  HTTP *
    ▼
cao-control-panel (port 8000)    ← FastAPI interface layer
    │  Proxy layer
    │  HTTP *
    ▼
cao-server (port 9889)           ← FastAPI backend
```

The Next.js API routes at `/api/cao/[...path]` act as a **middleware layer**
that proxies all requests from the browser to the `cao-control-panel`. The
`cao-control-panel` is a FastAPI interface layer that communicates with the
`cao-server` backend. This three-tier architecture keeps the frontend, control
panel, and backend services decoupled and independent.

## Getting Started

1. Start the `cao-server` backend (from the repo root):

   ```bash
   uv run cao-server
   ```

2. Start the `cao-control-panel` interface layer (from the repo root):

   ```bash
   uv run cao-control-panel
   ```

3. Install frontend dependencies:

   ```bash
   cd frontend
   npm install
   ```

4. Run the development server:

   ```bash
   npm run dev
   ```

5. Open [http://localhost:3000](http://localhost:3000) in your browser.

## Configuration

| Environment variable     | Default                 | Description                       |
| ------------------------ | ----------------------- | --------------------------------- |
| `CAO_CONTROL_PANEL_URL`  | `http://localhost:8000` | URL of the cao-control-panel      |

Set `CAO_CONTROL_PANEL_URL` to override the default control panel address:

```bash
CAO_CONTROL_PANEL_URL=http://my-control-panel:8000 npm run dev
```

## Scripts

| Command         | Description                  |
| --------------- | ---------------------------- |
| `npm run dev`   | Start development server     |
| `npm run build` | Build for production         |
| `npm run start` | Start production server      |
| `npm run lint`  | Run ESLint                   |


## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
