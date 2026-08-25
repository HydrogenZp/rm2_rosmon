#!/usr/bin/env python3
"""Small real ROS node used by rosmon2 lifecycle integration tests."""

import os
import signal
import sys

import rclpy
from rclpy.node import Node


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else 'rosmon2_probe'
    rclpy.init()
    node = Node(name)
    # Exit the Python process immediately after launch delivers a stop signal.
    # Calling only ``rclpy.shutdown`` does not reliably wake ``rclpy.spin`` on
    # every Jazzy executor path, which makes this fixture unsuitable for
    # deterministic lifecycle/orphan-process tests.
    signal.signal(signal.SIGTERM, lambda *_args: os._exit(0))
    signal.signal(signal.SIGINT, lambda *_args: os._exit(0))
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
