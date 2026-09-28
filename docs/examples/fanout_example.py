"""Fan-out shape — shim-based (M1, FR-7.1).

Runs several agent steps concurrently via ``concurrent.futures``. Each
concurrent call passes an EXPLICIT ``step_id`` — the sequential counter
fallback is UNSAFE across runs under concurrent scheduling (BR-13); an
explicit label per shard is REQUIRED, not optional, whenever ``run_step`` is
called from more than one thread.

Run standalone by copying it into the workflows directory and running it by its
stem — ``cao workflow run`` resolves a bare name, and rejects a path outside
that directory::

    cp docs/examples/fanout_example.py ~/.aws/cli-agent-orchestrator/workflows/
    cao workflow validate ~/.aws/cli-agent-orchestrator/workflows/fanout_example.py
    cao workflow run fanout_example
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from cao_workflow import emit_output, run_step

SHARDS = ["alpha", "beta", "gamma"]


def _run_shard(shard: str):
    handle = run_step("kiro_cli", "reviewer", f"review shard {shard}", step_id=f"shard-{shard}")
    return shard, handle.output


def main() -> None:
    results = {}
    with ThreadPoolExecutor(max_workers=len(SHARDS)) as pool:
        futures = [pool.submit(_run_shard, shard) for shard in SHARDS]
        for future in as_completed(futures):
            shard, output = future.result()
            results[shard] = output
    emit_output({"shards": results})


if __name__ == "__main__":
    main()
