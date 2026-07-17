# Instalação realizada

## Estado final

- `.venv` recriado com Python 3.12.3.
- dependências Python sincronizadas com `uv sync --all-extras`;
- extras OpenTelemetry instalados para a suíte de desenvolvimento;
- dependências web instaladas por `npm ci --include=dev`;
- frontend compilado para `src/cli_agent_orchestrator/web_ui`;
- nenhum pacote de sistema, credencial ou configuração global foi removido.

Comandos efetivamente usados:

```bash
cd /home/fred/Projetos/cli-agent-orchestrator
uv venv --python /usr/bin/python3.12
uv sync --all-extras
cd web
npm ci --include=dev
npm run build
```

O cache foi redirecionado para `/tmp/cao-uv-cache` e
`/tmp/cao-npm-cache` durante a automação contida. O `package-lock.json` indica
npm como gerenciador canônico do frontend; pnpm existe no host, mas não foi
usado para evitar gerar um segundo lockfile.

O build Vite passou, com um aviso não bloqueante: chunk principal de cerca de
755 kB após minificação. Os 81 testes web também passaram; mensagens de
`canvas/xterm` e do ErrorBoundary em stderr eram exercitadas pelos próprios
testes.

Para repetir com segurança, use `scripts/bootstrap-dev.sh`. Ele não executa
`sudo`, não apaga ambientes e informa dependências ausentes.

