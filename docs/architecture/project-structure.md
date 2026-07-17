# Estrutura do projeto

```text
src/cli_agent_orchestrator/
├── api/              FastAPI, lifespan, REST/WS/SSE
├── backends/         contrato de terminal, tmux e herdr
├── cli/commands/     comandos Click e cliente da API
├── clients/          SQLite e tmux
├── ext_apps/         MCP Apps e widget
├── graph/            projeções e exportadores de conhecimento
├── mcp_server/       ferramentas para supervisores/workers
├── ops_mcp_server/   ferramentas operacionais
├── models/           Pydantic/enums e modelos de domínio
├── plugins/          hooks e plugins built-in
├── providers/        adaptadores de CLIs
├── security/         JWT/JWKS e scopes
├── services/         casos de uso e infraestrutura em processo
├── skills/           skills distribuídas com o pacote
├── telemetry/        OpenTelemetry opcional
├── utils/            profiles, paths, prompts, skills e logging
└── web_ui/           artefato compilado do frontend
```

Outros diretórios:

| Caminho | Responsabilidade | Extensão/risco |
|---|---|---|
| `web/` | React, Vite, Zustand/xterm | bundle grande e contrato com API |
| `test/` | unitários, fixtures reais, integration/e2e | alguns testes tocam tmux/CLIs externos |
| `cao_mcp_apps/` | bundles de MCP Apps | build Node separado |
| `docs/` | documentação upstream e desta análise | parte antiga pode divergir da implementação |
| `scripts/` | release, segurança e bootstrap | scripts devem permanecer idempotentes |
| `examples/` | profiles, flows e workflows | exemplos também são validados por testes |
| `.cao-sandbox/` | repositório descartável do funcional | não usar para dados reais |

Arquivos centrais: `pyproject.toml`, `constants.py`, `api/main.py`,
`services/terminal_service.py`, `services/status_monitor.py`,
`clients/tmux.py`, `clients/database.py`, `providers/manager.py` e
`models/provider.py`.

Riscos de mudança: `api/main.py` e `memory_service.py` são grandes; constantes
são avaliadas no import; serviços e provider manager são singletons; muitos
adaptadores dependem de marcadores visuais externos. Prefira novas interfaces e
testes de contrato em vez de refatoração transversal inicial.

