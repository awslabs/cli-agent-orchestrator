"""The released-schema fixture generator can be rerun without stale homes."""

import importlib.util
from pathlib import Path


def test_generator_reuses_scratch_without_reusing_homes(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "fixtures/db_schemas/generate.py"
    spec = importlib.util.spec_from_file_location("db_schema_generator", source)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    monkeypatch.setattr(generator, "SCRATCH", tmp_path)
    monkeypatch.setattr(generator, "TAGS", ("v2.5.5",))
    homes_seen = []

    def fake_run(command, **kwargs):
        if command[0] == "env":
            target = Path(
                next(
                    value.split("=", 1)[1] for value in command if value.startswith("CAO_HOME_DIR=")
                )
            )
            assert not target.exists()
            target.mkdir(parents=True)
            homes_seen.append(target)

    monkeypatch.setattr(generator.subprocess, "check_output", lambda *args, **kwargs: "commit\n")
    monkeypatch.setattr(generator.subprocess, "run", fake_run)
    monkeypatch.setattr(generator, "write_ddl", lambda *args: None)
    generator.generate()
    generator.generate()
    assert len(homes_seen) == 6
    assert len({path.relative_to(tmp_path).parts[0] for path in homes_seen}) == 2
