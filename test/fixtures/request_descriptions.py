# Request descriptions captured from the unchanged creation and step contracts.
REQUEST_DESCRIPTIONS = {
    "CreateTerminalBody": "Optional JSON body for POST /sessions/{name}/terminals.\n"
    "\n"
    "Carries the deferred-init message payload OUT of the query string:\n"
    "prompt content can be large (URL-length 414 risk) and sensitive "
    "(query\n"
    "strings are routinely captured in HTTP access logs and traces). "
    "Routing\n"
    "fields (provider, defer_init, etc.) stay as query params; only the\n"
    "message content lives here.",
    "CreateSessionBody": "Optional JSON body for POST /sessions.\n"
    "\n"
    "Reuses the terminal-creation message payload and keeps "
    "operator-forwarded\n"
    "environment variables in the request body, preserving the existing\n"
    '``{"env_vars": {...}}`` wire shape.\n'
    "\n"
    "``group``/``metadata`` are the #432 discovery fields (see\n"
    "``UpdateGroupBody``/``UpdateMetadataBody`` below for their dedicated "
    "PATCH\n"
    "counterparts). They live here — rather than as separate top-level\n"
    "``Body(embed=True)`` params — so this endpoint keeps its single flat "
    "JSON\n"
    "body wire shape (adding a second embedded body param would force "
    "FastAPI\n"
    'to nest everything under a ``"body"`` key, breaking existing callers '
    "that\n"
    'already POST ``{"env_vars": ..., "initial_message": ...}`` at the top\n'
    "level, e.g. ``ops_mcp_server/server.py``'s ``_launch_session_impl``).",
    "RunStepRequest": "Request body for the combined step-execution endpoint (N0, #312).",
}
