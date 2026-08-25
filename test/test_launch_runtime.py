from rosmon2 import launch_runtime
from rosmon2.launch_runtime import LaunchRuntime


class _Context:
    def emit_event_sync(self, event):
        self.event = event


class _ShutdownProcess:
    def __init__(self, *, process_matcher):
        self.process_matcher = process_matcher


def test_stop_matcher_accepts_jazzy_process_name(monkeypatch):
    context = _Context()
    runtime = object.__new__(LaunchRuntime)
    runtime.context = context
    action = object()
    monkeypatch.setattr(launch_runtime, 'ShutdownProcess', _ShutdownProcess)

    runtime.request_process_stop(action, process_name='probe-1')

    matcher = context.event.process_matcher
    assert matcher(action)
    assert matcher('probe-1')
    assert not matcher('other-process')
