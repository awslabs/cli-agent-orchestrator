# Troubleshooting e limpeza segura

## Diagnóstico rápido

```bash
curl -fsS http://127.0.0.1:9889/health
tmux ls
ps -ef | grep -E 'pytest|cao|tmux|opencode|codex|claude' | grep -v grep
ls -la /tmp/pytest-of-fred
uv run cao session list
```

Nunca execute `tmux kill-server` sem confirmar que todas as sessões pertencem
ao CAO. Sessões do projeto têm prefixo `cao-`; testes funcionais desta análise
usaram `cao-prd-*` e fixtures usam `cao-caotest-*`.

## Encerrar

```bash
uv run cao shutdown --session cao-NOME
uv run cao shutdown --all  # somente quando todas as sessões CAO podem sair
```

Prefira a API/CLI, pois ela para `pipe-pane`, reader FIFO, provider e registro
SQLite. Se o servidor foi iniciado em primeiro plano, use Ctrl-C e aguarde o
shutdown do lifespan.

## Resíduos

Antes de matar uma sessão órfã, confirme seu nome e janelas:

```bash
tmux list-sessions -F '#S'
tmux list-windows -t cao-NOME
tmux kill-session -t cao-NOME
```

Temporários pytest podem ser removidos somente depois de garantir que não há
pytest ativo. Não remova `~/.aws/cli-agent-orchestrator` inteiro: contém banco,
perfis, memória e logs. Snapshots ficam em `~/.cao/logs/terminal` conforme a
documentação legada; logs ativos desta versão ficam sob
`~/.aws/cli-agent-orchestrator/logs`.

## Banco

Faça backup antes de resetar:

```bash
cp ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db \
  ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db.backup
```

Não há comando seguro de reset total. Para testes, prefira redirecionar o
ambiente do subprocesso para um diretório temporário. O reset manual do banco
deve ocorrer apenas com servidor e sessões encerrados.

## Sintomas conhecidos

- `Too many open files`: medir `/proc/$PID/fd`; não elevar `ulimit` como primeira
  resposta. Nesta máquina não houve crescimento.
- `Terminal ... not found in database`: evento FIFO atrasado após rollback;
  veja a investigação específica.
- OpenCode preso em `processing`: aguarde quiescência e inspecione
  `tmux capture-pane`; o TUI alt-screen pode atrasar a detecção.
- `Could not acquire lock` do uv em ambiente contido: use
  `UV_CACHE_DIR=/tmp/cao-uv-cache`.

