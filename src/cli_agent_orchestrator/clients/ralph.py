"""RalphRunner - Iterative loop management for CAO."""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

DEFAULT_STATE_PATH = Path.home() / ".kiro" / "ralph-loop.local.json"

@dataclass
class RalphState:
    id: str
    prompt: str
    iteration: int = 1
    minIterations: int = 3
    maxIterations: int = 10
    completionPromise: str = "COMPLETE"
    status: str = "running"
    startedAt: Optional[str] = None
    taskId: Optional[str] = None
    workDir: Optional[str] = None
    previousFeedback: Optional[dict] = None
    active: bool = True

class RalphRunner:
    def __init__(self, state_path: Path = DEFAULT_STATE_PATH):
        self.state_path = state_path

    def _load_state(self) -> Optional[RalphState]:
        if not self.state_path.exists():
            return None
        data = json.loads(self.state_path.read_text())
        return RalphState(**data)

    def _save_state(self, state: RalphState):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(state), indent=2))

    def start(self, prompt: str, min_iter: int = 3, max_iter: int = 10, 
              promise: str = "COMPLETE", task_id: Optional[str] = None,
              work_dir: Optional[str] = None) -> RalphState:
        state = RalphState(
            id=f"ralph-{uuid.uuid4().hex[:8]}",
            prompt=prompt,
            minIterations=min_iter,
            maxIterations=max_iter,
            completionPromise=promise,
            startedAt=datetime.utcnow().isoformat() + "Z",
            taskId=task_id,
            workDir=work_dir
        )
        self._save_state(state)
        return state

    def status(self) -> Optional[RalphState]:
        return self._load_state()

    def stop(self) -> bool:
        state = self._load_state()
        if state:
            state.status = "stopped"
            state.active = False
            self._save_state(state)
            return True
        return False

    def feedback(self, score: int, summary: str, improvements: list = None,
                 next_steps: list = None, ideas: list = None, blockers: list = None) -> Optional[RalphState]:
        state = self._load_state()
        if not state:
            return None
        state.previousFeedback = {
            "qualityScore": score,
            "qualitySummary": summary,
            "improvements": improvements or [],
            "nextSteps": next_steps or [],
            "ideas": ideas or [],
            "blockers": blockers or []
        }
        state.iteration += 1
        if state.iteration > state.maxIterations:
            state.status = "max_iterations"
            state.active = False
        self._save_state(state)
        return state

    def complete(self) -> Optional[RalphState]:
        state = self._load_state()
        if state:
            state.status = "completed"
            state.active = False
            self._save_state(state)
        return state
