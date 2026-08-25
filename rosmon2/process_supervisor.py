"""Lifecycle state machine for launch-managed processes."""

from __future__ import annotations

import asyncio
import os
import signal
import shutil
from typing import Callable, Optional

from launch.actions import ExecuteProcess
from launch_ros.actions import Node

from .model import ProcessRecord, ProcessState
from .registry import ProcessRegistry


class ProcessSupervisor:
    """Implement start/stop/restart and crash handling for records."""

    def __init__(
            self, registry: ProcessRegistry, runtime, *,
            stop_timeout: float = 5.0,
            on_changed: Optional[Callable[[ProcessRecord, str], None]] = None):
        self.registry = registry
        self.runtime = runtime
        self.stop_timeout = stop_timeout
        self.on_changed = on_changed
        self.shutting_down = False
        self._pending_restarts: set[int] = set()
        self._state_event = asyncio.Event()

    def _notify(self, record: ProcessRecord, reason: str) -> None:
        self._state_event.set()
        self._state_event = asyncio.Event()
        if self.on_changed is not None:
            self.on_changed(record, reason)

    @staticmethod
    def _set_state(record: ProcessRecord, state: ProcessState) -> None:
        allowed = {
            ProcessState.STOPPED: {ProcessState.STARTING},
            ProcessState.STARTING: {
                ProcessState.RUNNING, ProcessState.CRASHED,
            },
            ProcessState.RUNNING: {
                ProcessState.STOPPING, ProcessState.CRASHED,
            },
            ProcessState.STOPPING: {
                ProcessState.STOPPED, ProcessState.CRASHED,
            },
            ProcessState.CRASHED: {ProcessState.STARTING},
        }
        if state is record.state:
            return
        if state not in allowed.get(record.state, set()):
            raise RuntimeError(
                f'invalid process transition {record.state.value} -> {state.value}'
            )
        record.state = state

    def _begin_start(self, record: ProcessRecord) -> None:
        if record.state not in (ProcessState.STOPPED, ProcessState.CRASHED):
            return
        self._set_state(record, ProcessState.STARTING)
        record.expected_stop = False

    def start(
            self, record: ProcessRecord, *, count_restart: bool = True,
            command: Optional[list[str]] = None) -> Optional[ExecuteProcess]:
        if self.shutting_down or record.pid is not None or not record.cmd:
            return None
        if self.runtime.context is None:
            return None
        if record.state not in (ProcessState.STOPPED, ProcessState.CRASHED):
            return None
        self._begin_start(record)
        if count_restart:
            record.restart_count += 1
        action = ExecuteProcess(
            cmd=list(command or record.cmd),
            cwd=record.cwd,
            env=record.env,
            name=f'rosmon2_{record.key}_{record.restart_count}',
            output='log',
            sigterm_timeout=str(self.stop_timeout),
            sigkill_timeout=str(max(1.0, self.stop_timeout)),
        )
        self.registry.bind(action, record)
        try:
            self.runtime.include_process(action)
        except Exception:
            self.registry.unbind(action)
            record.pid = None
            record.exit_code = None
            self._set_state(record, ProcessState.CRASHED)
            self._notify(record, 'start failed')
            raise
        self._notify(record, 'start requested')
        return action

    def stop(self, record: ProcessRecord, *, reason: str = 'user') -> None:
        if record.pid is None or record.action is None:
            record.expected_stop = True
            self._notify(record, f'{reason} stop requested')
            return
        if record.state is ProcessState.STOPPING:
            record.expected_stop = True
            return
        if record.state is ProcessState.RUNNING:
            self._set_state(record, ProcessState.STOPPING)
        record.expected_stop = True
        self._notify(record, f'{reason} stop requested')
        self.runtime.request_process_stop(record.action)

    def restart(self, record: ProcessRecord) -> None:
        if self.shutting_down:
            return
        if record.state is ProcessState.STOPPING:
            return
        if record.pid is None:
            self.start(record)
            return
        self._pending_restarts.add(record.key)
        self.stop(record, reason='restart')

    def debug(self, record: ProcessRecord) -> None:
        if shutil.which('gdb') is None:
            raise RuntimeError('gdb is not installed')
        if record.pid is not None:
            self.stop(record, reason='debug')
            return
        self.start(record, command=['gdb', '--args', *record.cmd])

    def on_start(self, event, context) -> ProcessRecord:
        record = self.registry.by_action(event.action)
        if record is None:
            display_name = self.display_name(event.action, event.process_name)
            namespace = self.namespace_for_name(display_name)
            record = self.registry.create(display_name, namespace)
            self.registry.bind(event.action, record)
        record.action = event.action
        record.cmd = list(event.cmd)
        record.cwd = event.cwd
        record.env = dict(event.env) if event.env else None
        record.pid = event.pid
        record.exit_code = None
        if record.state is ProcessState.STOPPED:
            self._set_state(record, ProcessState.STARTING)
        if record.state is ProcessState.CRASHED:
            self._set_state(record, ProcessState.STARTING)
        self._set_state(record, ProcessState.RUNNING)
        self._notify(record, f'process started with pid {event.pid}')
        return record

    def on_exit(self, event, context) -> Optional[ProcessRecord]:
        record = self.registry.by_action(event.action)
        if record is None:
            return None
        # Ignore late events from an old action after a successful restart.
        if record.action is not event.action and record.pid is not None:
            self.registry.unbind(event.action)
            return None
        record.pid = None
        record.exit_code = event.returncode
        expected = record.expected_stop or self.shutting_down
        if record.state is ProcessState.RUNNING:
            self._set_state(record, ProcessState.STOPPING if expected else ProcessState.CRASHED)
        elif record.state is ProcessState.STARTING and not expected:
            self._set_state(record, ProcessState.CRASHED)
        if expected and record.state is ProcessState.STOPPING:
            self._set_state(record, ProcessState.STOPPED)
        elif expected and record.state is ProcessState.STARTING:
            # A launch can report exit before ProcessStarted is delivered.
            record.state = ProcessState.STOPPED
        elif not expected and record.state is ProcessState.STOPPING:
            self._set_state(record, ProcessState.CRASHED)
        self.registry.unbind(event.action)
        key = record.key
        should_restart = (
            key in self._pending_restarts and not self.shutting_down
        )
        if should_restart:
            self._pending_restarts.discard(key)
            loop = getattr(context, 'asyncio_loop', None)
            if loop is None:
                loop = asyncio.get_running_loop()
            loop.call_soon(self.start, record)
        self._notify(record, f'process exited with code {event.returncode}')
        return record

    async def shutdown(self) -> None:
        self.shutting_down = True
        self._pending_restarts.clear()
        for record in tuple(self.registry.records):
            if record.pid is not None:
                self.stop(record, reason='launch shutdown')
        await self.wait_stopped(self.stop_timeout)
        for record in tuple(self.registry.records):
            if record.pid is None:
                continue
            pid = record.pid
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                record.pid = None
            except OSError:
                continue
            record.exit_code = -signal.SIGKILL
            record.expected_stop = True
            await self._wait_pid_gone(pid, 1.0)
            if record.pid == pid:
                record.pid = None
                record.state = ProcessState.STOPPED
                self._notify(record, 'forced process termination')
        await self.wait_stopped(1.0)

    @staticmethod
    async def _wait_pid_gone(pid: int, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                return
            await asyncio.sleep(0.05)

    async def wait_stopped(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while any(record.pid is not None for record in self.registry.records):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._state_event.wait(), remaining)
            except asyncio.TimeoutError:
                return

    @staticmethod
    def process_name_without_counter(name: str) -> str:
        base, separator, counter = name.rpartition('-')
        return base if separator and counter.isdigit() else name

    @classmethod
    def display_name(cls, action, fallback: str) -> str:
        if isinstance(action, Node):
            try:
                name = action.node_name
                unspecified = getattr(Node, 'UNSPECIFIED_NODE_NAME', None)
                if unspecified and unspecified in name:
                    name = name.replace(
                        unspecified, cls.process_name_without_counter(fallback)
                    )
                return name.replace('<node_namespace_unspecified>', '').lstrip('/')
            except (AttributeError, RuntimeError):
                pass
        return cls.process_name_without_counter(fallback)

    @staticmethod
    def namespace_for_name(name: str) -> str:
        parts = [part for part in name.strip('/').split('/') if part]
        return parts[0] if len(parts) > 1 else '/'
