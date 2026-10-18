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
# output models captured before delegation wiring.
TOOL_DEFINITIONS_SHA256 = "3e144811fd442f49805146bcf9dbd6708e93a7915338ba04317576a8dfac445f"
TOOL_MODELS_SHA256 = "6b9c510f965a658909efa356cb18d8a2009bd5fe7f58ad2e56c87fda3a7523c2"
