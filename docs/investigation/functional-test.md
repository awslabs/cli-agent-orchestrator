# Teste funcional mínimo

Data: 17/07/2026. Repositório descartável:
`/home/fred/Projetos/cli-agent-orchestrator/.cao-sandbox`.

Tarefa: implementar `calculator.clamp`, executar testes, depois revisar a mesma
função para rejeitar intervalo invertido, usando a mesma sessão.

## Cadeia pretendida

Claude Code 2.1.212 foi lançado como `code_supervisor` e instruído a usar MCP
`assign` com worker OpenCode. A criação retornou HTTP 500 antes do supervisor
ficar pronto: o pane deixou de ser localizado, o rollback ocorreu e o
StatusMonitor processou eventos atrasados. A falha foi reproduzida duas vezes.
Assim, a delegação autônoma completa está **bloqueada** pelo bug documentado.

## Worker isolado

Para separar providers, foi criada `cao-prd-opencode`, terminal `c24c752e`,
provider `opencode_cli`, profile `developer`.

Primeiro turno:

- OpenCode editou `calculator.py` com uma implementação mínima;
- executou o arquivo de testes;
- retornou “3/3 testes passaram”.

Segundo turno, enviado por `cao session send` à mesma sessão:

- contexto foi preservado;
- adicionou `ValueError` para `minimum > maximum`;
- adicionou o quarto teste;
- retornou “4/4 testes passaram”.

Verificação independente:

```text
uv run pytest -c /dev/null -q test_calculator.py
....  4 passed in 0.28s
```

Intervenções manuais: iniciar backend, disparar o worker direto após falha do
supervisor, aguardar a estabilização do status e encerrar a sessão. OpenCode
permaneceu visualmente concluído por algum tempo enquanto a API reportava
`processing`; depois transitou para `completed`. Uma tentativa precoce de
`output?mode=last` retornou 404 por marcador ainda não extraível.

Conclusão: execução, edição, testes, segunda mensagem e continuidade do worker
OpenCode funcionam. Orquestração Claude→OpenCode não atende ao aceite imediato
até corrigir o lifecycle tmux/Claude e a tolerância a eventos atrasados.
