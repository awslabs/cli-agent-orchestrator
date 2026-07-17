# Banco de dados

Tecnologia: SQLite com SQLAlchemy para entidades principais e `sqlite3` direto
para migrations/journal de workflow. Arquivo:
`~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db`, modo de arquivo
pretendido 0600 e diretório 0700. `check_same_thread=False` permite acesso dos
workers; sessões SQLAlchemy usam context manager e são fechadas.

Tabelas encontradas no banco local:

- `terminals`: id, sessão/janela tmux, provider, profile, allowed tools,
  shell baseline, caller e atividade;
- `inbox`: mensagens, remetente/destinatário, status e criação;
- `flows`: agenda e profile/provider;
- `memory_metadata`: índice da wiki em arquivos;
- `project_aliases`: identidade canônica de projeto;
- `workflow_index`: projeção de specs YAML;
- `workflow_run` e `workflow_run_step`: journal durável de execução.

Contagem observada antes do funcional: 0 terminals, 0 inbox, 2 memórias, 1
alias, 0 flows, 0 workflow index/run e 2 run steps. Nenhuma linha/provider é
armazenada em tabela própria: o campo `terminals.provider` é a relação.

## Terminal

1. tmux cria sessão/janela;
2. profile/tools são resolvidos;
3. `create_terminal()` insere a linha;
4. FIFO/pipe/provider são iniciados;
5. `ProviderManager` mantém instância em memória;
6. consultas combinam metadata do banco com status do singleton monitor;
7. remoção captura snapshot, para pipe/FIFO, limpa monitor, mata janela,
   remove provider e por último apaga a linha.

Em falha de criação, o rollback tenta parar FIFO, limpar monitor/provider,
apagar DB e matar a sessão/janela criada. Esse rollback não drena eventos já
publicados, origem da corrida investigada.

## Migrações, crash e reset

`init_db()` chama `create_all` e migradores idempotentes para colunas/índices e
workflows. Não há Alembic. Em crash, tmux e DB podem divergir; o provider pode
ser reconstruído do registro, mas um processo inexistente continuará inválido.
Backups devem incluir arquivo principal e, se presentes, `-wal`/`-shm` com o
servidor parado. Não existe reset transacional exposto; use home isolado em
testes ou backup + recriação deliberada.

