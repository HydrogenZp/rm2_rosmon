#!/usr/bin/env python3
"""Small real ROS node used by rosmon2 lifecycle integration tests."""

import signal
import sys

import rclpy
from rclpy.node import Node


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else 'rosmon2_probe'
    rclpy.init()
    node = Node(name)
    signal.signal(signal.SIGTERM, lambda *_args: rclpy.shutdown())
    signal.signal(signal.SIGINT, lambda *_args: rclpy.shutdown())
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
