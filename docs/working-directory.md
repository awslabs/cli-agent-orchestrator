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

## Operator-typed paths over HTTP

`POST /sessions`, `POST /sessions/{session_name}/terminals` and its operator
form `POST /sessions/{session_name}/terminals:operator` normalize
`working_directory` before it reaches tmux, so a path pasted from a browser
on WSL works as typed:

| Typed | Used |
|-------|------|
| `"C:\Users\me\proj"` (Explorer "Copy as path") | `/mnt/c/Users/me/proj` |
| `C:/Users/me/proj` | `/mnt/c/Users/me/proj` |
| `~/proj` | `/home/me/proj` |
| `/home/me/scratch/../proj` | `/home/me/proj` (`scratch` is never created) |
| `/home/me/new-proj` (missing) | `400`, unless the request sets `create_working_directory=true` (see below) |

A relative path, a `~user` for a user that does not exist, an existing
non-directory (a file, FIFO, socket or device),
a Windows drive that is not mounted under `/mnt`, a Windows path whose `..`
would leave that drive, or a folder name over the filesystem's 255-byte limit
is rejected with a `400` and a plain-language `detail`.

### Creating a missing directory

A missing `working_directory` is a `400` by default: the directory must exist,
which is the behavior every caller has always had.

A request that wants it created says so, with the
`create_working_directory=true` query parameter on any of the three create
routes above. Who sends it:

| Caller | Sends it | What a missing directory means |
|--------|----------|--------------------------------|
| The dashboard's launch form and "Add Agent" button | yes, whenever the operator typed a path | the operator picked a folder that does not exist yet; it is created |
| The MCP `handoff` and `assign` tools | **no** | the documented must-exist refusal: a `400` the agent can read and correct |
| Any other HTTP client | only if it asks | must exist unless it opts in |

That split is the point and not an oversight. An agent that sends a path with
a typo under an allowed parent would otherwise materialize an empty tree, start
a worker in it, and report success; fail-fast makes the same typo a correctable
error. An operator typing into a folder field is making a request, not a
mistake.

When a request does opt in, a missing directory is still judged by the same
path policy as everything else on this page -- the nearest existing ancestor
must not be a blocked system directory, so `/tmp/new-proj` and `/etc/new-proj`
are refused -- and is created only at the moment the session or terminal is
created, after every check that can refuse the request (name validation,
duplicate session, session not found, the terminal cap, the delegation
ceiling): a rejected request leaves nothing behind. Creation is bounded by a
depth cap, and a directory the OS cannot create (a parent that is a file or not
traversable, a read-only filesystem) is a `400`, not a `500`.

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
