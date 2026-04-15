"""Plugin base class and hook decorator for CAO plugins.

This module defines the marker base class plugin authors subclass and the
decorator used to associate async plugin methods with CAO event types.
"""

from typing import Awaitable, Callable, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
AsyncMethodT = Callable[P, Awaitable[R]]

_HOOK_EVENT_ATTR = "_cao_hook_event"


class CaoPlugin:
    """Base class for CAO plugins.

    Subclass this and declare hooks with the @hook decorator.
    Register the subclass via the `cao.plugins` entry point group.
    """

    async def setup(self) -> None:
        """Called once after instantiation on server startup.

        Override to open connections, load config, or initialize state.
        """

    async def teardown(self) -> None:
        """Called once on server shutdown.

        Override to close connections or flush buffers.
        """


def hook(event_type: str) -> Callable[[AsyncMethodT[P, R]], AsyncMethodT[P, R]]:
    """Decorator that registers a plugin method as a hook for a CAO event.

    Args:
        event_type: The CAO event type to listen for (e.g. "post_send_message").

    Example:
        @hook("post_send_message")
        async def notify(self, event: PostSendMessageEvent) -> None:
            ...
    """

    def decorator(fn: AsyncMethodT[P, R]) -> AsyncMethodT[P, R]:
        setattr(fn, _HOOK_EVENT_ATTR, event_type)
        return fn

    return decorator
