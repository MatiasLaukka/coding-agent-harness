from coding_agent_harness.models import WorkerResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.worker import (
    WorkerRunResult,
    classify_worker_run,
)


def make_process(
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(
        argv=["qwen"],
        exit_code=exit_code,
        stdout="",
        stderr="",
        timed_out=timed_out,
        elapsed_seconds=1.0,
    )


def test_timeout_is_execution_failure() -> None:
    run = WorkerRunResult(
        process=make_process(
            exit_code=None,
            timed_out=True,
        ),
        completion=None,
        completion_error="missing HARNESS_RESULT",
    )

    assessment = classify_worker_run(run)

    assert assessment.outcome == "execution-failure"
    assert "timed out" in assessment.reason


def test_nonzero_exit_is_execution_failure() -> None:
    run = WorkerRunResult(
        process=make_process(exit_code=7),
        completion=None,
        completion_error="missing HARNESS_RESULT",
    )

    assessment = classify_worker_run(run)

    assert assessment.outcome == "execution-failure"
    assert "7" in assessment.reason


def test_missing_completion_is_protocol_failure() -> None:
    run = WorkerRunResult(
        process=make_process(),
        completion=None,
        completion_error="invalid HARNESS_RESULT JSON",
    )

    assessment = classify_worker_run(run)

    assert assessment.outcome == "protocol-failure"
    assert (
        assessment.reason
        == "invalid HARNESS_RESULT JSON"
    )


def test_completed_worker_result_is_completed() -> None:
    run = WorkerRunResult(
        process=make_process(),
        completion=WorkerResult(
            status="completed",
            summary="Implemented the task.",
            changed_files=["src/example.ts"],
            remaining_concerns=[],
        ),
        completion_error=None,
    )

    assessment = classify_worker_run(run)

    assert assessment.outcome == "completed"


def test_reported_blocked_and_failed_are_preserved() -> None:
    blocked = WorkerRunResult(
        process=make_process(),
        completion=WorkerResult(
            status="blocked",
            summary="Cannot continue.",
            changed_files=[],
            remaining_concerns=["Missing prerequisite."],
        ),
        completion_error=None,
    )

    failed = WorkerRunResult(
        process=make_process(),
        completion=WorkerResult(
            status="failed",
            summary="Implementation failed.",
            changed_files=[],
            remaining_concerns=["Tests cannot pass."],
        ),
        completion_error=None,
    )

    assert (
        classify_worker_run(blocked).outcome
        == "blocked"
    )
    assert (
        classify_worker_run(failed).outcome
        == "failed"
    )