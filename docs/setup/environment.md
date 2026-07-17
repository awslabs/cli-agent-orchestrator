# Ambiente validado

Levantamento em 17/07/2026, usuário `fred`, diretório
`/home/fred/Projetos/cli-agent-orchestrator`.

O host é Linux Mint 22.2 (base Ubuntu), kernel `6.17.0-40-generic`. O projeto
foi isolado em seu próprio diretório; o sandbox funcional fica em
`.cao-sandbox/`, nunca na raiz `/home/fred/Projetos`.

| Ferramenta | Versão | Executável |
|---|---:|---|
| Git | 2.43.0 | `/usr/bin/git` |
| tmux | 3.4 | `/usr/bin/tmux` |
| Python do sistema | 3.12.3 | `/usr/bin/python3` |
| Python do `.venv` | 3.12.3 | `.venv/bin/python` |
| uv | 0.11.29 | `/home/fred/.local/bin/uv` |
| Node.js | v24.11.1 | NVM, `/home/fred/.nvm/versions/node/v24.11.1/bin/node` |
| npm | 11.6.2 | NVM, `/home/fred/.nvm/versions/node/v24.11.1/bin/npm` |
| pnpm | 10.24.0 | NVM, `/home/fred/.nvm/versions/node/v24.11.1/bin/pnpm` |

`git`, `tmux`, `curl` e `build-essential` já estavam instalados. O limite
soft/hard de descritores é `1048576`; ele não foi alterado.

Comandos de verificação:

```bash
git --version; command -v git
tmux -V; command -v tmux
python3 --version; command -v python3
uv --version; command -v uv
node --version; command -v node
npm --version; command -v npm
pnpm --version; command -v pnpm
uv run python --version
ulimit -Sn; ulimit -Hn
```

Observação: o `.venv` anterior apontava para Python 3.13.13. Somente esse
ambiente local foi removido e recriado; nenhuma versão global foi removida.
