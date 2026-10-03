from test.fixtures.decision_conformance import EngineAdapter, conformance_cases, run_case

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("case", conformance_cases(), ids=lambda case: case.name)
async def test_engine_conformance(case, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    await run_case(case, lambda: EngineAdapter(tmp_path))


@pytest.mark.asyncio
async def test_adapter_capability_skip_names_missing_capabilities(tmp_path):
    class Unsupported:
        supports = frozenset()

    from test.fixtures.decision_conformance import Case

    with pytest.raises(pytest.skip.Exception, match="field_states, policy"):
        await run_case(
            Case("requires_policy", requires=frozenset(("policy", "field_states"))), Unsupported
        )
