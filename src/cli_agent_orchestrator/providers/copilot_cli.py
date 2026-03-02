"""GitHub Copilot CLI provider implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Strip broad ANSI/VT control sequences (not only color SGR codes) so
# status detection and completion signatures are stable across TUI redraws.
ANSI_CODE_PATTERN = r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
IDLE_PROMPT_PATTERN_LOG = r"(?i)(?:❯|›|copilot>|\? for shortcuts|type @ to mention files)"
USER_PROMPT_LINE_PATTERN = r"^\s*(?:❯|›|copilot>)\s+.*$"
BARE_ASCII_PROMPT_PATTERN = r"^\s*>\s*$"
ASSISTANT_PREFIX_PATTERN = r"^assistant\s*:|^[●◐◑◒◓◉].+"
WAITING_PROMPT_PATTERN = (
    r"(?:confirm folder trust|do you trust(?: the files in this folder| all the actions in this folder)?|"
    r"trust the contents of this directory|press enter to continue|\[\s*y/n\s*\])"
)
ERROR_PATTERN = r"(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
BUSY_PATTERN = (
    r"(?:thinking|working|running|executing|processing|analyzing|modifying|applying edits)"
)
SPINNER_PATTERN = r"(?:esc to cancel|◐|◑|◒|◓|∘)"
STARTUP_HINT_PATTERNS = (
    "github copilot v",
    "describe a task to get started",
    "copilot uses ai",
    "experimental mode is enabled",
    "no copilot instructions found",
    "run /init",
    "tip:",
    "loaded env:",
    "configured mcp servers:",
    "type @ to mention files",
    "remaining reqs",
)


class CopilotCliProvider(BaseProvider):
    """Provider for GitHub Copilot CLI integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile
        self._completion_candidate_signature = ""
        self._completion_candidate_since = 0.0
        self._runtime_mcp_config_path: Optional[Path] = None
        self._runtime_agent_profile_path: Optional[Path] = None
        self._uses_native_agent_profile = False
        self._awaiting_user_response = False

    @property
    def paste_enter_count(self) -> int:
        """Copilot submits on a single Enter after paste."""
        return 1

    @staticmethod
    def _clean(output: str) -> str:
        return re.sub(ANSI_CODE_PATTERN, "", output or "")

    def _safe_history(self, tail_lines: Optional[int] = None) -> str:
        try:
            return self._clean(
                tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
            )
        except Exception as exc:
            logger.warning(
                "history read failed for %s:%s: %s",
                self.session_name,
                self.window_name,
                exc,
            )
            return ""

    @staticmethod
    def _looks_like_copilot_ui(text: str) -> bool:
        lower = (text or "").lower()
        return (
            "github copilot" in lower
            or "type @ to mention files" in lower
            or "copilot uses ai" in lower
        )

    @staticmethod
    def _select_permissive_flag() -> str:
        """Pick the strongest available permissive flag for this Copilot version."""
        preferred = os.getenv("CAO_COPILOT_PERMISSIVE_FLAG", "auto").strip().lower()
        try:
            help_text = subprocess.run(
                ["copilot", "--help"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout
        except Exception:
            help_text = ""

        supports_allow_all = "--allow-all" in help_text
        supports_yolo = "--yolo" in help_text

        if preferred == "allow-all" and supports_allow_all:
            return "--allow-all"
        if preferred == "yolo" and supports_yolo:
            return "--yolo"
        if preferred == "none":
            return ""
        if supports_allow_all:
            return "--allow-all"
        if supports_yolo:
            return "--yolo"
        return ""

    @staticmethod
    def _ensure_copilot_config(config_dir: Path, model: str, reasoning_effort: str) -> None:
        """Persist model + reasoning effort defaults for TUI sessions.

        Copilot CLI exposes model via CLI flag, but reasoning effort is currently
        a config setting (`reasoning_effort`) rather than a documented CLI option.
        """
        allowed_efforts = {"low", "medium", "high", "xhigh"}
        config_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = config_dir / "config.json"

        data: dict = {}
        if cfg_path.exists():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}

        if model:
            data["model"] = model
        effort = reasoning_effort.strip().lower()
        if effort in allowed_efforts:
            data["reasoning_effort"] = effort

        cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _sanitize_agent_name(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", (name or "").strip())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip(".-")
        return cleaned or "cao-agent"

    def _runtime_role_guardrails(self) -> str:
        """Return profile-specific runtime guardrails for Copilot."""
        role = (self._agent_profile or "").strip().lower()

        if role == "data_analyst":
            return (
                "CAO runtime guardrails:\n"
                "- Do not call send_message unless the task explicitly asks for it and provides "
                "a receiver_id/callback target.\n"
                "- If no explicit callback target is provided, return the analysis directly in the response.\n"
                "- Do not run shell commands for pure statistical analysis prompts.\n"
            )

        if role == "analysis_supervisor":
            return (
                "CAO runtime guardrails:\n"
                f"- Your supervisor terminal ID is: {self.terminal_id}.\n"
                "- Use assign for analyst work and handoff for report generation.\n"
                "- Do not call send_message to workers unless explicitly requested.\n"
                "- Never run shell commands to discover CAO_TERMINAL_ID.\n"
                "- In worker callback instructions, use the exact supervisor terminal ID above "
                "(not '$CAO_TERMINAL_ID').\n"
                "- Wait for assigned worker callbacks before finalizing the report.\n"
                "- If the task includes Dataset A, Dataset B, and Dataset C, you MUST:\n"
                "  1) run exactly three assign calls (one per dataset),\n"
                "  2) wait for three distinct analyst callbacks,\n"
                "  3) retry assign for any missing dataset callback before finalizing.\n"
                "- Do not produce the final report until all required analyst callbacks arrive.\n"
                "- For multi-dataset reports, include explicit section headers: "
                "'Dataset A', 'Dataset B', 'Dataset C', 'Summary', and 'Conclusion'.\n"
                "- Final output MUST explicitly contain the words 'Report', 'Summary', and "
                "'Conclusion' as section headings.\n"
                "- Start the final response with the exact heading: 'Report Summary'.\n"
            )

        if role == "report_generator":
            return (
                "CAO runtime guardrails:\n"
                "- Return report template/content only.\n"
                "- Do not call assign or send_message unless explicitly requested.\n"
                "- When drafting a multi-dataset report template, include explicit headings: "
                "'Dataset A', 'Dataset B', 'Dataset C', 'Summary', and 'Conclusion'.\n"
                "- Output MUST include the heading 'Report Template' and sections titled "
                "'Summary', 'Analysis', and 'Conclusion'.\n"
            )

        return ""

    def _materialize_runtime_agent_profile(self, config_dir: Path) -> Optional[str]:
        """Write a temporary Copilot custom-agent profile for CAO agent markdown."""
        if not self._agent_profile:
            return None

        try:
            profile = load_agent_profile(self._agent_profile)
        except Exception as exc:
            logger.warning("failed to load agent profile '%s': %s", self._agent_profile, exc)
            return None

        prompt = (profile.system_prompt or "").strip()
        role_guardrails = self._runtime_role_guardrails()
        if role_guardrails:
            prompt = f"{prompt}\n\n{role_guardrails}".strip()
        if not prompt:
            return None

        agent_name = self._sanitize_agent_name(f"cao-{self._agent_profile}-{self.terminal_id}")
        description = (profile.description or f"CAO profile {self._agent_profile}").strip()
        agent_doc = (
            "---\n"
            f"name: {json.dumps(agent_name)}\n"
            f"description: {json.dumps(description)}\n"
            "---\n\n"
            f"{prompt}\n"
        )

        agents_dir = config_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agents_dir / f"{agent_name}.agent.md"
        agent_path.write_text(agent_doc, encoding="utf-8")
        self._runtime_agent_profile_path = agent_path
        return agent_name

    def _command(self) -> str:
        self._uses_native_agent_profile = False
        self._runtime_agent_profile_path = None
        configured = os.getenv("CAO_COPILOT_COMMAND", "").strip()
        if configured:
            return configured

        model = os.getenv("CAO_COPILOT_MODEL", "gpt-5-mini")
        config_dir = Path(
            os.getenv("CAO_COPILOT_CONFIG_DIR", str(Path.home() / ".copilot"))
        ).expanduser()
        autopilot = os.getenv("CAO_COPILOT_AUTOPILOT", "1") == "1"
        allow_all = os.getenv("CAO_COPILOT_ALLOW_ALL", "1") == "1"
        no_custom_instructions = os.getenv("CAO_COPILOT_NO_CUSTOM_INSTRUCTIONS", "1") == "1"
        disable_builtin_mcps = os.getenv("CAO_COPILOT_DISABLE_BUILTIN_MCPS", "0") == "1"
        no_auto_update = os.getenv("CAO_COPILOT_NO_AUTO_UPDATE", "1") == "1"
        no_ask_user = os.getenv("CAO_COPILOT_NO_ASK_USER", "1") == "1"
        reasoning_effort = os.getenv("CAO_COPILOT_REASONING_EFFORT", "high")
        additional_mcp_config = os.getenv("CAO_COPILOT_ADDITIONAL_MCP_CONFIG", "").strip()
        add_dirs = os.getenv("CAO_COPILOT_ADD_DIRS", "").strip()
        try:
            help_text = subprocess.run(
                ["copilot", "--help"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout
        except Exception:
            help_text = ""
        supports = lambda flag: flag in help_text

        try:
            self._ensure_copilot_config(config_dir, model, reasoning_effort)
        except Exception as exc:
            logger.warning("failed to update Copilot config: %s", exc)

        command_parts = ["copilot"]
        if allow_all:
            permissive_flag = self._select_permissive_flag()
            if permissive_flag:
                command_parts.append(permissive_flag)

        if no_custom_instructions and supports("--no-custom-instructions"):
            command_parts.append("--no-custom-instructions")
        if disable_builtin_mcps and supports("--disable-builtin-mcps"):
            command_parts.append("--disable-builtin-mcps")
        # Copilot CLI currently rejects the --autopilot + --no-auto-update
        # combination with "unknown option '--autopilot'".
        if no_auto_update and supports("--no-auto-update"):
            if not (autopilot and supports("--autopilot")):
                command_parts.append("--no-auto-update")
        if no_ask_user and supports("--no-ask-user"):
            command_parts.append("--no-ask-user")

        if self._agent_profile and supports("--agent"):
            native_agent_name = self._materialize_runtime_agent_profile(config_dir)
            command_parts.extend(["--agent", native_agent_name or self._agent_profile])
            self._uses_native_agent_profile = True

        command_parts.extend(["--model", model, "--config-dir", str(config_dir)])

        dir_items = [item for item in add_dirs.split(":") if item] if add_dirs else []
        if not dir_items:
            dir_items = [os.getcwd()]
        for directory in dir_items:
            command_parts.extend(["--add-dir", directory])

        additional_mcp_config = self._build_runtime_mcp_config(additional_mcp_config)
        if additional_mcp_config:
            command_parts.extend(["--additional-mcp-config", f"@{additional_mcp_config}"])
        if autopilot and supports("--autopilot"):
            command_parts.append("--autopilot")

        return shlex.join(command_parts)

    @staticmethod
    def _load_mcp_servers_from_file(path: str) -> dict:
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                servers = loaded.get("mcpServers", {})
                if isinstance(servers, dict):
                    return servers
        except Exception:
            return {}
        return {}

    def _build_runtime_mcp_config(self, additional_mcp_config: str) -> str:
        merged_servers: dict = {}
        if additional_mcp_config:
            merged_servers.update(self._load_mcp_servers_from_file(additional_mcp_config))

        # Copilot runs in a clean shell where PATH may not include the CAO venv.
        # Resolve a deterministic command path for cao-mcp-server.
        configured_command = os.getenv("CAO_COPILOT_CAO_MCP_COMMAND", "").strip()
        if configured_command:
            command_parts = shlex.split(configured_command)
            mcp_command = command_parts[0]
            mcp_args = command_parts[1:]
        else:
            venv_script = Path(sys.executable).with_name("cao-mcp-server")
            found_script = shutil.which("cao-mcp-server")
            if venv_script.exists():
                mcp_command = str(venv_script)
                mcp_args = []
            elif found_script:
                mcp_command = found_script
                mcp_args = []
            else:
                # Final fallback: invoke the module with the current interpreter.
                mcp_command = sys.executable
                mcp_args = ["-m", "cli_agent_orchestrator.mcp_server.server"]

        merged_servers["cao-mcp-server"] = {
            "command": mcp_command,
            "args": mcp_args,
            "disabled": False,
            "env": {
                "CAO_TERMINAL_ID": self.terminal_id,
            },
        }

        if not merged_servers:
            return additional_mcp_config

        cfg_path = Path(tempfile.gettempdir()) / f"cao_copilot_mcp_{self.terminal_id}.json"
        cfg_path.write_text(
            json.dumps({"mcpServers": merged_servers}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self._runtime_mcp_config_path = cfg_path
        return str(cfg_path)

    def _send_enter(self) -> None:
        tmux_client.send_special_key(self.session_name, self.window_name, "Enter")

    def _accept_trust_prompts(self, timeout: float = 30.0) -> None:
        start = time.time()
        while time.time() - start < timeout:
            raw_content = self._safe_history(tail_lines=120)
            content = raw_content.lower()

            # Numbered trust selector shown in newer Copilot builds.
            if (
                "confirm folder trust" in content
                and re.search(r"\b1\.\s*yes\b", content)
                and re.search(r"\b2\.\s*yes,\s*and remember", content)
            ):
                tmux_client.send_keys(self.session_name, self.window_name, "1")
                self._send_enter()
                time.sleep(1)
                continue

            if "do you trust all the actions in this folder" in content:
                tmux_client.send_keys(self.session_name, self.window_name, "Y")
                self._send_enter()
                time.sleep(1)
                continue

            if (
                "confirm folder trust" in content
                or re.search(r"do you trust[\s\S]{0,200}files[\s\S]{0,200}folder", content)
                or re.search(r"do you trust[\s\S]{0,200}contents[\s\S]{0,200}directory", content)
            ):
                self._send_enter()
                time.sleep(1)
                continue

            if re.search(r"\[\s*y/n\s*\]", content, re.IGNORECASE):
                tmux_client.send_keys(self.session_name, self.window_name, "Y")
                self._send_enter()
                time.sleep(1)
                continue

            if "press enter to continue" in content:
                self._send_enter()
                time.sleep(1)
                continue

            if re.search(WAITING_PROMPT_PATTERN, content, re.IGNORECASE):
                tmux_client.send_keys(self.session_name, self.window_name, "Y")
                self._send_enter()
                time.sleep(1)
                continue

            if self._looks_like_copilot_ui(raw_content):
                return
            if self._has_idle_prompt_near_end(raw_content.splitlines()):
                return
            time.sleep(1)

    def initialize(self) -> bool:
        """Initialize Copilot provider by starting copilot command."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        tmux_client.send_keys(self.session_name, self.window_name, self._command())
        self._accept_trust_prompts(timeout=30.0)
        startup_probe = self._safe_history(tail_lines=200)
        startup_has_waiting_prompt = bool(
            re.search(WAITING_PROMPT_PATTERN, startup_probe, re.IGNORECASE)
        )
        if startup_has_waiting_prompt:
            # One additional sweep for numbered trust prompts where the first
            # acceptance can still leave the UI in a consent state.
            self._accept_trust_prompts(timeout=10.0)
            startup_probe = self._safe_history(tail_lines=200)
            startup_has_waiting_prompt = bool(
                re.search(WAITING_PROMPT_PATTERN, startup_probe, re.IGNORECASE)
            )

        startup_lines = startup_probe.splitlines()
        startup_has_idle_prompt = self._has_idle_prompt_near_end(startup_lines)
        startup_is_loading = self._has_loading_environment_near_end(startup_lines)
        if (
            self._looks_like_copilot_ui(startup_probe)
            and startup_has_idle_prompt
            and not startup_has_waiting_prompt
            and not startup_is_loading
        ):
            self._initialized = True
            if self._agent_profile and not self._uses_native_agent_profile:
                self._inject_agent_profile_prompt()
            return True

        is_ready = wait_until_status(self, TerminalStatus.IDLE, timeout=45.0, polling_interval=1.0)
        if not is_ready:
            is_ready = wait_until_status(
                self, TerminalStatus.COMPLETED, timeout=10.0, polling_interval=1.0
            )
        if not is_ready:
            # One more trust/consent sweep before failing initialization.
            self._accept_trust_prompts(timeout=10.0)
            is_ready = wait_until_status(
                self, TerminalStatus.IDLE, timeout=15.0, polling_interval=1.0
            )
            if not is_ready:
                is_ready = wait_until_status(
                    self, TerminalStatus.COMPLETED, timeout=10.0, polling_interval=1.0
                )
            if not is_ready:
                fallback = self._safe_history(tail_lines=200)
                fallback_has_waiting_prompt = bool(
                    re.search(WAITING_PROMPT_PATTERN, fallback, re.IGNORECASE)
                )
                fallback_lines = fallback.splitlines()
                fallback_has_idle_prompt = self._has_idle_prompt_near_end(fallback_lines)
                fallback_is_loading = self._has_loading_environment_near_end(fallback_lines)
                if not (
                    self._looks_like_copilot_ui(fallback)
                    and fallback_has_idle_prompt
                    and not fallback_has_waiting_prompt
                    and not fallback_is_loading
                ):
                    raise TimeoutError("Copilot initialization timed out after 60 seconds")

        self._initialized = True
        if self._agent_profile and not self._uses_native_agent_profile:
            self._inject_agent_profile_prompt()
        return True

    def _inject_agent_profile_prompt(self) -> None:
        if not self._agent_profile:
            return

        try:
            profile = load_agent_profile(self._agent_profile)
        except Exception as exc:
            logger.warning("failed to load agent profile '%s': %s", self._agent_profile, exc)
            return

        system_prompt = (profile.system_prompt or "").strip()
        if not system_prompt:
            return

        tmux_client.send_keys_via_paste(self.session_name, self.window_name, system_prompt)
        deadline = time.time() + 180.0
        while time.time() < deadline:
            status = self.get_status()
            if status in (TerminalStatus.COMPLETED, TerminalStatus.IDLE, TerminalStatus.ERROR):
                return
            time.sleep(1.0)

    @staticmethod
    def _find_last_user_line(lines: list[str]) -> int:
        last_user = -1
        for idx, line in enumerate(lines):
            if not re.match(USER_PROMPT_LINE_PATTERN, line):
                continue
            stripped = line.strip()
            if stripped in {"❯", ">", "›"}:
                continue
            # Ignore static instruction footer line shown at idle.
            if "type @ to mention files" in stripped.lower():
                continue
            last_user = idx
        return last_user

    @staticmethod
    def _is_footer_line(line: str) -> bool:
        stripped = line.strip().lower()
        if not stripped:
            return True
        if re.match(r"^[─-]{8,}$", stripped):
            return True
        if "shift+tab switch mode" in stripped:
            return True
        if "shift+tab cycle mode" in stripped:
            return True
        if "remaining reqs" in stripped:
            return True
        if "? for shortcuts" in stripped:
            return True
        # Copilot can wrap the footer hint and render "shortcuts" on a separate line.
        if stripped == "shortcuts":
            return True
        if "gpt-" in stripped and ("[⎇" in stripped or "(0x)" in stripped):
            return True
        if "type @ to mention files" in stripped:
            return True
        if stripped.startswith("╭") or stripped.startswith("╰") or stripped.startswith("│"):
            return True
        return False

    @classmethod
    def _is_non_response_noise_line(cls, line: str) -> bool:
        stripped = line.strip().lower()
        content = re.sub(r"^[●◐◑◒◓◉]\s*", "", stripped).strip()
        if not stripped:
            return True
        if cls._is_footer_line(line):
            return True
        if re.match(USER_PROMPT_LINE_PATTERN, line):
            return True
        if stripped.startswith("➜") or stripped.startswith("~/") or stripped.startswith("/"):
            return True
        if re.match(r"^[╭╰│─-]+$", stripped):
            return True
        if any(hint in content for hint in STARTUP_HINT_PATTERNS):
            return True
        if "loaded env" in content:
            return True
        if "environment loaded" in content:
            return True
        if "loading environment" in content:
            return True
        if re.search(r"loading\s+envi\w*\s*ronment", content):
            return True
        if content.startswith("error auto updating:"):
            return True
        if re.match(r"^\\d{4}-\\d{2}-\\d{2}t\\d{2}:", content) and "[error]" in content:
            return True
        if "[mcp server" in content:
            return True
        if "starting mcp client for" in content:
            return True
        if "mcp client for" in content and (
            "connected" in content or "closed" in content or "errored" in content
        ):
            return True
        if "recorded failure for server" in content:
            return True
        if content.startswith("total usage est:"):
            return True
        if content.startswith("api time spent:"):
            return True
        if content.startswith("total session time:"):
            return True
        if content.startswith("total code changes:"):
            return True
        if content.startswith("breakdown by ai model:"):
            return True
        if content.startswith("resume this session with copilot --resume="):
            return True
        if content.startswith("/exit, /quit"):
            return True
        if content.startswith("%"):
            return True
        return False

    @classmethod
    def _meaningful_response_lines(cls, lines: list[str]) -> list[str]:
        return [line for line in lines if not cls._is_non_response_noise_line(line)]

    @classmethod
    def _looks_like_startup_output(cls, lines: list[str]) -> bool:
        meaningful: list[str] = []
        for raw in lines:
            if cls._is_non_response_noise_line(raw):
                continue
            line = raw.strip().lower()
            if not line:
                continue
            meaningful.append(line)
        return not meaningful

    def _reset_completion_candidate(self) -> None:
        self._completion_candidate_signature = ""
        self._completion_candidate_since = 0.0

    @classmethod
    def _completion_signature(cls, lines: list[str]) -> str:
        """Build a stability signature from semantic response lines only."""
        meaningful = cls._meaningful_response_lines(lines)
        normalized: list[str] = []
        for raw in meaningful[-160:]:
            stripped = raw.strip()
            if not stripped:
                continue
            normalized.append(re.sub(r"\s+", " ", stripped))
        return "\n".join(normalized[-120:])

    def _is_completion_stable(self, lines: list[str]) -> bool:
        if not self._initialized:
            return True

        signature = self._completion_signature(lines)
        now = time.time()
        if signature != self._completion_candidate_signature:
            self._completion_candidate_signature = signature
            self._completion_candidate_since = now
            return False
        return (now - self._completion_candidate_since) >= 2.0

    @classmethod
    def _has_idle_prompt_near_end(cls, lines: list[str]) -> bool:
        if not lines:
            return False
        tail = lines[-25:]
        last_prompt_idx = -1
        for idx, line in enumerate(tail):
            if re.match(r"^\s*(?:❯|›|copilot>)(?:\s+.*)?$", line) or re.match(
                BARE_ASCII_PROMPT_PATTERN, line
            ):
                last_prompt_idx = idx
        if last_prompt_idx < 0:
            return False

        for line in tail[last_prompt_idx + 1 :]:
            if cls._is_footer_line(line):
                continue
            if line.strip():
                return False

        recent_block = "\n".join(tail[max(0, last_prompt_idx - 6) : last_prompt_idx + 1])
        if re.search(SPINNER_PATTERN, recent_block, re.IGNORECASE):
            return False

        return True

    @staticmethod
    def _has_shell_prompt_near_end(lines: list[str]) -> bool:
        """Detect a regular shell prompt after Copilot exits."""
        if not lines:
            return False
        tail = [line.strip() for line in lines[-20:] if line.strip()]
        if not tail:
            return False
        return any(
            re.match(r"^(?:➜|\$|#)\s+", line) or re.match(r"^.+\s+git:\(.+\)", line)
            for line in tail[-4:]
        )

    @staticmethod
    def _has_loading_environment_near_end(lines: list[str]) -> bool:
        if not lines:
            return False
        tail = "\n".join(lines[-20:])
        return bool(re.search(r"\bloading\s+environment\b", tail, re.IGNORECASE))

    @classmethod
    def _trim_tail_prompts(cls, lines: list[str]) -> list[str]:
        trimmed = list(lines)
        while trimmed:
            tail = trimmed[-1].strip()
            if not tail:
                trimmed.pop()
                continue
            if cls._is_footer_line(tail):
                trimmed.pop()
                continue
            if re.match(r"^(?:❯|›|copilot>)(?:\s+.*)?$", tail) or re.match(
                BARE_ASCII_PROMPT_PATTERN, tail
            ):
                trimmed.pop()
                continue
            break
        return trimmed

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get provider status by analyzing terminal output."""
        clean_output = self._safe_history(tail_lines=tail_lines)

        if not clean_output.strip():
            return TerminalStatus.PROCESSING

        lines = clean_output.splitlines()
        if self._has_shell_prompt_near_end(lines):
            self._reset_completion_candidate()
            if self._awaiting_user_response:
                self._awaiting_user_response = False
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        has_idle_prompt_at_end = self._has_idle_prompt_near_end(lines)
        tail_output = "\n".join(lines[-40:])
        waiting_now = bool(
            re.search(
                r"(?:do you trust all the actions in this folder|confirm folder trust|"
                r"press enter to continue|\[\s*y/n\s*\]:\s*$)",
                tail_output,
                re.IGNORECASE | re.MULTILINE,
            )
        )
        if waiting_now and not has_idle_prompt_at_end:
            self._reset_completion_candidate()
            # Trust prompts can re-appear after initialize() (for example after
            # custom agent selection). Attempt best-effort auto-accept while in
            # runtime status polling to avoid getting stuck in WAITING forever.
            if self._initialized:
                try:
                    self._accept_trust_prompts(timeout=3.0)
                except Exception as exc:
                    logger.debug("trust prompt auto-accept during status polling failed: %s", exc)

                refreshed_output = self._safe_history(tail_lines=120)
                refreshed_lines = refreshed_output.splitlines()
                refreshed_tail = "\n".join(refreshed_lines[-40:])
                refreshed_waiting = bool(
                    re.search(
                        r"(?:do you trust all the actions in this folder|confirm folder trust|"
                        r"press enter to continue|\[\s*y/n\s*\]:\s*$)",
                        refreshed_tail,
                        re.IGNORECASE | re.MULTILINE,
                    )
                )
                if self._has_idle_prompt_near_end(refreshed_lines) and not refreshed_waiting:
                    return TerminalStatus.IDLE
            return TerminalStatus.WAITING_USER_ANSWER

        if self._has_loading_environment_near_end(lines):
            self._reset_completion_candidate()
            return TerminalStatus.PROCESSING

        last_user = self._find_last_user_line(lines)

        if not has_idle_prompt_at_end:
            self._reset_completion_candidate()
            # While command is running, avoid false ERROR unless explicit failure is clear.
            if last_user >= 0:
                post = self._meaningful_response_lines(lines[last_user + 1 :])
                if re.search(ERROR_PATTERN, "\n".join(post), re.IGNORECASE):
                    self._awaiting_user_response = False
                    return TerminalStatus.ERROR
            return TerminalStatus.PROCESSING

        if last_user < 0:
            # Startup/log churn can include wrapped warning lines (for example
            # auto-update rate-limit notices) that should not block readiness.
            if self._looks_like_copilot_ui(clean_output):
                if "error auto updating:" in clean_output.lower():
                    self._reset_completion_candidate()
                    return TerminalStatus.IDLE
                meaningful = self._meaningful_response_lines(lines)
                if not meaningful:
                    self._reset_completion_candidate()
                    return TerminalStatus.IDLE

            startup_filtered = self._trim_tail_prompts([line for line in lines if line.strip()])
            if not startup_filtered:
                self._reset_completion_candidate()
                return TerminalStatus.IDLE
            if self._looks_like_startup_output(startup_filtered):
                self._reset_completion_candidate()
                return TerminalStatus.IDLE
            if re.search(ERROR_PATTERN, "\n".join(startup_filtered), re.IGNORECASE):
                self._reset_completion_candidate()
                return TerminalStatus.ERROR
            if not self._is_completion_stable(lines):
                return TerminalStatus.PROCESSING
            return TerminalStatus.COMPLETED

        post_lines = self._trim_tail_prompts(
            [line for line in lines[last_user + 1 :] if line.strip()]
        )
        if not post_lines:
            if self._awaiting_user_response:
                if not self._is_completion_stable(lines):
                    return TerminalStatus.PROCESSING
                self._awaiting_user_response = False
                return TerminalStatus.COMPLETED
            self._reset_completion_candidate()
            return TerminalStatus.IDLE

        meaningful_lines = self._meaningful_response_lines(post_lines)
        if not meaningful_lines:
            if self._awaiting_user_response:
                if not self._is_completion_stable(lines):
                    return TerminalStatus.PROCESSING
                self._awaiting_user_response = False
                return TerminalStatus.COMPLETED
            self._reset_completion_candidate()
            return TerminalStatus.IDLE

        post_text = "\n".join(meaningful_lines)

        if re.search(ERROR_PATTERN, post_text, re.IGNORECASE):
            self._reset_completion_candidate()
            self._awaiting_user_response = False
            # If tool produced any assistant-style text before error, still treat as completion.
            if re.search(ASSISTANT_PREFIX_PATTERN, post_text, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.COMPLETED
            return TerminalStatus.ERROR

        # Copilot often does not print explicit assistant prefixes; any non-empty post-user body
        # with idle prompt indicates completion for CAO's handoff lifecycle.
        if not self._is_completion_stable(lines):
            return TerminalStatus.PROCESSING
        self._awaiting_user_response = False
        return TerminalStatus.COMPLETED

    def get_idle_pattern_for_log(self) -> str:
        """Return IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract final response content from terminal output."""
        clean_output = self._clean(script_output)
        lines = clean_output.splitlines()
        last_user = self._find_last_user_line(lines)

        if last_user >= 0:
            post = self._trim_tail_prompts(
                [line for line in lines[last_user + 1 :] if line.strip()]
            )
            message = "\n".join(post).strip()
            if message:
                return message

        # Fallback for label-based output.
        matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        if matches:
            start_pos = matches[-1].start()
            tail = clean_output[start_pos:]
            lines = [line for line in tail.splitlines() if line.strip()]
            if lines:
                lines[0] = re.sub(
                    r"^(?:assistant\s*:|[●◐◑◒◓◉]\s*)",
                    "",
                    lines[0],
                    flags=re.IGNORECASE,
                ).strip()
            trimmed = self._trim_tail_prompts(lines)
            message = "\n".join(trimmed).strip()
            if message:
                return message

        # Final fallback: recover the most recent meaningful text block.
        meaningful = self._meaningful_response_lines(clean_output.splitlines())
        if meaningful:
            tail = "\n".join([line.rstrip() for line in meaningful[-120:]]).strip()
            if tail:
                return tail

        raise ValueError("No provider response content found in terminal output")

    def exit_cli(self) -> str:
        """Get provider exit command."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up provider state."""
        self._initialized = False
        self._awaiting_user_response = False
        self._reset_completion_candidate()
        if self._runtime_mcp_config_path:
            try:
                self._runtime_mcp_config_path.unlink(missing_ok=True)
            except Exception:
                pass
            self._runtime_mcp_config_path = None
        self._uses_native_agent_profile = False
        if self._runtime_agent_profile_path:
            try:
                self._runtime_agent_profile_path.unlink(missing_ok=True)
            except Exception:
                pass
            self._runtime_agent_profile_path = None

    def mark_input_received(self) -> None:
        """Mark that a user task was submitted and a response is expected."""
        self._awaiting_user_response = True
        self._reset_completion_candidate()
