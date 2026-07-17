# Providers e CLIs

Verificação em 17/07/2026. “Autenticado” foi validado por comando de status ou
uso funcional, sem registrar e-mails, organizações, tokens ou valores de
variáveis.

| Provider | Comando/versão | Caminho | Autenticação | Suporte CAO | Estado |
|---|---|---|---|---|---|
| Claude Code | `claude` 2.1.212 | `/home/fred/.local/bin/claude` | assinatura claude.ai, autenticado | `claude_code.py` | detectado; lançamento CAO falha no teste funcional |
| Codex CLI | `codex-cli` 0.144.5 | NVM | ChatGPT, autenticado | `codex.py` | detectado |
| OpenCode | 1.14.46 | `/home/fred/.opencode/bin/opencode` | providers locais configurados; funcional | `opencode_cli.py` | obrigatório, testado em duas rodadas |
| Gemini CLI | ausente | — | não verificada | sem provider dedicado | não instalado |
| Kimi CLI | 1.43.0 | `/home/fred/.local/bin/kimi` | sem status não interativo seguro | `kimi_cli.py` | detectado; runtime próprio usa Python 3.13.13 |
| GitHub CLI | 2.45.0 | `/usr/bin/gh` | autenticado | não é provider | detectado |
| GitHub Copilot CLI | ausente | — | não verificada | `copilot_cli.py` | provider implementado, binário ausente |
| Cursor Agent | 3.9.16 | `/home/fred/.local/bin/agent` | autenticado | `cursor_cli.py` | detectado; `cursor` é o editor, `agent` é o CLI |
| Kiro CLI | ausente | — | não verificada | `kiro_cli.py` | provider implementado, binário ausente |
| Hermes | ausente | — | não verificada | `hermes.py` | provider implementado, binário ausente |
| Antigravity | ausente | — | não verificada | `antigravity_cli.py` | provider implementado, binário ausente |

O endpoint `GET /agents/providers` reconheceu Claude, Codex, Kimi, OpenCode e
Cursor no PATH do servidor. Ausência de provider opcional não bloqueia o
bootstrap.

Formas de autenticação preservadas: Claude e Codex reutilizam assinaturas dos
CLIs; OpenCode reutiliza seu armazenamento/configuração de providers; Cursor
reutiliza o login do Agent CLI. CAO não deve copiar esses dados para o projeto.

