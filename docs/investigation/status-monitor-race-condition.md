# Investigação: StatusMonitor e terminal removido

## Reprodução real

O erro histórico do terminal `371d2d6d` foi encontrado nos logs. O teste
funcional desta análise o reproduziu duas vezes em Python 3.12, terminais
`50427c3a` e `3325d8a5`, ao iniciar Claude Code.

Sequência observada no primeiro funcional:

```text
19:10:25.914 sessão cao-prd-functional/janela criada
19:10:25.934 FIFO reader iniciado
19:10:25.957 pipe-pane iniciado
19:10:25.975 ClaudeCodeProvider registrado
19:10:28.979 shell declarado pronto; comando Claude enviado
19:10:29.946 janela não encontrada por libtmux
19:10:30.340 create_terminal entra em rollback
19:10:30.751 FIFO parado
19:10:30.752 provider removido
19:10:30.772 sessão morta; registro DB já removido
19:10:30.781–.788 quatro eventos atrasados chegam
ProviderManager consulta DB e lança ValueError
```

Stack principal:

```text
StatusMonitor.run
 → asyncio.to_thread(StatusMonitor._process_chunk)
 → provider_manager.get_provider(terminal_id)
 → get_terminal_metadata
 → ValueError: Terminal ... not found in database
```

Arquivos: `services/status_monitor.py:95-137`,
`providers/manager.py:151-169`, `services/terminal_service.py:147-480` e
`:1052-1134`, `services/fifo_reader.py`, `services/event_bus.py`,
`clients/tmux.py`, `clients/database.py`.

## Causa

Causa secundária comprovada: o event bus já contém chunks publicados quando o
rollback limpa monitor/provider e apaga a linha. `_process_chunk` não possui
tombstone nem trata terminal ausente como evento obsoleto; `get_provider` tenta
recriar pelo banco e falha. Como cada chunk é processado por `to_thread`, parar
o reader impede novos eventos, mas não cancela o já enfileirado/em voo.

A causa primária que disparou o rollback funcional é o desaparecimento da
janela/sessão tmux logo após o TUI Claude iniciar. Desativar renomeação de janela
foi testado e não resolveu, portanto a tentativa foi revertida. Ainda é preciso
instrumentar PID/exit status do shell/Claude para explicar esse encerramento.

## Fluxo atual e sugerido

Atual: `parar reader → limpar monitor → limpar provider → apagar DB → matar
sessão/janela (ordem varia entre rollback/delete)`, sem drenar fila.

Sugerido:

```text
marcar STOPPING e incrementar geração
 → bloquear input
 → desinscrever terminal/cancelar debounce e detecções
 → parar pipe e FIFO
 → drenar/invalidar eventos da geração antiga
 → aguardar to_thread/tarefas em voo
 → encerrar processo/pane
 → limpar provider
 → apagar DB por último
 → reter tombstone curto
```

Correção mínima: em `_process_chunk`, consultar um conjunto thread-safe de
terminais em teardown e retornar; adicionalmente, tratar ausência de metadata
como evento tardio em nível DEBUG, sem recriar provider. Isso precisa de teste
que publique chunk, remova terminal e então processe o chunk.

Correção arquitetural: envelope `{terminal_id, runtime_generation, sequence}`,
subscription por terminal cancelável, state machine persistida e resource
supervisor com structured concurrency. DB, backend e event pipeline precisam de
reconciliação no startup.

Impacto: ruído/error storm, trabalho em executor, possível atraso de outros
terminais e estado inconsistente. Relação com FD: possível apenas indiretamente
se teardown incompleto deixar reader/subprocesso; medições atuais não mostraram
leak. Status: corrida secundária confirmada; encerramento primário do Claude
pendente.

