# Investigação: Too many open files

## Ocorrência reportada

`OSError: [Errno 24] Too many open files` havia sido atribuído a
`TestLoadSkillMetadata::test_rejects_empty_skill_name` em
`test/utils/test_skills.py`.

## Reprodução e evidências

```bash
uv run pytest test/utils/test_skills.py -vv
uv run pytest \
  'test/utils/test_skills.py::TestLoadSkillMetadata::test_rejects_empty_skill_name' -vv
(ulimit -n 256; uv run pytest test/utils -v)
```

Resultados: arquivo 50/50, caso parametrizado 2/2, utils 400/400 mesmo com
limite 256. Foram coletadas 295 amostras de `/proc/<pytest-pid>/fd`: primeira e
mínima 3, máxima 11, sem tendência crescente. Na suíte ampla, o pico também foi
11. Limite real do host: soft/hard 1.048.576.

`validate_skill_name` faz `strip` e rejeita nome vazio antes de qualquer acesso
a arquivo. O teste usa apenas `tmp_path`/`monkeypatch` e espera `ValueError`.
Logo, o caso indicado é o ponto em que o processo previamente esgotado tentou
abrir algo, não a origem do vazamento.

Foram verificados processos pytest/CAO/providers, sessões tmux, FIFO readers,
watchdog, subprocessos, sockets e `/tmp/pytest-of-fred`. A suíte atual possui
testes explícitos de ciclos repetidos do FIFO que confirmam ausência de threads
reader residuais. Um resíduo `cao-caotest-*` foi associado a uma fixture Kiro
interrompida, não a crescimento de FD no teste de skills.

## Conclusão

Status: **não reproduzido no ambiente estabilizado**. Não há evidência de leak
no teste indicado nem relação demonstrada com Python 3.13. O ambiente anterior
usava Python 3.13.13 e podia conter serviços/sessões acumulados; isso é hipótese,
não causa provada. A corrida do StatusMonitor produz exceções repetidas, mas nas
medições atuais não aumentou descritores.

Solução temporária: encerrar de forma direcionada processos/sessões de teste
órfãos e repetir com monitoramento. Não elevar `ulimit`.

Solução definitiva sugerida: adicionar teste CI de soak que repita criação e
teardown de servidor/FIFO/tmux sob `ulimit -n 256`, registrando FD por tipo com
`lsof`; impor timeout e cleanup a todas as requisições de fixtures reais. Se
reaparecer, capturar PID e duas fotografias `lsof -p` separadas no tempo.

