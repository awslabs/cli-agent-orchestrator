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
"""

from __future__ import annotations

from typing import Dict, Mapping

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


class UnsupportedGitSourceError(ValueError):
    """A ``git+`` plugin source CAO deliberately refuses.

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
        f"{supported} (they are rewritten to the plain scheme). For a local "
        f"repository use a plain path or a 'file://' URL instead."
    )


def is_git_plus(location: str) -> bool:
    """Whether ``location`` uses the ``git+`` requirement spelling at all."""
    return location.strip().startswith(GIT_PLUS_PREFIX)


def git_clone_target(location: str) -> str:
    """Return the exact string to hand ``git clone``, or refuse the source.

    The single seam. A non-``git+`` location is returned stripped but otherwise
    byte-identical, so ``https://``, ``ssh://``, ``git://``, ``file://`` and
    ``scp``-style targets are unaffected.

    Args:
        location: The operator-supplied source string.

    Returns:
        The location with a supported ``git+`` prefix rewritten to its plain
        transport.

    Raises:
        UnsupportedGitSourceError: ``location`` starts with ``git+`` in any form
            other than ``git+https://`` or ``git+ssh://``.
    """
    candidate = location.strip()
    if not candidate.startswith(GIT_PLUS_PREFIX):
        return candidate

    for prefix, transport in SUPPORTED_GIT_PLUS_PREFIXES.items():
        if candidate.startswith(prefix):
            return transport + candidate[len(prefix) :]

    raise _unsupported(candidate)


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
