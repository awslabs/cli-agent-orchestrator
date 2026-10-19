"""Validation for operator-forwarded session environment variables.

The same constraints are enforced by every client entry point that forwards
env vars into a launched session -- ``cao launch --env``
(``cli/commands/launch.py``) and the ops-MCP ``launch_session`` tool -- and
mirror the server-side filtering in ``TmuxClient._merge_extra_env``. Keeping the
canonical constants and the validator here stops the two client paths from
drifting apart. See issue #248.

The server silently *drops* a var that violates these rules
(``SessionEnvStore._merge_extra_env``), so each client validates at its own
boundary and surfaces a clear error instead of letting a forwarded var vanish.

The validator also rejects inputs that would survive the schema but break the
launch downstream -- NUL bytes, non-UTF-8 values, and mappings whose aggregate
size would overflow the tmux argv (E2BIG). Those failures make libtmux log the
full command, so rejecting them here keeps forwarded secrets out of the log.

Error messages name the offending KEY and the violated rule only; they never
echo the VALUE, so a secret passed as a value cannot leak into an error string.
"""

from typing import Dict, Mapping

# Prefixes reserved for provider-managed env; forwarding them is rejected so an
# operator cannot clobber the provider's own auth/config vars. Mirrored in
# ``TmuxClient._merge_extra_env`` server-side.
FORWARDED_ENV_BLOCKED_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")

# Explicit exceptions to the blocked prefixes: the documented Claude Code
# auth-routing flags an operator legitimately needs to forward.
FORWARDED_ENV_PREFIX_ALLOWLIST = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    }
)

# Variables that decide what the spawned pane runs BEFORE the provider CLI's
# first tool call: the dynamic loader's hooks, which program and rc file the
# shell starts with, the shell's own prompt hooks, the interpreters' startup
# imports, and the files the AWS SDK reads to authenticate the provider itself.
# A forwarded value here executes attacker code as the operator before the
# agent has done anything, regardless of provider or role, so they are refused
# outright. Exact names for the shell/interpreter hooks; prefixes for the
# loaders, whose whole ``LD_*`` / ``DYLD_*`` families are control knobs.
# Variables that only act when the agent itself runs a program
# (``GIT_SSH_COMMAND``, ``EDITOR``, ``PAGER``, ...) are not listed: whether
# the agent may run programs at all is the tool policy's decision, and a value
# in those cannot run before it. This is a denylist and will trail new
# providers and new startup hooks; an allowlist of known-safe forwarded names
# is the shape that would not. Mirrored server-side in
# ``TmuxClient._is_blocked_env_key``.
FORWARDED_ENV_HIJACK_KEYS = frozenset(
    {
        # the pane's shell resolves the typed launch command through PATH,
        # sources its rc files from HOME, and programs consult SHELL
        "PATH",
        "HOME",
        "SHELL",
        # sh/bash/zsh read these on startup and source the file they name
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        # bash runs PROMPT_COMMAND and expands command substitutions in the
        # prompt strings at the first prompt, before anything is typed
        "PROMPT_COMMAND",
        "PS0",
        "PS1",
        "PS2",
        "PS4",
        # interpreters import or execute from these before the script runs
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PYTHONHOME",
        # relocates the user site-packages, whose usercustomize.py is imported
        # automatically at interpreter start -- same effect as PYTHONPATH
        "PYTHONUSERBASE",
        "PERL5OPT",
        "PERL5LIB",
        # PERLLIB is PERL5LIB's older alias; NODE_PATH is the fallback module
        # search path for a program's own require() calls
        "PERLLIB",
        "NODE_PATH",
        "NODE_OPTIONS",
        "RUBYOPT",
        "RUBYLIB",
        # glibc loads charset-conversion modules (shared objects) from here
        "GCONV_PATH",
        # the AWS SDK reads these files when the provider CLI authenticates at
        # startup (CAO first-classes Bedrock auth, and AWS_* is otherwise
        # forwarded); a profile's credential_process runs whatever command the
        # file names, before the provider's first tool call
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    }
)
FORWARDED_ENV_HIJACK_PREFIXES = ("LD_", "DYLD_")


def is_hijack_env_key(key: str) -> bool:
    """True if forwarding ``key`` would let its value run code at pane start."""
    return key in FORWARDED_ENV_HIJACK_KEYS or key.startswith(FORWARDED_ENV_HIJACK_PREFIXES)


# Per-value byte cap. Forwarded vars ride the ``tmux new-session -e`` argv, so an
# oversized value risks the kernel "command too long" limit (see PR #246).
FORWARDED_ENV_MAX_VALUE_BYTES = 2048

# Per-key byte cap. Keys are ASCII identifiers (see ``_is_valid_env_key``), so
# bytes == chars; this bounds a single pathological key on the argv.
FORWARDED_ENV_MAX_KEY_BYTES = 128

# Max number of forwarded entries. Guards the argv against a mapping with a
# huge entry count (each entry becomes one ``-eKEY=VALUE`` argument).
FORWARDED_ENV_MAX_ENTRIES = 256

# Aggregate argv budget for the whole forwarded set. Each entry rides tmux argv
# as ``-eKEY=VALUE``; the kernel rejects an over-long argv with E2BIG, and
# libtmux logs the full command (values included) on that failure -- so a set
# that would blow the argv limit is rejected here, before launch, both to keep
# the launch working and to keep forwarded secrets out of the server log. Set
# well under the ~2 MiB kernel ARG_MAX to leave room for the rest of argv and
# the ambient environment.
FORWARDED_ENV_MAX_TOTAL_BYTES = 128 * 1024

# Per-entry argv overhead: the ``-e`` flag plus the ``=`` separator in
# ``-eKEY=VALUE``. Added to each entry's key+value bytes for the aggregate.
_ARGV_ENTRY_OVERHEAD = 3


class ForwardedEnvError(ValueError):
    """A forwarded env var violates the forwarding constraints.

    Subclasses ``ValueError`` so callers that only catch ``ValueError`` still
    work; the message names the key and rule but never the value.
    """


def _is_valid_env_key(key: str) -> bool:
    """POSIX env-name shape: leading letter/underscore, then ASCII alnum/underscore.

    Stricter than ``str.isidentifier`` only in forbidding non-ASCII.
    """
    return bool(
        key
        and (key[0].isalpha() or key[0] == "_")
        and all(c.isalnum() or c == "_" for c in key)
        and key.isascii()
    )


def _uses_blocked_prefix(key: str) -> bool:
    if key in FORWARDED_ENV_PREFIX_ALLOWLIST:
        return False
    return any(key.startswith(p) for p in FORWARDED_ENV_BLOCKED_PREFIXES)


def validate_forwarded_env(mapping: Mapping[str, str]) -> Dict[str, str]:
    """Validate an already-parsed env mapping; return it as a plain dict.

    Every message starts with ``env `` so a caller can prefix it (the CLI turns
    it into a ``--env `` message) without re-implementing the rules.

    Raises ``ForwardedEnvError`` on the first violation:
      * more than ``FORWARDED_ENV_MAX_ENTRIES`` entries,
      * a key that is not a ``[A-Za-z_][A-Za-z0-9_]*`` ASCII identifier,
      * a key longer than ``FORWARDED_ENV_MAX_KEY_BYTES`` bytes,
      * a key using a blocked provider prefix (outside the allowlist),
      * a key the loader, shell or an interpreter consults at startup
        (``FORWARDED_ENV_HIJACK_KEYS`` / ``FORWARDED_ENV_HIJACK_PREFIXES``),
      * a value containing a NUL byte (breaks ``Popen`` with "embedded null
        byte" and leaks the argv into logs),
      * a value that is not encodable as UTF-8 (e.g. a lone surrogate that
        JSON/FastMCP accepts as a str),
      * a value whose UTF-8 encoding is >= ``FORWARDED_ENV_MAX_VALUE_BYTES``,
      * a set whose aggregate argv size exceeds ``FORWARDED_ENV_MAX_TOTAL_BYTES``.
    """
    if len(mapping) > FORWARDED_ENV_MAX_ENTRIES:
        raise ForwardedEnvError(
            f"env var count {len(mapping)} exceeds the limit of {FORWARDED_ENV_MAX_ENTRIES}"
        )

    validated: Dict[str, str] = {}
    total_argv_bytes = 0
    for key, value in mapping.items():
        if not _is_valid_env_key(key):
            raise ForwardedEnvError(f"env key must match [A-Za-z_][A-Za-z0-9_]* (got {key!r})")
        # Keys are guaranteed ASCII here, so byte length == character length.
        key_bytes = len(key)
        if key_bytes > FORWARDED_ENV_MAX_KEY_BYTES:
            raise ForwardedEnvError(f"env key {key!r} exceeds {FORWARDED_ENV_MAX_KEY_BYTES} bytes")
        if _uses_blocked_prefix(key):
            raise ForwardedEnvError(
                f"env key {key!r} uses a blocked prefix "
                f"({', '.join(FORWARDED_ENV_BLOCKED_PREFIXES)}) reserved for provider env"
            )
        if is_hijack_env_key(key):
            raise ForwardedEnvError(
                f"env key {key!r} is read by the loader, shell or an interpreter "
                "when the pane starts and cannot be forwarded"
            )
        # A NUL passes the byte-length check but makes ``Popen`` raise
        # "embedded null byte"; libtmux logs the whole argv (values included)
        # on that failure, so reject it here before launch.
        if "\x00" in value:
            raise ForwardedEnvError(
                f"env value for {key!r} contains a NUL byte and cannot be forwarded"
            )
        # JSON/FastMCP accept a lone surrogate as a str, but ``str.encode`` then
        # raises before any downstream use -- treat it as invalid input so the
        # caller returns a clean error instead of crashing.
        try:
            value_bytes = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ForwardedEnvError(
                f"env value for {key!r} is not valid UTF-8 and cannot be forwarded"
            ) from None
        if value_bytes >= FORWARDED_ENV_MAX_VALUE_BYTES:
            raise ForwardedEnvError(
                f"env value for {key!r} exceeds {FORWARDED_ENV_MAX_VALUE_BYTES} bytes "
                "(tmux argv limit, PR #246)"
            )
        total_argv_bytes += key_bytes + value_bytes + _ARGV_ENTRY_OVERHEAD
        if total_argv_bytes > FORWARDED_ENV_MAX_TOTAL_BYTES:
            raise ForwardedEnvError(
                f"env vars exceed the total argv budget of {FORWARDED_ENV_MAX_TOTAL_BYTES} "
                "bytes (tmux argv limit)"
            )
        validated[key] = value
    return validated
