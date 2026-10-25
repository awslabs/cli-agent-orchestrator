"""Remote execution runtimes for a central cao-server (#745).

- ``protocol``: the frames on the runtime channel.
- ``registry``: which runtimes are connected, which terminals they execute, and
  correlated command calls to them.
- ``server``: the server side, ``WS /runtime/channel`` and the ``/runtimes`` routes.
- ``bridge``: ``cao-bridge``, the execution-only process in a runtime pod.
"""
