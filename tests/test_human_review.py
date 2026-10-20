import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent_harness.control_records import RecordDirectory, RecordReference
from coding_agent_harness.git import GitBaseline, GitEvidence
from coding_agent_harness.human_review import (
    EvidenceFreshnessError,
    HumanReviewer,
    _remediation_task,
    render_review_packet,
)
from coding_agent_harness.models import ReviewDecision, TaskPacket, VerificationCommand, WorkerResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.verifier import VerificationCheck, VerificationResult
from coding_agent_harness.worker import WorkerRunAssessment, WorkerRunResult
from coding_agent_harness.console import ConsoleDeadlineExpired, InvalidConsoleLine


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class FakeConsole:
    def __init__(self, lines, *, clock=None, step=0):
        self.lines = list(lines)
        self.clock = clock
        self.step = step
        self.read_deadlines = []
        self.writes = []
        self.discards = 0

    def write(self, text):
        self.writes.append(text)

    def discard_typeahead(self):
        self.discards += 1

    def read_line(self, *, deadline):
        self.read_deadlines.append(deadline)
        if self.clock is not None:
            self.clock.value += self.step
        if not self.lines:
            raise ConsoleDeadlineExpired("fake timeout")
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def fixtures(tmp_path):
    record_dir = tmp_path / "records"
    record_dir.mkdir(parents=True, exist_ok=True)
    start = RecordReference("start-id", record_dir / "start.json", "a" * 64)
    task = TaskPacket(
        task_id="task-1", goal="Improve this file.",
        constraints=["Keep the change focused."], acceptance=["The check passes."],
        suggested_files=["src/example.txt"], allowed_files=["src/example.txt"],
    )
    baseline = GitBaseline(commit="1" * 40, status="")
    process = ProcessResult(
        argv=["worker"], exit_code=0, stdout="worker output\x1b[31m\x9b\x1b]0;evil\x07\u202e",
        stderr="", timed_out=False, elapsed_seconds=0.1,
    )
    worker = WorkerRunResult(
        process=process,
        completion=WorkerResult(status="completed", summary="done", changed_files=["src/example.txt"]),
    )
    assessment = WorkerRunAssessment(outcome="completed", reason="worker says done")
    evidence = GitEvidence(
        baseline_commit=baseline.commit, final_head=baseline.commit, final_status=" M src/example.txt",
        changed_files=["src/example.txt"], patch="diff --git a/src/example.txt b/src/example.txt\n+unsafe\x1b[2J\u202e\n",
        commits=[], index_fingerprint="b" * 64, patch_sha256="d" * 64,
        index_flags_fingerprint="e" * 64, unsupported_index_flag_count=0,
    )
    check = VerificationCheck(
        argv=["python", "-c", "print('check')"], exit_code=0, stdout="check output",
        stderr="", timed_out=False, elapsed_seconds=0.1, id="tests", passed=True,
    )
    verification = VerificationResult(passed=True, checks=[check])
    commands = [VerificationCommand(id="tests", argv=["python", "-c", "print('check')"])]
    return record_dir, start, task, baseline, worker, assessment, evidence, verification, commands


def make_reviewer(tmp_path, console, *, budget=1, clock=None, challenge="ABCDEF12"):
    record_dir, start, task, baseline, *_ = fixtures(tmp_path)
    return HumanReviewer(
        console=console, repo_dir=tmp_path / "repo", baseline=baseline,
        verification_commands=[VerificationCommand(id="tests", argv=["python", "-c", "print('check')"])],
        review_timeout=30, max_remediation_attempts=budget,
        record_directory=RecordDirectory(record_dir, ()), workflow_id="workflow-id",
        start_record=start, config_sha256="c" * 64,
        challenge_factory=lambda: challenge, clock=clock or FakeClock(),
    )


def invoke(reviewer, fixture, *, attempt=1):
    _record_dir, _start, task, _baseline, worker, assessment, evidence, verification, _commands = fixture
    return reviewer.review(
        task=task, worker_result=worker, worker_assessment=assessment,
        evidence=evidence, verification=verification, attempt=attempt,
    )


def test_acceptance_challenge_is_fresh_and_recorded_before_callback_returns(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    console = FakeConsole(["ACCEPT 00000000", "ACCEPT ABCDEF12", "criteria verified"])
    reviewer = make_reviewer(tmp_path, console)
    calls = 0

    def fresh(*, repo, baseline, inspect_index_flags):
        nonlocal calls
        assert inspect_index_flags is True
        calls += 1
        if calls == 2:
            files = list((tmp_path / "records").glob("review-*.json"))
            assert len(files) == 1
            assert json.loads(files[0].read_text(encoding="utf-8"))["human_decision"]["requested_action"] == "accept"
        return fixture[6]

    monkeypatch.setattr(human_review, "capture_evidence", fresh)
    decision = invoke(reviewer, fixture)

    assert decision.decision == "accept"
    assert calls == 2
    assert len(set(console.read_deadlines)) == 1
    assert reviewer.final_freshness_completed_record_ids == [reviewer.review_records[0].record_id]
    assert reviewer.decision_record_published_ids == [reviewer.review_records[0].record_id]
    record = json.loads(reviewer.review_records[0].path.read_text(encoding="utf-8"))
    assert record["human_decision"]["rationale"] == "criteria verified"
    assert record["human_decision"]["controller_callback_return_pending_final_freshness"] is True
    assert record["authorization_binding"]["challenge_sha256"] == __import__("hashlib").sha256(b"ABCDEF12").hexdigest()
    assert "ABCDEF12" not in reviewer.review_records[0].path.read_text(encoding="utf-8")
    assert record["evidence_packet"]["independent_cumulative_git_evidence"]["changed_paths_label"].startswith("cumulative")


def test_review_entry_freshness_mismatch_publishes_nondecision(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    console = FakeConsole([])
    reviewer = make_reviewer(tmp_path, console)
    changed = fixture[6].model_copy(update={"final_status": " M changed.txt"})
    monkeypatch.setattr(human_review, "capture_evidence", lambda **kwargs: changed)

    with pytest.raises(EvidenceFreshnessError) as caught:
        invoke(reviewer, fixture)

    assert caught.value.stage == "review-entry"
    assert reviewer.review_records
    record = json.loads(reviewer.review_records[0].path.read_text(encoding="utf-8"))
    assert record["human_decision"] is None
    assert record["evidence_provenance"]["freshness"]["differences"]
    assert reviewer.final_freshness_completed_record_ids == []


def test_final_freshness_runs_after_decision_publication_and_aborts_return(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    console = FakeConsole(["ACCEPT ABCDEF12", "looks good"])
    reviewer = make_reviewer(tmp_path, console)
    calls = 0

    def fresh(*, repo, baseline, inspect_index_flags):
        nonlocal calls
        assert inspect_index_flags is True
        calls += 1
        return fixture[6] if calls == 1 else fixture[6].model_copy(update={"final_head": "2" * 40})

    monkeypatch.setattr(human_review, "capture_evidence", fresh)
    with pytest.raises(EvidenceFreshnessError) as caught:
        invoke(reviewer, fixture)

    assert caught.value.stage == "final-before-controller-return"
    assert len(reviewer.review_records) == 1
    assert reviewer.final_freshness_completed_record_ids == []
    record = json.loads(reviewer.review_records[0].path.read_text(encoding="utf-8"))
    assert record["human_decision"]["requested_action"] == "accept"


def test_remediation_requires_rationale_and_bounded_instructions(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    console = FakeConsole(["REMEDIATE ABCDEF12", "fix the failing detail", "update only the test"])
    reviewer = make_reviewer(tmp_path, console)
    monkeypatch.setattr(human_review, "capture_evidence", lambda **kwargs: fixture[6])

    decision = invoke(reviewer, fixture)
    task = _remediation_task(fixture[2], decision.remediation)

    assert decision.decision == "remediate"
    assert task.allowed_files == fixture[2].allowed_files
    assert task.constraints == fixture[2].constraints
    assert task.acceptance == fixture[2].acceptance
    assert task.goal.endswith("Remediation required:\nupdate only the test")


def test_no_remediation_capacity_rejects_action_and_stale_challenge(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    console = FakeConsole([
        "REMEDIATE ABCDEF12", "ACCEPT 00000000", "STOP", "stop after review",
    ])
    reviewer = make_reviewer(tmp_path, console, budget=1)
    monkeypatch.setattr(human_review, "capture_evidence", lambda **kwargs: fixture[6])

    decision = invoke(reviewer, fixture, attempt=2)

    assert decision.decision == "stop"
    rendered = "".join(console.writes)
    assert "No remediation attempt remains" in rendered
    assert "Acceptance has NOT yet occurred" in rendered


def test_invalid_console_events_do_not_reset_review_transaction_deadline(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    clock = FakeClock()
    console = FakeConsole([
        InvalidConsoleLine("oversized"), "ACCEPT stale", "ACCEPT ABCDEF12", "safe",
    ], clock=clock, step=0.1)
    reviewer = make_reviewer(tmp_path, console, clock=clock)
    monkeypatch.setattr(human_review, "capture_evidence", lambda **kwargs: fixture[6])

    decision = invoke(reviewer, fixture)

    assert decision.decision == "accept"
    assert len(set(console.read_deadlines)) == 1
    assert console.read_deadlines[0] == pytest.approx(130.0)


def test_review_timeout_and_ctrl_c_publish_nondecision(tmp_path, monkeypatch):
    from coding_agent_harness import human_review

    fixture = fixtures(tmp_path)
    clock = FakeClock()
    console = FakeConsole(["ACCEPT ABCDEF12"], clock=clock, step=31)
    reviewer = make_reviewer(tmp_path, console, clock=clock)
    monkeypatch.setattr(human_review, "capture_evidence", lambda **kwargs: fixture[6])
    with pytest.raises(ConsoleDeadlineExpired):
        invoke(reviewer, fixture)
    assert json.loads(reviewer.review_records[0].path.read_text(encoding="utf-8"))["human_decision"] is None

    second_root = tmp_path / "second"
    second_root.mkdir()
    second = fixtures(second_root)
    interrupt_console = FakeConsole([KeyboardInterrupt()])
    interrupt_reviewer = make_reviewer(second_root, interrupt_console)
    monkeypatch.setattr(human_review, "capture_evidence", lambda **kwargs: second[6])
    with pytest.raises(KeyboardInterrupt):
        invoke(interrupt_reviewer, second)
    interruption = json.loads(interrupt_reviewer.review_records[0].path.read_text(encoding="utf-8"))
    assert interruption["human_decision"] is None
    assert interruption["nondecision"]["exception_type"] == "KeyboardInterrupt"


def test_terminal_renderer_escapes_control_sequences_bidi_and_prompt_injection(tmp_path):
    fixture = fixtures(tmp_path)
    text = render_review_packet(
        task=fixture[2], verification_commands=fixture[8], worker_result=fixture[4],
        worker_assessment=fixture[5], evidence=fixture[6], verification=fixture[7],
        attempt=1, configured_budget=1, remaining_budget=1, challenge="ABCDEF12",
    )

    for control in ("\x1b", "\x9b", "\x07", "\u202e"):
        assert control not in text
    assert "\\u001b" in text
    assert "\\u009b" in text
    assert "Acceptance has NOT yet occurred" in text
    assert "cumulative from the approved baseline; not per-attempt changes" in text
