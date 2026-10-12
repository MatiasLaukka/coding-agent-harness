from __future__ import annotations

import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence

import psutil
from pydantic import Field

from coding_agent_harness.models import StrictModel

# Retain prefixes, keep draining discarded bytes, and never wait for pipe EOF
# without a deadline. These bounds are independent of worker protocol content.
OUTPUT_LIMIT_BYTES = 1024 * 1024
DRAIN_SECONDS = 0.2


class ProcessResult(StrictModel):
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_seconds: float = Field(ge=0)
    started: bool = True
    failure_type: str | None = None
    cancelled: bool = False
    output_complete: bool = True
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cleanup_attempted: bool = False
    cleanup_errors: list[str] = Field(default_factory=list)
    surviving_pids: list[int] = Field(default_factory=list)

    @property
    def infrastructure_failed(self) -> bool:
        return bool(
            not self.started or self.failure_type or self.cancelled or self.timed_out
            or not self.output_complete or self.stdout_truncated or self.stderr_truncated
            or self.cleanup_errors or self.surviving_pids or self.exit_code is None
        )


class CleanupResult(StrictModel):
    errors: list[str] = Field(default_factory=list)
    surviving_pids: list[int] = Field(default_factory=list)


def _kill_process_tree(pid: int) -> CleanupResult:
    """Best effort for currently discoverable descendants, not containment."""
    result = CleanupResult()
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        # An exited root no longer proves that its children exited.
        result.errors.append('tree-root-unavailable')
        return result

    try:
        children = parent.children(recursive=True)
    except psutil.Error as exc:
        children = []
        result.errors.append('discover:' + type(exc).__name__)
    processes = [*children, parent]
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            result.errors.append('terminate:' + type(exc).__name__)
    _, alive = psutil.wait_procs(processes, timeout=0.5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            result.errors.append('kill:' + type(exc).__name__)
    _, alive = psutil.wait_procs(alive, timeout=1)
    result.surviving_pids = [p.pid for p in alive]
    return result


class _Capture:
    def __init__(self):
        self.data = bytearray()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.stop = threading.Event()
        self.eof = False
        self.truncated = False
        self.error: str | None = None
        self.thread: threading.Thread | None = None

    def start(self, stream):
        def read():
            try:
                while not self.stop.is_set():
                    chunk = stream.read1(8192)
                    if not chunk:
                        self.eof = True
                        break
                    with self.lock:
                        if self.stop.is_set():
                            break
                        remaining = OUTPUT_LIMIT_BYTES - len(self.data)
                        self.data.extend(chunk[:remaining])
                        self.truncated |= len(chunk) > remaining
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    self.error = self.error or type(exc).__name__
                self.done.set()
        thread = threading.Thread(target=read, daemon=True, name='harness-output')
        # Record ownership before start can be interrupted. A started reader
        # alone closes its stream; main must not block on BufferedReader.close.
        self.thread = thread
        try:
            thread.start()
        except BaseException as exc:
            if isinstance(exc, Exception) and thread.ident is None:
                self.thread = None  # Ordinary failed start, no reader owns it.
            raise

    def snapshot(self) -> str:
        with self.lock:
            return bytes(self.data).decode('utf-8', errors='replace')


def run_process(*, argv: Sequence[str], cwd: Path, timeout_seconds: float) -> ProcessResult:
    if not argv:
        raise ValueError('argv must not be empty')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('timeout_seconds must be finite and > 0')
    started = time.monotonic()
    command = list(argv)
    stdout_capture, stderr_capture = _Capture(), _Capture()
    try:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except OSError as exc:
        return ProcessResult(argv=command, exit_code=None, stdout='', stderr='',
                             timed_out=False, elapsed_seconds=time.monotonic() - started,
                             started=False, failure_type=type(exc).__name__, output_complete=False)

    timed_out = False
    failure: BaseException | None = None
    cleanup = CleanupResult()
    cleanup_attempted = False
    try:
        stdout_capture.start(process.stdout)
        stderr_capture.start(process.stderr)
        deadline = started + timeout_seconds
        while True:
            if stdout_capture.error or stderr_capture.error:
                break
            if process.poll() is not None and stdout_capture.done.is_set() and stderr_capture.done.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(0.01, remaining))
    except BaseException as exc:
        failure = exc
    finally:
        if timed_out or failure or stdout_capture.error or stderr_capture.error:
            cleanup_attempted = True
            try:
                cleanup = _kill_process_tree(process.pid)
                if cleanup is None:
                    cleanup = CleanupResult(errors=['cleanup-result-unavailable'])
            except BaseException as exc:
                cleanup = CleanupResult(errors=['cleanup:' + type(exc).__name__])
                if not isinstance(exc, Exception):
                    failure = failure or exc
            # One shared drain budget; no communicate()/wait() or synchronous
            # stream close can wait indefinitely behind a surviving writer.
            drain_deadline = time.monotonic() + DRAIN_SECONDS
            for capture in (stdout_capture, stderr_capture):
                try:
                    capture.done.wait(max(0, drain_deadline - time.monotonic()))
                except BaseException as exc:
                    cleanup.errors.append('drain:' + type(exc).__name__)
                    failure = failure or exc
        for capture, stream in ((stdout_capture, process.stdout), (stderr_capture, process.stderr)):
            capture.stop.set()
            if capture.thread is None:
                try:
                    stream.close()
                except BaseException as exc:
                    cleanup.errors.append('close:' + type(exc).__name__)
                    failure = failure or exc

    try:
        exit_code = process.poll()
    except Exception as exc:
        exit_code = None
        failure = failure or exc
    if cleanup_attempted and exit_code is None:
        cleanup.errors.append('parent-still-running')
        if process.pid not in cleanup.surviving_pids:
            cleanup.surviving_pids.append(process.pid)
    result = ProcessResult(
        argv=command, exit_code=exit_code, stdout=stdout_capture.snapshot(), stderr=stderr_capture.snapshot(),
        timed_out=timed_out, elapsed_seconds=time.monotonic() - started,
        failure_type=type(failure).__name__ if failure else stdout_capture.error or stderr_capture.error,
        cancelled=isinstance(failure, (KeyboardInterrupt, SystemExit)),
        output_complete=all(c.eof and c.done.is_set() and not c.error for c in (stdout_capture, stderr_capture)),
        stdout_truncated=stdout_capture.truncated, stderr_truncated=stderr_capture.truncated,
        cleanup_attempted=cleanup_attempted, cleanup_errors=cleanup.errors, surviving_pids=cleanup.surviving_pids,
    )
    if failure:
        # Preserve cancellation/exception identity and partial execution evidence.
        failure.process_result = result
        raise failure
    return result
