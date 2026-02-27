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

## Path Validation and Security

All working directory paths are canonicalized and validated before use. Both the working directory and the user's home directory are resolved via `os.path.realpath` to handle symlinked home directories (e.g., `/home/user` -> `/local/home/user` on AWS).

### Allowed (safe) directories

- The user's home directory itself (`~/`)
- Any subdirectory under the home directory (`~/projects/foo`)
- Paths that resolve to the home tree after symlink resolution

### Blocked (unsafe) directories

The following system directories are explicitly blocked:

`/`, `/bin`, `/sbin`, `/usr/bin`, `/usr/sbin`, `/etc`, `/var`, `/tmp`, `/dev`, `/proc`, `/sys`, `/root`, `/boot`, `/lib`, `/lib64`

Any path outside the user's home directory tree is also rejected.

### Symlink handling

Symlinks are resolved at validation time to prevent escapes from the home directory. For example, a symlink at `~/escape` pointing to `/etc` would be rejected after resolution. This also ensures environments with symlinked home directories (common on AWS where `/home/user` symlinks to `/local/home/user`) work correctly.

## Why Disabled by Default?

When the `working_directory` parameter is visible to agents, they may hallucinate or incorrectly infer directory paths instead of using the default (current working directory). Disabling by default prevents this behavior for users who don't need explicit directory control. If your workflow requires delegating tasks to specific directories, enable this feature and provide explicit paths in your agent instructions.
