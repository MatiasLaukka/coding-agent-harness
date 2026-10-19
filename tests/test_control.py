import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent_harness import control, controller as controller_module
from coding_agent_harness.controller import HarnessRunResult
from coding_agent_harness.git import GitBaseline
from coding_agent_harness.models import TaskPacket, VerificationCommand, WorkerResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.worker import WorkerRunResult


class FakeConsole:
    def __init__(self, lines):
        self.lines = list(lines)
        self.output = []
        self.discard_count = 0
        self.deadlines = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def write(self, text):
        self.output.append(text)

    def discard_typeahead(self):
        self.discard_count += 1

    def read_line(self, *, deadline):
        self.deadlines.append(deadline)
        if not self.lines:
            raise RuntimeError("unexpected input read")
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "example.txt").write_text("before\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "R6a Test")
    git(repo, "config", "user.email", "r6a@example.invalid")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
    records = tmp_path / "records"
    records.mkdir()
    return repo, records


def write_task(path, *, commands=None):
    path.write_text(json.dumps({
        "task": {
            "task_id": "task-1",
            "goal": "Implement the bounded offline change.",
            "constraints": ["Keep configuration frozen."],
            "acceptance": ["Independent verification succeeds."],
            "suggested_files": ["src/example.txt"],
            "allowed_files": ["src/example.txt"],
        },
        "verification_commands": commands or [{
            "id": "offline-check",
            "argv": [sys.executable, "-c", "print('verified')"],
            "timeout_seconds": 20,
        }],
    }), encoding="utf-8")


def args_for(repo, records, task_file, *, budget=0, review_timeout=30):
    return SimpleNamespace(
        task_file=task_file, repo=repo, model="offline-model",
        worker_command="offline-worker", worker_timeout=30.0,
        max_remediation_attempts=budget, review_timeout=review_timeout,
        record_dir=records,
    )


class SyntheticWorker:
    def __init__(self, *, command, model, timeout_seconds, stdin_devnull, records,
                 snapshots=None):
        self.command = list(command)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.stdin_devnull = stdin_devnull
        self.records = records
        self.calls = []
        self.snapshots = snapshots or ["first result\n", "second result\n"]

    def command_for(self, task):
        return [*self.command, "--model", self.model, "-p", f"task={task.task_id};goal={task.goal}"]

    def run(self, *, task, repo_dir):
        self.calls.append(task.model_copy(deep=True))
        assert self.stdin_devnull is True
        assert list(self.records.glob("start-*.json")), "start record must be published before dispatch"
        (repo_dir / "src" / "example.txt").write_text(self.snapshots[len(self.calls) - 1], encoding="utf-8")
        process = ProcessResult(
            argv=self.command_for(task), exit_code=0,
            stdout="worker claim\x1b[2J\u202e", stderr="", timed_out=False,
            elapsed_seconds=0.01,
        )
        completion = WorkerResult(
            status="completed", summary="finished", changed_files=["src/example.txt"],
            remaining_concerns=[],
        )
        return WorkerRunResult(process=process, completion=completion)


def make_worker_factory(records, instances):
    def factory(*, command, model, timeout_seconds, stdin_devnull):
        worker = SyntheticWorker(
            command=command, model=model, timeout_seconds=timeout_seconds,
            stdin_devnull=stdin_devnull, records=records,
        )
        instances.append(worker)
        return worker
    return factory


def codes(*values):
    values = iter(values)
    return lambda: next(values)


def test_control_accepts_only_after_start_and_publishes_terminal(tmp_path):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4", "ACCEPT E5F6A7B8", "meets the criteria"])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 0
    assert len(instances) == 1 and len(instances[0].calls) == 1
    assert list(records.glob("start-*.json"))
    reviews = list(records.glob("review-*.json"))
    terminals = list(records.glob("terminal-*.json"))
    assert len(reviews) == len(terminals) == 1
    start = json.loads(next(records.glob("start-*.json")).read_text(encoding="utf-8"))
    terminal = json.loads(terminals[0].read_text(encoding="utf-8"))
    assert start["initial_authorization"]["scope"].startswith("exactly the first")
    assert start["approved_clean_baseline"]["commit"] == git(repo, "rev-parse", "HEAD")
    assert terminal["controller_result"]["status"] == "accepted"
    assert terminal["worker_invocation_observation"]["count"] == 1
    assert terminal["review_records"][0]["controller_callback_returned"] is True


def test_one_human_remediation_is_frozen_and_no_third_attempt_is_possible(tmp_path):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole([
        "START A1B2C3D4",
        "REMEDIATE E5F6A7B8", "repair the requested behavior", "change only the existing file",
        "REMEDIATE 9ABCDEF0", "STOP", "stop after the one approved remediation",
    ])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file, budget=1), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8", "9ABCDEF0"),
    )

    assert status == 2
    worker = instances[0]
    assert len(instances) == 1 and len(worker.calls) == 2
    original, remediated = worker.calls
    assert remediated.task_id == "task-1-remediation-1"
    assert remediated.allowed_files == original.allowed_files
    assert remediated.constraints == original.constraints
    assert remediated.acceptance == original.acceptance
    assert "change only the existing file" in remediated.goal
    assert worker.model == "offline-model"
    assert worker.command == ["offline-worker"]
    assert worker.timeout_seconds == 30.0

    reviews = [json.loads(path.read_text(encoding="utf-8")) for path in records.glob("review-*.json")]
    reviews.sort(key=lambda item: item["attempt"])
    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    assert [item["human_decision"]["requested_action"] for item in reviews] == ["remediate", "stop"]
    assert terminal["controller_result"]["status"] == "stopped"
    assert terminal["controller_result"]["attempts"] == 2
    assert terminal["worker_invocation_observation"]["count"] == 2
    assert "No remediation attempt remains" in "".join(console.output)


def test_unauthorized_remediation_does_not_dispatch_another_worker(tmp_path):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4", "REMEDIATE E5F6A7B8", "STOP", "operator stop"])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file, budget=0), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 2
    assert len(instances[0].calls) == 1
    assert json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))["worker_invocation_observation"]["count"] == 1


def test_controller_baseline_mismatch_after_start_prevents_dispatch(tmp_path, monkeypatch):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4"])
    instances = []
    monkeypatch.setattr(controller_module, "capture_baseline", lambda repo: GitBaseline(commit="f" * 40, status=""))

    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4"),
    )

    assert status == 1
    assert len(instances) == 1 and instances[0].calls == []
    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    assert terminal["worker_invocation_observation"]["count"] == 0
    assert terminal["workflow_failure"]["controller_stage"] == "baseline"


def test_verification_induced_git_change_fails_before_human_action(tmp_path):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    verifier = tmp_path / "mutate.py"
    verifier.write_text(
        f"from pathlib import Path\nPath({str(repo / 'src' / 'example.txt')!r}).write_text('verification changed\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    write_task(task_file, commands=[{
        "id": "mutating-check", "argv": [sys.executable, str(verifier)], "timeout_seconds": 20,
    }])
    console = FakeConsole(["START A1B2C3D4"])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 1
    assert len(instances[0].calls) == 1
    assert not any("Action: type ACCEPT" in value for value in console.output)
    review = json.loads(next(records.glob("review-*.json")).read_text(encoding="utf-8"))
    assert review["human_decision"] is None
    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    assert terminal["workflow_failure"]["exception_type"] == "EvidenceFreshnessError"
    assert terminal["worker_invocation_observation"]["count"] == 1


def test_start_record_publication_failure_never_dispatches_worker(tmp_path, monkeypatch):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4"])
    instances = []
    original_publish = control.publish_record

    def fail_start(directory, *, kind, record):
        if kind == "start":
            raise OSError("record disk failure")
        return original_publish(directory, kind=kind, record=record)

    monkeypatch.setattr(control, "publish_record", fail_start)
    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4"),
    )

    assert status == 1
    assert len(instances) == 1 and instances[0].calls == []
    assert not list(records.glob("start-*.json"))
    assert len(list(records.glob("terminal-*.json"))) == 1


def test_terminal_publication_failure_preserves_actual_controller_outcome(tmp_path, monkeypatch):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4", "ACCEPT E5F6A7B8", "accepted by human"])
    instances = []
    original_publish = control.publish_record

    def fail_terminal(directory, *, kind, record):
        if kind == "terminal":
            raise OSError("terminal record disk failure")
        return original_publish(directory, kind=kind, record=record)

    monkeypatch.setattr(control, "publish_record", fail_terminal)
    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 1
    assert len(instances[0].calls) == 1
    assert "terminal_publication_failed" in "".join(console.output)
    assert '"controller_status": "accepted"' in "".join(console.output)
    assert not list(records.glob("terminal-*.json"))


def test_control_admission_rejects_missing_scope_duplicate_checks_and_advisory_only(tmp_path):
    repo, _records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    payload["task"]["allowed_files"] = None
    task_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(control.ControlAdmissionError):
        control._load_control_task_file(task_file)

    payload["task"]["allowed_files"] = ["src/example.txt"]
    payload["verification_commands"] = [
        {"id": "same", "argv": ["python", "-c", "pass"]},
        {"id": "SAME", "argv": ["python", "-c", "pass"]},
    ]
    task_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(control.ControlAdmissionError, match="unique"):
        control._load_control_task_file(task_file)

    payload["verification_commands"] = [
        {"id": "advisory", "argv": ["python", "-c", "pass"], "expectation": "advisory"},
    ]
    task_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(control.ControlAdmissionError, match="required"):
        control._load_control_task_file(task_file)
