# Comandos de uso diário

Execute na raiz do repositório.

```bash
# backend + UI empacotada
uv run cao-server

# frontend em desenvolvimento (outro terminal)
cd web && npm run dev

# validar CLI e providers
uv run cao --help
curl -fsS http://127.0.0.1:9889/agents/providers

# criar/listar/consultar/enviar
uv run cao launch --agents developer --provider opencode_cli --headless \
  --working-directory /caminho/do/projeto --auto-approve
uv run cao session list
uv run cao session status cao-NOME
uv run cao session send cao-NOME 'nova instrução' --timeout 300

# encerrar
uv run cao shutdown --session cao-NOME

# testes
uv run pytest test/utils -v
uv run pytest 'test/utils/test_skills.py::TestLoadSkillMetadata::test_rejects_empty_skill_name' -vv
uv run pytest test/ --ignore=test/e2e -m 'not integration' -vv
cd web && npm test

# debug
CAO_LOG_LEVEL=DEBUG uv run cao-server
tmux ls
```

Reset conservador: encerre sessões, pare o servidor, mova (não apague) o banco
para um nome `.backup`, então rode `uv run cao init`. Em testes automatizados,
use um home temporário em vez de tocar dados reais.

