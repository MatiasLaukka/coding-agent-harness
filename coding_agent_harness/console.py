from __future__ import annotations

import ctypes
import math
import os
import time
from ctypes import wintypes
from typing import Protocol


STD_INPUT_HANDLE = 0xFFFFFFF6
STD_OUTPUT_HANDLE = 0xFFFFFFF5
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
KEY_EVENT = 0x0001
VK_RETURN = 0x000D
VK_BACK = 0x0008
MAX_EVENT_BATCH = 256
MAX_EVENTS_PER_TRANSACTION = 4096
MAX_KEY_REPEAT = 16
MAX_LINE_CHARS = 4096
MAX_WRITE_CHARS = 32 * 1024 * 1024


class ConsoleError(RuntimeError):
    """Base class for failures in the supported console backend."""


class ConsoleUnavailableError(ConsoleError):
    pass


class ConsoleInputFailure(ConsoleError):
    pass


class ConsoleDeadlineExpired(ConsoleError):
    pass


class InvalidConsoleLine(ConsoleError):
    """A bounded input line was rejected; the caller may retry in its deadline."""


class ReviewConsole(Protocol):
    def write(self, text: str) -> None: ...

    def discard_typeahead(self) -> None: ...

    def read_line(self, *, deadline: float) -> str: ...


class _CharUnion(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", wintypes.CHAR)]


class _KeyEvent(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _CharUnion),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _InputEventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEvent), ("Padding", ctypes.c_byte * 16)]


class _InputRecord(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _InputEventUnion)]


class _Coord(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _SmallRect(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT),
    ]


class _ConsoleScreenBufferInfo(ctypes.Structure):
    _fields_ = [
        ("dwSize", _Coord),
        ("dwCursorPosition", _Coord),
        ("wAttributes", wintypes.WORD),
        ("srWindow", _SmallRect),
        ("dwMaximumWindowSize", _Coord),
    ]


def _value(handle) -> int | None:
    if type(handle) is int:
        return handle
    return getattr(handle, "value", None)


def _valid_handle(handle) -> bool:
    value = _value(handle)
    return value not in (None, 0, -1, ctypes.c_void_p(-1).value)


def _set_api_signatures(api) -> None:
    signatures = {
        "GetStdHandle": (wintypes.HANDLE, [wintypes.DWORD]),
        "GetConsoleMode": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
        "SetConsoleMode": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD]),
        "GetConsoleScreenBufferInfo": (
            wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_ConsoleScreenBufferInfo)],
        ),
        "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        "GetNumberOfConsoleInputEvents": (
            wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        ),
        "ReadConsoleInputW": (
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(_InputRecord), wintypes.DWORD,
             ctypes.POINTER(wintypes.DWORD)],
        ),
        "FlushConsoleInputBuffer": (wintypes.BOOL, [wintypes.HANDLE]),
        "WriteConsoleW": (
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p],
        ),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(api, name, None)
        if function is not None:
            try:
                function.restype = restype
                function.argtypes = argtypes
            except (AttributeError, TypeError):
                # Injectable test APIs use ordinary Python callables.
                pass


class WindowsConsole:
    """Single-reader console I/O using bounded Win32 console events."""

    def __init__(self, *, api, input_handle, output_handle, now=time.monotonic):
        self._api = api
        self._input = input_handle
        self._output = output_handle
        self._now = now
        self._closed = False
        self._input_mode: int | None = None
        self._input_mode_changed = False

    @classmethod
    def open(cls, *, api=None, now=time.monotonic) -> "WindowsConsole":
        if os.name != "nt" and api is None:
            raise ConsoleUnavailableError("a Windows console is required")
        if api is None:
            api = ctypes.WinDLL("kernel32", use_last_error=True)
        _set_api_signatures(api)

        input_handle = api.GetStdHandle(STD_INPUT_HANDLE)
        output_handle = api.GetStdHandle(STD_OUTPUT_HANDLE)
        if not _valid_handle(input_handle) or not _valid_handle(output_handle):
            raise ConsoleUnavailableError("supported console handles are unavailable")

        input_mode = wintypes.DWORD()
        output_mode = wintypes.DWORD()
        if not api.GetConsoleMode(input_handle, ctypes.byref(input_mode)):
            raise ConsoleUnavailableError("standard input is not a console handle")
        if not api.GetConsoleMode(output_handle, ctypes.byref(output_mode)):
            raise ConsoleUnavailableError("standard output is not a console handle")
        screen = _ConsoleScreenBufferInfo()
        if not api.GetConsoleScreenBufferInfo(output_handle, ctypes.byref(screen)):
            raise ConsoleUnavailableError("standard output is not a supported console screen buffer")

        console = cls(api=api, input_handle=input_handle, output_handle=output_handle, now=now)
        console._input_mode = int(input_mode.value)
        desired = (console._input_mode | ENABLE_PROCESSED_INPUT | ENABLE_EXTENDED_FLAGS)
        desired &= ~ENABLE_QUICK_EDIT_MODE
        if desired != console._input_mode:
            if not api.SetConsoleMode(input_handle, desired):
                raise ConsoleUnavailableError("could not configure console input mode")
            console._input_mode_changed = True
        return console

    def __enter__(self) -> "WindowsConsole":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.close()
        except BaseException as restore_error:
            if exc is None:
                raise
            try:
                exc.console_restore_error = type(restore_error).__name__
            except Exception:
                pass
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._input_mode_changed and self._input_mode is not None:
            if not self._api.SetConsoleMode(self._input, self._input_mode):
                raise ConsoleInputFailure("console input mode restoration failed")
            self._input_mode_changed = False

    def write(self, text: str) -> None:
        if type(text) is not str or len(text) > MAX_WRITE_CHARS:
            raise ConsoleInputFailure("review output exceeded its bound")
        offset = 0
        while offset < len(text):
            chunk = text[offset:offset + 8192]
            written = wintypes.DWORD()
            if not self._api.WriteConsoleW(
                self._output, chunk, len(chunk), ctypes.byref(written), None,
            ) or written.value <= 0:
                raise ConsoleInputFailure("console output failed")
            offset += int(written.value)

    def discard_typeahead(self) -> None:
        if not self._api.FlushConsoleInputBuffer(self._input):
            raise ConsoleInputFailure("console input flush failed")

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._now()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ConsoleDeadlineExpired("review deadline expired")
        return remaining

    def _read_batch(self, deadline: float) -> list[_InputRecord]:
        while True:
            remaining = self._remaining(deadline)
            milliseconds = max(1, min(50, math.ceil(remaining * 1000)))
            result = self._api.WaitForSingleObject(self._input, milliseconds)
            self._remaining(deadline)
            if result == WAIT_TIMEOUT:
                continue
            if result == WAIT_FAILED or result != WAIT_OBJECT_0:
                raise ConsoleInputFailure("console wait failed")

            available = wintypes.DWORD()
            if not self._api.GetNumberOfConsoleInputEvents(self._input, ctypes.byref(available)):
                raise ConsoleInputFailure("console event count failed")
            count = min(int(available.value), MAX_EVENT_BATCH)
            if count == 0:
                continue
            records = (_InputRecord * count)()
            read_count = wintypes.DWORD()
            if not self._api.ReadConsoleInputW(
                self._input, records, count, ctypes.byref(read_count),
            ):
                raise ConsoleInputFailure("console event read failed")
            self._remaining(deadline)
            if read_count.value == 0 or read_count.value > count:
                raise ConsoleInputFailure("console returned an invalid event batch")
            return list(records[:read_count.value])

    def read_line(self, *, deadline: float) -> str:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("deadline must be a finite monotonic timestamp")
        characters: list[str] = []
        events_seen = 0
        while True:
            records = self._read_batch(deadline)
            events_seen += len(records)
            if events_seen > MAX_EVENTS_PER_TRANSACTION:
                self.discard_typeahead()
                raise InvalidConsoleLine("too many input events")
            for record in records:
                self._remaining(deadline)
                if record.EventType != KEY_EVENT:
                    continue
                key = record.Event.KeyEvent
                if not key.bKeyDown:
                    continue
                repeats = int(key.wRepeatCount)
                if repeats < 1 or repeats > MAX_KEY_REPEAT:
                    self.discard_typeahead()
                    raise InvalidConsoleLine("key repeat bound exceeded")
                char = key.uChar.UnicodeChar
                if char == "\x03":
                    raise KeyboardInterrupt
                if int(key.wVirtualKeyCode) == VK_RETURN or char in ("\r", "\n"):
                    self._remaining(deadline)
                    self.discard_typeahead()
                    return "".join(characters)
                if int(key.wVirtualKeyCode) == VK_BACK or char == "\b":
                    del characters[max(0, len(characters) - repeats):]
                    continue
                if not char or char == "\x00":
                    continue
                if not char.isprintable():
                    self.discard_typeahead()
                    raise InvalidConsoleLine("control input is not accepted")
                if len(characters) + repeats > MAX_LINE_CHARS:
                    self.discard_typeahead()
                    raise InvalidConsoleLine("input line exceeded its bound")
                characters.extend([char] * repeats)
