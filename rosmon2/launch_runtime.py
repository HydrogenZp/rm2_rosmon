"""Small adapter around the public ROS 2 launch service lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import sys
from typing import Callable, Optional

from launch import LaunchDescription, LaunchService
from launch.actions import ExecuteProcess, IncludeLaunchDescription, RegisterEventHandler
from launch.event_handlers import OnProcessExit, OnProcessIO, OnProcessStart
from launch.events import IncludeLaunchDescription as IncludeLaunchDescriptionEvent
from launch.events import Shutdown
from launch.events.process import ShutdownProcess
from launch.launch_description_sources import AnyLaunchDescriptionSource

from .launch_compat import attach_screen_stream, parse_arguments, screen_handler


class _UILogStream:
    """File-like adapter used only at the launch/UI integration boundary."""

    encoding = getattr(sys.stdout, 'encoding', 'utf-8')

    def __init__(self, log: Callable[[str], None], flush: Callable[[], None]):
        self._log = log
        self._flush = flush

    def write(self, message: str) -> int:
        if message and message.strip():
            self._log(message)
        return len(message)

    def flush(self) -> None:
        self._flush()


class LaunchRuntime:
    """Own LaunchService and all launch-framework interactions.

    Process lifecycle code never executes actions directly.  Runtime additions
    are included through ``LaunchService.include_launch_description`` so the
    launch service remains the sole owner of action execution and shutdown.
    """

    def __init__(
            self, launch_file: str, launch_arguments: list[str], *,
            on_start, on_exit, on_stdout, on_stderr) -> None:
        self.launch_file = launch_file
        self.launch_arguments = list(launch_arguments)
        self._on_start = on_start
        self._on_exit = on_exit
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self.service: Optional[LaunchService] = None
        self.context = None
        self._shutdown_requested = False
        self._shutdown_emitted = False
        self._screen_restore: Optional[Callable[[], None]] = None
        self._event_tasks: set[asyncio.Task] = set()

    def prepare(self) -> None:
        """Create the service and include the initial launch description."""
        handlers = [
            RegisterEventHandler(OnProcessStart(on_start=self._on_start)),
            RegisterEventHandler(OnProcessIO(
                on_stdout=self._on_stdout,
                on_stderr=self._on_stderr,
            )),
            RegisterEventHandler(OnProcessExit(on_exit=self._on_exit)),
        ]
        include = IncludeLaunchDescription(
            AnyLaunchDescriptionSource(self.launch_file),
            launch_arguments=parse_arguments(self.launch_arguments),
        )
        self.service = LaunchService(
            argv=self.launch_arguments,
            noninteractive=True,
        )
        self.context = self.service.context
        self.service.include_launch_description(
            LaunchDescription([*handlers, include])
        )
        if self._shutdown_requested:
            self._emit_shutdown()

    async def run(self) -> int:
        if self.service is None:
            self.prepare()
        return await self.service.run_async(shutdown_when_idle=False)

    def include_process(self, action: ExecuteProcess) -> None:
        """Add a restart action through the public LaunchService API."""
        if self.service is None:
            raise RuntimeError('launch runtime is not prepared')
        description = LaunchDescription([action])
        # ``LaunchService.include_launch_description`` is thread-safe but its
        # Jazzy implementation waits on a future when called while the launch
        # loop is already running.  Calling it from a control-socket callback
        # would therefore deadlock the loop and never return the response.
        # Emit the public IncludeLaunchDescription event directly in that
        # same-loop case; LaunchService remains the sole action executor.
        if self.context is not None:
            self._emit_context_event(IncludeLaunchDescriptionEvent(description))
            return
        self.service.include_launch_description(description)

    @staticmethod
    def call_soon(callback, *args) -> None:
        asyncio.get_running_loop().call_soon(callback, *args)

    def request_process_stop(
            self, action: object, *, process_name: Optional[str] = None) -> None:
        if self.context is None:
            raise RuntimeError('launch context is not available')

        names = {
            value for value in (
                process_name,
                getattr(action, 'name', None),
                getattr(action, 'process_name', None),
            ) if isinstance(value, str)
        }

        def matches(candidate) -> bool:
            if candidate is action:
                return True
            if isinstance(candidate, str):
                return candidate in names
            for attribute in ('action', 'process_name', 'name'):
                value = getattr(candidate, attribute, None)
                if value is action or (isinstance(value, str) and value in names):
                    return True
            return False

        self._emit_context_event(ShutdownProcess(process_matcher=matches))

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        if self.context is not None and not self._shutdown_emitted:
            self._emit_shutdown()

    def _emit_shutdown(self) -> None:
        if self._shutdown_emitted:
            return
        self._shutdown_emitted = True
        if self.service is not None:
            result = self.service.shutdown()
            if inspect.isawaitable(result):
                task = asyncio.create_task(result)
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)
            return
        self._emit_context_event(Shutdown(reason='rosmon2 shutdown requested'))

    def _emit_context_event(self, event) -> None:
        """Queue an event without blocking the currently running launch loop."""
        if self.context is None:
            return
        if not hasattr(self.context, 'emit_event'):
            self.context.emit_event_sync(event)
            return
        try:
            task = asyncio.create_task(self.context.emit_event(event))
        except RuntimeError:
            self.context.emit_event_sync(event)
            return
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def cancel_tasks(self) -> None:
        tasks = list(self._event_tasks)
        self._event_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def attach_screen_stream(
            self, log: Callable[[str], None], flush: Callable[[], None]) -> None:
        """Route launch screen output through the UI.

        ROS 2 does not expose a stable screen-handler setter across all target
        distros.  This small compatibility boundary is the only place where
        the launch logging implementation is touched.
        """
        self._screen_restore = attach_screen_stream(
            screen_handler(), _UILogStream(log, flush)
        )

    def restore_screen_stream(self) -> None:
        if self._screen_restore is not None:
            self._screen_restore()
            self._screen_restore = None
