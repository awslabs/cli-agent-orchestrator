# Adding a New `ui://cao/<name>` View

Step-by-step procedure for extending CAO's MCP Apps surface with a new view resource.

## Prerequisites

- Node.js 20+ installed (build-time only)
- `CAO_MCP_APPS_ENABLED=true` set in your environment
- Familiarity with the existing views in `cao_mcp_apps/src/`

## File Structure

A new view named `my-view` requires additions in two directories:

```
src/cli_agent_orchestrator/ext_apps/
└── apps.py                          # Register the ui://cao/my-view resource

cao_mcp_apps/
├── src/
│   └── my-view/
│       ├── my-view.html             # Entry point HTML
│       ├── my-view.tsx              # React root component
│       └── components/              # View-specific components (optional)
├── vite.config.my-view.ts           # Per-view Vite config (or extend shared factory)
└── apps_static/
    └── my-view.html                 # Built single-file artifact (output)
```

## Step-by-Step Procedure

### 1. Register the resource URI in `ext_apps/apps.py`

Add a constant for the new URI and map it to an artifact filename:

```python
MY_VIEW_RESOURCE_URI = "ui://cao/my-view"

# Add to _RESOURCE_FILES dict:
_RESOURCE_FILES = {
    DASHBOARD_RESOURCE_URI: "dashboard.html",
    AGENT_RESOURCE_URI: "agent.html",
    EVENT_STREAM_RESOURCE_URI: "event-stream.html",
    MY_VIEW_RESOURCE_URI: "my-view.html",       # ← new
}

# Add to PREFERRED_FRAMES dict:
PREFERRED_FRAMES = {
    ...
    MY_VIEW_RESOURCE_URI: {"width": 800, "height": 600},  # ← new
}
```

### 2. Create the view entry point

Create `cao_mcp_apps/src/my-view/my-view.html` with a mount point:

```html
<!doctype html>
<html lang="en">
<head><meta charset="UTF-8"><title>CAO My View</title></head>
<body><div id="root"></div><script type="module" src="./my-view.tsx"></script></body>
</html>
```

Create `cao_mcp_apps/src/my-view/my-view.tsx` using the shared `McpApp` bridge:

```tsx
import { createRoot } from "react-dom/client";
import { McpApp } from "../shared/mcpApp";

function MyView() {
  // Use McpApp.callTool("render_dashboard", {}) etc.
  return <div>...</div>;
}

createRoot(document.getElementById("root")!).render(<MyView />);
```

### 3. Add a build config

Either add to `vite.config.ts`'s shared factory or create a per-view config. Add a `package.json` script:

```json
{
  "scripts": {
    "build:my-view": "vite build --config vite.config.my-view.ts"
  }
}
```

Ensure `build:all` includes the new view.

### 4. Tag the rendering tool with `ui_meta(...)`

In `mcp_server/app_tools.py`, when registering the tool that renders this view's data, pass the resource URI:

```python
ok &= _register(
    render_my_view, "render_my_view", ["model", "app"], MY_VIEW_RESOURCE_URI, None
)
```

The `ui_meta(...)` call adds `_meta.ui.resourceUri`, `preferredFrameSize`, `prefersBorder`, `csp`, and `domain` automatically.

### 5. Build and verify

```bash
cd cao_mcp_apps
npm run build:all          # Build all views including the new one
npm run scan:jit           # Ensure no eval/new Function (JIT-free)
npm run check:size         # Verify bundle-size budget
npm run coverage:ratchet   # Check coverage hasn't dropped
```

### 6. Test the HTTP-only boundary

Run the AST guard to confirm the new code doesn't import `clients.*`:

```bash
uv run pytest test/test_http_only_boundary.py -v
```

## Guardrails Checklist

Before merging a new view, verify:

- [ ] **Default-off**: view only registers when `CAO_MCP_APPS_ENABLED=true`
- [ ] **JIT-free**: no `eval` or `new Function` in the bundle (`npm run scan:jit`)
- [ ] **HTTP-only boundary**: `mcp_server/*` reaches state only via HTTP (`test_http_only_boundary.py`)
- [ ] **Bundle-size budget**: gzipped size within limits (`npm run check:size`)
- [ ] **Coverage ratchet**: test coverage hasn't dropped (`npm run coverage:ratchet`)
- [ ] **Single-file output**: `vite-plugin-singlefile` produces one HTML artifact
- [ ] **CSP-safe**: the structured `_meta.ui.csp` declares only loopback `connectDomains`

## Common Issues

- **View not appearing in host**: Ensure the resource is registered in `_RESOURCE_FILES` and the built artifact exists in `apps_static/`.
- **CSP violation at runtime**: Check that no external domains are used; only `http://127.0.0.1:9889` and `http://localhost:9889` are allowed in `connectDomains`.
- **Build fails with JIT token**: Look for third-party libraries that use `eval` internally; replace with JIT-free alternatives.
