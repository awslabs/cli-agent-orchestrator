# Configuração local

Precedência principal: flag CLI > variável `CAO_*` >
`~/.aws/cli-agent-orchestrator/settings.json` > padrão. `ConfigService` cobre
agents, skills, server, memory, terminal e apps. Network/auth ainda são lidos
diretamente de variáveis.

Arquivos relevantes:

| Arquivo | Uso |
|---|---|
| `pyproject.toml` / `uv.lock` | Python, scripts, extras e testes |
| `web/package.json` / `package-lock.json` | frontend Vite/React |
| `web/vite.config.ts` | build e proxy de desenvolvimento |
| `~/.aws/cli-agent-orchestrator/settings.json` | configuração persistente |
| `~/.aws/cli-agent-orchestrator/.env` | valores gerenciados por `cao env`; pode conter segredos |
| `~/.aws/opencode/opencode.json` | MCP/permissões OpenCode gerenciados pelo CAO |
| perfis Markdown | provider, role, prompt, model, MCP e allowedTools |

Principais variáveis: `CAO_API_HOST` (127.0.0.1), `CAO_API_PORT` (9889),
`CAO_LOG_LEVEL`, `CAO_TERMINAL_BACKEND`, `CAO_HERDR_SESSION`,
`CAO_MCP_APPS_ENABLED`, `CAO_AGUI_ENABLED`, `CAO_MEMORY_ENABLED`,
`CAO_MEMORY_COMPILE_MODE`, `CAO_PROVIDER_INIT_TIMEOUT`,
`CAO_EVENT_BUS_MAX_QUEUE_SIZE`, `CAO_PYTE_STATUS`, `CAO_CORS_ORIGINS`,
`CAO_ALLOWED_HOSTS`, `CAO_WS_ALLOWED_CLIENTS`, `CAO_FORWARDED_ALLOW_IPS`,
`CAO_AUTH_JWKS_URI`, `CAO_AUTH_AUDIENCE`, `CAO_AUTH_ISSUER` e variáveis OTEL.

O banco é fixo sob o CAO home; tmux é backend padrão. `cao config list` mostra
valores resolvidos e `cao config path` mostra o JSON. Nunca inclua valores do
`.env` em relatórios. O `.env.example` da raiz contém apenas exemplos vazios e
seguros; ele não substitui o `.env` gerenciado no home.

