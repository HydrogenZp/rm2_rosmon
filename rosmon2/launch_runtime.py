"""Small adapter around the public ROS 2 launch service lifecycle."""

from __future__ import annotations

import asyncio
import sys
from typing import Callable, Optional

from launch import LaunchDescription, LaunchService
from launch.actions import ExecuteProcess, IncludeLaunchDescription, RegisterEventHandler
from launch.event_handlers import OnProcessExit, OnProcessIO, OnProcessStart
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
        self.service.include_launch_description(LaunchDescription([action]))

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

        self.context.emit_event_sync(
            ShutdownProcess(process_matcher=matches)
        )

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        if self.context is not None and not self._shutdown_emitted:
            self._emit_shutdown()

    def _emit_shutdown(self) -> None:
        if self._shutdown_emitted:
            return
        self._shutdown_emitted = True
        self.context.emit_event_sync(
            Shutdown(reason='rosmon2 shutdown requested')
        )

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
