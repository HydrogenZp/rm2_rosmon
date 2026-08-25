"""Launch one real rclpy process for rosmon2 integration tests."""

import sys
from pathlib import Path

from launch import LaunchDescription
from launch.actions import ExecuteProcess


def generate_launch_description():
    helper = Path(__file__).with_name('graph_node.py')
    return LaunchDescription([
        ExecuteProcess(
            cmd=[sys.executable, str(helper), 'rosmon2_probe'],
            name='rosmon2_probe',
            output='both',
        ),
    ])
