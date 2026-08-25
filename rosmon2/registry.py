"""Process record registry independent of ROS launch and terminal code."""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from .model import ProcessRecord


class ProcessRegistry:
    """Own logical records and action-to-record associations."""

    def __init__(self) -> None:
        self._records: list[ProcessRecord] = []
        self._by_action: Dict[object, ProcessRecord] = {}
        self._next_key = 0

    @property
    def records(self) -> list[ProcessRecord]:
        return self._records

    def __iter__(self) -> Iterable[ProcessRecord]:
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def by_action(self, action: object) -> Optional[ProcessRecord]:
        return self._by_action.get(action)

    def bind(self, action: object, record: ProcessRecord) -> None:
        self._by_action[action] = record
        record.action = action

    def unbind(self, action: object) -> None:
        self._by_action.pop(action, None)

    def add(self, record: ProcessRecord) -> ProcessRecord:
        if record not in self._records:
            self._records.append(record)
        self._next_key = max(self._next_key, record.key + 1)
        return record

    def create(self, name: str, namespace: str = '/') -> ProcessRecord:
        record = ProcessRecord(
            key=self._next_key,
            display_name=name,
            namespace=namespace,
        )
        self._next_key += 1
        self._records.append(record)
        return record

    def remove(self, record: ProcessRecord) -> None:
        self._records.remove(record)
        for action, candidate in tuple(self._by_action.items()):
            if candidate is record:
                self._by_action.pop(action, None)
