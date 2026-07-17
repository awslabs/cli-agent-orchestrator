# Resultados de testes

Data: 17/07/2026. Python 3.12.3, CAO 2.3.0.

## Resultado final

| Comando | Resultado | Duração |
|---|---:|---:|
| `pytest test/utils -v` | 400 passed | 11.29 s |
| `pytest test/utils/test_skills.py -vv` | 50 passed | 1.88 s |
| caso vazio parametrizado | 2 passed | 1.59 s |
| utils com `ulimit -n 256` | 400 passed | 6.61 s |
| `pytest test/clients/test_tmux_client.py -q` | 65 passed | 2.02 s |
| frontend `npm test` | 81 passed em 6 arquivos | aprovado |
| suíte `-x` | 4592 passed, 15 skipped, 37 deselected | 289.79 s |
| suíte final sem `-x`, após rebase em `upstream/main` | 4596 passed, 15 skipped, 37 deselected, 13 warnings | 164.56 s |
| sandbox funcional após revisão | 4 passed | 0.28 s |

Cobertura da suíte final: 89%. Pico RSS observado na passagem cronometrada:
340428 KiB. O log integral final foi produzido em `/tmp/cao-pytest-full.log`
(artefato temporário, não versionado).

## Falhas encontradas e resolução

1. Smoke OpenCode falhou porque herdava configuração global e a versão 1.14
   executa migração na primeira chamada. O teste foi isolado e recebe bootstrap
   anterior; passou 3 vezes com diretórios novos.
2. Telemetria falhou porque o ambiente base não instala o extra `otel`. A
   configuração correta de dev é `uv sync --all-extras`; o caso isolado passou.
3. A fixture `cao_terminal` criava Kiro antes de o corpo do teste verificar o
   executável. Como `kiro-cli` não existe, bloqueava no timeout e podia deixar
   tmux órfão. A checagem foi movida para a fixture antes do POST.
4. JWT malformado disparava busca JWKS antes de ser rejeitado. A validação do
   header sem confiança agora ocorre antes da rede; verificação criptográfica
   completa continua igual.

## Warnings e skips

Treze warnings permaneceram não bloqueantes; incluem avisos de depreciação e
cenários intencionais dos testes. Quinze skips dependem de runtimes/condições
externas, incluindo provider ausente. Nenhum teste foi removido, mascarado ou
marcado artificialmente como sucesso.

Dentro do sandbox de execução do agente, um `TestClient`/AnyIO mínimo bloqueou
ao cruzar thread/event loop. Por isso a suíte que usa subprocessos/socket/tmux
foi executada no host aprovado. Isso não se reproduziu no host.
