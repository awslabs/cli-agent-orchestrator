# Web E2E Playwright Tests with API Mocking

## Problem

The web/e2e Playwright tests currently require the full backend API server to be running on port 9889. This makes them difficult to run in CI without setting up the entire backend infrastructure (Python, database, dependencies, etc.).

## Proposed Solution

Add API mocking to the web/e2e Playwright tests using Playwright's `page.route()` API to intercept backend API calls and return mock responses. This would allow the tests to run without the backend server.

### Implementation Plan

1. **Add API mocking to devin-provider.spec.ts**
   - Mock `/agents/providers` endpoint to return devin_cli in the provider list
   - Mock `/agents/profiles` endpoint to return analysis_supervisor profile
   - Mock `/health` endpoint to return {status: "ok"}
   - Mock `/sessions` and `/terminals` endpoints for spawn agent tests

2. **Configure webServer in playwright.config.ts**
   - Add webServer configuration to start the Vite dev server
   - Set baseURL to the Vite dev server port (5173)
   - Configure timeout and reuseExistingServer options

3. **Add web-e2e job to CI workflow**
   - Install dependencies
   - Install Playwright browsers
   - Run tests (Playwright will start the dev server via webServer config)

### Example Configuration

```typescript
// playwright.config.ts
webServer: {
  command: 'npm run dev',
  url: 'http://localhost:5173',
  timeout: 120 * 1000,
  reuseExistingServer: !process.env.CI,
}
```

```typescript
// devin-provider.spec.ts
test.beforeEach(async ({ page }) => {
  await page.route('**/agents/providers', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { name: 'devin_cli', binary: 'devin', description: 'Devin CLI provider' },
      ]),
    });
  });
  // ... other mocks
});
```

### Benefits

- Web/e2e tests can run in CI without backend infrastructure
- Faster test execution (no backend startup time)
- Tests become pure UI tests, decoupled from backend implementation
- Easier to maintain and debug

### Trade-offs

- Tests no longer verify real backend integration
- Need to keep mock responses in sync with actual API contracts
- UI tests won't catch backend API changes

### Alternatives Considered

1. **Start backend server in CI**: Requires significant infrastructure setup (Python, database, dependencies)
2. **Use existing cao-mcp-apps-e2e**: Has test harness server, but tests MCP apps not web UI
3. **Keep web/e2e out of CI**: Current state, loses web UI test coverage

## Out of Scope

- Setting up the full backend API server in CI
- Mocking complex API interactions (websocket connections, streaming responses)
- Integration tests that verify backend behavior

## Related

- cao-mcp-apps-e2e already uses webServer pattern successfully
- Playwright documentation: https://playwright.dev/docs/mock
