# Tool results captured with fixed worker identifiers and a frozen clock.
GOLDEN_RESULTS = {
    "assign": {
        "success": True,
        "terminal_id": "worker-fixed",
        "message": "Task assigned to developer (terminal: worker-fixed). Worker is "
        "initializing in the background; your task will be delivered once it "
        "is ready. Call delete_terminal('worker-fixed') when you no longer "
        "need this terminal.",
    },
    "handoff": {
        "success": True,
        "message": "Successfully handed off to developer (codex) in 0.00s",
        "rule": None,
        "output": "worker result",
        "terminal_id": "worker-fixed",
        "job_id": None,
        "pending": None,
    },
    "assign_elastic": {
        "success": True,
        "terminal_id": "worker-fixed",
        "target_host": "worker.example",
        "message": "Task assigned to developer on node worker.example (remote "
        "terminal: worker-fixed, session: cao-fixture). The worker is "
        "initializing in the background; your task will be delivered "
        "once it is ready and results will arrive via send_message. "
        "Cleanup when finished: delete_terminal('worker-fixed', "
        "target_host='worker.example'), or drop the whole remote "
        "session with DELETE "
        "http://worker.example:9889/sessions/cao-fixture (requires "
        "admin scope when auth is enabled).",
        "session_name": "cao-fixture",
        "delete_url": "http://worker.example:9889/sessions/cao-fixture",
        "worker_id": "worker-fixed",
        "elastic": True,
    },
}

# Agent tool definitions captured from main 11f164da (#942 descriptions);
# Output models include main's optional ephemeral refusal rule (#905).
TOOL_DEFINITIONS_SHA256 = "4e6c7593f8fb6f83a7e636e57966a6b98928276611e445dcc794ee67cf439501"
TOOL_MODELS_SHA256 = "c2611fc69ad1a6835369af9c43f6c40289561ec53c39f9cfea369e9f12973a73"
