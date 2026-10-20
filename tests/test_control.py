import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent_harness import control, controller as controller_module
from coding_agent_harness.console import ConsoleDeadlineExpired
from coding_agent_harness.controller import HarnessRunResult
from coding_agent_harness.git import GitBaseline
from coding_agent_harness.models import TaskPacket, VerificationCommand, WorkerResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.worker import WorkerRunResult


class FakeConsole:
    def __init__(self, lines, *, on_read=None):
        self.lines = list(lines)
        self.output = []
        self.discard_count = 0
        self.deadlines = []
        self.on_read = on_read

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
        if self.on_read is not None:
            self.on_read(len(self.deadlines), self)
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
    reference = terminal["review_records"][0]
    assert reference["decision_record_published"] is True
    assert reference["final_freshness_validation_completed"] is True
    assert reference["controller_decision_receipt"] == "observed"
    assert reference["controller_decision_processed"] is True
    assert reference["controller_decision_processing_basis"] == "controller run returned after processing the decision"


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
    assert all(reference["decision_record_published"] for reference in terminal["review_records"])
    assert all(reference["final_freshness_validation_completed"] for reference in terminal["review_records"])
    assert all(reference["controller_decision_receipt"] == "observed" for reference in terminal["review_records"])
    assert all(reference["controller_decision_processed"] for reference in terminal["review_records"])
    assert all(
        reference["controller_decision_processing_basis"]
        == "controller run returned after processing the decision"
        for reference in terminal["review_records"]
    )
    assert "No remediation attempt remains" in "".join(console.output)


@pytest.mark.parametrize(
    ("second_review_end", "expected_exit"),
    [
        pytest.param("timeout", 1, id="timeout"),
        pytest.param("interrupt", 130, id="interrupt"),
    ],
)
def test_prior_remediation_projection_uses_native_attempt_evidence(
    tmp_path, second_review_end, expected_exit,
):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    end = (
        ConsoleDeadlineExpired("second review deadline expired")
        if second_review_end == "timeout"
        else KeyboardInterrupt()
    )
    console = FakeConsole([
        "START A1B2C3D4",
        "REMEDIATE E5F6A7B8", "repair the requested behavior", "change only the existing file",
        end,
    ])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file, budget=1), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8", "9ABCDEF0"),
    )

    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    native = terminal["native_evidence"]["harness_evidence"]
    first = terminal["review_records"][0]
    assert status == expected_exit
    assert len(instances[0].calls) == 2
    assert terminal["controller_result"] is None
    assert native["attempts"][0]["decision"]["decision"] == "remediate"
    assert native["attempts"][1]["attempt"] == 2
    assert native["worker_invocation_count"] == 2
    assert first["decision_record_published"] is True
    assert first["controller_decision_receipt"] == "observed"
    assert first["controller_decision_processed"] is True
    assert first["controller_decision_processing_basis"] == "subsequent native controller attempt record"
    second = terminal["review_records"][1]
    assert second["decision_record_published"] is False
    assert "decision" not in native["attempts"][1]
    assert second["controller_decision_receipt"] == "not-applicable"
    assert second["controller_decision_processed"] is None


def test_second_review_stop_confirms_prior_remediation_processing(tmp_path):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole([
        "START A1B2C3D4",
        "REMEDIATE E5F6A7B8", "repair the requested behavior", "change only the existing file",
        "STOP", "stop after the authorized remediation",
    ])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file, budget=1), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8", "9ABCDEF0"),
    )

    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    native = terminal["controller_result"]
    first = terminal["review_records"][0]
    assert status == 2
    assert native["status"] == "stopped" and native["attempts"] == 2
    assert len(instances[0].calls) == 2
    assert first["controller_decision_receipt"] == "observed"
    assert first["controller_decision_processed"] is True
    assert first["controller_decision_processing_basis"] == "controller run returned after processing the decision"


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


def test_interrupt_before_human_callback_return_does_not_claim_controller_receipt(tmp_path):
    import inspect

    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4", "ACCEPT E5F6A7B8", "looks good"])
    instances = []
    source, first_line = inspect.getsourcelines(control.HumanReviewer.review)
    return_line = first_line + next(index for index, line in enumerate(source) if line.strip() == "return decision")
    injected = False

    def interrupt_before_return(frame, event, _argument):
        nonlocal injected
        if (
            not injected and event == "line" and frame.f_code is control.HumanReviewer.review.__code__
            and frame.f_lineno == return_line
        ):
            injected = True
            sys.settrace(None)
            raise KeyboardInterrupt()
        return interrupt_before_return

    sys.settrace(interrupt_before_return)
    try:
        status = control.run_control(
            args_for(repo, records, task_file), console_factory=lambda: console,
            worker_factory=make_worker_factory(records, instances),
            challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
        )
    finally:
        sys.settrace(None)

    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    reference = terminal["review_records"][0]
    controller_evidence = terminal["native_evidence"]["harness_evidence"]
    assert injected is True
    assert status == 130
    assert len(instances[0].calls) == 1
    assert terminal["controller_result"] is None
    assert controller_evidence["stage"] == "review"
    assert "decision" not in controller_evidence["attempts"][-1]
    assert reference["final_freshness_validation_completed"] is True
    assert reference["decision_record_published"] is True
    assert reference["controller_decision_receipt"] == "unknown"
    assert reference["controller_decision_processed"] is None
    assert reference["controller_decision_processing_basis"] == "not established by native controller evidence"
    assert "controller_callback_returned" not in reference


def test_native_controller_receipt_does_not_overstate_unconfirmed_processing(tmp_path):
    import inspect

    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4", "ACCEPT E5F6A7B8", "meets the criteria"])
    instances = []
    source, first_line = inspect.getsourcelines(controller_module.HarnessController._run)
    decision_branch_line = first_line + next(
        index for index, line in enumerate(source)
        if line.strip() == 'if decision.decision == "accept":'
    )
    injected = False

    def interrupt_before_decision_branch(frame, event, _argument):
        nonlocal injected
        if (
            not injected and event == "line"
            and frame.f_code is controller_module.HarnessController._run.__code__
            and frame.f_lineno == decision_branch_line
        ):
            injected = True
            sys.settrace(None)
            raise KeyboardInterrupt()
        return interrupt_before_decision_branch

    sys.settrace(interrupt_before_decision_branch)
    try:
        status = control.run_control(
            args_for(repo, records, task_file), console_factory=lambda: console,
            worker_factory=make_worker_factory(records, instances),
            challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
        )
    finally:
        sys.settrace(None)

    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    native = terminal["native_evidence"]["harness_evidence"]
    reference = terminal["review_records"][0]
    assert injected is True and status == 130
    assert len(instances[0].calls) == 1
    assert terminal["controller_result"] is None
    assert native["attempts"][0]["decision"]["decision"] == "accept"
    assert native["stage"] == "result-or-retry"
    assert reference["decision_record_published"] is True
    assert reference["controller_decision_receipt"] == "observed"
    assert reference["controller_decision_processed"] is None
    assert reference["controller_decision_processing_basis"] == "native decision recorded; further processing is unconfirmed"


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


def _commit_protected_file(repo):
    (repo / "protected.txt").write_text("protected baseline\n", encoding="utf-8")
    git(repo, "add", "protected.txt")
    git(repo, "commit", "-m", "add protected file")


def _set_index_flag(repo, flag, path="protected.txt"):
    git(repo, "update-index", flag, path)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree", "--both"])
def test_control_rejects_preexisting_index_flags_before_start_authorization(tmp_path, flag):
    repo, records = make_repo(tmp_path)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    selected_flag = "--assume-unchanged" if flag == "--both" else flag
    _set_index_flag(repo, selected_flag, "src/example.txt")
    if flag == "--both":
        _set_index_flag(repo, "--skip-worktree", "src/example.txt")
    console = FakeConsole(["START A1B2C3D4", "STOP", "operator stop"])
    instances = []

    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 1
    assert all(not worker.calls for worker in instances)
    assert not list(records.glob("start-*.json"))
    assert not any("validated_task" in output for output in console.output)
    assert json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))["worker_invocation_observation"]["count"] == 0


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flag_added_by_worker_fails_at_post_worker_evidence(tmp_path, flag):
    repo, records = make_repo(tmp_path)
    _commit_protected_file(repo)
    task_file = tmp_path / "task.json"
    write_task(task_file)
    console = FakeConsole(["START A1B2C3D4", "ACCEPT E5F6A7B8", "looks good"])
    instances = []

    def factory(*, command, model, timeout_seconds, stdin_devnull):
        class FlaggedWorker(SyntheticWorker):
            def run(self, *, task, repo_dir):
                result = super().run(task=task, repo_dir=repo_dir)
                _set_index_flag(repo_dir, flag)
                (repo_dir / "protected.txt").write_text("hidden out-of-scope edit\n", encoding="utf-8")
                return result

        worker = FlaggedWorker(
            command=command, model=model, timeout_seconds=timeout_seconds,
            stdin_devnull=stdin_devnull, records=records,
        )
        instances.append(worker)
        return worker

    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=factory,
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 1
    assert len(instances[0].calls) == 1
    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    assert terminal["controller_result"] is None
    assert terminal["workflow_failure"]["controller_stage"] == "git-evidence"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_verifier_index_flag_and_out_of_scope_edit_fail_at_review_entry(tmp_path, flag):
    repo, records = make_repo(tmp_path)
    _commit_protected_file(repo)
    task_file = tmp_path / "task.json"
    verifier = tmp_path / "mutate.py"
    verifier.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        f"subprocess.run(['git','update-index',{flag!r},'protected.txt'], cwd={str(repo)!r}, check=True)\n"
        f"Path({str(repo / 'protected.txt')!r}).write_text('hidden verifier edit\\n', encoding='utf-8')\n",
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
    review = json.loads(next(records.glob("review-*.json")).read_text(encoding="utf-8"))
    assert review["human_decision"] is None
    assert review["nondecision"]["stage"] == "review-entry"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flag_and_out_of_scope_edit_during_review_abort_final_freshness(tmp_path, flag):
    repo, records = make_repo(tmp_path)
    _commit_protected_file(repo)
    task_file = tmp_path / "task.json"
    write_task(task_file)

    def on_read(number, _console):
        if number == 3:  # rationale follows the confirmed ACCEPT action
            _set_index_flag(repo, flag)
            (repo / "protected.txt").write_text("hidden in-review edit\n", encoding="utf-8")

    console = FakeConsole(["START A1B2C3D4", "ACCEPT E5F6A7B8", "looks good"], on_read=on_read)
    instances = []
    status = control.run_control(
        args_for(repo, records, task_file), console_factory=lambda: console,
        worker_factory=make_worker_factory(records, instances),
        challenge_factory=codes("A1B2C3D4", "E5F6A7B8"),
    )

    assert status == 1
    assert len(instances[0].calls) == 1
    review = json.loads(next(records.glob("review-*.json")).read_text(encoding="utf-8"))
    terminal = json.loads(next(records.glob("terminal-*.json")).read_text(encoding="utf-8"))
    assert review["human_decision"]["requested_action"] == "accept"
    assert terminal["controller_result"] is None
