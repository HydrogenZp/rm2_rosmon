"""ROS 2 launch integration and process supervision."""

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from .control import ControlError, ControlServer
from .launch_runtime import LaunchRuntime
from .model import ProcessRecord, selection_key, State
from .process_supervisor import ProcessSupervisor
from .registry import ProcessRegistry
from .terminal import TerminalUI


class Supervisor:
    """Coordinate LaunchRuntime, ProcessSupervisor, registry, and TerminalUI."""

    def __init__(self, launch_file: str, launch_arguments, *, ui: bool = True,
                 no_start: bool = False, stop_timeout: float = 5.0,
                 log_file: Optional[str] = None, flush_log: bool = False,
                 flush_stdout: bool = False, session: str = 'default',
                 json_events: bool = False, control: bool = True):
        self.launch_file = launch_file
        self.launch_arguments = list(launch_arguments)
        self.no_start = no_start
        self.stop_timeout = stop_timeout
        self.flush_stdout = flush_stdout
        self.session = session
        self.json_events = json_events
        self.registry = ProcessRegistry()
        self.records = self.registry.records
        # Kept as a read-only compatibility view for existing integrations;
        # all writes go through ProcessRegistry.
        self._by_action = self.registry._by_action
        self.runtime = LaunchRuntime(
            launch_file,
            self.launch_arguments,
            on_start=self._on_start,
            on_exit=self._on_exit,
            on_stdout=lambda event: self._on_output(event, False),
            on_stderr=lambda event: self._on_output(event, True),
        )
        self.process_supervisor = ProcessSupervisor(
            self.registry,
            self.runtime,
            stop_timeout=stop_timeout,
            on_changed=self._on_process_changed,
        )
        self._context = None
        self._shutting_down = False
        self._shutdown_complete = False
        self._shutdown_lock: Optional[asyncio.Lock] = None
        self._event_sequence = 0
        self._event_listeners = []
        self._logs = deque(maxlen=5000)
        self._tasks: set[asyncio.Task] = set()
        self._control_server = ControlServer(self, session) if control else None
        self._log_handle = (
            open(log_file, 'a', buffering=1 if flush_log else -1)
            if log_file else None
        )
        self.ui = TerminalUI(ui, self.handle_key, output_enabled=not json_events)
        if json_events:
            self.add_event_listener(self._print_json_event)

    @property
    def _context(self):
        """Compatibility view of the context owned by LaunchRuntime."""
        return self.runtime.context

    @_context.setter
    def _context(self, value) -> None:
        self.runtime.context = value

    async def run(self) -> int:
        """Run launch and always complete the process shutdown protocol."""
        loop = asyncio.get_running_loop()
        self._shutdown_lock = asyncio.Lock()
        self.runtime.prepare()
        self._context = self.runtime.context
        # ExecuteProcess output is already delivered through OnProcessIO,
        # where it gets the real process label.  The launch screen handler
        # formats the same bytes as ``launch: [name-N] ...``; forwarding that
        # stream as well produces duplicate lines and a misleading ``launch``
        # source column in the rosmon UI.  Keep the handler attached only to
        # drain its output.  Launch lifecycle failures are still reported by
        # the process and launch callbacks below.
        self.runtime.attach_screen_stream(lambda _message: None, lambda: None)
        control_started = False
        session_started = False
        try:
            if self._control_server is not None:
                await self._control_server.start()
                control_started = True
            self.ui.start(loop)
            self.ui.set_records(self.records)
            self._emit_event(
                'session_started',
                launch_file=self.launch_file,
                launch_arguments=self.launch_arguments,
                socket=str(self._control_server.path) if self._control_server else None,
            )
            session_started = True
            return await self.runtime.run()
        finally:
            if session_started:
                self._emit_event('session_stopping')
            await self.shutdown()
            await self.runtime.cancel_tasks()
            self.runtime.restore_screen_stream()
            if self._control_server is not None and control_started:
                await self._control_server.close()
            if self._log_handle:
                self._log_handle.close()
            self.ui.close(loop)
            await self._cancel_tasks()

    async def shutdown(self) -> None:
        """Stop children, wait/kill stragglers, then request launch shutdown."""
        if self._shutdown_lock is None:
            self._shutdown_lock = asyncio.Lock()
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutting_down = True
            self.ui.pause_input()
            if self._control_server is not None:
                await self._control_server.close()
            await self.process_supervisor.shutdown()
            self.runtime.request_shutdown()
            self._shutdown_complete = True

    def create_task(self, coroutine) -> asyncio.Task:
        """Create a task owned by this supervisor and awaited on shutdown."""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _cancel_tasks(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def request_shutdown(self) -> asyncio.Task:
        """Schedule shutdown for signal handlers without losing ownership."""
        return self.create_task(self.shutdown())

    def _on_start(self, event, context):
        self._context = context
        record = self.process_supervisor.on_start(event, context)
        self._write_log(record.display_name, f'process started with pid {event.pid}', False)
        self._emit_event('node_started', node=self._record_dict(record))
        if self.no_start:
            self.runtime.call_soon(self.stop, record)
        self.ui.set_records(self.records)

    def _on_process_changed(self, record: ProcessRecord, reason: str) -> None:
        self.ui.set_records(self.records)

    @staticmethod
    def _display_name(action, fallback: str) -> str:
        return ProcessSupervisor.display_name(action, fallback)

    @staticmethod
    def _process_name_without_counter(name: str) -> str:
        """Remove launch's numeric ``-N`` suffix from a process label."""
        return ProcessSupervisor.process_name_without_counter(name)

    @staticmethod
    def _normalize_display_name(name: str) -> str:
        """Format a ROS name like rosmon, without its leading root slash."""
        name = name.replace('<node_namespace_unspecified>', '')
        return name.lstrip('/')

    def _on_output(self, event, is_stderr: bool):
        record = self.registry.by_action(event.action)
        source = record.display_name if record else event.process_name
        raw_text = event.text
        text = (raw_text.decode(errors='replace')
                if isinstance(raw_text, (bytes, bytearray)) else str(raw_text))
        self._write_log(source, text.rstrip('\n'), is_stderr)
        self._record_output(source, text, is_stderr)
        if record is None or not record.muted:
            self.ui.log(source, text, is_stderr=is_stderr)
        if self.flush_stdout:
            self.ui.flush()

    def _write_log(self, source: str, text: str, is_stderr: bool) -> None:
        if not self._log_handle:
            return
        channel = 'stderr' if is_stderr else 'stdout'
        for line in text.splitlines() or ['']:
            self._log_handle.write(f'[{channel}] {source}: {line}\n')

    def _on_exit(self, event, context):
        record = self.process_supervisor.on_exit(event, context)
        if record is None:
            return
        self._write_log(record.display_name,
                        f'process exited with code {event.returncode}', event.returncode != 0)
        self._emit_event('node_exited', node=self._record_dict(record))
        self.ui.set_records(self.records)

    def handle_key(self, key: str) -> None:
        """Apply rosmon's two-key node action interface."""
        if self.ui.search_active:
            self._handle_search_key(key)
            return

        if key == 'F5':
            self.ui.namespace_mode = not self.ui.namespace_mode
            self.ui.namespace_inspect = None
            self.ui.selected = None
            self.ui.redraw()
            return

        if self.ui.selected is None:
            if key == '/':
                self.ui.search_active = True
                self.ui.search_query = ''
                self.ui.search_selected = 0
                self.ui.redraw()
                return
            if key == 'F6':
                for record in self.records:
                    self.start(record)
                return
            if key == 'F7':
                for record in self.records:
                    self.stop(record)
                return
            if key == 'F8':
                self.ui.warn_only = not self.ui.warn_only
                self.ui.redraw()
                return
            if key == 'F9':
                for record in self.records:
                    record.muted = True
                self.ui.redraw()
                return
            if key == 'F10':
                for record in self.records:
                    record.muted = False
                self.ui.redraw()
                return
            if (self.ui.namespace_mode and self.ui.namespace_inspect is not None
                    and key in ('\b', '\x7f')):
                self.ui.namespace_inspect = None
                self.ui.redraw()
                return
            selectable_count = (
                len(self.ui.namespaces())
                if self.ui.namespace_mode and self.ui.namespace_inspect is None
                else len(self.ui.visible_records())
            )
            for index in range(selectable_count):
                if key == selection_key(index):
                    self.ui.selected = index
                    self.ui.redraw()
                    return
            return

        index = self.ui.selected
        self.ui.selected = None
        if self.ui.namespace_mode and self.ui.namespace_inspect is None:
            namespaces = self.ui.namespaces()
            if index >= len(namespaces):
                return
            namespace = namespaces[index]
            records = self.ui.records_in_namespace(namespace)
            if key == 's':
                for record in records:
                    self.start(record)
            elif key == 'k':
                for record in records:
                    self.stop(record)
            elif key == 'i':
                self.ui.namespace_inspect = namespace
            elif key == 'm':
                for record in records:
                    record.muted = True
            elif key == 'u':
                for record in records:
                    record.muted = False
            self.ui.redraw()
            return

        records = self.ui.visible_records()
        if index >= len(records):
            return
        record = records[index]
        if key == 's':
            self.start(record)
        elif key == 'k':
            self.stop(record)
        elif key == 'm':
            record.muted = True
        elif key == 'u':
            record.muted = False
        elif key == 'd':
            self.debug(record)
        self.ui.redraw()

    def _handle_search_key(self, key: str) -> None:
        """Edit or navigate the interactive full-name node search."""
        matches = self.ui.search_matches()
        if key in ('\n', '\r'):
            selected = (
                matches[self.ui.search_selected]
                if self.ui.search_selected < len(matches) else None
            )
            self.ui.search_active = False
            self.ui.search_query = ''
            self.ui.search_selected = 0
            self.ui.selected = None
            if selected is not None:
                # Search always selects an individual node, even when it was
                # opened from the namespace overview.
                self.ui.namespace_mode = False
                self.ui.namespace_inspect = None
                self.ui.selected = self.records.index(selected)
            self.ui.redraw()
            return

        if key == 'ESC':
            self.ui.search_active = False
            self.ui.search_query = ''
            self.ui.search_selected = 0
            self.ui.selected = None
            self.ui.redraw()
            return

        if key in ('\b', '\x7f'):
            self.ui.search_query = self.ui.search_query[:-1]
            self.ui.search_selected = 0
        elif key in ('\t', 'RIGHT', 'DOWN'):
            if matches:
                self.ui.search_selected = (self.ui.search_selected + 1) % len(matches)
        elif key in ('LEFT', 'UP'):
            if matches:
                self.ui.search_selected = (self.ui.search_selected - 1) % len(matches)
        elif len(key) == 1 and key.isprintable() and not key.isspace():
            self.ui.search_query += key
            self.ui.search_selected = 0

        matches = self.ui.search_matches()
        if matches and self.ui.search_selected >= len(matches):
            self.ui.search_selected = 0
        self.ui.redraw()

    def stop(self, record: ProcessRecord) -> None:
        """Gracefully stop one launch process through ProcessSupervisor."""
        self.process_supervisor.stop(record)
        self.ui.redraw()

    def start(self, record: ProcessRecord) -> None:
        """Start a stopped process through LaunchRuntime."""
        self.process_supervisor.start(record)
        self.ui.redraw()

    def restart(self, record: ProcessRecord) -> None:
        """Restart a process, waiting for its matching exit event."""
        self.process_supervisor.restart(record)
        self.ui.redraw()

    def debug(self, record: ProcessRecord) -> None:
        """Start a stopped process under gdb without mutating its command."""
        try:
            self.process_supervisor.debug(record)
        except RuntimeError as exc:
            self.ui.notice(str(exc), error=True)
        self.ui.redraw()

    def add_event_listener(self, listener: Callable[[Dict], None]) -> None:
        """Register a callback for structured supervisor events."""
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[Dict], None]) -> None:
        """Remove a structured event callback."""
        try:
            self._event_listeners.remove(listener)
        except ValueError:
            pass

    def _emit_event(self, event_type: str, **fields) -> Dict:
        self._event_sequence += 1
        event = {
            'event': event_type,
            'sequence': self._event_sequence,
            'session': self.session,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        event.update(fields)
        for listener in tuple(self._event_listeners):
            listener(event)
        return event

    @staticmethod
    def _print_json_event(event: Dict) -> None:
        print(json.dumps(event, separators=(',', ':'), sort_keys=True), flush=True)

    def _record_output(self, source: str, text: str, is_stderr: bool) -> None:
        for line in text.replace('\r\n', '\n').replace('\r', '\n').splitlines():
            severity = self.ui._severity(line, None, is_stderr)
            entry = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'timestamp_epoch': time.time(),
                'node': source,
                'stream': 'stderr' if is_stderr else 'stdout',
                'severity': severity,
                'message': line,
            }
            self._logs.append(entry)
            public_entry = dict(entry)
            public_entry.pop('timestamp_epoch')
            self._emit_event('log', log=public_entry)

    @staticmethod
    def _namespace_for(record: ProcessRecord) -> str:
        if record.namespace != '/':
            return record.namespace
        parts = [part for part in record.display_name.strip('/').split('/') if part]
        return parts[0] if len(parts) > 1 else '/'

    def _record_dict(self, record: ProcessRecord) -> Dict:
        return {
            'key': record.key,
            'name': '/' + record.display_name.lstrip('/'),
            'namespace': self._namespace_for(record),
            'state': record.state.value,
            'pid': record.pid,
            'muted': record.muted,
            'restart_count': record.restart_count,
            'exit_code': record.exit_code,
            'return_code': record.exit_code,
            'process_name': record.process_name,
            'expected_stop': record.expected_stop,
            'command': list(record.cmd),
        }

    def _selected_records(self, request: Dict, *, strict: bool = True):
        node = request.get('node')
        namespace = request.get('namespace')
        all_nodes = bool(request.get('all'))
        selected = list(self.records)
        if node:
            normalized = str(node).lstrip('/')
            selected = [
                record for record in selected
                if record.display_name.lstrip('/') == normalized
            ]
        elif namespace:
            normalized = str(namespace).strip('/')
            if not normalized:
                selected = [
                    record for record in selected
                    if self._namespace_for(record) == '/'
                ]
            else:
                prefix = normalized + '/'
                selected = [
                    record for record in selected
                    if record.display_name.lstrip('/').startswith(prefix)
                ]
        elif not all_nodes:
            if strict:
                raise ControlError('specify node, namespace, or all=true')
            return selected
        if strict and not selected:
            target = node if node else namespace
            raise ControlError(f'no processes match target {target!r}')
        return selected

    def _status(self, request: Dict) -> Dict:
        if any(request.get(field) for field in ('node', 'namespace')):
            records = self._selected_records(request)
        else:
            records = list(self.records)
        states = {state.value: 0 for state in State}
        for record in records:
            states[record.state.value] += 1
        namespaces = []
        for namespace in sorted(
                {self._namespace_for(record) for record in records},
                key=lambda value: (value != '/', value)):
            members = [
                record for record in records
                if self._namespace_for(record) == namespace
            ]
            alive = sum(record.state is State.RUNNING for record in members)
            namespaces.append({
                'name': namespace,
                'alive': alive,
                'dead': len(members) - alive,
                'muted': bool(members) and all(record.muted for record in members),
            })
        return {
            'ok': True,
            'session': self.session,
            'launch_file': self.launch_file,
            'shutting_down': self._shutting_down,
            'summary': {'total': len(records), **states},
            'namespaces': namespaces,
            'nodes': [self._record_dict(record) for record in records],
        }

    def _log_response(self, request: Dict) -> Dict:
        selected_names = None
        if any(request.get(field) for field in ('node', 'namespace')):
            selected_names = {
                record.display_name.lstrip('/')
                for record in self._selected_records(request)
            }
        severity = request.get('severity')
        if severity:
            severity = str(severity).upper()
            if severity == 'WARN':
                severity = 'WARNING'
        since_seconds = float(request.get('since_seconds', 0))
        cutoff = time.time() - since_seconds if since_seconds > 0 else 0
        limit = int(request.get('limit', 200))
        if limit < 1 or limit > 5000:
            raise ControlError('log limit must be between 1 and 5000')
        matches = []
        for entry in self._logs:
            if selected_names is not None and entry['node'].lstrip('/') not in selected_names:
                continue
            if severity and entry['severity'] != severity:
                continue
            if entry['timestamp_epoch'] < cutoff:
                continue
            public_entry = dict(entry)
            public_entry.pop('timestamp_epoch')
            matches.append(public_entry)
        return {
            'ok': True,
            'session': self.session,
            'logs': matches[-limit:],
        }

    async def control_request(self, request: Dict) -> Dict:
        """Execute one machine-facing request in the launch event loop."""
        command = request.get('command')
        if command == 'status':
            return self._status(request)
        if command == 'logs':
            return self._log_response(request)
        if command == 'wait':
            return await self._wait_for_state(request)
        if command not in ('start', 'stop', 'restart', 'mute', 'unmute'):
            raise ControlError(f'unknown control command: {command!r}')

        records = self._selected_records(request)
        for record in records:
            if command == 'start':
                self.start(record)
            elif command == 'stop':
                self.stop(record)
            elif command == 'restart':
                self.restart(record)
            elif command == 'mute':
                record.muted = True
            elif command == 'unmute':
                record.muted = False
        self.ui.redraw()
        self._emit_event(
            'control_action',
            action=command,
            nodes=[self._record_dict(record) for record in records],
        )
        return {
            'ok': True,
            'session': self.session,
            'action': command,
            'matched': len(records),
            'nodes': [self._record_dict(record) for record in records],
        }

    async def _wait_for_state(self, request: Dict) -> Dict:
        desired = str(request.get('state', State.RUNNING.value)).lower()
        if desired not in {state.value for state in State}:
            raise ControlError(
                'state must be one of: ' +
                ', '.join(state.value for state in State)
            )
        timeout = float(request.get('timeout', 30.0))
        if timeout < 0:
            raise ControlError('timeout cannot be negative')
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            records = self._selected_records(request, strict=False)
            if records and all(record.state.value == desired for record in records):
                return {
                    'ok': True,
                    'session': self.session,
                    'state': desired,
                    'matched': len(records),
                    'nodes': [self._record_dict(record) for record in records],
                }
            if asyncio.get_running_loop().time() >= deadline:
                current = [self._record_dict(record) for record in records]
                raise ControlError(
                    f'timed out after {timeout:g}s waiting for state {desired}; '
                    f'current nodes: {current}'
                )
            await asyncio.sleep(0.1)
