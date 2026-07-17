# Logging

`utils/logging.py` configura arquivo por inicialização em
`~/.aws/cli-agent-orchestrator/logs/cao_YYYY-MM-DD_HH-MM-SS.log`. O nível padrão
é INFO; use `CAO_LOG_LEVEL=DEBUG`. Uvicorn também escreve startup, acesso e
shutdown no terminal.

Saída bruta de cada terminal passa pelo `LogWriter` e fica em
`~/.aws/cli-agent-orchestrator/logs/terminal/<terminal_id>.log`. Na remoção,
scrollback e snapshot JSON preservam terminal, sessão, provider, perfil e
diretório. Pytest captura logs por padrão; `-s` libera console.

Correlação recomendada:

1. obtenha `terminal_id` por `cao session status`;
2. filtre o log do servidor por esse ID;
3. abra o log/snapshot do terminal;
4. correlacione `tmux_session`, `tmux_window`, provider e timestamps;
5. para eventos, siga `terminal.<id>.output` e `terminal.<id>.status`.

```bash
CAO_LOG_LEVEL=DEBUG uv run cao-server
rg -n 'TERMINAL_ID|StatusMonitor|fifo|provider' \
  ~/.aws/cli-agent-orchestrator/logs/cao_*.log
```

Não publique logs sem revisão: prompts e saídas de agentes podem conter código
ou dados sensíveis, embora tokens não sejam intencionalmente logados.

