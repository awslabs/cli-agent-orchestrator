#!/usr/bin/env python3
"""Script to create handoff test fixtures with proper ANSI codes."""

from pathlib import Path

# ANSI color codes
GREEN_ARROW = "\x1b[38;5;10m> \x1b[39m"
AGENT_PROMPT_START = "\x1b[36m["
AGENT_PROMPT_END = "]\x1b[35m>\x1b[39m "

def create_handoff_successful():
    """Create successful handoff fixture."""
    content = f"""$ q chat --agent supervisor
What task would you like to delegate?
{GREEN_ARROW}I'll delegate this task to the developer agent for implementation.

Initiating handoff to developer agent...

{GREEN_ARROW}Handoff completed successfully. The developer agent will handle the implementation.
{AGENT_PROMPT_START}supervisor{AGENT_PROMPT_END}
"""
    return content

def create_handoff_error():
    """Create failed handoff fixture."""
    content = f"""$ q chat --agent supervisor
Please handle this complex task.
{GREEN_ARROW}Attempting to delegate to the specialist agent...

Error: Unable to complete handoff - target agent is not available.

Amazon Q is having trouble responding right now. Please try again.
{AGENT_PROMPT_START}supervisor{AGENT_PROMPT_END}
"""
    return content

def create_handoff_with_permission():
    """Create handoff with permission prompt fixture."""
    content = f"""$ q chat --agent supervisor
Delegate this task to another agent.
{GREEN_ARROW}I can delegate this to the developer agent. This will require accessing their workspace.

Allow this action? [y/n/t]:\x1b[39m {AGENT_PROMPT_START}supervisor{AGENT_PROMPT_END}
"""
    return content

if __name__ == "__main__":
    fixtures_dir = Path(__file__).parent
    
    # Create fixtures
    fixtures = {
        "q_cli_handoff_successful.txt": create_handoff_successful(),
        "q_cli_handoff_error.txt": create_handoff_error(),
        "q_cli_handoff_with_permission.txt": create_handoff_with_permission(),
    }
    
    for filename, content in fixtures.items():
        filepath = fixtures_dir / filename
        with open(filepath, 'w') as f:
            f.write(content)
        print(f"Created {filename}")
    
    print("\nHandoff fixtures created successfully!")
