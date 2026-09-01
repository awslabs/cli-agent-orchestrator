"""Select local or remote CAO memory without changing local defaults."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import requests

from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.services.elastic_worker_gateway import (
    elastic_worker_gateway_headers,
)
from cli_agent_orchestrator.services.memory_service import MemoryPartialWriteError, MemoryService


def remote_memory_url() -> Optional[str]:
    value = os.environ.get("CAO_MEMORY_API_URL", "").strip()
    return value.rstrip("/") if value else None


def _headers() -> dict[str, str]:
    headers = elastic_worker_gateway_headers()
    token = get_local_bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _timeout() -> float:
    return float(MCP_REQUEST_TIMEOUT)


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    base_url = remote_memory_url()
    if not base_url:
        raise RuntimeError("CAO_MEMORY_API_URL is not configured")
    response = requests.post(
        f"{base_url}{path}",
        json=body,
        headers=_headers() or None,
        timeout=_timeout(),
    )
    if response.status_code >= 400:
        try:
            error = response.json()
        except (TypeError, ValueError):
            error = {}
        if error.get("error_kind") == MemoryPartialWriteError.error_kind:
            partial = error.get("partial_write")
            if isinstance(partial, dict):
                required = ("key", "scope", "file_path")
                if all(isinstance(partial.get(field), str) for field in required):
                    scope_id = partial.get("scope_id")
                    if scope_id is None or isinstance(scope_id, str):
                        raise MemoryPartialWriteError(
                            key=partial["key"],
                            scope=partial["scope"],
                            scope_id=scope_id,
                            file_path=partial["file_path"],
                        )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


async def store_memory(
    *,
    content: str,
    scope: str,
    memory_type: str,
    key: Optional[str],
    tags: str,
    terminal_context: Optional[dict[str, Any]],
):
    if not remote_memory_url():
        return await MemoryService().store(
            content=content,
            scope=scope,
            memory_type=memory_type,
            key=key,
            tags=tags,
            terminal_context=terminal_context,
        )
    payload = await asyncio.to_thread(
        _post,
        "/internal/memory/store",
        {
            "content": content,
            "scope": scope,
            "memory_type": memory_type,
            "key": key,
            "tags": tags,
            "terminal_context": terminal_context,
        },
    )
    from cli_agent_orchestrator.models.memory import Memory

    memory = Memory.model_validate(payload["memory"])
    memory.action = payload.get("action")
    return memory


async def recall_memory(
    *,
    query: Optional[str],
    scope: Optional[str],
    memory_type: Optional[str],
    limit: int,
    terminal_context: Optional[dict[str, Any]],
    search_mode: str,
    sort_by: str,
    include_related: bool,
):
    if not remote_memory_url():
        return await MemoryService().recall(
            query=query,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            terminal_context=terminal_context,
            search_mode=search_mode,
            sort_by=sort_by,
            include_related=include_related,
        )
    payload = await asyncio.to_thread(
        _post,
        "/internal/memory/recall",
        {
            "query": query,
            "scope": scope,
            "memory_type": memory_type,
            "limit": limit,
            "terminal_context": terminal_context,
            "search_mode": search_mode,
            "sort_by": sort_by,
            "include_related": include_related,
        },
    )
    from cli_agent_orchestrator.models.memory import Memory

    return [Memory.model_validate(item) for item in payload["memories"]]


async def forget_memory(
    *,
    key: str,
    scope: str,
    terminal_context: Optional[dict[str, Any]],
) -> bool:
    if not remote_memory_url():
        return await MemoryService().forget(
            key=key,
            scope=scope,
            terminal_context=terminal_context,
        )
    payload = await asyncio.to_thread(
        _post,
        "/internal/memory/forget",
        {"key": key, "scope": scope, "terminal_context": terminal_context},
    )
    return bool(payload["deleted"])


def memory_context_for_terminal(terminal_id: str, task_description: str = "") -> str:
    service = MemoryService()
    if not remote_memory_url():
        return service.get_curated_memory_context(
            terminal_id,
            task_description=task_description,
        )
    terminal_context = service._get_terminal_context(terminal_id)
    if not terminal_context:
        return ""
    payload = _post(
        "/internal/memory/context",
        {"terminal_context": terminal_context, "budget_chars": 3000},
    )
    return str(payload.get("context", ""))
