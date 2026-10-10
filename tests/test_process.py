import os
import sys
import time
from pathlib import Path

import psutil

from coding_agent_harness.process import run_process


def test_run_process_captures_output_and_exit_code(
    tmp_path: Path,
) -> None:
    script = tmp_path / "emit.py"

    script.write_text(
        "import sys\n"
        "print('hello stdout')\n"
        "print('hello stderr', file=sys.stderr)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    result = run_process(
        argv=[
            sys.executable,
            str(script),
        ],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert result.exit_code == 7
    assert result.timed_out is False
    assert "hello stdout" in result.stdout
    assert "hello stderr" in result.stderr
    assert result.elapsed_seconds >= 0


def test_run_process_times_out(
    tmp_path: Path,
) -> None:
    script = tmp_path / "sleep.py"

    script.write_text(
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    started = time.monotonic()

    result = run_process(
        argv=[
            sys.executable,
            str(script),
        ],
        cwd=tmp_path,
        timeout_seconds=0.5,
    )

    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code is not None
    assert elapsed < 5


def test_timeout_kills_child_process_tree(
    tmp_path: Path,
) -> None:
    child_pid_file = (
        tmp_path / "child.pid"
    )

    child = tmp_path / "child.py"

    child.write_text(
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    parent = tmp_path / "parent.py"

    parent.write_text(
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "child = subprocess.Popen([\n"
        "    sys.executable,\n"
        f"    {str(child)!r},\n"
        "])\n"
        f"Path({str(child_pid_file)!r}).write_text(\n"
        "    str(child.pid),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    result = run_process(
        argv=[
            sys.executable,
            str(parent),
        ],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.timed_out is True
    assert child_pid_file.exists()

    child_pid = int(
        child_pid_file.read_text(
            encoding="utf-8"
        )
    )

    for _ in range(20):
        if not psutil.pid_exists(
            child_pid
        ):
            break

        try:
            process = psutil.Process(
                child_pid
            )

            if process.status() == (
                psutil.STATUS_ZOMBIE
            ):
                break

        except psutil.NoSuchProcess:
            break

        time.sleep(0.1)

    assert not psutil.pid_exists(
        child_pid
    )