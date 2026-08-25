"""ROS distribution compatibility boundary for rosmon2.

Humble and Jazzy expose the launch argument parser through the nested
``ros2launch.api.api`` module and do not provide a stable screen-handler
setter.  All such version-sensitive access is intentionally kept here.
"""

from __future__ import annotations

import launch.logging
from ros2launch.api.api import parse_launch_arguments


def parse_arguments(arguments: list[str]):
    return parse_launch_arguments(arguments)


def attach_screen_stream(screen_handler, stream):
    """Replace the launch screen stream and return a restoration callback."""
    original = screen_handler.setStream(stream)

    def restore() -> None:
        screen_handler.setStream(original)

    return restore


def screen_handler():
    return launch.logging.launch_config.get_screen_handler()
