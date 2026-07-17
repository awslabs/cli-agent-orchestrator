# Ciclo de vida de sessões e terminais

Os estados reais do terminal são: `unknown`, `idle`, `processing`, `completed`,
`waiting_user_answer` e `error`. Não existem enums persistidos `Created`,
`Starting`, `Stopping`, `Stopped` ou `Deleted`; essas fases são implícitas nas
operações. Sessão é derivada do backend + linhas `terminals`, não uma tabela
própria.

```text
create requested
 → tmux session/window
 → DB terminal
 → FIFO/pipe/provider init        = unknown
 → prompt pronto                  = idle
 → input enviado                  = processing
 → resposta + prompt              = completed
 → novo input                     = processing
 → delete: snapshot → stop FIFO → clear monitor → kill pane
          → cleanup provider → delete DB
```

`waiting_user_answer` representa diálogo interativo. `error` é detecção/falha
do provider; exceções durante criação acionam rollback e podem não produzir um
terminal consultável. O status reside no `StatusMonitor` e não sobrevive a
restart; após restart começa `unknown` até nova saída/detecção.

Sticky latch impede regressões espúrias de pronto para processing/unknown sem
um input armado. `clear_rolling_buffer` preserva latch; `reset_buffer` também
apaga status. Eventos de status alimentam inbox.

Crash pode deixar quatro combinações: DB+pane válidos (reconstruível), DB sem
pane (órfão lógico), pane sem DB (órfão físico) ou nenhum. Um evento atrasado
para terminal já apagado hoje chama `ProviderManager.get_provider`, consulta o
banco e lança `ValueError`; não existe tombstone/generation para descartá-lo.

Fluxo recomendado para robustez:

```text
ACTIVE → STOPPING (tombstone/generation)
 → impedir novos inputs
 → desinscrever/drenar eventos e aguardar detecções
 → parar pipe/FIFO
 → encerrar processo/pane
 → remover provider
 → apagar registro durável por último
 → manter tombstone curto para ignorar eventos atrasados
```

Para sessões lógicas reiniciáveis, separar `logical_session_id` de
`terminal_runtime_id`, persistir geração/checkpoint/resume handle do provider e
reconciliar DB/backend no startup.
