"""Real-process lifecycle and ROS graph regression tests.

These tests intentionally do not mock LaunchService, ExecuteProcess, or
process events.  They are run in the ROS2 Humble/Jazzy test container.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

import pytest

from rosmon2.control import ControlClient, session_socket_path


ROOT = Path(__file__).parent
LAUNCH_FILE = ROOT / 'resources' / 'lifecycle.launch.py'
pytestmark = pytest.mark.skipif(
    shutil.which('ros2') is None,
    reason='ROS2 CLI is required for lifecycle integration tests',
)


def _wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError('timed out waiting for lifecycle condition')


def _graph_nodes():
    result = subprocess.run(
        ['ros2', 'node', 'list'],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return set(result.stdout.splitlines())


@pytest.fixture
def session(tmp_path, monkeypatch):
    name = f'test_{os.getpid()}_{time.time_ns()}'[-50:]
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    log_file = tmp_path / 'rosmon.log'
    environment = os.environ.copy()
    environment['ROSMON2_RUNTIME_DIR'] = str(runtime)
    monkeypatch.setenv('ROSMON2_RUNTIME_DIR', str(runtime))
    command = [
        sys.executable, '-m', 'rosmon2.cli', 'launch', '--disable-ui',
        '--session', name, '--log', str(log_file), str(LAUNCH_FILE),
    ]
    process = subprocess.Popen(command, env=environment)
    client = ControlClient(name, timeout=3)

    def socket_ready():
        return session_socket_path(name).exists()

    try:
        _wait_until(socket_ready)
        _wait_until(lambda: _status(client)['summary']['running'] == 1)
    except Exception:
        process.terminate()
        process.wait(timeout=15)
        raise
    yield process, client
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    assert not session_socket_path(name).exists()


def _status(client):
    return client.request({'command': 'status'})


def test_real_start_stop_restart_updates_ros_graph(session):
    process, client = session
    _wait_until(lambda: '/rosmon2_probe' in _graph_nodes())

    client.request({'command': 'stop', 'node': '/rosmon2_probe'})
    _wait_until(lambda: _status(client)['summary']['stopped'] == 1)
    _wait_until(lambda: '/rosmon2_probe' not in _graph_nodes())

    client.request({'command': 'restart', 'node': '/rosmon2_probe'})
    _wait_until(lambda: _status(client)['summary']['running'] == 1)
    _wait_until(lambda: '/rosmon2_probe' in _graph_nodes())
    assert process.poll() is None


def test_real_crash_is_distinguished_and_restart_has_new_pid(session):
    _process, client = session
    first = _status(client)['nodes'][0]
    os.kill(first['pid'], signal.SIGKILL)
    _wait_until(lambda: _status(client)['summary']['crashed'] == 1)

    client.request({'command': 'restart', 'node': first['name']})
    _wait_until(lambda: _status(client)['summary']['running'] == 1)
    second = _status(client)['nodes'][0]
    assert second['pid'] != first['pid']


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
def test_signal_shutdown_leaves_no_ros_graph_node(session, sig):
    process, client = session
    _wait_until(lambda: '/rosmon2_probe' in _graph_nodes())
    process.send_signal(sig)
    process.wait(timeout=15)
    assert not session_socket_path(client.session).exists()
    _wait_until(lambda: '/rosmon2_probe' not in _graph_nodes())
