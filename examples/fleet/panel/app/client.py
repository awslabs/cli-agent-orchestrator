"""Async HTTP client for a single machine's cao-server REST API.

Every function takes an httpx.AsyncClient and the machine base URL. Functions
raise httpx.HTTPError on transport/4xx/5xx; callers decide how to isolate
per-machine failures.
"""
import httpx

TIMEOUT = httpx.Timeout(8.0, connect=4.0)
# CAO's POST /sessions blocks until the agent CLI reaches a ready prompt
# (it uses a 60s internal init timeout), so allow headroom beyond that.
LAUNCH_TIMEOUT = httpx.Timeout(90.0, connect=5.0)
SENDER_ID = "fleet-panel"


async def _get(c, base, path, params=None):
    r = await c.get(f"{base}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def health(c, base):
    return await _get(c, base, "/health")


async def list_sessions(c, base):
    return await _get(c, base, "/sessions")


async def get_session(c, base, name):
    return await _get(c, base, f"/sessions/{name}")


async def terminal_output(c, base, terminal_id, mode="last"):
    return await _get(c, base, f"/terminals/{terminal_id}/output", {"mode": mode})


async def get_screen(c, base, terminal_id, ansi=True):
    return await _get(
        c, base, f"/terminals/{terminal_id}/screen", {"ansi": str(ansi).lower()}
    )


async def send_key(c, base, terminal_id, key):
    r = await c.post(f"{base}/terminals/{terminal_id}/key", params={"key": key})
    r.raise_for_status()
    return r.json() if r.content else {}


async def send_input(c, base, terminal_id, text, sender_id=SENDER_ID):
    r = await c.post(
        f"{base}/terminals/{terminal_id}/input",
        params={"message": text, "sender_id": sender_id},
    )
    r.raise_for_status()
    return r.json() if r.content else {}


async def launch(c, base, agent_profile, provider, session_name, working_directory=None):
    params = {"agent_profile": agent_profile, "session_name": session_name}
    if provider:
        params["provider"] = provider
    if working_directory:
        params["working_directory"] = working_directory
    r = await c.post(f"{base}/sessions", params=params)
    r.raise_for_status()
    return r.json()


async def send_message(c, base, terminal_id, message, sender_id=SENDER_ID):
    r = await c.post(
        f"{base}/terminals/{terminal_id}/inbox/messages",
        params={"sender_id": sender_id, "message": message},
    )
    r.raise_for_status()
    return r.json() if r.content else {}


async def shutdown(c, base, name):
    r = await c.delete(f"{base}/sessions/{name}")
    r.raise_for_status()
    return r.json()


async def list_providers(c, base):
    return await _get(c, base, "/agents/providers")


async def list_profiles(c, base):
    return await _get(c, base, "/agents/profiles")


async def working_directory(c, base, terminal_id):
    return await _get(c, base, f"/terminals/{terminal_id}/working-directory")
