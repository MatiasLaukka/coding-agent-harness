from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence

import psutil
from pydantic import Field

from coding_agent_harness.models import StrictModel


class ProcessResult(StrictModel):
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_seconds: float = Field(
        ge=0,
    )


def _kill_process_tree(
    pid: int,
) -> None:
    try:
        parent = psutil.Process(
            pid
        )
    except psutil.NoSuchProcess:
        return

    try:
        children = parent.children(
            recursive=True
        )
    except psutil.Error:
        children = []

    processes = [
        *children,
        parent,
    ]

    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            pass

    _, alive = psutil.wait_procs(
        processes,
        timeout=0.5,
    )

    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            pass

    psutil.wait_procs(
        alive,
        timeout=1,
    )


def run_process(
    *,
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
) -> ProcessResult:
    if not argv:
        raise ValueError(
            "argv must not be empty"
        )

    if timeout_seconds <= 0:
        raise ValueError(
            "timeout_seconds must be > 0"
        )

    started = time.monotonic()

    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    timed_out = False

    try:
        stdout, stderr = (
            process.communicate(
                timeout=timeout_seconds
            )
        )

    except subprocess.TimeoutExpired:
        timed_out = True

        _kill_process_tree(
            process.pid
        )

        stdout, stderr = (
            process.communicate()
        )

    elapsed_seconds = (
        time.monotonic()
        - started
    )

    return ProcessResult(
        argv=list(argv),
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        elapsed_seconds=(
            elapsed_seconds
        ),
    )