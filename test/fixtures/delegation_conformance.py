"""Drive real delegation clients through the in-process HTTP handlers."""

import asyncio
from datetime import datetime
from test.fixtures.decision_conformance import EngineAdapter, Observation
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import httpx


class DelegationAdapter(EngineAdapter):
    supports = frozenset()

    def __init__(self, root, monkeypatch, entrypoint="assign"):
        super().__init__(root)
        self.monkeypatch = monkeypatch
        self.entrypoint = entrypoint
        self.new_session = entrypoint.endswith("_new_session")
        if self.new_session:
            self.provider = "claude_code"
        self.result: Any = None
        self.posts = []
        self.responses = []
        self.probe_refuses_fallback = False
        self.creates = []
        self.client = None
        self.reprobe = False
        self.exclude = False

    def configure(self, **kwargs):
        super().configure(**kwargs)
        from dataclasses import replace

        from cli_agent_orchestrator.api import main as api
        from cli_agent_orchestrator.mcp_server import server
        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
        from cli_agent_orchestrator.plugins import PluginRegistry
        from cli_agent_orchestrator.services import agent_step, session_service, terminal_service
        from cli_agent_orchestrator.utils import orchestration as o

        patch = self.monkeypatch
        patch.setattr(api.app.state, "decision_engine", self.engine, raising=False)
        patch.setattr(api.app.state, "plugin_registry", PluginRegistry(), raising=False)
        patch.setattr(api, "_record_job_state", AsyncMock())
        patch.setattr(api, "resolve_provider", lambda *a, **k: self.provider)
        patch.setattr(o, "resolve_provider", lambda *a, **k: self.provider)
        patch.setattr(o, "_current_terminal_id", lambda: None if self.new_session else "a1b2c3d4")
        patch.setattr(server, "_current_terminal_id", lambda: "a1b2c3d4")
        patch.setattr(server, "_tool_denied_reason", lambda name: None)
        patch.setattr(server, "_elastic_ready_wait", lambda: 0)
        patch.setattr(server, "_release_elastic_worker", lambda *args: True)
        patch.delenv("CAO_ELASTIC_WORKER_ID", raising=False)
        patch.setattr(o, "_get_cleanup_nudge", lambda: "")
        patch.setattr(o, "_auth_headers", lambda: {})
        patch.setattr(o, "_auth_headers_for", lambda url: {})
        patch.setattr(o, "_resolve_child_allowed_tools", lambda *a, **k: None)
        patch.setattr(
            o,
            "_resolve_handoff_provider",
            lambda *a: o.HandoffContext(self.provider, "cao-fixture", "a1b2c3d4", None),
        )
        patch.setattr(o, "_send_direct_input_handoff", Mock())
        patch.setattr(
            o, "_delete_terminal_impl", lambda tid, *, check_binding=True: {"success": True}
        )
        patch.setattr(o.time, "time", lambda: 1000.0)
        patch.setenv("CAO_ELASTIC_BROKER_URL", "http://broker.example")
        patch.setenv("CAO_ELASTIC_BROKER_TOKEN", "fixture-token")
        patch.setenv("CAO_ELASTIC_CALLBACK_URL", "http://broker.example")
        patch.setenv("CAO_ADVERTISED_URL", "http://broker.example")
        patch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
        patch.setattr(agent_step.status_monitor, "get_status", lambda tid: TerminalStatus.COMPLETED)
        patch.setattr(agent_step, "upsert_handoff_result", Mock())
        patch.setattr(terminal_service, "send_input", Mock(return_value=True))
        patch.setattr(terminal_service, "get_output", lambda *a: "worker result")
        patch.setattr(terminal_service, "delete_terminal", Mock(return_value=True))
        patch.setattr(
            terminal_service, "get_terminal", lambda tid: self.terminal.model_dump(mode="json")
        )

        real_create = terminal_service.create_terminal

        async def create(provider, agent_profile, **options):
            self.creates.append({"provider": provider, "agent_profile": agent_profile, **options})
            plan = options.get("launch_plan")
            if self.reprobe:
                from test.fixtures.terminal_launch import install_launch_stubs

                from cli_agent_orchestrator.models.kiro_engine import KiroEngine
                from cli_agent_orchestrator.providers.kiro_capabilities import (
                    KiroCapabilities,
                    KiroCapabilityError,
                )

                _, backend, _ = install_launch_stubs(
                    patch, self.store.key_path.parent, profile_model=self.profile_model
                )
                from cli_agent_orchestrator.clients.database import (
                    create_terminal as db_create_terminal,
                )

                # This is an explicitly unrestricted synthetic supervisor. Keep the
                # real server ceiling check in the capability-reprobe path.
                db_create_terminal(
                    "a1b2c3d4",
                    "cao-fixture",
                    "supervisor",
                    self.provider,
                    "supervisor",
                    ["*"],
                    delegate_tools=["*"],
                )
                backend.session_exists.return_value = not options.get("new_session", False)
                backend.session_exists_strict.return_value = not options.get("new_session", False)
                probe = Mock(
                    side_effect=[
                        KiroCapabilityError(
                            "missing_flag", KiroEngine.V2, "unsupported decided model"
                        ),
                        KiroCapabilities(version="2.0", flags=frozenset()),
                    ]
                )
                if self.probe_refuses_fallback:
                    probe.side_effect = KiroCapabilityError(
                        "missing_flag", KiroEngine.V2, "unsupported fallback model"
                    )
                self.probe = probe
                before = set(terminal_service._deferred_init_tasks)
                self.terminal = await real_create(
                    provider, agent_profile, kiro_capability_probe=probe, **options
                )
                self.deferred_tasks = set(terminal_service._deferred_init_tasks) - before
                self.launched_model = self.terminal.model
                return self.terminal
            profile = AgentProfile(
                name=agent_profile, description="worker description", model=self.profile_model
            )
            launch_model = terminal_service.resolve_launch_model(
                options.get("model"), profile, None if self.reprobe else plan
            )
            self.launched_model = launch_model
            self.terminal = Terminal(
                id="worker-fixed",
                name="worker",
                provider=provider,
                session_name="cao-fixture",
                agent_profile=agent_profile,
                model=launch_model,
                model_honored=terminal_service.ProviderManager.provider_class(
                    provider
                ).honors_model(agent_profile, profile, launch_model),
                status=TerminalStatus.IDLE,
                last_active=datetime(2026, 1, 1),
            )
            self.engine.bind_launch(
                plan,
                terminal_id=self.terminal.id,
                model=launch_model,
                honored=self.terminal.model_honored,
                not_honored=self.reprobe,
            )
            return self.terminal

        patch.setattr(terminal_service, "create_terminal", create)
        patch.setattr(session_service, "create_terminal", create)
        from cli_agent_orchestrator.clients.database import get_terminal_metadata

        patch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: (
                get_terminal_metadata(tid)
                if tid == "a1b2c3d4" and self.reprobe
                else {"provider": self.provider, "engine": None}
            ),
        )
        patch.setattr(
            terminal_service,
            "get_backend",
            Mock(side_effect=AssertionError("backend outside transport fixture")),
        )
        patch.setattr(o.requests, "get", self.get)
        patch.setattr(o.requests, "post", self.post)
        self.api = api
        if self.exclude:
            original = self.engine.settings_loader()
            self.engine.settings_loader = lambda: replace(
                original,
                points={
                    p: replace(v, exclude_profiles=("developer",))
                    for p, v in original.points.items()
                },
            )

    @staticmethod
    def response(data, url="http://fixture.example", method="GET"):
        return httpx.Response(200, json=data, request=httpx.Request(method, url))

    def get(self, url, **kwargs):
        return self.response(
            {
                "provider": self.provider,
                "session_name": "cao-fixture",
                "allowed_tools": ["*"],
                "delegate_tools": ["*"],
                "features": ["delegation_ceiling"],
                "working_directory": "/workspace",
            },
            url,
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/workers"):
            return self.response(
                {
                    "worker_id": "worker-fixed",
                    "target_host": "worker.example",
                    "working_directory": "/workspace",
                    "session_name": "cao-fixture",
                    "release_token": "release-fixed",
                },
                url,
                "POST",
            )
        future = asyncio.run_coroutine_threadsafe(
            self.client.post(
                urlsplit(url).path, params=kwargs.get("params"), json=kwargs.get("json")
            ),
            self.loop,
        )
        response = future.result(timeout=10)
        self.responses.append(response)
        return response

    async def delegate(self, *, message, model=None, field_states=None, purpose=None):
        from cli_agent_orchestrator.mcp_server import server
        from cli_agent_orchestrator.utils import orchestration as o

        # Installed delegation has no purpose wire; the protocol argument stays local.
        assert field_states is None
        self.loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app), base_url="http://localhost"
        ) as client:
            self.client = client
            if self.entrypoint in ("assign", "remote_assign"):
                self.result = await asyncio.to_thread(
                    o._assign_impl,
                    "developer",
                    message,
                    "/workspace",
                    model=model,
                    target_host="worker.example" if self.entrypoint == "remote_assign" else None,
                )
            elif self.entrypoint == "elastic":
                self.result = await asyncio.to_thread(
                    lambda: asyncio.run(
                        server.assign_elastic(
                            "developer", message, provider=None, engine=None, model=model
                        )
                    )
                )
            else:
                options = {"model": model, "working_directory": "/workspace"}
                if self.entrypoint.startswith("handoff_early"):
                    options["wait"] = False
                elif self.entrypoint.startswith("handoff_reuse"):
                    options["on_terminal_id"] = lambda tid: None
                self.result = await asyncio.to_thread(
                    lambda: asyncio.run(o._handoff_impl("developer", message, **options))
                )
        if getattr(self, "deferred_tasks", None):
            await asyncio.gather(*self.deferred_tasks)
        success = (
            self.result.get("success") if isinstance(self.result, dict) else self.result.success
        )
        error = (
            None
            if success
            else (
                self.result.get("message") if isinstance(self.result, dict) else self.result.message
            )
        )
        return Observation(
            getattr(self, "launched_model", None),
            bool(success),
            error,
            self.store.list(),
            self.spans,
            [],
        )
