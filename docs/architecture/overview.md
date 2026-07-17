# Arquitetura atual

CAO 2.3.0 é um servidor FastAPI local que coordena CLIs interativos dentro de
um backend de terminal. A CLI Click, o frontend React e os servidores MCP são
planos de controle sobre a mesma API/serviços.

```text
CLI / React / MCP / AG-UI
          |
       FastAPI
          |
  services + plugins + workflows
     |                 |
 providers          SQLite
     |
 terminal backend (tmux padrão; herdr experimental)
     |
 Claude / Codex / OpenCode / Kimi / outros CLIs
```

Componentes:

- `api/main.py`: lifespan, REST, WebSocket PTY, SSE/AG-UI e UI estática;
- `cli/`: comandos `cao`, cliente HTTP e instalação/configuração;
- `mcp_server/` e `ops_mcp_server/`: assign, handoff, mensagens, memória e
  operação por agentes;
- `services/`: regras de sessão/terminal, inbox, eventos, workflows, memória,
  limpeza e settings;
- `providers/`: comando, inicialização, estado e extração de resposta de cada
  CLI;
- `backends/` + `clients/tmux.py`: abstração de terminal e integração externa;
- `clients/database.py`: SQLite/SQLAlchemy e migrações incrementais;
- `web/`: React/Vite; o build é incorporado em `web_ui/`;
- `plugins/`: hooks por entry points; memória, event log e MCP Apps;
- `graph/`, `ext_apps/`, `telemetry/`: conhecimento, UI MCP e OTel opcionais.

O estado é híbrido: metadados duráveis no SQLite, processos/janelas no tmux,
providers e status em memória, saída em FIFO/log. Após crash, o banco pode
conter referências sem processo; o provider é reconstruído sob demanda, mas
não existe retomada completa e transacional do processo lógico.

O caminho de saída é event-driven:
`tmux pipe-pane → FIFO reader thread → EventBus → StatusMonitor/LogWriter →
status event → InboxService`. Chamadas bloqueantes do detector são enviadas por
`asyncio.to_thread`.

Pontos de extensão: `ProviderType` + `ProviderManager`, `BaseProvider`,
`TerminalBackend`, entry points `cao.plugins`, profiles/skills e o motor de
workflows. Acoplamentos principais: nome/janela tmux como identidade operacional,
regex/TUI por provider e singletons globais de serviços.

