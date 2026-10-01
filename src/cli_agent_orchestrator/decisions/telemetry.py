"""Content-free decision spans and closed-label metrics."""

from typing import Any

from opentelemetry import metrics, trace

FIELDS = {
    "point": "point",
    "state": "state",
    "decider": "decider",
    "decider_version": "decider_version",
    "answer": "option",
    "applied_value": "applied_option",
    "confidence": "confidence",
    "outcome": "outcome",
    "reason": "reason",
    "latency_ms": "latency_ms",
    "model_honored": "model_honored",
}


def emit_record(row: dict[str, Any]) -> None:
    attributes = {
        "cao.decision." + dest: row[src] for src, dest in FIELDS.items() if row.get(src) is not None
    }
    if row.get("launched_model") is not None:
        attributes["gen_ai.request.model"] = row["launched_model"]
    with trace.get_tracer("cli_agent_orchestrator").start_as_current_span(
        "cao.decision", attributes=attributes
    ):
        pass
    labels = {
        "cao.decision." + key: row[key]
        for key in ("point", "state", "decider", "outcome", "reason")
        if row.get(key) is not None
    }
    meter = metrics.get_meter("cli_agent_orchestrator")
    meter.create_counter("cao.decision.requests", unit="1").add(1, labels)
    if row.get("latency_ms") is not None:
        meter.create_histogram("cao.decision.latency", unit="ms").record(
            row["latency_ms"],
            {
                key: value
                for key, value in labels.items()
                if key.rsplit(".", 1)[1] in ("point", "state", "decider")
            },
        )
