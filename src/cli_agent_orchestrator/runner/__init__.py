"""Execution-only runners for the opt-in ``remote`` terminal backend (#745).

A runner is a small process that lives beside an agent CLI in another
container or host. It dials OUT to the one central ``cao-server`` over an
authenticated WebSocket, executes the server's terminal operations against its
own local tmux, and streams pane output back. It is not a control server: it
has no HTTP API, no database and no orchestration logic.

- :mod:`.protocol` is the wire contract shared by both ends.
- :mod:`.hub` is the server side: it accepts runner connections and routes.
- :mod:`.agent` is the runner process itself (the ``cao-runner`` command).
"""
