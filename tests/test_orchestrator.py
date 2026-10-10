from pathlib import Path

from coding_agent_harness import orchestrator
from coding_agent_harness.git import (
    GitBaseline,
    GitEvidence,
)
from coding_agent_harness.models import (
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
from coding_agent_harness.worker import (
    WorkerRunResult,
)


def make_task(
    *,
    allowed_files: list[str] | None = None,
) -> TaskPacket:
    return TaskPacket(
        task_id="task-1",
        goal="Make the requested bounded change.",
        constraints=[
            "Do not make unrelated changes.",
        ],
        acceptance=[
            "Independent verification passes.",
        ],
        suggested_files=[
            "src/example.py",
        ],
        allowed_files=allowed_files,
    )


def make_worker_result(
    *,
    claimed_files: list[str] | None = None,
    exit_code: int = 0,
    timed_out: bool = False,
    completion: WorkerResult | None = None,
) -> WorkerRunResult:
    return WorkerRunResult(
        process=ProcessResult(
            argv=[
                "qwen",
            ],
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.1,
        ),
        completion=WorkerResult(
            status="completed",
            summary="Implemented the requested change.",
            changed_files=(
                claimed_files
                if claimed_files is not None
                else ["src/example.py"]
            ),
            remaining_concerns=[],
        ),
        completion_error=None,
    )


def make_baseline() -> GitBaseline:
    return GitBaseline(
        commit="a" * 40,
        status="",
    )


def make_evidence(
    *,
    changed_files: list[str] | None = None,
    commits: list[str] | None = None,
) -> GitEvidence:
    baseline = make_baseline()

    return GitEvidence(
        baseline_commit=baseline.commit,
        final_head=baseline.commit,
        final_status="",
        changed_files=(
            changed_files
            if changed_files is not None
            else ["src/example.py"]
        ),
        patch="diff --git a/src/example.py b/src/example.py\n",
        commits=(
            commits
            if commits is not None
            else []
        ),
    )


def make_verification(
    *,
    passed: bool = True,
) -> VerificationResult:
    return VerificationResult(
        passed=passed,
        checks=[
            VerificationCheck(
                id="tests",
                argv=[
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                ],
                passed=passed,
                exit_code=(
                    0
                    if passed
                    else 1
                ),
                timed_out=False,
                stdout=(
                    "tests passed"
                    if passed
                    else ""
                ),
                stderr=(
                    ""
                    if passed
                    else "tests failed"
                ),
                elapsed_seconds=0.1,
            )
        ],
    )


class FakeWorker:
    def __init__(
        self,
        result: WorkerRunResult,
    ) -> None:
        self.result = result
        self.calls: list[
            tuple[
                TaskPacket,
                Path,
            ]
        ] = []

    def run(
        self,
        *,
        task: TaskPacket,
        repo_dir: Path,
    ) -> WorkerRunResult:
        self.calls.append(
            (
                task,
                repo_dir,
            )
        )

        return self.result


def arrange_harness_dependencies(
    monkeypatch,
    *,
    evidence: GitEvidence | None = None,
    verification: VerificationResult | None = None,
) -> GitBaseline:
    baseline = make_baseline()

    monkeypatch.setattr(
        orchestrator,
        "capture_baseline",
        lambda *args, **kwargs: baseline,
    )

    monkeypatch.setattr(
        orchestrator,
        "capture_evidence",
        lambda *args, **kwargs: (
            evidence
            if evidence is not None
            else make_evidence()
        ),
    )

    monkeypatch.setattr(
        orchestrator,
        "run_verification",
        lambda *args, **kwargs: (
            verification
            if verification is not None
            else make_verification()
        ),
    )

    return baseline


def finding_codes(
    result,
) -> list[str]:
    return [
        finding.code
        for finding in result.findings
    ]


def test_run_task_returns_ready_for_review_for_clean_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    baseline = arrange_harness_dependencies(
        monkeypatch,
    )

    worker = FakeWorker(
        make_worker_result()
    )

    task = make_task(
        allowed_files=[
            "src/example.py",
        ],
    )

    result = orchestrator.run_task(
        task=task,
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[
            VerificationCommand(
                id="tests",
                argv=[
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                ],
            )
        ],
    )

    assert result.task_id == "task-1"
    assert result.baseline_sha == baseline.commit
    assert result.final_head_sha == baseline.commit
    assert result.changed_files == [
        "src/example.py",
    ]
    assert result.status == "ready-for-review"
    assert result.findings == []
    assert result.verification.passed is True

    assert worker.calls == [
        (
            task,
            tmp_path,
        )
    ]


def test_run_task_flags_worker_changed_files_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    arrange_harness_dependencies(
        monkeypatch,
        evidence=make_evidence(
            changed_files=[
                "src/actual.py",
            ],
        ),
    )

    worker = FakeWorker(
        make_worker_result(
            claimed_files=[
                "src/claimed.py",
            ],
        )
    )

    result = orchestrator.run_task(
        task=make_task(),
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[],
    )

    assert result.status == "needs-attention"
    assert (
        "worker-changed-files-mismatch"
        in finding_codes(result)
    )


def test_run_task_flags_file_outside_allowed_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    arrange_harness_dependencies(
        monkeypatch,
        evidence=make_evidence(
            changed_files=[
                "src/unexpected.py",
            ],
        ),
    )

    worker = FakeWorker(
        make_worker_result(
            claimed_files=[
                "src/unexpected.py",
            ],
        )
    )

    result = orchestrator.run_task(
        task=make_task(
            allowed_files=[
                "src/example.py",
            ],
        ),
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[],
    )

    assert result.status == "needs-attention"
    assert (
        "disallowed-file"
        in finding_codes(result)
    )


def test_run_task_flags_agent_created_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    arrange_harness_dependencies(
        monkeypatch,
        evidence=make_evidence(
            commits=[
                (
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    "\tworker commit"
                ),
            ],
        ),
    )

    worker = FakeWorker(
        make_worker_result()
    )

    result = orchestrator.run_task(
        task=make_task(),
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[],
    )

    assert result.status == "needs-attention"
    assert (
        "agent-created-commit"
        in finding_codes(result)
    )


def test_run_task_flags_failed_independent_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    arrange_harness_dependencies(
        monkeypatch,
        verification=make_verification(
            passed=False,
        ),
    )

    worker = FakeWorker(
        make_worker_result()
    )

    result = orchestrator.run_task(
        task=make_task(),
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[
            VerificationCommand(
                id="tests",
                argv=[
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                ],
            )
        ],
    )

    assert result.status == "needs-attention"
    assert (
        "verification-failed"
        in finding_codes(result)
    )

def test_run_task_flags_worker_process_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    arrange_harness_dependencies(monkeypatch)

    result = make_worker_result()
    result.process.timed_out = True
    result.process.exit_code = 1

    worker = FakeWorker(result)

    run = orchestrator.run_task(
        task=make_task(),
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[],
    )

    codes = finding_codes(run)

    assert run.status == "needs-attention"
    assert "worker-timeout" in codes
    assert "worker-failed" in codes

def test_run_task_flags_missing_worker_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    arrange_harness_dependencies(monkeypatch)

    worker = FakeWorker(
        WorkerRunResult(
            process=ProcessResult(
                argv=["qwen"],
                exit_code=0,
                stdout="",
                stderr="",
                timed_out=False,
                elapsed_seconds=0.1,
            ),
            completion=None,
            completion_error="missing HARNESS_RESULT",
        )
    )

    result = orchestrator.run_task(
        task=make_task(),
        repo_dir=tmp_path,
        worker=worker,
        verification_commands=[],
    )

    assert result.status == "needs-attention"
    assert "worker-result-missing" in finding_codes(result)