# Fluxo de execução

```text
Usuário
  → Click CLI, React ou MCP
  → FastAPI (api/main.py)
  → session_service / terminal_service
  → backend factory (tmux por padrão)
  → registro SQLite
  → FIFO + pipe-pane
  → ProviderManager + provider.initialize()
  → CLI do agente no pane
  → saída FIFO/EventBus
  → StatusMonitor + LogWriter
  → status persistido apenas em memória/evento
  → InboxService/API/UI
```

Passo a passo de uma delegação:

1. supervisor chama `assign`/`handoff` em `mcp_server/server.py`;
2. MCP chama endpoints de sessão/terminal, propagando `CAO_TERMINAL_ID` como
   `caller_id`;
3. `terminal_service.create_terminal` gera ID/nome, cria janela e linha SQLite;
4. reader FIFO inicia antes do `pipe-pane` para não perder o prompt;
5. provider carrega profile, modelo, skills/MCP e inicia o CLI;
6. `send_input` arma o monitor, limpa buffer antigo e envia paste/keys;
7. saída é coalescida pelo FIFO reader e publicada;
8. detector do provider deriva UNKNOWN/IDLE/PROCESSING/COMPLETED/WAITING/ERROR;
9. Inbox entrega callbacks quando o destinatário fica pronto;
10. `handoff` espera, extrai última resposta e remove worker; `assign` retorna
    cedo e mantém o terminal até callback/cleanup.

O frontend usa REST para estado e WebSocket para attach PTY. MCP Apps/AG-UI
projetam eventos para clientes compatíveis. Workflows reutilizam
`run_agent_step`, journal SQLite e script runner; não substituem o ciclo de
terminal.

Falhas importantes: API e DB não formam transação com tmux; eventos podem
chegar depois de rollback; extração/status dependem do viewport; IDs lógicos e
recursos físicos ainda não têm geração/tombstone comum.

