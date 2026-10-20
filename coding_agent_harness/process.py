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
PROCESS_CLEANUP_RESERVE_SECONDS = 2.0
PROCESS_TREE_TERMINATE_WAIT_SECONDS = 0.5
PROCESS_TREE_KILL_WAIT_SECONDS = 1.0


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
    raw_stdout_bytes: bytes | None = Field(default=None, exclude=True, repr=False)

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
    _, alive = psutil.wait_procs(processes, timeout=PROCESS_TREE_TERMINATE_WAIT_SECONDS)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            result.errors.append('kill:' + type(exc).__name__)
    _, alive = psutil.wait_procs(alive, timeout=PROCESS_TREE_KILL_WAIT_SECONDS)
    result.surviving_pids = [p.pid for p in alive]
    return result


def _kill_process_tree_before_deadline(pid: int, deadline: float) -> CleanupResult:
    """Bound process-tree cleanup to the caller's absolute phase deadline."""
    result: list[CleanupResult] = []
    errors: list[str] = []

    def cleanup() -> None:
        try:
            value = _kill_process_tree(pid)
            if isinstance(value, CleanupResult):
                result.append(value)
            else:
                errors.append('cleanup-result-unavailable')
        except BaseException as exc:
            errors.append('cleanup:' + type(exc).__name__)

    thread = threading.Thread(target=cleanup, daemon=True, name='harness-process-cleanup')
    try:
        thread.start()
    except Exception as exc:
        return CleanupResult(
            errors=['cleanup-start:' + type(exc).__name__],
            surviving_pids=[pid],
        )

    thread.join(max(0.0, deadline - time.monotonic()))
    if thread.is_alive() or time.monotonic() >= deadline:
        return CleanupResult(
            errors=['cleanup-deadline'],
            surviving_pids=[pid],
        )
    if errors:
        return CleanupResult(errors=errors, surviving_pids=[pid])
    if not result:
        return CleanupResult(
            errors=['cleanup-result-unavailable'],
            surviving_pids=[pid],
        )
    return result[0]


class _Capture:
    def __init__(self):
        self.output_limit_bytes = OUTPUT_LIMIT_BYTES
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
                        remaining = self.output_limit_bytes - len(self.data)
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

    def snapshot_bytes(self) -> bytes:
        with self.lock:
            return bytes(self.data)


def run_process(
    *,
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    stdout_limit_bytes: int = OUTPUT_LIMIT_BYTES,
    stderr_limit_bytes: int = OUTPUT_LIMIT_BYTES,
    deadline: float | None = None,
    stdin_devnull: bool = False,
    capture_stdout_bytes: bool = False,
) -> ProcessResult:
    if not argv:
        raise ValueError('argv must not be empty')
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError('timeout_seconds must be finite and > 0')
    for name, limit in (
        ('stdout_limit_bytes', stdout_limit_bytes),
        ('stderr_limit_bytes', stderr_limit_bytes),
    ):
        if type(limit) is not int or limit <= 0:
            raise ValueError(f'{name} must be a positive integer')
    if type(stdin_devnull) is not bool or type(capture_stdout_bytes) is not bool:
        raise ValueError('stdin_devnull and capture_stdout_bytes must be booleans')
    if deadline is not None and (
        type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0
    ):
        raise ValueError('deadline must be a finite positive monotonic timestamp')
    started = time.monotonic()
    execution_deadline = started + timeout_seconds
    if deadline is not None:
        execution_deadline = min(
            execution_deadline,
            deadline - PROCESS_CLEANUP_RESERVE_SECONDS,
        )
    command = list(argv)
    stdout_capture, stderr_capture = _Capture(), _Capture()
    stdout_capture.output_limit_bytes = stdout_limit_bytes
    stderr_capture.output_limit_bytes = stderr_limit_bytes
    if deadline is not None and time.monotonic() >= execution_deadline:
        return ProcessResult(
            argv=command,
            exit_code=None,
            stdout='',
            stderr='',
            timed_out=True,
            elapsed_seconds=max(0.0, time.monotonic() - started),
            started=False,
            output_complete=False,
            raw_stdout_bytes=b'' if capture_stdout_bytes else None,
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL if stdin_devnull else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return ProcessResult(argv=command, exit_code=None, stdout='', stderr='',
                             timed_out=False, elapsed_seconds=time.monotonic() - started,
                             started=False, failure_type=type(exc).__name__, output_complete=False,
                             raw_stdout_bytes=b'' if capture_stdout_bytes else None)

    timed_out = False
    failure: BaseException | None = None
    cleanup = CleanupResult()
    cleanup_attempted = False
    try:
        stdout_capture.start(process.stdout)
        stderr_capture.start(process.stderr)
        while True:
            remaining = execution_deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if stdout_capture.error or stderr_capture.error:
                break
            process_exited = process.poll() is not None
            output_complete = stdout_capture.done.is_set() and stderr_capture.done.is_set()
            remaining = execution_deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if process_exited and output_complete:
                break
            time.sleep(min(0.01, remaining))
    except BaseException as exc:
        failure = exc
    finally:
        if timed_out or failure or stdout_capture.error or stderr_capture.error:
            cleanup_attempted = True
            try:
                if deadline is None:
                    cleanup = _kill_process_tree(process.pid)
                else:
                    cleanup = _kill_process_tree_before_deadline(process.pid, deadline)
                if cleanup is None:
                    cleanup = CleanupResult(errors=['cleanup-result-unavailable'])
            except BaseException as exc:
                cleanup = CleanupResult(errors=['cleanup:' + type(exc).__name__])
                if not isinstance(exc, Exception):
                    failure = failure or exc
            # One shared drain budget; no communicate()/wait() or synchronous
            # stream close can wait indefinitely behind a surviving writer.
            drain_deadline = time.monotonic() + DRAIN_SECONDS
            if deadline is not None:
                drain_deadline = min(drain_deadline, deadline)
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
        raw_stdout_bytes=stdout_capture.snapshot_bytes() if capture_stdout_bytes else None,
    )
    if failure:
        # Preserve cancellation/exception identity and partial execution evidence.
        failure.process_result = result
        raise failure
    return result
