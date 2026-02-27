# CAO Control Panel

FastAPI interface layer that acts as a middleware between the frontend control panel and the CAO server.

## Architecture

```
Frontend (Next.js) → Control Panel (FastAPI) → CAO Server (FastAPI)
   port 3000            port 8000                port 9889
```

The control panel serves as an independent service that:
- Receives requests from the frontend
- Proxies them to the CAO server backend
- Returns responses back to the frontend

This three-tier architecture provides:
- **Decoupling**: Frontend and backend can evolve independently
- **Security**: Additional layer for authentication/authorization (future)
- **Monitoring**: Central point for logging and metrics (future)
- **Flexibility**: Easy to add business logic without touching core CAO server

## Running the Control Panel

Start the control panel server:

```bash
cao-control-panel
```

Or with development tools:

```bash
uv run cao-control-panel
```

The server will start on `http://localhost:8000` by default.

## Configuration

The control panel reads configuration from environment variables:

- `CONTROL_PANEL_HOST`: Host to bind to (default: `localhost`)
- `CONTROL_PANEL_PORT`: Port to listen on (default: `8000`)
- `CAO_SERVER_URL`: URL of the CAO server to proxy to (default: `http://localhost:9889`)

## API Endpoints

The control panel proxies all requests to the CAO server:

- `GET /health` - Health check (includes CAO server status)
- `GET|POST|PUT|PATCH|DELETE /{path:path}` - Proxy all requests to CAO server

## Testing

Run the control panel tests:

```bash
pytest test/control_panel/ -v
```

## Development

The control panel is a lightweight FastAPI application that uses the `requests` library to communicate with the CAO server. All endpoints (except `/health`) are automatically proxied through the catch-all route handler.
