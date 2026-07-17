# Backlog técnico

## P0 — Lifecycle supervisor e eventos geracionais

- **Descrição:** state machine ACTIVE/STOPPING/STOPPED, generation em eventos,
  cancelamento/drenagem e tombstones.
- **Motivação:** eliminar `Terminal not found` e teardown não determinístico.
- **Prioridade:** P0. **Impacto:** confiabilidade/segurança. **Esforço:** alto.
- **Arquivos:** status_monitor, event_bus, fifo_reader, terminal_service,
  database/models. **Dependências:** migration e testes de corrida.
- **Riscos:** alterar ordem de teardown e latência de shutdown.

## P0 — Diagnosticar encerramento Claude/tmux

- **Descrição:** registrar pane ID/PID, comando/exit status e motivo do
  desaparecimento; criar reprodução automatizada.
- **Motivação:** bloqueia supervisor Claude no funcional.
- **Prioridade:** P0. **Impacto:** uso pessoal imediato. **Esforço:** médio.
- **Arquivos:** clients/tmux, claude_code, terminal_service, fixtures.
- **Dependências:** Claude autenticado. **Riscos:** logs não devem expor prompt.

## P1 — Estabilizar status e output OpenCode

- **Descrição:** reduzir atraso de completion e garantir LAST após marcador
  visual, com fixtures ANSI 1.14.
- **Motivação:** loops autônomos dependem de conclusão confiável.
- **Prioridade:** P1. **Impacto:** automação OpenCode. **Esforço:** médio.
- **Arquivos:** opencode_cli, status_monitor, terminal_service, testes provider.
- **Dependências:** versões do TUI. **Riscos:** regressões entre releases.

## P1 — Registry de providers e Capability Router

- **Descrição:** descriptors registráveis com executable, auth probe, resume,
  output, MCP e enforcement.
- **Motivação:** remover `if/elif` e rotear por capacidade real.
- **Prioridade:** P1. **Impacto:** extensibilidade. **Esforço:** alto.
- **Arquivos:** models/provider, providers/manager, API, install e UI.
- **Dependências:** schema de capabilities. **Riscos:** compatibilidade da API.

## P1 — Sessões lógicas persistentes

- **Descrição:** separar sessão lógica de runtime, persistir generation/resume e
  reconciliar no startup.
- **Motivação:** sobreviver a restart/crash.
- **Prioridade:** P1. **Impacto:** durabilidade. **Esforço:** alto.
- **Arquivos:** database, session_service, terminal_service, providers.
- **Dependências:** lifecycle supervisor. **Riscos:** retomada varia por CLI.

## P1 — Context Broker externo

- **Descrição:** ContextBundle versionado, orçamento, redação e referências a
  artefatos/memória.
- **Motivação:** contexto consistente entre CLIs sem copiar toda conversa.
- **Prioridade:** P1. **Impacto:** qualidade/custo. **Esforço:** alto.
- **Arquivos:** novo serviço/camada, adapters memory/graph.
- **Dependências:** sessão lógica/router. **Riscos:** vazamento de contexto.

## P2 — Fixture real com timeout obrigatório

- **Descrição:** timeout em todo `requests.post/delete` das fixtures e cleanup
  garantido por process/session ID.
- **Motivação:** uma fixture sem CLI bloqueou a suíte e deixou tmux.
- **Prioridade:** P2. **Impacto:** CI. **Esforço:** baixo.
- **Arquivos:** test/fixtures/cao_server.py. **Dependências:** nenhuma.
- **Riscos:** timeout curto pode gerar flake em hosts lentos.

## P2 — Soak de descritores sob limite baixo

- **Descrição:** repetir servidor/FIFO/terminal, medir `/proc/PID/fd` e tipos.
- **Motivação:** tornar regressão Errno 24 diagnosticável.
- **Prioridade:** P2. **Impacto:** observabilidade. **Esforço:** médio.
- **Arquivos:** novos testes/CI. **Dependências:** tmux/mock provider.
- **Riscos:** teste lento/flaky; separar job nightly.

## P2 — Bootstrap OpenCode explícito

- **Descrição:** detectar/migrar config 1.14 antes de instalar/listar agents e
  serializar escrita de `opencode.json`.
- **Motivação:** primeira chamada e installs paralelos são frágeis.
- **Prioridade:** P2. **Impacto:** onboarding. **Esforço:** médio.
- **Arquivos:** install_service, opencode_config, CLI/tests.
- **Dependências:** contrato upstream. **Riscos:** mudança de migration.

## P2 — Frontend code splitting

- **Descrição:** dividir views pesadas do bundle Vite.
- **Motivação:** aviso de chunk ~755 kB.
- **Prioridade:** P2. **Impacto:** carregamento. **Esforço:** baixo/médio.
- **Arquivos:** web/src, vite.config. **Dependências:** medição de bundle.
- **Riscos:** complexidade de lazy-loading.

## P3 — Unificar documentação de paths de logs

- **Descrição:** reconciliar referências `~/.cao` e `~/.aws/cli-agent-orchestrator`.
- **Motivação:** troubleshooting ambíguo.
- **Prioridade:** P3. **Impacto:** operação. **Esforço:** baixo.
- **Arquivos:** docs, constants. **Dependências:** decisão de compatibilidade.
- **Riscos:** instalações antigas usam path legado.
