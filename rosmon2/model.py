"""Pure process state and record types used by rosmon2."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class ProcessState(Enum):
    """States in the process lifecycle state machine."""

    STOPPED = 'stopped'
    STARTING = 'starting'
    RUNNING = 'running'
    STOPPING = 'stopping'
    CRASHED = 'crashed'



# Short import retained for callers; the enum itself has explicit names.
State = ProcessState


@dataclass
class ProcessRecord:
    """Observable information for one logical launch process."""

    key: int
    display_name: str
    namespace: str = '/'
    state: ProcessState = ProcessState.STOPPED
    action: object = None
    cmd: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    process_name: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    restart_count: int = 0
    expected_stop: bool = False
    muted: bool = False

    @property
    def name(self) -> str:
        return self.display_name

    @name.setter
    def name(self, value: str) -> None:
        self.display_name = value

    @property
    def command(self) -> List[str]:
        return self.cmd

    @command.setter
    def command(self, value: List[str]) -> None:
        self.cmd = value

    @property
    def environment(self) -> Optional[Dict[str, str]]:
        return self.env

    @environment.setter
    def environment(self, value: Optional[Dict[str, str]]) -> None:
        self.env = value

    @property
    def return_code(self) -> Optional[int]:
        return self.exit_code

    @return_code.setter
    def return_code(self, value: Optional[int]) -> None:
        self.exit_code = value

    @property
    def manually_stopped(self) -> bool:
        return self.expected_stop

    @manually_stopped.setter
    def manually_stopped(self, value: bool) -> None:
        self.expected_stop = value


def selection_key(index: int) -> Optional[str]:
    """Return rosmon's a-z, A-Z, 0-9 selection key for an index."""
    alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return alphabet[index] if 0 <= index < len(alphabet) else None
