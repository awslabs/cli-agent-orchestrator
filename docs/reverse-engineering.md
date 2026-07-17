# Reverse Engineering Report

## Síntese

CAO é uma base útil para um harness pessoal, sobretudo como adaptador de CLIs,
terminal service, MCP e painel. A recomendação é **camada externa primeiro**, com
um fork curto apenas para correções bloqueantes upstreamáveis. Reescrever agora
perderia muito código de integração; construir toda a estratégia dentro do fork
aumentaria acoplamento e custo de merge.

## 1. Reutilizar integralmente

Profiles/skills, modelos de provider, CLI/API local, instalação de agents,
frontend de observabilidade, JWT/scopes, event log, SQLite básico, exporters de
graph e boa parte dos testes. `BaseProvider`, tool mapping e MCP tools são uma
boa anti-corruption layer, mesmo que evoluam.

## 2. Estender

Provider registry com capabilities; lifecycle/reconciliation; workflow engine;
event envelopes; persistência de sessão lógica; telemetria/correlation IDs;
OpenCode e Codex em modos headless/structured quando disponíveis.

## 3. Substituir

No horizonte, substituir detecção exclusivamente por regex do TUI e identidade
por nome de janela. `ProviderManager` central `if/elif` deve virar registry. O
status singleton volátil deve ceder a uma state machine com generation e
tombstones. Não é necessário substituir FastAPI, SQLite ou todo terminal
service no início.

## 4. Acoplamento ao tmux

Criação/remoção, identidade sessão+janela, input, scrollback, status, logs,
attach WebSocket, working directory, shell baseline, snapshots e FIFO
`pipe-pane`. A interface `TerminalBackend` e herdr iniciam a separação, mas o
modelo de domínio ainda fala em `tmux_session`/`tmux_window`.

## 5. Acoplamento a providers

Regex e marcadores visuais, flags de segurança, arquivos temporários de prompt,
formato MCP, instalação de profiles, extração de resposta e mecanismos de
resume. Tool enforcement varia entre hard, soft e install-time; tratá-los como
equivalentes é risco de segurança.

## 6. Planejamento, execução, revisão e correção

Adicionar um orchestrator externo com estados duráveis:
`PLAN → ROUTE → EXECUTE → VERIFY → REVIEW → REPAIR → DONE`. Cada step chama a
API CAO com idempotency key, input/output schema, timeout/retry e artifact refs.
O workflow atual pode executar steps, mas o policy loop deve ficar fora dos
adaptadores de terminal.

## 7. Context Broker

Serviço separado que recebe task/project/session, indexa artefatos e memória,
aplica orçamento/redação e entrega `ContextBundle` versionado. Providers recebem
somente referências/material necessário. Reutilizar memory/graph do CAO como
storage adapters, não como único modelo de contexto.

## 8. Capability Router

Manter catálogo dinâmico por runtime: modelos, custo, auth, hard permissions,
MCP, structured output, resume, contexto, latência e saúde. O router aplica
constraints obrigatórias antes de scoring. Nunca enviar tarefa sensível a um
provider de enforcement apenas textual.

## 9. Sessões lógicas após restart

Persistir `logical_session`, `runtime_instance`, `generation`, provider,
provider resume handle, cwd, last acknowledged message/sequence e checkpoints.
No startup, reconciliar DB com processos, reattach quando possível ou criar novo
runtime e reidratar contexto. Mensagens precisam de IDs idempotentes/ACK.

## 10. OpenCode como runtime de primeira classe

Manter config isolado, agents/permissions nativos e mesma sessão comprovada.
Adicionar descriptor de capability, bootstrap/migration explícito, health probe,
extração estruturada quando possível e testes versionados contra fixtures ANSI.
Resolver atraso `processing→completed` e output 404 antes de usá-lo em loops
totalmente autônomos.

## 11. Reutilizar assinaturas

Sempre iniciar o executável do usuário e seu login oficial; não copiar tokens,
não traduzir credenciais para variáveis do harness. Detectar status por comando
oficial e deixar ações manuais de login fora dos logs. Claude, Codex, OpenCode e
Cursor já provaram esse modelo no host.

## 12. Riscos de fork

Grande velocidade upstream, contratos de TUI instáveis, conflitos em arquivos
centrais grandes, patches de segurança, lockfiles e documentação divergente.
Um fork também pode assumir responsabilidade por bypass de permissões e
credenciais. Patches devem ser pequenos, testados e enviados upstream.

## 13. Fork ou camada externa

Camada externa é melhor para planner/router/context broker e sessão lógica. Use
um fork mínimo temporário para P0: teardown/event generation, Claude lifecycle e
status OpenCode. Mantenha API estável entre camada e CAO para poder voltar ao
upstream.

## 14. Bloqueios imediatos

- lançamento Claude dentro do CAO encerra/perde o pane neste host;
- eventos atrasados geram `Terminal not found` após rollback;
- status/output OpenCode pode estabilizar tardiamente;
- sessões lógicas não sobrevivem de modo confiável a crash;
- provider capabilities/enforcement não são modeladas formalmente.

O erro de descritores não foi reproduzido e não é bloqueio atual.

## 15. Primeira melhoria

Implementar um **Resource/Lifecycle Supervisor** com runtime generation,
tombstone, cancelamento/drenagem de eventos e reconciliação DB↔backend. Em
paralelo, instrumentar exit status do pane/CLI para fechar a causa do Claude.
Sem lifecycle determinístico, planner e contexto apenas amplificariam corridas.

