# Arquitetura de providers

`ProviderType` enumera `kiro_cli`, `claude_code`, `codex`, `kimi_cli`,
`copilot_cli`, `opencode_cli`, `hermes`, `cursor_cli`, `antigravity_cli` e
`mock_cli`. `ProviderManager.create_provider` é um `if/elif` central que
instancia o adaptador e indexa por `terminal_id`; `get_provider` reconstrói do
SQLite quando o singleton não possui a instância.

Contrato comum (`BaseProvider`): `initialize`, construção/envio de comando,
`get_status`, extração da última resposta, cleanup, restrições/skills e hooks de
input. O terminal service cria DB/FIFO antes de `initialize`, envia mensagens
ao mesmo pane e encerra provider durante teardown.

| Provider | Execução e sessão | Estado/saída | Restrições e contexto |
|---|---|---|---|
| Claude Code | `claude`, prompt/profile/MCP por arquivos temporários | prompt/spinners/`⏺` | nativo `--disallowedTools`/permission mode; MCP recebe terminal ID |
| Codex | `codex`, instructions e MCP | prompt `›`, bullets e marcadores | restrição é parcialmente prompt-level; sessão é o TUI vivo |
| OpenCode | `opencode --agent`, config isolado | footer `ctrl+p`, `esc`, `▣` | permission frontmatter; skills nativas; TUI alt-screen |
| Kimi | `kimi`, profile/skills | padrões do TUI e fallback | soft/prompt-level; runtime próprio |
| Copilot | CLI + agent instalado | prompt e padrões específicos | agent/MCP instalados, enforcement nativo |
| Cursor | `agent` | padrões do Agent CLI | skill prompt/model; terminal vivo |
| Kiro | `kiro-cli chat --agent` e fallback legacy | prompt/TUI/shell baseline | agent nativo e tool policy |
| Hermes | CLI configurável | regex configuráveis por env | prompt/skills |
| Antigravity | `agy` | prompts/status específicos | soft/prompt-level |

Retomada significa continuar no mesmo processo tmux; não há serialização de
conversa neutra entre providers. Se o servidor reinicia mas o pane permanece,
metadata permite reconstruir o adaptador e shell baseline; se o CLI reinicia,
contexto depende do mecanismo próprio do CLI.

Adicionar provider exige enum, classe, branch manager, detecção no endpoint,
instalação/profile mapping, documentação e testes de comandos/status/TUI. O
melhor desenho futuro é substituir o `if/elif` por registry de descriptors com
capabilities explícitas (headless, resume, hard permissions, MCP, structured
output), evitando inferência por nome.

