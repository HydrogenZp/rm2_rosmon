"""ROS distribution compatibility boundary for rosmon2.

Humble and Jazzy expose the launch argument parser through the nested
``ros2launch.api.api`` module and do not provide a stable screen-handler
setter.  All such version-sensitive access is intentionally kept here.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

import launch.logging
from ros2launch.api.api import parse_launch_arguments


def parse_arguments(arguments: list[str]):
    return parse_launch_arguments(arguments)


def _screen_stream(screen_handler: Any) -> Any:
    """Return the current stream across Humble/Jazzy screen-handler variants.

    ``ScreenHandler.setStream`` is the only setter exposed by the launch
    logging API, but it does not consistently return the previous stream.
    Humble and Jazzy also do not expose a stable getter.  Keep the narrowly
    scoped private-field fallback here so the rest of rosmon2 never needs to
    know about this distro difference.
    """
    for attribute in ('stream', '_stream', '_ScreenHandler__stream'):
        try:
            value = getattr(screen_handler, attribute)
        except AttributeError:
            continue
        if value is not None:
            return value
    return sys.stdout


def attach_screen_stream(screen_handler: Any, stream: Any) -> Callable[[], None]:
    """Replace the launch screen stream and return a reliable restore hook."""
    original = _screen_stream(screen_handler)
    result = screen_handler.setStream(stream)
    # Some launch releases return the previous stream; prefer it when they do.
    if result is not None:
        original = result

    def restore() -> None:
        screen_handler.setStream(original)

    return restore


def screen_handler():
    return launch.logging.launch_config.get_screen_handler()
