"""The one decision about ``git+`` plugin sources: supported, and what to clone.

``pip``/``uv`` popularised ``git+<scheme>://`` as a *requirement* spelling, so
operators type it at ``cao plugin add`` too. Git itself does not read it that
way: for ``git clone``, the text before ``://`` names a **transport**, and a
transport called ``git+https`` is looked up as the remote helper
``git-remote-git+https``, which does not exist. The clone therefore dies with
``fatal: remote helper 'git+file' aborted session`` before any plugin discovery
happens.

Source-kind detection used to classify **every** ``git+`` location as a git
source while the resolver passed that same string to ``git clone`` unchanged, so
one side claimed a form the other could not consume. Both sides now call
:func:`git_clone_target`, which is the only place that answers either question —
"is this supported" and "what do we clone" are deliberately the *same* function
call, because as two functions they already drifted once.

Only ``git+https://`` and ``git+ssh://`` are accepted, and they are rewritten to
``https://`` and ``ssh://``. **Every** other ``git+`` form is refused rather than
stripped. That asymmetry is the point rather than an omission:
``git+file://`` was the reviewer's own reproduction of the defect, and
normalizing the prefix broadly would have turned that clear, loud failure into a
silently *accepted* read of an arbitrary local filesystem path — the one outcome
worse than the crash being fixed. A refusal that names the supported forms costs
an operator one retype; a wrongly-accepted local read costs them a plugin staged
from somewhere they never named. So the rule errs toward refusing, and a new
form joins :data:`SUPPORTED_GIT_PLUS_PREFIXES` only by deliberate decision.

The same function is also the **scheme and host allowlist** for every git
source, ``git+`` or not. The profile downloader in ``services.install_service``
already refuses anything but ``https://`` to an allowlisted host; the plugin
path handed whatever it was given to ``git clone``, and for ``git clone`` the
text before ``://`` is a *transport*: ``file://`` reads any repository the
server user can, ``git://`` opens a TCP connection to any host and port the
server can reach, and ``ext::`` runs a command wherever git's protocol policy
permits it. ``--`` on the argv guards against option injection and nothing
else. So a git source is now accepted only when it is ``https://`` or ``ssh://``
(URL or ``scp``-style) to a host in :func:`allowed_hosts` -- ``github.com`` by
default, replaced by ``CAO_PLUGIN_ALLOWED_HOSTS`` -- with no query, fragment or
embedded credential, and the string handed to git is rebuilt from the validated
parts. The resolver pins ``GIT_ALLOW_PROTOCOL`` as well, so the transport rule
holds even for a caller that reaches ``git clone`` some other way.
"""

from __future__ import annotations

import os
import re
from typing import Dict, FrozenSet, Mapping
from urllib.parse import urlsplit

#: The prefix that triggers this whole module.
GIT_PLUS_PREFIX = "git+"

#: The only supported ``git+`` spellings, mapped to the transport ``git clone``
#: actually speaks. Ordering is irrelevant; the prefixes are mutually exclusive.
#:
#: Adding an entry here is a decision that a form is safe to accept, not a
#: formatting change — see this module's docstring on why ``git+file://`` is
#: absent. Tests derive their expectations from this mapping, so an addition
#: cannot land without the behavioural cases moving with it.
SUPPORTED_GIT_PLUS_PREFIXES: Mapping[str, str] = {
    "git+https://": "https://",
    "git+ssh://": "ssh://",
}


#: Transports a plugin git source may use. ``file``, ``git``, ``http`` and the
#: ``ext``/remote-helper forms are refused: see the module docstring.
ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"https", "ssh"})

#: Hosts a plugin may be cloned from unless the operator replaces the list.
DEFAULT_ALLOWED_HOSTS: FrozenSet[str] = frozenset({"github.com"})

#: Comma-separated hostnames that REPLACE the default allowlist (an internal
#: GitLab or CodeCommit mirror, for example). Same shape as
#: ``CAO_PROFILE_ALLOWED_HOSTS`` for profile downloads.
ALLOWED_HOSTS_ENV = "CAO_PLUGIN_ALLOWED_HOSTS"

# scp-style target ``[user@]host:path``. Host: letters, digits, dots, hyphens.
# Path: no whitespace, no further ``:``, and not absolute (``host:/abs`` is not a
# hosted repository path).
_SCP_TARGET_RE = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r":(?P<path>(?!/)[^\s:]+)$"
)
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
# A user name for ssh: must not begin with ``-`` (an ssh option) and carries no
# shell metacharacters.
_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# One path segment of a hosted repository: no leading ``-`` (an option to the
# remote ``git-upload-pack``), no ``.``/``..``, no shell or URL metacharacters.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _repository_path_ok(path: str) -> bool:
    """Every segment of ``path`` is a plain repository path segment.

    Empty segments are allowed only as the trailing slash. ``.`` and ``..`` are
    refused (``_SEGMENT_RE`` requires a leading word character), as is a
    segment beginning with ``-``.
    """
    segments = path.strip("/").split("/") if path.strip("/") else []
    return all(_SEGMENT_RE.match(seg) for seg in segments)


def allowed_hosts() -> FrozenSet[str]:
    """Hosts a plugin git source may name: the operator's list, else the default."""
    override = os.environ.get(ALLOWED_HOSTS_ENV, "")
    hosts = {h.strip().lower() for h in override.split(",") if h.strip()}
    return frozenset(hosts) if hosts else DEFAULT_ALLOWED_HOSTS


class UnsupportedGitSourceError(ValueError):
    """A plugin git source CAO deliberately refuses.

    Deliberately **not** a :class:`~cli_agent_orchestrator.agent_plugins.resolver.ResolverError`:
    this is a validation verdict about the source string, reached before any
    subprocess runs, and it carries a message naming the supported forms rather
    than a raw ``git`` stderr line. The installer maps it to
    ``PluginInstallError`` so the CLI and the HTTP surface both report it as the
    bad request it is.
    """


def _unsupported(location: str) -> UnsupportedGitSourceError:
    supported = ", ".join(sorted(SUPPORTED_GIT_PLUS_PREFIXES))
    return UnsupportedGitSourceError(
        f"Unsupported git source form: {location!r}. Git reads the text before "
        f"'://' as a transport, so a 'git+' prefix it does not know is looked up "
        f"as a remote helper and the clone fails. Supported 'git+' forms are: "
        f"{supported} (they are rewritten to the plain scheme), and the result must "
        f"name a host in the allowed list ({', '.join(sorted(allowed_hosts()))}; set "
        f"{ALLOWED_HOSTS_ENV} to change it). For a repository on this machine use a "
        f"plain directory path."
    )


def is_git_plus(location: str) -> bool:
    """Whether ``location`` uses the ``git+`` requirement spelling at all."""
    return location.strip().startswith(GIT_PLUS_PREFIX)


def git_clone_target(location: str) -> str:
    """Return the exact string to hand ``git clone``, or refuse the source.

    The single seam, and the allowlist. A ``git+https://``/``git+ssh://`` prefix
    is rewritten to its plain transport first; then every location, ``git+`` or
    not, must be ``https://`` or ``ssh://`` (URL or ``scp``-style) to a host in
    :func:`allowed_hosts`, with no query, fragment or embedded credential.
    ``file://``, ``git://``, ``http://``, ``ext::`` and plain paths are refused
    before any subprocess runs, and the accepted location is rebuilt from its
    validated parts rather than echoed.

    Args:
        location: The operator-supplied source string.

    Returns:
        The location with a supported ``git+`` prefix rewritten to its plain
        transport.

    Raises:
        UnsupportedGitSourceError: ``location`` starts with ``git+`` in any form
            other than ``git+https://`` or ``git+ssh://``, uses a transport other
            than https/ssh, names a host outside the allowlist, or carries a
            query, fragment or credential.
    """
    candidate = location.strip()
    if candidate.startswith(GIT_PLUS_PREFIX):
        for prefix, transport in SUPPORTED_GIT_PLUS_PREFIXES.items():
            if candidate.startswith(prefix):
                candidate = transport + candidate[len(prefix) :]
                break
        else:
            raise _unsupported(candidate)
    return _validated_clone_target(candidate)


def _refused(location: str, why: str) -> UnsupportedGitSourceError:
    hosts = ", ".join(sorted(allowed_hosts()))
    return UnsupportedGitSourceError(
        f"Refusing plugin git source {location!r}: {why}. A plugin is cloned only "
        f"over https:// or ssh:// (URL or scp-style) from an allowed host ({hosts}); "
        f"set {ALLOWED_HOSTS_ENV} to a comma-separated list to allow other hosts. "
        f"For a repository on this machine use a plain directory path."
    )


def _validated_clone_target(target: str) -> str:
    """Accept ``target`` only as https/ssh to an allowed host; return it rebuilt.

    Rebuilt from the validated parts rather than echoed, as the profile
    downloader does: git never sees a byte that did not survive validation, and
    the host is the allowlist's own spelling.
    """
    if not target:
        raise _refused(target, "it is empty")
    if "://" not in target and "::" in target.split("/", 1)[0]:
        # ``ext::``, ``fd::`` and any other remote-helper spelling. Checked
        # first so the refusal names the transport, not a space in its command.
        raise _refused(target, "remote-helper transports ('helper::') are not allowed")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in target):
        raise _refused(target, "it contains whitespace or control characters")

    if "://" in target:
        parsed = urlsplit(target)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise _refused(target, f"the '{scheme}' transport is not allowed")
        host = (parsed.hostname or "").lower()
        if not host or not _HOST_RE.match(host):
            raise _refused(target, "it has no valid host")
        if host not in allowed_hosts():
            raise _refused(target, f"host '{host}' is not in the allowed hosts")
        if parsed.query or parsed.fragment:
            raise _refused(target, "a query string or fragment is not allowed")
        if parsed.password or (scheme == "https" and parsed.username):
            raise _refused(target, "credentials in the URL are not allowed")
        if parsed.username and not _USER_RE.match(parsed.username):
            raise _refused(target, "the user name is not valid")
        try:
            port_number = parsed.port
        except ValueError:
            raise _refused(target, "the port is not valid") from None
        path = parsed.path or "/"
        if not _repository_path_ok(path):
            raise _refused(target, "the repository path is not valid")
        userinfo = f"{parsed.username}@" if scheme == "ssh" and parsed.username else ""
        # An explicit port stays: the allowlist is about the HOST (a GitHub
        # Enterprise or GitLab on 8443 is that host), and the transport pin
        # decides what may be spoken to it. The default port is dropped so the
        # same repository is spelled one way.
        default_port = 443 if scheme == "https" else 22
        port = f":{port_number}" if port_number and port_number != default_port else ""
        return f"{scheme}://{userinfo}{host}{port}{path}"

    match = _SCP_TARGET_RE.match(target)
    if not match:
        raise _refused(target, "it is not an https://, ssh:// or scp-style git location")
    host = match.group("host").lower()
    if host not in allowed_hosts():
        raise _refused(target, f"host '{host}' is not in the allowed hosts")
    if match.group("user") and not _USER_RE.match(match.group("user")):
        raise _refused(target, "the user name is not valid")
    if not _repository_path_ok(match.group("path")):
        raise _refused(target, "the repository path is not valid")
    user = f"{match.group('user')}@" if match.group("user") else ""
    return f"{user}{host}:{match.group('path')}"


def is_supported_git_location(location: str) -> bool:
    """Whether :func:`git_clone_target` would accept ``location``.

    A convenience for callers that need the predicate without the value — and
    implemented *through* ``git_clone_target`` rather than beside it, so it
    cannot answer differently from the thing that produces the clone argv.
    """
    try:
        git_clone_target(location)
    except UnsupportedGitSourceError:
        return False
    return True


def supported_git_plus_prefixes() -> Dict[str, str]:
    """A copy of the supported table, for callers that render it to a user."""
    return dict(SUPPORTED_GIT_PLUS_PREFIXES)
