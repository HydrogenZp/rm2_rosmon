from rosmon2.launch_compat import attach_screen_stream


class _ScreenHandler:
    def __init__(self, stream):
        self._ScreenHandler__stream = stream

    def setStream(self, stream):
        self._ScreenHandler__stream = stream


def test_screen_stream_restore_works_when_setter_returns_nothing():
    original = object()
    replacement = object()
    handler = _ScreenHandler(original)

    restore = attach_screen_stream(handler, replacement)
    assert handler._ScreenHandler__stream is replacement

    restore()
    assert handler._ScreenHandler__stream is original


class _ReturningScreenHandler(_ScreenHandler):
    def setStream(self, stream):
        previous = self._ScreenHandler__stream
        self._ScreenHandler__stream = stream
        return previous


def test_screen_stream_restore_prefers_previous_stream_returned_by_setter():
    original = object()
    replacement = object()
    handler = _ReturningScreenHandler(original)

    restore = attach_screen_stream(handler, replacement)
    restore()
    assert handler._ScreenHandler__stream is original
