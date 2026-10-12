from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, Sequence

from .git import GitFailureMetadata, capture_baseline, capture_evidence
from .models import (
    StrictModel,
    TaskPacket,
    VerificationCommand,
)
from .verifier import (
    VerificationResult,
    run_verification,
)
from .worker import WorkerRunResult, classify_worker_run

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
    context = {"stage": "baseline", "worker_invocation_count": 0}
    try:
        return _run_task(task=task, repo_dir=repo_dir, worker=worker,
                         verification_commands=verification_commands, context=context)
    except BaseException as exc:
        context["failure_type"] = type(exc).__name__
        if hasattr(exc, "verification_result"):
            context["verification"] = exc.verification_result
            context["failed_command"] = exc.failed_command
        if hasattr(exc, "process_result"):
            context["failed_process"] = exc.process_result
        if hasattr(exc, "git_evidence"):
            context["partial_git_evidence"] = exc.git_evidence
        if isinstance(getattr(exc, "git_failure", None), GitFailureMetadata):
            context["git_failure"] = exc.git_failure
        if isinstance(getattr(exc, "git_cleanup_failure", None), GitFailureMetadata):
            context["git_cleanup_failure"] = exc.git_cleanup_failure
        exc.harness_evidence = context
        raise


def _run_task(*, task, repo_dir, worker, verification_commands, context) -> TaskRunResult:
    baseline = capture_baseline(repo_dir)
    context["baseline"] = baseline
    context["stage"] = "worker"

    worker_run = worker.run
    context["worker_invocation_count"] += 1
    worker_result = worker_run(
        task=task,
        repo_dir=repo_dir,
    )

    context["worker_result"] = worker_result
    context["stage"] = "git-evidence"
    evidence = capture_evidence(
        repo=repo_dir,
        baseline=baseline,
    )

    context["git_evidence"] = evidence
    context["stage"] = "verification"
    verification = run_verification(
        repo_dir=repo_dir,
        commands=verification_commands,
    )

    context["verification"] = verification
    context["stage"] = "findings"
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

    assessment = classify_worker_run(worker_result)
    if worker_result.process.infrastructure_failed and not worker_result.process.timed_out:
        findings.append(RunFinding(code="worker-failed", detail=assessment.reason))

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

    context["stage"] = "result-construction"
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
