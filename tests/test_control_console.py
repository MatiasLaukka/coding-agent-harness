import ctypes

import pytest

from coding_agent_harness import console as console_module
from coding_agent_harness.console import (
    ConsoleDeadlineExpired,
    ConsoleInputFailure,
    InvalidConsoleLine,
    WindowsConsole,
)


class FakeClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


class FakeWinApi:
    def __init__(self, *, input_handle=11, output_handle=22, clock=None, input_mode=0x00C0):
        self.input_handle = input_handle
        self.output_handle = output_handle
        self.clock = clock or FakeClock()
        self.modes = {input_handle: input_mode, output_handle: 0x0001}
        self.events = []
        self.output = []
        self.flush_count = 0
        self.fail_input_mode = False
        self.fail_output_screen = False

    def GetStdHandle(self, kind):
        if kind == console_module.STD_INPUT_HANDLE:
            return self.input_handle
        if kind == console_module.STD_OUTPUT_HANDLE:
            return self.output_handle
        return 0

    def GetConsoleMode(self, handle, mode_ptr):
        if handle not in self.modes:
            return 0
        mode_ptr._obj.value = self.modes[handle]
        return 1

    def SetConsoleMode(self, handle, mode):
        if self.fail_input_mode:
            return 0
        self.modes[handle] = int(mode)
        return 1

    def GetConsoleScreenBufferInfo(self, handle, info_ptr):
        return 0 if self.fail_output_screen else 1

    def WaitForSingleObject(self, handle, milliseconds):
        if self.events:
            return console_module.WAIT_OBJECT_0
        self.clock.value += milliseconds / 1000
        return console_module.WAIT_TIMEOUT

    def GetNumberOfConsoleInputEvents(self, handle, count_ptr):
        count_ptr._obj.value = len(self.events)
        return 1

    def ReadConsoleInputW(self, handle, records, requested, read_ptr):
        count = min(len(self.events), requested)
        for index in range(count):
            records[index] = self.events.pop(0)
        read_ptr._obj.value = count
        return 1

    def FlushConsoleInputBuffer(self, handle):
        self.flush_count += 1
        self.events.clear()
        return 1

    def WriteConsoleW(self, handle, text, count, written_ptr, _reserved):
        value = text[:count]
        self.output.append(value)
        written_ptr._obj.value = len(value)
        return 1


def key_event(char="", *, virtual_key=0, repeats=1, key_down=True):
    record = console_module._InputRecord()
    record.EventType = console_module.KEY_EVENT
    key = record.Event.KeyEvent
    key.bKeyDown = key_down
    key.wRepeatCount = repeats
    key.wVirtualKeyCode = virtual_key
    if char:
        key.uChar.UnicodeChar = char
    return record


def type_line(api, line):
    api.events.extend(key_event(char) for char in line)
    api.events.append(key_event("\r", virtual_key=console_module.VK_RETURN))


def test_windows_console_validates_handles_and_restores_changed_mode():
    clock = FakeClock()
    api = FakeWinApi(clock=clock)
    original = api.modes[api.input_handle]

    with WindowsConsole.open(api=api, now=clock) as console:
        changed = api.modes[api.input_handle]
        assert changed & console_module.ENABLE_PROCESSED_INPUT
        assert changed & console_module.ENABLE_QUICK_EDIT_MODE == 0
        console.write("trusted output\n")
        assert "trusted output\n" == "".join(api.output)

    assert api.modes[api.input_handle] == original


def test_windows_console_rejects_redirected_or_unsupported_handles():
    with pytest.raises(console_module.ConsoleUnavailableError):
        WindowsConsole.open(api=FakeWinApi(input_handle=0))

    api = FakeWinApi()
    api.fail_output_screen = True
    with pytest.raises(console_module.ConsoleUnavailableError):
        WindowsConsole.open(api=api)


def test_console_reads_one_line_and_discards_pasted_trailing_approvals():
    clock = FakeClock()
    api = FakeWinApi(clock=clock)
    type_line(api, "START A1B2C3D4")
    type_line(api, "ACCEPT E5F6A7B8")
    console = WindowsConsole.open(api=api, now=clock)

    assert console.read_line(deadline=clock.value + 1) == "START A1B2C3D4"
    assert api.flush_count == 1
    with pytest.raises(ConsoleDeadlineExpired):
        console.read_line(deadline=clock.value + 0.1)
    console.close()


def test_console_ignores_modifier_events_with_nul_unicode_character():
    clock = FakeClock()
    api = FakeWinApi(clock=clock)
    api.events.append(key_event("\x00", virtual_key=0x10))
    type_line(api, "START A1B2C3D4")
    console = WindowsConsole.open(api=api, now=clock)

    assert console.read_line(deadline=clock.value + 1) == "START A1B2C3D4"
    console.close()


def test_console_bounds_key_repeats_and_line_length():
    clock = FakeClock()
    api = FakeWinApi(clock=clock)
    api.events.append(key_event("x", repeats=console_module.MAX_KEY_REPEAT + 1))
    console = WindowsConsole.open(api=api, now=clock)
    with pytest.raises(InvalidConsoleLine):
        console.read_line(deadline=clock.value + 2)

    type_line(api, "x" * (console_module.MAX_LINE_CHARS + 1))
    with pytest.raises(InvalidConsoleLine):
        console.read_line(deadline=clock.value + 3)
    console.close()


def test_console_deadline_expires_without_blocking_input_reader():
    clock = FakeClock()
    api = FakeWinApi(clock=clock)
    console = WindowsConsole.open(api=api, now=clock)
    with pytest.raises(ConsoleDeadlineExpired):
        console.read_line(deadline=clock.value + 0.1)
    console.close()
