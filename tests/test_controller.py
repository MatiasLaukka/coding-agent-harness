from pathlib import Path
import pytest
from coding_agent_harness.git import GitAttemptEvidence, GitEvidence
import coding_agent_harness.controller as controller_module

from coding_agent_harness.controller import (
    HarnessController,
)
from coding_agent_harness.models import (
    ReviewDecision,
    TaskPacket,
    VerificationCommand,
    WorkerResult,
)
from coding_agent_harness.process import (
    ProcessResult,
)
from coding_agent_harness.verifier import (
    VerificationCheck,
    VerificationResult,
)


class FakeWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.tasks = []

    def run(
        self,
        *,
        task,
        repo_dir,
    ):
        self.calls += 1
        self.tasks.append(task)

        return type(
            "WorkerRun",
            (),
            {
                "process": ProcessResult(
                    argv=["fake"],
                    exit_code=0,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    elapsed_seconds=0.1,
                ),
                "completion": WorkerResult(
                    status="completed",
                    summary="Implemented task.",
                    changed_files=[],
                    remaining_concerns=[],
                ),
                "completion_error": None,
            },
        )()


class FakeReviewer:
    def __init__(
        self,
        decisions: list[ReviewDecision],
    ) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    def review(
        self,
        *,
        task,
        worker_result,
        worker_assessment,
        evidence,
        verification,
        attempt,
    ) -> ReviewDecision:
        self.calls += 1

        return self.decisions.pop(0)


def make_task(
    *,
    allowed_files: list[str] | None = None,
) -> TaskPacket:
    return TaskPacket(
        task_id="task-1",
        goal="Make a small change.",
        constraints=[],
        acceptance=[
            "verification passes",
        ],
        allowed_files=allowed_files,
    )

@pytest.fixture(autouse=True)
def stub_per_attempt_git_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        controller_module,
        "capture_worktree_snapshot",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        controller_module,
        "capture_attempt_evidence",
        lambda **kwargs: object(),
    )


def test_controller_accepts_successful_first_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale="Looks good.",
            )
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 1
    assert worker.calls == 1
    assert reviewer.calls == 1
    assert result.audit_events == []

def test_controller_runs_focused_remediation_then_accepts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="remediate",
                rationale=(
                    "Error handling changed."
                ),
                remediation=(
                    "Restore the original error "
                    "handling behavior."
                ),
            ),
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The remediation fixed it."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(
            allowed_files=[
                "src/example.py",
            ],
        ),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 2

    assert worker.calls == 2
    assert reviewer.calls == 2

    first_task = worker.tasks[0]
    remediation_task = worker.tasks[1]

    assert first_task.allowed_files == [
        "src/example.py",
    ]

    assert first_task.task_id == "task-1"

    assert (
        remediation_task.task_id
        == "task-1-remediation-1"
    )

    assert (
        "Restore the original error handling behavior."
        in remediation_task.goal
    )

    assert (
        "Make a small change."
        in remediation_task.goal
    )

    assert (
        remediation_task.allowed_files
        == first_task.allowed_files
    )

    assert (
        remediation_task.acceptance
        == first_task.acceptance
    )

    assert (
        remediation_task.constraints
        == first_task.constraints
    )

def test_controller_stops_when_remediation_limit_is_exhausted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="remediate",
                rationale="First problem.",
                remediation="Fix the first problem.",
            ),
            ReviewDecision(
                decision="remediate",
                rationale="Second problem.",
                remediation="Fix the second problem.",
            ),
            ReviewDecision(
                decision="remediate",
                rationale="Still not acceptable.",
                remediation="Try another fix.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "remediation-exhausted"
    assert result.attempts == 3

    assert worker.calls == 3
    assert reviewer.calls == 3

    assert [
        task.task_id
        for task in worker.tasks
    ] == [
        "task-1",
        "task-1-remediation-1",
        "task-1-remediation-2",
    ]

def test_controller_stops_when_remediation_limit_is_exhausted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="remediate",
                rationale="First problem.",
                remediation="Fix the first problem.",
            ),
            ReviewDecision(
                decision="remediate",
                rationale="Second problem.",
                remediation="Fix the second problem.",
            ),
            ReviewDecision(
                decision="remediate",
                rationale="Still not acceptable.",
                remediation="Try another fix.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "remediation-exhausted"
    assert result.attempts == 3

    assert worker.calls == 3
    assert reviewer.calls == 3

    assert [
        task.task_id
        for task in worker.tasks
    ] == [
        "task-1",
        "task-1-remediation-1",
        "task-1-remediation-2",
    ]

def test_controller_stops_immediately_when_reviewer_says_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="stop",
                rationale=(
                    "The task requires material replanning."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "stopped"
    assert result.attempts == 1

    assert worker.calls == 1
    assert reviewer.calls == 1

    assert [
        task.task_id
        for task in worker.tasks
    ] == [
        "task-1",
    ]

def test_controller_reviews_partial_work_after_worker_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TimeoutWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=None,
                        stdout="Partial worker output.",
                        stderr="",
                        timed_out=True,
                        elapsed_seconds=120.0,
                    ),
                    "completion": None,
                    "completion_error": (
                        "missing HARNESS_RESULT"
                    ),
                },
            )()

    class CapturingReviewer:
        def __init__(self) -> None:
            self.calls = 0
            self.worker_result = None
            self.worker_assessment = None

        def review(
            self,
            *,
            task,
            worker_result,
            worker_assessment,
            evidence,
            verification,
            attempt,
        ):
            self.calls += 1
            self.worker_result = worker_result
            self.worker_assessment = worker_assessment

            return ReviewDecision(
                decision="stop",
                rationale=(
                    "Worker timed out; partial work "
                    "requires review."
                ),
            )

    evidence_calls = []
    verification_calls = []

    reviewer = CapturingReviewer()

    controller = HarnessController(
        worker=TimeoutWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    def fake_capture_evidence(**kwargs):
        evidence_calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        fake_capture_evidence,
    )

    def fake_run_verification(**kwargs):
        verification_calls.append(kwargs)
        return VerificationResult(
            passed=False,
            checks=[],
        )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        fake_run_verification,
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "stopped"
    assert result.attempts == 1

    assert len(evidence_calls) == 1
    assert len(verification_calls) == 1

    assert reviewer.calls == 1
    assert reviewer.worker_result is not None
    assert reviewer.worker_result.process.timed_out is True
    assert reviewer.worker_result.completion is None
    assert (
        reviewer.worker_result.completion_error
        == "missing HARNESS_RESULT"
    )
    assert reviewer.worker_assessment is not None
    assert (
        reviewer.worker_assessment.outcome
        == "execution-failure"
    )
    assert (
        reviewer.worker_assessment.reason
        == "worker process timed out"
    )

def test_controller_reviews_partial_work_after_invalid_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InvalidCompletionWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=0,
                        stdout="Finished editing files.",
                        stderr="",
                        timed_out=False,
                        elapsed_seconds=4.2,
                    ),
                    "completion": None,
                    "completion_error": (
                        "invalid HARNESS_RESULT JSON"
                    ),
                },
            )()

    class CapturingReviewer:
        def __init__(self) -> None:
            self.worker_result = None
            self.worker_assessment = None

        def review(
            self,
            *,
            task,
            worker_result,
            worker_assessment,
            evidence,
            verification,
            attempt,
        ) -> ReviewDecision:
            self.worker_result = worker_result
            self.worker_assessment = worker_assessment

            return ReviewDecision(
                decision="stop",
                rationale=(
                    "Worker completion protocol failed."
                ),
            )

    evidence_calls = []
    verification_calls = []

    reviewer = CapturingReviewer()

    controller = HarnessController(
        worker=InvalidCompletionWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    def fake_capture_evidence(**kwargs):
        evidence_calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        fake_capture_evidence,
    )

    def fake_run_verification(**kwargs):
        verification_calls.append(kwargs)
        return VerificationResult(
            passed=True,
            checks=[],
        )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        fake_run_verification,
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "stopped"
    assert result.attempts == 1

    assert len(evidence_calls) == 1
    assert len(verification_calls) == 1

    assert reviewer.worker_assessment is not None
    assert (
        reviewer.worker_assessment.outcome
        == "protocol-failure"
    )
    assert (
        reviewer.worker_assessment.reason
        == "invalid HARNESS_RESULT JSON"
    )
    assert reviewer.worker_result is not None
    assert reviewer.worker_result.process.exit_code == 0
    assert reviewer.worker_result.process.timed_out is False
    assert reviewer.worker_result.completion is None
    assert (
        reviewer.worker_result.completion_error
        == "invalid HARNESS_RESULT JSON"
    )

def test_controller_does_not_accept_timed_out_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TimeoutWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=None,
                        stdout="Partial output.",
                        stderr="",
                        timed_out=True,
                        elapsed_seconds=120.0,
                    ),
                    "completion": None,
                    "completion_error": (
                        "missing HARNESS_RESULT"
                    ),
                },
            )()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The partial implementation "
                    "appears correct."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=TimeoutWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "worker-failure"
    assert result.attempts == 1

def test_controller_does_not_accept_protocol_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InvalidCompletionWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=0,
                        stdout="Finished editing.",
                        stderr="",
                        timed_out=False,
                        elapsed_seconds=4.0,
                    ),
                    "completion": None,
                    "completion_error": (
                        "invalid HARNESS_RESULT JSON"
                    ),
                },
            )()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The implementation appears correct."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=InvalidCompletionWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "worker-failure"
    assert result.attempts == 1

def test_controller_may_accept_worker_reported_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class ReportedFailureWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=0,
                        stdout="",
                        stderr="",
                        timed_out=False,
                        elapsed_seconds=4.0,
                    ),
                    "completion": WorkerResult(
                        status="failed",
                        summary=(
                            "I believe the implementation "
                            "did not succeed."
                        ),
                        changed_files=["src/example.ts"],
                        remaining_concerns=[
                            "A test appeared to fail."
                        ],
                    ),
                    "completion_error": None,
                },
            )()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale=(
                    "Independent verification passes and "
                    "the implementation is correct."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=ReportedFailureWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 1
    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert event.kind == "worker-reviewer-disagreement"
    assert event.attempt == 1
    assert event.worker_outcome == "failed"
    assert event.reviewer_decision == "accept"
    assert (
        event.rationale
        == (
            "Independent verification passes and "
            "the implementation is correct."
        )
    )

def test_controller_may_accept_worker_reported_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class ReportedBlockedWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=0,
                        stdout="",
                        stderr="",
                        timed_out=False,
                        elapsed_seconds=4.0,
                    ),
                    "completion": WorkerResult(
                        status="blocked",
                        summary="I believe I am blocked.",
                        changed_files=["src/example.ts"],
                        remaining_concerns=[
                            "A prerequisite appears missing."
                        ],
                    ),
                    "completion_error": None,
                },
            )()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The supposed blocker was not real; "
                    "verification passes."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=ReportedBlockedWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 1
    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert event.kind == "worker-reviewer-disagreement"
    assert event.attempt == 1
    assert event.worker_outcome == "blocked"
    assert event.reviewer_decision == "accept"
    assert (
        event.rationale
        == (
            "The supposed blocker was not real; "
            "verification passes."
        )
    )

def test_controller_audits_unexpected_verification_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale="Implementation is correct.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[
                VerificationCheck(
                    id="typecheck",
                    argv=[
                        "npm",
                        "run",
                        "typecheck",
                    ],
                    passed=True,
                    expectation="expected_failure",
                    expectation_reason=(
                        "Caller migration was expected "
                        "in the next slice."
                    ),
                    expectation_met=False,
                    exit_code=0,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    elapsed_seconds=0.1,
                )
            ],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="typecheck",
                argv=[
                    "npm",
                    "run",
                    "typecheck",
                ],
                expectation="expected_failure",
                expectation_reason=(
                    "Caller migration was expected "
                    "in the next slice."
                ),
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 1

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert (
        event.kind
        == "verification-expectation-mismatch"
    )
    assert event.attempt == 1
    assert event.check_id == "typecheck"
    assert event.expectation == "expected_failure"
    assert event.observed_passed is True
    assert (
        event.expectation_reason
        == (
            "Caller migration was expected "
            "in the next slice."
        )
    )

def test_controller_audits_failed_must_pass_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="stop",
                rationale=(
                    "Required verification failed."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=False,
            checks=[
                VerificationCheck(
                    id="typecheck",
                    argv=[
                        "npm",
                        "run",
                        "typecheck",
                    ],
                    passed=False,
                    expectation="must_pass",
                    expectation_reason=None,
                    expectation_met=False,
                    exit_code=1,
                    timed_out=False,
                    stdout="",
                    stderr="type error",
                    elapsed_seconds=0.1,
                )
            ],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="typecheck",
                argv=[
                    "npm",
                    "run",
                    "typecheck",
                ],
                expectation="must_pass",
            )
        ],
    )

    assert result.status == "stopped"
    assert result.attempts == 1

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert (
        event.kind
        == "verification-expectation-mismatch"
    )
    assert event.attempt == 1
    assert event.check_id == "typecheck"
    assert event.expectation == "must_pass"
    assert event.observed_passed is False
    assert event.expectation_reason is None

def test_controller_does_not_accept_failed_required_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The implementation still looks correct."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=False,
            checks=[
                VerificationCheck(
                    id="typecheck",
                    argv=[
                        "npm",
                        "run",
                        "typecheck",
                    ],
                    passed=False,
                    expectation="must_pass",
                    expectation_reason=None,
                    expectation_met=False,
                    exit_code=1,
                    timed_out=False,
                    stdout="",
                    stderr="type error",
                    elapsed_seconds=0.1,
                )
            ],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="typecheck",
                argv=[
                    "npm",
                    "run",
                    "typecheck",
                ],
                expectation="must_pass",
            )
        ],
    )

    assert result.status == "verification-failure"
    assert result.attempts == 1

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert (
        event.kind
        == "verification-expectation-mismatch"
    )
    assert event.check_id == "typecheck"
    assert event.expectation == "must_pass"
    assert event.observed_passed is False

def test_worker_failure_takes_precedence_over_verification_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TimeoutWorker:
        def run(
            self,
            *,
            task,
            repo_dir,
        ):
            return type(
                "WorkerRun",
                (),
                {
                    "process": ProcessResult(
                        argv=["qwen"],
                        exit_code=None,
                        stdout="Partial output.",
                        stderr="",
                        timed_out=True,
                        elapsed_seconds=120.0,
                    ),
                    "completion": None,
                    "completion_error": (
                        "missing HARNESS_RESULT"
                    ),
                },
            )()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The partial implementation "
                    "looks usable."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=TimeoutWorker(),
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=False,
            checks=[
                VerificationCheck(
                    id="typecheck",
                    argv=[
                        "npm",
                        "run",
                        "typecheck",
                    ],
                    passed=False,
                    expectation="must_pass",
                    expectation_met=False,
                    exit_code=1,
                    timed_out=False,
                    stdout="",
                    stderr="type error",
                    elapsed_seconds=0.1,
                )
            ],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="typecheck",
                argv=[
                    "npm",
                    "run",
                    "typecheck",
                ],
            )
        ],
    )

    assert result.status == "worker-failure"
    assert result.attempts == 1

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert (
        event.kind
        == "verification-expectation-mismatch"
    )
    assert event.check_id == "typecheck"

def test_controller_applies_explicit_scope_revision_and_audits_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="revise-scope",
                rationale=(
                    "The implementation also requires "
                    "the shared type definition."
                ),
                revised_allowed_files=[
                    "src/retrieval/execute.ts",
                    "src/types.ts",
                ],
            ),
            ReviewDecision(
                decision="accept",
                rationale=(
                    "The implementation is now complete."
                ),
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    baseline = object()

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: baseline,
    )

    evidence_baselines = []

    def fake_capture_evidence(**kwargs):
        evidence_baselines.append(
            kwargs["baseline"]
        )
        return object()

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        fake_capture_evidence,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(
            allowed_files=[
                "src/retrieval/execute.ts",
            ],
        ),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 2

    assert worker.calls == 2
    assert reviewer.calls == 2

    first_task = worker.tasks[0]
    revised_task = worker.tasks[1]

    assert first_task.allowed_files == [
        "src/retrieval/execute.ts",
    ]

    assert revised_task.task_id == (
        "task-1-scope-revision-1"
    )

    assert revised_task.allowed_files == [
        "src/retrieval/execute.ts",
        "src/types.ts",
    ]

    # The original packet remains unchanged.
    assert first_task.allowed_files == [
        "src/retrieval/execute.ts",
    ]

    # Both attempts are still compared with the
    # original pre-worker Git baseline.
    assert evidence_baselines == [
        baseline,
        baseline,
    ]

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert event.kind == "scope-revision"
    assert event.attempt == 1
    assert event.previous_allowed_files == [
        "src/retrieval/execute.ts",
    ]
    assert event.revised_allowed_files == [
        "src/retrieval/execute.ts",
        "src/types.ts",
    ]
    assert event.rationale == (
        "The implementation also requires "
        "the shared type definition."
    )

def test_remediation_after_scope_revision_preserves_revised_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="revise-scope",
                rationale=(
                    "The shared type definition is required."
                ),
                revised_allowed_files=[
                    "src/retrieval/execute.ts",
                    "src/types.ts",
                ],
            ),
            ReviewDecision(
                decision="remediate",
                rationale=(
                    "The error handling still needs work."
                ),
                remediation=(
                    "Restore the original error handling."
                ),
            ),
            ReviewDecision(
                decision="accept",
                rationale="Looks good now.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(
            allowed_files=[
                "src/retrieval/execute.ts",
            ],
        ),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 3

    assert worker.calls == 3
    assert reviewer.calls == 3

    original_task = worker.tasks[0]
    revised_task = worker.tasks[1]
    remediation_task = worker.tasks[2]

    assert original_task.allowed_files == [
        "src/retrieval/execute.ts",
    ]

    assert revised_task.task_id == (
        "task-1-scope-revision-1"
    )
    assert revised_task.allowed_files == [
        "src/retrieval/execute.ts",
        "src/types.ts",
    ]

    assert remediation_task.task_id == (
        "task-1-remediation-1"
    )
    assert remediation_task.allowed_files == [
        "src/retrieval/execute.ts",
        "src/types.ts",
    ]

    assert len(result.audit_events) == 1
    assert result.audit_events[0].kind == "scope-revision"

def test_scope_revision_and_remediation_share_retry_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="revise-scope",
                rationale=(
                    "The shared type definition is required."
                ),
                revised_allowed_files=[
                    "src/retrieval/execute.ts",
                    "src/types.ts",
                ],
            ),
            ReviewDecision(
                decision="remediate",
                rationale=(
                    "Error handling still needs correction."
                ),
                remediation=(
                    "Restore the original error handling."
                ),
            ),
            ReviewDecision(
                decision="revise-scope",
                rationale=(
                    "Another shared file may be required."
                ),
                revised_allowed_files=[
                    "src/retrieval/execute.ts",
                    "src/types.ts",
                    "src/errors.ts",
                ],
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(
            allowed_files=[
                "src/retrieval/execute.ts",
            ],
        ),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "remediation-exhausted"
    assert result.attempts == 3

    # Initial task + two bounded retries.
    assert worker.calls == 3
    assert reviewer.calls == 3

    assert [
        task.task_id
        for task in worker.tasks
    ] == [
        "task-1",
        "task-1-scope-revision-1",
        "task-1-remediation-1",
    ]

    # The third reviewer request must not trigger
    # a fourth worker attempt.
    assert all(
        task.task_id
        != "task-1-scope-revision-2"
        for task in worker.tasks
    )

    assert len(result.audit_events) == 1
    assert result.audit_events[0].kind == "scope-revision"

def test_controller_does_not_accept_git_observed_scope_violation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale="The implementation looks correct.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    evidence = GitEvidence(
        baseline_commit="abc123",
        final_head="abc123",
        final_status=(
            " M src/a.py\n"
            " M src/b.py"
        ),
        changed_files=[
            "src/a.py",
            "src/b.py",
        ],
        patch="",
        commits=[],
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: evidence,
    )

    monkeypatch.setattr(
        controller_module,
        "capture_attempt_evidence",
        lambda **kwargs: GitAttemptEvidence(
            before_tree="before",
            after_tree="after",
            changed_files=[
                "src/a.py",
                "src/b.py",
            ],
        ),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(
            allowed_files=[
                "src/a.py",
            ],
        ),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert len(result.audit_events) == 2

    disagreement_event = result.audit_events[0]

    assert disagreement_event.kind == (
        "worker-git-disagreement"
    )
    assert disagreement_event.attempt == 1
    assert disagreement_event.worker_claimed_files == []
    assert disagreement_event.git_observed_files == [
        "src/a.py",
        "src/b.py",
    ]
    assert disagreement_event.claimed_only_files == []
    assert disagreement_event.observed_only_files == [
        "src/a.py",
        "src/b.py",
    ]

    violation_event = result.audit_events[1]

    assert violation_event.kind == "scope-violation"
    assert violation_event.attempt == 1
    assert violation_event.allowed_files == [
        "src/a.py",
    ]
    assert violation_event.out_of_scope_files == [
        "src/b.py",
    ]

def test_scope_revision_can_resolve_git_observed_scope_violation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="revise-scope",
                rationale=(
                    "The implementation legitimately requires "
                    "the shared file."
                ),
                revised_allowed_files=[
                    "src/a.py",
                    "src/b.py",
                ],
            ),
            ReviewDecision(
                decision="accept",
                rationale="The revised scope now covers the changes.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    baseline = object()

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: baseline,
    )

    evidence = GitEvidence(
        baseline_commit="abc123",
        final_head="abc123",
        final_status=(
            " M src/a.py\n"
            " M src/b.py"
        ),
        changed_files=[
            "src/a.py",
            "src/b.py",
        ],
        patch="",
        commits=[],
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: evidence,
    )

    attempt_changes = iter(
        [
            ["src/a.py", "src/b.py"],
            [],
        ]
    )

    monkeypatch.setattr(
        controller_module,
        "capture_attempt_evidence",
        lambda **kwargs: GitAttemptEvidence(
            before_tree="before",
            after_tree="after",
            changed_files=next(attempt_changes),
        ),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(
            allowed_files=[
                "src/a.py",
            ],
        ),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 2

    assert worker.calls == 2
    assert reviewer.calls == 2

    first_task = worker.tasks[0]
    revised_task = worker.tasks[1]

    assert first_task.allowed_files == [
        "src/a.py",
    ]

    assert revised_task.allowed_files == [
        "src/a.py",
        "src/b.py",
    ]

    assert revised_task.task_id == (
        "task-1-scope-revision-1"
    )

    assert len(result.audit_events) == 3

    disagreement_event = result.audit_events[0]

    assert disagreement_event.kind == (
        "worker-git-disagreement"
    )
    assert disagreement_event.attempt == 1
    assert disagreement_event.worker_claimed_files == []
    assert disagreement_event.git_observed_files == [
        "src/a.py",
        "src/b.py",
    ]
    assert disagreement_event.claimed_only_files == []
    assert disagreement_event.observed_only_files == [
        "src/a.py",
        "src/b.py",
    ]

    violation_event = result.audit_events[1]

    assert violation_event.kind == "scope-violation"
    assert violation_event.attempt == 1
    assert violation_event.allowed_files == [
        "src/a.py",
    ]
    assert violation_event.out_of_scope_files == [
        "src/b.py",
    ]

    revision_event = result.audit_events[2]

    assert revision_event.kind == "scope-revision"
    assert revision_event.attempt == 1
    assert revision_event.previous_allowed_files == [
        "src/a.py",
    ]
    assert revision_event.revised_allowed_files == [
        "src/a.py",
        "src/b.py",
    ]

def test_controller_audits_worker_git_changed_file_disagreement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="accept",
                rationale="Independent evidence supports acceptance.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: object(),
    )

    evidence = GitEvidence(
        baseline_commit="abc123",
        final_head="abc123",
        final_status=" M src/a.py",
        changed_files=[
            "src/a.py",
        ],
        patch="",
        commits=[],
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: evidence,
    )

    monkeypatch.setattr(
        controller_module,
        "capture_attempt_evidence",
        lambda **kwargs: GitAttemptEvidence(
            before_tree="before",
            after_tree="after",
            changed_files=[
                "src/a.py",
            ],
        ),
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 1

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert event.kind == "worker-git-disagreement"
    assert event.attempt == 1
    assert event.worker_claimed_files == []
    assert event.git_observed_files == [
        "src/a.py",
    ]
    assert event.claimed_only_files == []
    assert event.observed_only_files == [
        "src/a.py",
    ]

def test_controller_audits_worker_git_disagreement_on_later_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker = FakeWorker()

    reviewer = FakeReviewer(
        [
            ReviewDecision(
                decision="remediate",
                rationale="One correction is required.",
                remediation="Make the focused correction.",
            ),
            ReviewDecision(
                decision="accept",
                rationale="The correction is complete.",
            ),
        ]
    )

    controller = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    )

    baseline = type(
        "Baseline",
        (),
        {"commit": "abc123"},
    )()

    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_baseline",
        lambda repo: baseline,
    )

    # Cumulative evidence is deliberately unavailable here.
    # The disagreement must come from per-attempt evidence.
    monkeypatch.setattr(
        "coding_agent_harness.controller.capture_evidence",
        lambda **kwargs: object(),
    )

    snapshots = [
        object(),
        object(),
    ]

    snapshot_calls = []

    def fake_capture_worktree_snapshot(repo):
        snapshot_calls.append(repo)
        return snapshots[
            len(snapshot_calls) - 1
        ]

    monkeypatch.setattr(
        controller_module,
        "capture_worktree_snapshot",
        fake_capture_worktree_snapshot,
        raising=False,
    )

    attempt_changes = iter(
        [
            [],
            ["src/second.py"],
        ]
    )

    def fake_capture_attempt_evidence(
        *,
        repo,
        before,
    ):
        return type(
            "AttemptEvidence",
            (),
            {
                "changed_files": next(
                    attempt_changes
                )
            },
        )()

    monkeypatch.setattr(
        controller_module,
        "capture_attempt_evidence",
        fake_capture_attempt_evidence,
        raising=False,
    )

    monkeypatch.setattr(
        "coding_agent_harness.controller.run_verification",
        lambda **kwargs: VerificationResult(
            passed=True,
            checks=[],
        ),
    )

    result = controller.run(
        task=make_task(),
        repo_dir=tmp_path,
        verification_commands=[
            VerificationCommand(
                id="check",
                argv=["fake"],
            )
        ],
    )

    assert result.status == "accepted"
    assert result.attempts == 2

    assert worker.calls == 2
    assert reviewer.calls == 2

    assert len(snapshot_calls) == 2

    assert len(result.audit_events) == 1

    event = result.audit_events[0]

    assert event.kind == "worker-git-disagreement"
    assert event.attempt == 2
    assert event.worker_claimed_files == []
    assert event.git_observed_files == [
        "src/second.py",
    ]
    assert event.claimed_only_files == []
    assert event.observed_only_files == [
        "src/second.py",
    ]