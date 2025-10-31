"""Generate test fixture files with proper ANSI escape sequences."""

import os
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent

# Define fixtures with proper ANSI escape codes
fixtures = {
    "q_cli_idle_output.txt": "\x1b[36m[developer]\x1b[35m>\x1b[39m ",
    "q_cli_completed_output.txt": (
        "$ q chat --agent developer\n"
        "\x1b[38;5;10m> \x1b[39mHere is a comprehensive response to your query.\n\n"
        "This response includes multiple paragraphs and demonstrates\n"
        "how Q CLI formats its output with proper spacing and structure.\n\n"
        "The response can include:\n"
        "- Bullet points\n"
        "- Code examples\n"
        "- Multiple sections\n\n"
        "\x1b[36m[developer]\x1b[35m>\x1b[39m "
    ),
    "q_cli_processing_output.txt": (
        "$ q chat --agent developer\n"
        "User input received, processing your request..."
    ),
    "q_cli_permission_output.txt": (
        "$ q chat --agent developer\n"
        "\x1b[38;5;10m> \x1b[39mI'd like to execute a command that requires your permission.\n\n"
        "Allow this action? [y/n/t]:\x1b[39m \x1b[36m[developer]\x1b[35m>\x1b[39m "
    ),
    "q_cli_error_output.txt": (
        "$ q chat --agent developer\n"
        "Amazon Q is having trouble responding right now. Please try again later.\n\n"
        "\x1b[36m[developer]\x1b[35m>\x1b[39m "
    ),
    "q_cli_complex_response.txt": (
        "$ q chat --agent developer\n"
        "\x1b[38;5;10m> \x1b[39mHere's a detailed response with code examples.\n\n"
        "First, let me explain the concept:\n\n"
        "## Python Example\n\n"
        "```python\n"
        "def hello_world():\n"
        '    print("Hello, World!")\n'
        "    return True\n"
        "```\n\n"
        "## JavaScript Example\n\n"
        "```javascript\n"
        "function helloWorld() {\n"
        '    console.log("Hello, World!");\n'
        "    return true;\n"
        "}\n"
        "```\n\n"
        "Key points to consider:\n"
        "1. Function naming conventions\n"
        "2. Return values\n"
        "3. Side effects\n\n"
        "Additional notes with special characters:\n"
        "- Emoji support: 🚀 ✅ 🎉\n"
        "- Unicode: café, naïve, 日本語\n"
        "- Control chars: \x1b[K\x1b[2J\n\n"
        "Let me know if you need more details!\n\n"
        "\x1b[36m[developer]\x1b[35m>\x1b[39m "
    ),
}

if __name__ == "__main__":
    for filename, content in fixtures.items():
        filepath = FIXTURES_DIR / filename
        with open(filepath, "w") as f:
            f.write(content)
        print(f"Generated: {filename}")
