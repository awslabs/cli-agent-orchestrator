# Working Directory Support

CAO supports specifying working directories for agent handoff/delegation operations.

## Configuration

Enable working directory parameter in MCP tools:

```bash
export CAO_ENABLE_WORKING_DIRECTORY=true
```

## Behavior

- **When disabled (default)**: Working directory parameter is hidden from tools, agents start in supervisor's current directory
- **When enabled**: Tools expose `working_directory` parameter, allowing explicit directory specification
- **Default directory**: Current working directory (`cwd`) of the supervisor agent

## Usage Example

With `CAO_ENABLE_WORKING_DIRECTORY=true`:

```python
# Handoff to agent in specific package directory
result = await handoff(
    agent_profile="developer",
    message="Fix the bug in UserService.java",
    working_directory="/workspace/src/MyPackage"
)

# Assign task with specific working directory
result = await assign(
    agent_profile="reviewer",
    message="Review the changes in the authentication module",
    working_directory="/workspace/src/AuthModule"
)
```

## Why Disabled by Default?

When the `working_directory` parameter is visible to agents, they may hallucinate or incorrectly infer directory paths instead of using the default (current working directory). Disabling by default prevents this behavior for users who don't need explicit directory control. If your workflow requires delegating tasks to specific directories, enable this feature and provide explicit paths in your agent instructions.
