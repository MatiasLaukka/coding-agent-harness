import json
import sys
from pathlib import Path

from coding_agent_harness.models import (
    TaskPacket,
)
from coding_agent_harness.worker import (
    _render_task_prompt,
    QwenWorker,
)


def make_task() -> TaskPacket:
    return TaskPacket(
        task_id="task-1",
        goal="Change the greeting to hello.",
        constraints=[
            "Only modify greeting.txt.",
        ],
        acceptance=[
            "greeting.txt contains hello",
        ],
        suggested_files=[
            "greeting.txt",
        ],
    )


def test_qwen_worker_parses_structured_completion(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_qwen.py"

    completion = {
        "status": "completed",
        "summary": "Updated greeting.",
        "changed_files": [
            "greeting.txt",
        ],
        "remaining_concerns": [],
    }

    script.write_text(
        "import json\n"
        "import sys\n"
        "print('worker prose')\n"
        "print('HARNESS_RESULT: ' + "
        f"json.dumps({completion!r}))\n",
        encoding="utf-8",
    )

    worker = QwenWorker(
        command=[
            sys.executable,
            str(script),
        ],
        model="qwen-triage",
        timeout_seconds=10,
    )

    result = worker.run(
        task=make_task(),
        repo_dir=tmp_path,
    )

    assert result.process.exit_code == 0
    assert result.process.timed_out is False
    assert result.completion is not None
    assert result.completion.status == "completed"
    assert result.completion.changed_files == [
        "greeting.txt",
    ]
    assert result.completion_error is None


def test_qwen_worker_reports_missing_completion_record(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_qwen.py"

    script.write_text(
        "print('I finished the task')\n",
        encoding="utf-8",
    )

    worker = QwenWorker(
        command=[
            sys.executable,
            str(script),
        ],
        model="qwen-triage",
        timeout_seconds=10,
    )

    result = worker.run(
        task=make_task(),
        repo_dir=tmp_path,
    )

    assert result.completion is None
    assert (
        result.completion_error
        == "missing HARNESS_RESULT completion record"
    )


def test_qwen_worker_reports_invalid_completion_json(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_qwen.py"

    script.write_text(
        "print('HARNESS_RESULT: {not-json}')\n",
        encoding="utf-8",
    )

    worker = QwenWorker(
        command=[
            sys.executable,
            str(script),
        ],
        model="qwen-triage",
        timeout_seconds=10,
    )

    result = worker.run(
        task=make_task(),
        repo_dir=tmp_path,
    )

    assert result.completion is None
    assert result.completion_error is not None


def test_qwen_worker_prompt_contains_task_boundaries(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "prompt.txt"
    script = tmp_path / "capture_qwen.py"

    completion = {
        "status": "completed",
        "summary": "Done.",
        "changed_files": [],
        "remaining_concerns": [],
    }

    script.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "prompt = sys.argv[-1]\n"
        f"Path({str(capture)!r}).write_text("
        "prompt, encoding='utf-8')\n"
        "print('HARNESS_RESULT: ' + "
        f"json.dumps({completion!r}))\n",
        encoding="utf-8",
    )

    worker = QwenWorker(
        command=[
            sys.executable,
            str(script),
        ],
        model="qwen-triage",
        timeout_seconds=10,
    )

    worker.run(
        task=make_task(),
        repo_dir=tmp_path,
    )

    prompt = capture.read_text(
        encoding="utf-8",
    )

    assert "task-1" in prompt
    assert "Change the greeting to hello." in prompt
    assert "Only modify greeting.txt." in prompt
    assert "greeting.txt contains hello" in prompt
    assert "HARNESS_RESULT:" in prompt

def test_render_task_prompt_states_allowed_file_boundary() -> None:
    task = TaskPacket(
        task_id="scope-test",
        goal="Make one bounded change.",
        constraints=[],
        acceptance=[
            "The requested change is complete.",
        ],
        suggested_files=[
            "test/example.py",
        ],
        allowed_files=[
            "test/example.py",
        ],
    )

    prompt = _render_task_prompt(
        task
    )

    assert (
        "Allowed files:\n"
        "- test/example.py"
        in prompt
    )
    assert (
        "Do not modify any file not listed "
        "under Allowed files."
        in prompt
    )


def test_render_task_prompt_distinguishes_unrestricted_from_no_files() -> None:
    unrestricted = TaskPacket(
        task_id="unrestricted",
        goal="Inspect the repository.",
        constraints=[],
        acceptance=[
            "Inspection is complete.",
        ],
        allowed_files=None,
    )

    no_files = TaskPacket(
        task_id="no-files",
        goal="Inspect without editing.",
        constraints=[],
        acceptance=[
            "Inspection is complete.",
        ],
        allowed_files=[],
    )

    unrestricted_prompt = _render_task_prompt(
        unrestricted
    )
    no_files_prompt = _render_task_prompt(
        no_files
    )

    assert (
        "- No machine-enforced file restriction."
        in unrestricted_prompt
    )
    assert (
        "Do not modify any files."
        not in unrestricted_prompt
    )

    assert (
        "Allowed files:\n- None."
        in no_files_prompt
    )
    assert (
        "Do not modify any files."
        in no_files_prompt
    )

def test_render_task_prompt_defines_changed_files_as_per_attempt() -> None:
    prompt = _render_task_prompt(
        make_task()
    )

    assert (
        "changed_files must contain repository-relative paths "
        "modified during this worker invocation only."
        in prompt
    )


def test_render_task_prompt_routes_completion_to_final_response() -> None:
    prompt = _render_task_prompt(make_task())
    assert "HARNESS_RESULT: is an output marker, not a filename." in prompt
    assert "final assistant response through the CLI's normal stdout" in prompt
    assert "Emit exactly one final" in prompt


def test_render_task_prompt_prohibits_completion_artifacts_and_tools() -> None:
    prompt = _render_task_prompt(make_task())
    assert "Do not create HARNESS_RESULT," in prompt
    assert "HARNESS_RESULT.txt, or similar completion artifacts." in prompt
    assert "Do not invoke tools or shell" in prompt
    assert "commands to create or print the completion record." in prompt


def test_render_task_prompt_stops_tools_and_preserves_completion_schema() -> None:
    prompt = _render_task_prompt(make_task())
    assert "When implementation work is finished, stop invoking tools." in prompt
    assert '"status": "completed" | "blocked" | "failed"' in prompt
    for field in ('summary', 'changed_files', 'remaining_concerns'):
        assert f'"{field}"' in prompt
    assert "modified during this worker invocation only." in prompt


def test_worker_opt_in_passes_devnull_to_common_runner(tmp_path, monkeypatch):
    import coding_agent_harness.worker as worker_module
    from coding_agent_harness.models import TaskPacket
    from coding_agent_harness.process import ProcessResult
    from coding_agent_harness.worker import QwenWorker

    captured = {}
    def fake_run_process(**kwargs):
        captured.update(kwargs)
        return ProcessResult(
            argv=kwargs["argv"], exit_code=0,
            stdout='HARNESS_RESULT: {"status":"completed","summary":"ok"}',
            stderr="", timed_out=False, elapsed_seconds=0.01,
        )
    monkeypatch.setattr(worker_module, "run_process", fake_run_process)
    worker = QwenWorker(command=["offline"], model="offline-model", timeout_seconds=5, stdin_devnull=True)
    task = TaskPacket(task_id="task", goal="do work", acceptance=["check"])

    worker.run(task=task, repo_dir=tmp_path)

    assert captured["stdin_devnull"] is True
