"""Event bus utilities."""


def terminal_id_from_topic(topic: str) -> str:
    """Extract terminal ID from event topic (e.g., 'terminal.abc123.output' → 'abc123')."""
    return topic.split(".")[1]
