# OpenCode no CAO

## Resultado

OpenCode 1.14.46 está instalado, autenticado e é provider de primeira classe no
código, embora a documentação upstream do CAO ainda o classifique como
experimental. O teste funcional criou o terminal `c24c752e`, implementou uma
função, executou 3 testes e recebeu uma segunda correção na mesma sessão; a
revisão terminou com 4 testes aprovados.

## Integração

- `providers/opencode_cli.py` inicia o TUI com `opencode --agent <perfil>`;
- define `OPENCODE_CONFIG` e `OPENCODE_CONFIG_DIR` para
  `~/.aws/opencode`, isolado da configuração pessoal;
- `cao install --provider opencode_cli` grava o agent Markdown em
  `~/.aws/opencode/agents` e atualiza `opencode.json`;
- permissões são frontmatter nativo (`allow`/`deny`), não prompt textual;
- skills são descobertas por link para o catálogo CAO;
- estado é inferido do TUI: `ctrl+p commands`, `esc interrupt`, marcador `▣` e
  diálogo de permissão;
- envio de nova mensagem usa o mesmo terminal/tmux, preservando contexto;
- encerramento ocorre pela remoção do terminal/sessão CAO.

OpenCode usa TUI, não SDK. A saída vem de `tmux pipe-pane` → FIFO → event bus →
`StatusMonitor`. O modo alt-screen limita scrollback e torna a extração/status
sensível à versão visual do TUI.

## Compatibilidade 1.14

O smoke test herdava plugins/agents globais e falhava na primeira execução. Em
configuração nova, OpenCode 1.14 executa uma migração SQLite e só expõe agentes
customizados na chamada seguinte. O teste agora:

1. cria o diretório pai do config;
2. isola `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` e
   `XDG_STATE_HOME`;
3. desativa importação de prompts/skills Claude;
4. executa `opencode agent list --pure` uma vez antes do smoke real.

Limitações observadas: conclusão visual pode permanecer temporariamente como
`processing`; `GET output?mode=last` chegou a retornar 404 antes de o marcador
estabilizar; instalação paralela de perfis pode disputar `opencode.json`; `--yolo`
não amplia permissões do TUI em runtime.

