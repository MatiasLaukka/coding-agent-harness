from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coding_agent_harness import cli


def write_task_file(
    path: Path,
) -> None:
    path.write_text(
        json.dumps(
            {
                "task": {
                    "task_id": "task-1",
                    "goal": (
                        "Make one bounded change."
                    ),
                    "constraints": [
                        (
                            "Do not make unrelated "
                            "changes."
                        ),
                    ],
                    "acceptance": [
                        (
                            "Independent verification "
                            "passes."
                        ),
                    ],
                    "suggested_files": [
                        "src/example.py",
                    ],
                    "allowed_files": [
                        "src/example.py",
                    ],
                },
                "verification_commands": [
                    {
                        "id": "tests",
                        "argv": [
                            "python",
                            "-m",
                            "pytest",
                            "-q",
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def make_result(
    *,
    status: str = "ready-for-review",
    findings: list | None = None,
):
    return SimpleNamespace(
        task_id="task-1",
        baseline_sha="a" * 40,
        final_head_sha="a" * 40,
        worker=SimpleNamespace(
            process=SimpleNamespace(
                exit_code=0,
                timed_out=False,
            ),
            completion=SimpleNamespace(
                status="completed",
                summary=(
                    "Implemented the requested change."
                ),
            ),
            completion_error=None,
        ),
        changed_files=[
            "src/example.py",
        ],
        patch=(
            "diff --git a/src/example.py "
            "b/src/example.py\n"
        ),
        verification=SimpleNamespace(
            passed=True,
        ),
        findings=(
            findings
            if findings is not None
            else []
        ),
        status=status,
    )


class FakeQwenWorker:
    created: list[
        "FakeQwenWorker"
    ] = []

    def __init__(
        self,
        *,
        command,
        model,
        timeout_seconds,
    ) -> None:
        self.command = command
        self.model = model
        self.timeout_seconds = (
            timeout_seconds
        )

        self.created.append(
            self
        )


def test_run_command_executes_task_and_prints_review_surface(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    task_file = (
        tmp_path
        / "task.json"
    )

    write_task_file(
        task_file
    )

    repo_dir = (
        tmp_path
        / "repo"
    )
    repo_dir.mkdir()

    FakeQwenWorker.created.clear()

    captured = {}

    def fake_run_task(
        *,
        task,
        repo_dir,
        worker,
        verification_commands,
    ):
        captured["task"] = task
        captured["repo_dir"] = repo_dir
        captured["worker"] = worker
        captured[
            "verification_commands"
        ] = verification_commands

        return make_result()

    monkeypatch.setattr(
        cli,
        "QwenWorker",
        FakeQwenWorker,
    )

    monkeypatch.setattr(
        cli,
        "run_task",
        fake_run_task,
    )

    exit_code = cli.app(
        [
            "run",
            str(task_file),
            "--repo",
            str(repo_dir),
            "--model",
            "qwen-test-model",
            "--worker-timeout",
            "123",
        ]
    )

    output = (
        capsys
        .readouterr()
    )

    assert exit_code == 0

    assert len(
        FakeQwenWorker.created
    ) == 1

    worker = (
        FakeQwenWorker.created[0]
    )

    assert worker.command == [
        "qwen",
    ]
    assert worker.model == (
        "qwen-test-model"
    )
    assert worker.timeout_seconds == 123.0

    assert captured[
        "task"
    ].task_id == "task-1"

    assert captured[
        "task"
    ].allowed_files == [
        "src/example.py",
    ]

    assert captured[
        "repo_dir"
    ] == repo_dir.resolve()

    assert len(
        captured[
            "verification_commands"
        ]
    ) == 1

    assert captured[
        "verification_commands"
    ][0].id == "tests"

    assert (
        "Status: ready-for-review"
        in output.out
    )
    assert (
        "Verification: PASS"
        in output.out
    )
    assert (
        "src/example.py"
        in output.out
    )
    assert (
        "Patch:"
        in output.out
    )
    assert (
        "diff --git"
        in output.out
    )

    assert output.err == ""


def test_run_command_returns_two_for_mechanical_findings(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    task_file = (
        tmp_path
        / "task.json"
    )

    write_task_file(
        task_file
    )

    repo_dir = (
        tmp_path
        / "repo"
    )
    repo_dir.mkdir()

    finding = SimpleNamespace(
        code="disallowed-file",
        detail=(
            "Changed file is outside "
            "allowed scope: other.py"
        ),
    )

    monkeypatch.setattr(
        cli,
        "QwenWorker",
        FakeQwenWorker,
    )

    monkeypatch.setattr(
        cli,
        "run_task",
        lambda **kwargs: make_result(
            status="needs-attention",
            findings=[
                finding,
            ],
        ),
    )

    exit_code = cli.app(
        [
            "run",
            str(task_file),
            "--repo",
            str(repo_dir),
            "--model",
            "qwen-test-model",
        ]
    )

    output = (
        capsys
        .readouterr()
    )

    assert exit_code == 2

    assert (
        "Status: needs-attention"
        in output.out
    )
    assert (
        "[disallowed-file]"
        in output.out
    )


def test_run_command_rejects_invalid_task_file_before_worker_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    task_file = (
        tmp_path
        / "invalid.json"
    )

    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "task_id": "task-1",
                }
            }
        ),
        encoding="utf-8",
    )

    repo_dir = (
        tmp_path
        / "repo"
    )
    repo_dir.mkdir()

    FakeQwenWorker.created.clear()

    monkeypatch.setattr(
        cli,
        "QwenWorker",
        FakeQwenWorker,
    )

    exit_code = cli.app(
        [
            "run",
            str(task_file),
            "--repo",
            str(repo_dir),
            "--model",
            "qwen-test-model",
        ]
    )

    output = (
        capsys
        .readouterr()
    )

    assert exit_code == 1
    assert (
        "Task file error:"
        in output.err
    )
    assert (
        FakeQwenWorker.created
        == []
    )