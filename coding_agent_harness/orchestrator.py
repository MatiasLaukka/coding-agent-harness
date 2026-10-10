from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, Sequence

from .git import capture_baseline, capture_evidence
from .models import (
    StrictModel,
    TaskPacket,
    VerificationCommand,
)
from .verifier import (
    VerificationResult,
    run_verification,
)
from .worker import WorkerRunResult

class RunFinding(StrictModel):
    code: Literal[
        "worker-timeout",
        "worker-failed",
        "worker-result-missing",
        "worker-changed-files-mismatch",
        "disallowed-file",
        "agent-created-commit",
        "verification-failed",
    ]
    detail: str


class TaskRunResult(StrictModel):
    task_id: str
    baseline_sha: str
    final_head_sha: str
    worker: WorkerRunResult
    changed_files: list[str]
    patch: str
    verification: VerificationResult
    findings: list[RunFinding]
    status: Literal[
        "ready-for-review",
        "needs-attention",
    ]

class Worker(Protocol):
    def run(
        self,
        *,
        task: TaskPacket,
        repo_dir: Path,
    ) -> WorkerRunResult:
        ...


def run_task(
    *,
    task: TaskPacket,
    repo_dir: Path,
    worker: Worker,
    verification_commands: Sequence[VerificationCommand],
) -> TaskRunResult:
    baseline = capture_baseline(repo_dir)

    worker_result = worker.run(
        task=task,
        repo_dir=repo_dir,
    )

    evidence = capture_evidence(
        repo=repo_dir,
        baseline=baseline,
    )

    verification = run_verification(
        repo_dir=repo_dir,
        commands=verification_commands,
    )

    findings: list[RunFinding] = []

    if worker_result.process.timed_out:
        findings.append(
            RunFinding(
                code="worker-timeout",
                detail="Worker execution timed out.",
            )
        )

    if worker_result.process.exit_code != 0:
        findings.append(
            RunFinding(
                code="worker-failed",
                detail=(
                    "Worker exited with code "
                    f"{worker_result.process.exit_code}."
                ),
            )
        )

    completion = worker_result.completion

    if completion is None:
        findings.append(
            RunFinding(
                code="worker-result-missing",
                detail=(
                    "Worker did not produce a valid completion result."
                ),
            )
        )
        claimed_changed: list[str] | None = None
    else:
        claimed_changed = sorted(completion.changed_files)

        if completion.status != "completed":
            findings.append(
                RunFinding(
                    code="worker-failed",
                    detail=(
                        "Worker reported completion status "
                        f"{completion.status!r}."
                    ),
                )
            )

    actual_changed = sorted(evidence.changed_files)

    if (
        claimed_changed is not None
        and actual_changed != claimed_changed
    ):
        findings.append(
            RunFinding(
                code="worker-changed-files-mismatch",
                detail=(
                    f"Worker claimed {claimed_changed}, "
                    f"but Git evidence found {actual_changed}."
                ),
            )
        )

    if task.allowed_files is not None:
        allowed = set(task.allowed_files)

        for path in actual_changed:
            if path not in allowed:
                findings.append(
                    RunFinding(
                        code="disallowed-file",
                        detail=(
                            "Changed file is outside allowed scope: "
                            f"{path}"
                        ),
                    )
                )

    if evidence.commits:
        findings.append(
            RunFinding(
                code="agent-created-commit",
                detail=(
                    "Worker created commit(s): "
                    + ", ".join(evidence.commits)
                ),
            )
        )

    if not verification.passed:
        findings.append(
            RunFinding(
                code="verification-failed",
                detail="Independent verification failed.",
            )
        )

    return TaskRunResult(
        task_id=task.task_id,
        baseline_sha=baseline.commit,
        final_head_sha=evidence.final_head,
        worker=worker_result,
        changed_files=actual_changed,
        patch=evidence.patch,
        verification=verification,
        findings=findings,
        status=(
            "ready-for-review"
            if not findings
            else "needs-attention"
        ),
    )
