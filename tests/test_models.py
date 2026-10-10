import pytest
from pydantic import ValidationError

from coding_agent_harness.models import (
    ReviewDecision,
    TaskPacket,
    VerificationCommand,
    WorkerResult,
)


def test_task_packet_requires_bounded_goal_and_acceptance() -> None:
    task = TaskPacket(
        task_id="h1-03",
        goal=(
            "Extract retrieval execution logic "
            "into a reusable function."
        ),
        constraints=[
            "Preserve observable behavior.",
            "Do not modify unrelated files.",
        ],
        acceptance=[
            "npm run typecheck passes",
            "relevant retrieval tests pass",
            "git diff --check passes",
        ],
        suggested_files=[
            "src/retrieval/stage.ts",
            "src/retrieval/execute.ts",
        ],
    )

    assert task.task_id == "h1-03"
    assert len(task.acceptance) == 3


def test_task_packet_rejects_missing_acceptance() -> None:
    with pytest.raises(ValidationError):
        TaskPacket(
            task_id="bad-task",
            goal="Change some code.",
            acceptance=[],
        )


def test_verification_command_is_argument_vector() -> None:
    command = VerificationCommand(
        id="typecheck",
        argv=[
            "npm",
            "run",
            "typecheck",
        ],
        timeout_seconds=120,
    )

    assert command.argv[0] == "npm"
    assert command.timeout_seconds == 120


def test_worker_result_has_structured_completion_state() -> None:
    result = WorkerResult(
        status="completed",
        summary="Implemented the requested extraction.",
        changed_files=[
            "src/retrieval/execute.ts",
        ],
        remaining_concerns=[],
    )

    assert result.status == "completed"


def test_review_decision_requires_focused_remediation() -> None:
    decision = ReviewDecision(
        decision="remediate",
        rationale=(
            "The implementation changes error behavior."
        ),
        remediation=(
            "Restore the original failure path and "
            "rerun the focused retrieval tests."
        ),
    )

    assert decision.remediation is not None


def test_review_decision_rejects_missing_remediation_instruction() -> None:
    with pytest.raises(ValidationError):
        ReviewDecision(
            decision="remediate",
            rationale="Verification failed.",
            remediation=None,
        )

def test_verification_command_defaults_to_must_pass() -> None:
    command = VerificationCommand(
        id="typecheck",
        argv=["npm", "run", "typecheck"],
    )

    assert command.expectation == "must_pass"
    assert command.expectation_reason is None


def test_verification_command_accepts_advisory() -> None:
    command = VerificationCommand(
        id="full-suite",
        argv=["npm", "test"],
        expectation="advisory",
    )

    assert command.expectation == "advisory"


def test_expected_failure_requires_reason() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "expectation_reason is required "
            "when expectation is expected_failure"
        ),
    ):
        VerificationCommand(
            id="typecheck",
            argv=["npm", "run", "typecheck"],
            expectation="expected_failure",
        )


def test_expected_failure_accepts_reason() -> None:
    command = VerificationCommand(
        id="typecheck",
        argv=["npm", "run", "typecheck"],
        expectation="expected_failure",
        expectation_reason=(
            "Caller migration happens in the next slice."
        ),
    )

    assert command.expectation == "expected_failure"
    assert (
        command.expectation_reason
        == "Caller migration happens in the next slice."
    )

def test_scope_revision_requires_revised_allowed_files() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "revised_allowed_files is required "
            "when decision is revise-scope"
        ),
    ):
        ReviewDecision(
            decision="revise-scope",
            rationale=(
                "The implementation also needs "
                "the shared type definition."
            ),
        )


def test_scope_revision_accepts_revised_allowed_files() -> None:
    decision = ReviewDecision(
        decision="revise-scope",
        rationale=(
            "The implementation also needs "
            "the shared type definition."
        ),
        revised_allowed_files=[
            "src/retrieval/execute.ts",
            "src/types.ts",
        ],
    )

    assert decision.decision == "revise-scope"
    assert decision.revised_allowed_files == [
        "src/retrieval/execute.ts",
        "src/types.ts",
    ]