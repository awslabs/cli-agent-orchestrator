# FastAPI Interface Layer Architecture

This document describes the new three-tier architecture with the FastAPI control panel interface layer.

## Architecture Overview

```
┌─────────────────┐
│  Browser (User) │
└────────┬────────┘
         │ HTTP /api/cao/*
         ▼
┌─────────────────────────────┐
│  Next.js Frontend           │
│  Port 3000                  │
│  - React UI                 │
│  - API Routes (proxy)       │
└────────┬────────────────────┘
         │ HTTP *
         │ (proxied requests)
         ▼
┌─────────────────────────────┐
│  CAO Control Panel          │  ← NEW LAYER
│  Port 8000                  │
│  - FastAPI interface        │
│  - Request proxy            │
│  - CORS handling            │
└────────┬────────────────────┘
         │ HTTP *
         │ (forwarded to backend)
         ▼
┌─────────────────────────────┐
│  CAO Server                 │
│  Port 9889                  │
│  - FastAPI backend          │
│  - Terminal services        │
│  - Session management       │
│  - Database operations      │
└─────────────────────────────┘
```

## Component Responsibilities

### 1. Next.js Frontend (Port 3000)
- **Purpose**: User interface and initial request handling
- **Location**: `frontend/`
- **Technology**: Next.js 16, React 19, Tailwind CSS
- **Responsibilities**:
  - Render UI components
  - Handle user interactions
  - Proxy browser requests to control panel via `/api/cao/[...path]` routes
- **Configuration**: `CAO_SERVER_URL` env var (default: `http://localhost:8000`)

### 2. CAO Control Panel (Port 8000) ← NEW
- **Purpose**: Interface layer between frontend and backend
- **Location**: `src/cli_agent_orchestrator/control_panel/`
- **Technology**: FastAPI, Requests library
- **Responsibilities**:
  - Accept requests from Next.js frontend
  - Proxy all requests to CAO server
  - Handle CORS for frontend communication
  - Provide health check with backend status
  - (Future) Authentication, rate limiting, logging
- **Entry point**: `cao-control-panel` command
- **Configuration**:
  - `CONTROL_PANEL_HOST`: localhost
  - `CONTROL_PANEL_PORT`: 8000
  - `CAO_SERVER_URL`: http://localhost:9889

### 3. CAO Server (Port 9889)
- **Purpose**: Core backend services
- **Location**: `src/cli_agent_orchestrator/api/`
- **Technology**: FastAPI, SQLAlchemy, tmux integration
- **Responsibilities**:
  - Terminal lifecycle management
  - Session operations
  - Inbox messaging
  - Flow execution
  - Database persistence
- **Entry point**: `cao-server` command

## Request Flow Example

### Creating a New Session

1. **User Action**: Click "Create Session" in browser UI
2. **Frontend**: POST `/api/cao/sessions` with JSON body
3. **Next.js API Route**: Proxy to `http://localhost:8000/sessions`
4. **Control Panel**: Forward to `http://localhost:9889/sessions`
5. **CAO Server**:
   - Validate request
   - Create tmux session
   - Initialize provider
   - Save to database
   - Return terminal object
6. **Response**: Flows back through control panel → Next.js → browser

## Benefits of This Architecture

### Separation of Concerns
- Frontend focuses on UI/UX
- Control panel handles interface/proxy logic
- CAO server focuses on core business logic

### Independent Deployment
- Each service can be deployed separately
- Different scaling strategies per tier
- Version updates don't affect other tiers

### Security & Monitoring (Future)
- Control panel can add authentication layer
- Centralized logging and metrics
- Rate limiting and request validation
- API versioning without touching core server

### Development Flexibility
- Frontend can be developed/tested independently
- Control panel can add features without modifying CAO server
- CAO server remains focused on terminal orchestration

## Running the Stack

### Development Mode

1. Start CAO Server:
```bash
uv run cao-server
```

2. Start Control Panel:
```bash
uv run cao-control-panel
```

3. Start Frontend:
```bash
cd frontend
npm run dev
```

4. Access UI: http://localhost:3000

### Production Considerations

- Use environment variables to configure URLs
- Deploy each service independently
- Consider using a reverse proxy (nginx) in front
- Add authentication at control panel layer
- Implement monitoring and logging
- Use process managers (systemd, pm2) for services

## Testing

### Unit Tests
```bash
# Control panel tests
pytest test/control_panel/ -v

# CAO server tests
pytest test/api/ -v
```

### Integration Testing
The control panel acts as a transparent proxy, so existing API tests should work through it:

```bash
# Set environment to point to control panel
CAO_SERVER_URL=http://localhost:8000 pytest test/api/ -v
```

## Configuration

### Environment Variables

| Service | Variable | Default | Purpose |
|---------|----------|---------|---------|
| Frontend | `CAO_SERVER_URL` | `http://localhost:8000` | Control panel URL |
| Control Panel | `CONTROL_PANEL_HOST` | `localhost` | Bind address |
| Control Panel | `CONTROL_PANEL_PORT` | `8000` | Listen port |
| Control Panel | `CAO_SERVER_URL` | From constants | Backend URL |
| CAO Server | `SERVER_HOST` | `localhost` | Bind address |
| CAO Server | `SERVER_PORT` | `9889` | Listen port |

## Migration Notes

### What Changed

1. **New Service**: Control panel added at port 8000
2. **Frontend Update**: Default backend URL changed from 9889 → 8000
3. **New Command**: `cao-control-panel` entry point added
4. **Architecture**: Two-tier → three-tier

### What Stayed the Same

- CAO server API remains unchanged
- All existing endpoints work identically
- Database schema unchanged
- Terminal providers unchanged
- MCP server unchanged

### Backward Compatibility

To use the old architecture (frontend → CAO server directly):

```bash
# In frontend directory
CAO_SERVER_URL=http://localhost:9889 npm run dev
```

This bypasses the control panel entirely.

## Future Enhancements

### Control Panel Extensions
- **Authentication**: JWT tokens, OAuth integration
- **Rate Limiting**: Per-user/IP rate limits
- **Request Validation**: Additional schema validation
- **Response Transformation**: Format conversion, filtering
- **Caching**: Response caching for read operations
- **Metrics**: Request tracking, performance monitoring
- **Logging**: Structured logging, audit trails
- **API Versioning**: Support multiple API versions

### Multi-Tenant Support
- User isolation
- Resource quotas
- Per-tenant configuration

## Troubleshooting

### Control Panel Won't Start
- Check port 8000 is available: `lsof -i :8000`
- Verify CAO server is running on port 9889
- Check logs for errors

### Frontend Can't Connect
- Verify control panel is running: `curl http://localhost:8000/health`
- Check `CAO_SERVER_URL` in frontend environment
- Verify CORS configuration in control panel

### Requests Not Reaching CAO Server
- Check control panel health endpoint shows `cao_server_status: healthy`
- Verify CAO server is running: `curl http://localhost:9889/health`
- Check control panel logs for proxy errors

## Files Modified/Added

### New Files
- `src/cli_agent_orchestrator/control_panel/__init__.py`
- `src/cli_agent_orchestrator/control_panel/main.py`
- `src/cli_agent_orchestrator/control_panel/README.md`
- `test/control_panel/__init__.py`
- `test/control_panel/test_control_panel.py`
- `ARCHITECTURE.md` (this file)

### Modified Files
- `pyproject.toml` - Added `cao-control-panel` entry point
- `frontend/src/app/api/cao/[...path]/route.ts` - Changed default port to 8000
- `frontend/README.md` - Updated architecture documentation
