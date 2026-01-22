"""
Self-Learning Context System for CAO
Based on ReasoningBank + ACE architecture
"""
import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

# Storage paths
LEARNING_DIR = Path.home() / ".cao" / "learning"
MEMORY_FILE = LEARNING_DIR / "memory_bank.json"
CONTEXT_FILE = LEARNING_DIR / "context_deltas.json"

LEARNING_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Memory:
    id: str
    title: str
    description: str
    content: str
    source: str  # session_id or "human"
    outcome: str  # "success", "failure", "neutral"
    human_validated: bool = False
    created_at: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.id:
            self.id = f"mem-{hashlib.md5(self.content.encode()).hexdigest()[:8]}"


@dataclass 
class ContextDelta:
    id: str
    bullets: list[str]
    source: str
    status: str = "pending"  # pending, approved, rejected
    human_feedback: Optional[str] = None
    created_at: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class LearningSystem:
    def __init__(self):
        self.memories: list[dict] = self._load_json(MEMORY_FILE, [])
        self.deltas: list[dict] = self._load_json(CONTEXT_FILE, [])
    
    def _load_json(self, path: Path, default):
        if path.exists():
            return json.loads(path.read_text())
        return default
    
    def _save(self):
        MEMORY_FILE.write_text(json.dumps(self.memories, indent=2))
        CONTEXT_FILE.write_text(json.dumps(self.deltas, indent=2))
    
    # ─────────────────────────────────────────────────────────
    # EXTRACTION - Analyze session output for learnings
    # ─────────────────────────────────────────────────────────
    
    def extract_from_session(self, session_id: str, output: str, outcome: str = "neutral") -> dict:
        """Extract structured memories and context deltas from session output."""
        
        memories = []
        bullets = []
        
        # Extract tool usage patterns
        tools = re.findall(r'<invoke name="([^"]+)"', output)
        tool_counts = {}
        for t in tools:
            tool_counts[t] = tool_counts.get(t, 0) + 1
        
        if tool_counts:
            top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:5]
            memories.append(Memory(
                id="",
                title="Frequently used tools",
                description=f"Tools used in session {session_id[-8:]}",
                content=f"Most used: {', '.join(f'{t}({c}x)' for t,c in top_tools)}",
                source=f"session:{session_id}",
                outcome=outcome
            ))
        
        # Extract errors and solutions
        errors = re.findall(r'(?:Error|Exception|Failed|error):\s*(.{10,100})', output)
        for err in errors[:3]:
            memories.append(Memory(
                id="",
                title=f"Error encountered: {err[:30]}...",
                description="Error pattern to handle",
                content=f"Failed approach (avoid): {err}",
                source=f"session:{session_id}",
                outcome="failure"
            ))
            bullets.append(f"Handle error: {err[:50]}")
        
        # Extract file modifications
        files = re.findall(r'(?:modified|created|wrote).*?([/\w.-]+\.\w+)', output, re.I)
        if files:
            bullets.append(f"Files commonly modified: {', '.join(set(files)[:5])}")
        
        # Extract successful patterns
        if "success" in output.lower() or outcome == "success":
            # Look for command patterns that worked
            cmds = re.findall(r'\$ ([^\n]+)', output)
            for cmd in cmds[:3]:
                if len(cmd) > 10:
                    memories.append(Memory(
                        id="",
                        title=f"Working command pattern",
                        description="Command that succeeded",
                        content=cmd[:200],
                        source=f"session:{session_id}",
                        outcome="success"
                    ))
        
        # Create context delta
        delta = None
        if bullets:
            delta = ContextDelta(
                id=f"delta-{session_id[-8:]}",
                bullets=bullets,
                source=f"session:{session_id}"
            )
        
        return {
            "memories": [asdict(m) for m in memories],
            "delta": asdict(delta) if delta else None
        }
    
    # ─────────────────────────────────────────────────────────
    # HUMAN FEEDBACK - Process approvals, rejections, edits
    # ─────────────────────────────────────────────────────────
    
    def add_human_memory(self, title: str, content: str, outcome: str = "success") -> dict:
        """Add a memory directly from human input (highest priority)."""
        mem = Memory(
            id="",
            title=title,
            description="Human-provided guidance",
            content=content,
            source="human",
            outcome=outcome,
            human_validated=True
        )
        self.memories.append(asdict(mem))
        self._save()
        return asdict(mem)
    
    def approve_delta(self, delta_id: str, feedback: str = None) -> dict:
        """Approve a context delta and merge into active context."""
        for d in self.deltas:
            if d["id"] == delta_id:
                d["status"] = "approved"
                d["human_feedback"] = feedback
                d["human_validated"] = True
                self._save()
                return d
        return None
    
    def reject_delta(self, delta_id: str, feedback: str = None) -> dict:
        """Reject a context delta with optional feedback."""
        for d in self.deltas:
            if d["id"] == delta_id:
                d["status"] = "rejected"
                d["human_feedback"] = feedback
                self._save()
                return d
        return None
    
    def edit_delta(self, delta_id: str, new_bullets: list[str]) -> dict:
        """Edit a delta's bullets (human refinement)."""
        for d in self.deltas:
            if d["id"] == delta_id:
                d["bullets"] = new_bullets
                d["human_validated"] = True
                self._save()
                return d
        return None
    
    # ─────────────────────────────────────────────────────────
    # RETRIEVAL - Get relevant memories for a query
    # ─────────────────────────────────────────────────────────
    
    def get_relevant_memories(self, query: str, limit: int = 5) -> list[dict]:
        """Simple keyword-based retrieval (upgrade to embeddings later)."""
        query_words = set(query.lower().split())
        scored = []
        
        for mem in self.memories:
            content = f"{mem['title']} {mem['description']} {mem['content']}".lower()
            score = len(query_words & set(content.split()))
            # Boost human-validated memories
            if mem.get("human_validated"):
                score *= 2
            # Boost successful outcomes
            if mem.get("outcome") == "success":
                score *= 1.5
            if score > 0:
                scored.append((score, mem))
        
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:limit]]
    
    def get_active_context(self) -> list[str]:
        """Get all approved context bullets."""
        bullets = []
        for d in self.deltas:
            if d["status"] == "approved":
                bullets.extend(d["bullets"])
        return bullets
    
    # ─────────────────────────────────────────────────────────
    # PROPOSALS - Create and manage learning proposals
    # ─────────────────────────────────────────────────────────
    
    def create_proposal(self, session_id: str, output: str, outcome: str = "neutral") -> dict:
        """Create a learning proposal from session for human review."""
        extraction = self.extract_from_session(session_id, output, outcome)
        
        proposal = {
            "id": f"prop-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "session_id": session_id,
            "memories": extraction["memories"],
            "delta": extraction["delta"],
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }
        
        # Store delta for review
        if extraction["delta"]:
            self.deltas.append(extraction["delta"])
            self._save()
        
        return proposal
    
    def apply_proposal(self, proposal: dict) -> dict:
        """Apply an approved proposal - add memories to bank."""
        added = []
        for mem in proposal.get("memories", []):
            mem["human_validated"] = True
            self.memories.append(mem)
            added.append(mem["id"])
        self._save()
        return {"added_memories": added}
    
    # ─────────────────────────────────────────────────────────
    # STATS
    # ─────────────────────────────────────────────────────────
    
    def stats(self) -> dict:
        return {
            "total_memories": len(self.memories),
            "human_validated": sum(1 for m in self.memories if m.get("human_validated")),
            "success_memories": sum(1 for m in self.memories if m.get("outcome") == "success"),
            "failure_memories": sum(1 for m in self.memories if m.get("outcome") == "failure"),
            "pending_deltas": sum(1 for d in self.deltas if d["status"] == "pending"),
            "approved_deltas": sum(1 for d in self.deltas if d["status"] == "approved"),
            "active_context_bullets": len(self.get_active_context())
        }


# Singleton instance
learning_system = LearningSystem()
