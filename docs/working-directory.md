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

All working directory paths are canonicalized and validated before use. Paths are resolved via `os.path.realpath` to normalize symlinks and `..` sequences.

### Allowed directories

- The user's home directory and any subdirectory (`~/projects/foo`)
- External volumes and mount points (e.g., `/Volumes/workplace/project`)
- Custom paths like `/opt/projects`, NFS mounts, corporate dev desktops
- Any real directory that is **not** a blocked system path (see below)

### Blocked (unsafe) directories

Two rules, applied to the path after symlinks and `..` are resolved:

- **Whole subtrees.** Nothing at any depth beneath these is accepted:
  `/etc`, `/proc`, `/sys`, `/dev`, `/boot`, `/root`, `/bin`, `/sbin`,
  `/usr/bin`, `/usr/sbin`, `/lib`, `/lib64`, `/usr/lib`, `/usr/lib64`,
  `/var/spool/cron` and, on macOS, `/private/etc`. The `/usr/lib*` entries
  matter on usr-merged Linux, where `/lib` resolves to `/usr/lib`. `/root` is
  a subtree because its dotfiles (`.ssh/authorized_keys`, `.bashrc`) are
  persistence for anyone who can reach the API of a cao-server running as
  root; such a deployment keeps its projects elsewhere (`/workspace`,
  `/srv`). One carve-out: `/dev/shm`, the tmpfs scratch area, stays allowed.
- **Exact roots only.** These are refused as the directory itself, while
  their children stay allowed because projects and temp directories
  legitimately live there: `/`, `/tmp`, `/var`, and on macOS `/private/var`
  and `/private/tmp`.

### Symlink handling

Symlinks are resolved at validation time. A symlink pointing to a blocked system path (e.g., `~/escape` -> `/etc`) is rejected after resolution.

## Why Disabled by Default?

When the `working_directory` parameter is visible to agents, they may hallucinate or incorrectly infer directory paths instead of using the default (current working directory). Disabling by default prevents this behavior for users who don't need explicit directory control. If your workflow requires delegating tasks to specific directories, enable this feature and provide explicit paths in your agent instructions.
