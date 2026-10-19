from __future__ import annotations

from pathlib import Path
from typing import Literal, Sequence

from pydantic import Field

from coding_agent_harness.git import (
    GitBaseline,
    GitFailureMetadata,
    capture_attempt_evidence,
    capture_baseline,
    capture_evidence,
    capture_worktree_snapshot,
)
from coding_agent_harness.models import (
    ReviewDecision,
    StrictModel,
    TaskPacket,
    VerificationCommand,
)
from coding_agent_harness.verifier import run_verification
from coding_agent_harness.worker import classify_worker_run

class WorkerReviewerDisagreementEvent(StrictModel):
    kind: Literal[
        "worker-reviewer-disagreement",
    ]
    attempt: int = Field(ge=1)
    worker_outcome: Literal[
        "blocked",
        "failed",
    ]
    reviewer_decision: Literal[
        "accept",
    ]
    rationale: str = Field(min_length=1)


class VerificationExpectationMismatchEvent(StrictModel):
    kind: Literal[
        "verification-expectation-mismatch",
    ]
    attempt: int = Field(ge=1)
    check_id: str = Field(min_length=1)
    expectation: Literal[
        "must_pass",
        "expected_failure",
    ]
    observed_passed: bool
    expectation_reason: str | None = None

class ScopeRevisionEvent(StrictModel):
    kind: Literal["scope-revision"]
    attempt: int = Field(ge=1)
    previous_allowed_files: list[str] | None
    revised_allowed_files: list[str]
    rationale: str = Field(min_length=1)

class ScopeViolationEvent(StrictModel):
    kind: Literal["scope-violation"]
    attempt: int = Field(ge=1)
    allowed_files: list[str]
    out_of_scope_files: list[str]

class WorkerGitDisagreementEvent(StrictModel):
    kind: Literal["worker-git-disagreement"]
    attempt: int = Field(ge=1)
    worker_claimed_files: list[str]
    git_observed_files: list[str]
    claimed_only_files: list[str]
    observed_only_files: list[str]

AuditEvent = (
    WorkerReviewerDisagreementEvent
    | VerificationExpectationMismatchEvent
    | ScopeRevisionEvent
    | ScopeViolationEvent
    | WorkerGitDisagreementEvent
)

class HarnessRunResult(StrictModel):
    status: Literal[
        "accepted",
        "stopped",
        "remediation-exhausted",
        "worker-failure",
        "verification-failure",
        "scope-violation",
    ]
    attempts: int = Field(ge=1)
    audit_events: list[AuditEvent] = Field(
        default_factory=list
    )


class HarnessController:
    def __init__(
        self,
        *,
        worker,
        reviewer,
        max_remediation_attempts: int = 2,
        stdin_devnull: bool = False,
    ):
        if type(max_remediation_attempts) is not int or max_remediation_attempts < 0:
            raise ValueError(
                "max_remediation_attempts must be a nonnegative integer"
            )

        if type(stdin_devnull) is not bool:
            raise ValueError("stdin_devnull must be a boolean")
        self.worker = worker
        self.reviewer = reviewer
        self.stdin_devnull = stdin_devnull
        self.max_remediation_attempts = (
            max_remediation_attempts
        )

    def run(
        self,
        *,
        task: TaskPacket,
        repo_dir: Path,
        verification_commands: Sequence[
            VerificationCommand
        ],
        expected_baseline: GitBaseline | None = None,
    ) -> HarnessRunResult:
        context = {"attempts": [], "stage": "baseline", "worker_invocation_count": 0}
        try:
            return self._run(task=task, repo_dir=repo_dir,
                             verification_commands=verification_commands,
                             expected_baseline=expected_baseline, context=context)
        except BaseException as exc:
            context["failure_type"] = type(exc).__name__
            if context["attempts"]:
                record = context["attempts"][-1]
                if hasattr(exc, "verification_result"):
                    record["verification"] = exc.verification_result
                    record["failed_command"] = exc.failed_command
                if hasattr(exc, "process_result"):
                    record["failed_process"] = exc.process_result
            if hasattr(exc, "git_evidence"):
                context["partial_git_evidence"] = exc.git_evidence
            if isinstance(getattr(exc, "git_failure", None), GitFailureMetadata):
                context["git_failure"] = exc.git_failure
            if isinstance(getattr(exc, "git_cleanup_failure", None), GitFailureMetadata):
                context["git_cleanup_failure"] = exc.git_cleanup_failure
            exc.harness_evidence = context
            raise

    def _run(self, *, task, repo_dir, verification_commands, expected_baseline, context) -> HarnessRunResult:
        baseline = capture_baseline(repo_dir)
        context["baseline"] = baseline
        if expected_baseline is not None:
            approved = GitBaseline.model_validate(
                expected_baseline.model_dump(mode="python", warnings=False), strict=True,
            )
            if baseline.commit != approved.commit or baseline.status != approved.status:
                raise ValueError("controller baseline differs from the approved baseline")

        original_task = task
        current_task = task
        attempt = 1
        retry_count = 0
        remediation_count = 0
        scope_revision_count = 0
        audit_events: list[AuditEvent] = []
        context["audit_events"] = audit_events

        while True:
            record = {"attempt": attempt, "task": current_task}
            context["attempts"].append(record)
            context["stage"] = "attempt-snapshot"
            attempt_snapshot = capture_worktree_snapshot(
                repo_dir
            )

            record["snapshot"] = attempt_snapshot
            context["stage"] = "worker"
            worker_run = self.worker.run
            context["worker_invocation_count"] += 1
            worker_result = worker_run(
                task=current_task,
                repo_dir=repo_dir,
            )

            record["worker_result"] = worker_result
            context["stage"] = "worker-classification"
            worker_assessment = classify_worker_run(
                worker_result
            )

            record["worker_assessment"] = worker_assessment
            context["stage"] = "git-evidence"
            evidence = capture_evidence(
                repo=repo_dir,
                baseline=baseline,
            )

            record["git_evidence"] = evidence
            context["stage"] = "attempt-evidence"
            attempt_evidence = capture_attempt_evidence(
                repo=repo_dir,
                before=attempt_snapshot,
            )

            record["attempt_evidence"] = attempt_evidence
            context["stage"] = "scope-assessment"
            completion = getattr(
                worker_result,
                "completion",
                None,
            )

            attempt_changed_files = getattr(
                attempt_evidence,
                "changed_files",
                None,
            )

            if (
                completion is not None
                and attempt_changed_files is not None
            ):
                worker_claimed_files = sorted(
                    set(completion.changed_files)
                )
                git_observed_files = sorted(
                    set(attempt_changed_files)
                )

                claimed_only_files = sorted(
                    set(worker_claimed_files)
                    - set(git_observed_files)
                )
                observed_only_files = sorted(
                    set(git_observed_files)
                    - set(worker_claimed_files)
                )

                if (
                    claimed_only_files
                    or observed_only_files
                ):
                    audit_events.append(
                        WorkerGitDisagreementEvent(
                            kind="worker-git-disagreement",
                            attempt=attempt,
                            worker_claimed_files=(
                                worker_claimed_files
                            ),
                            git_observed_files=(
                                git_observed_files
                            ),
                            claimed_only_files=(
                                claimed_only_files
                            ),
                            observed_only_files=(
                                observed_only_files
                            ),
                        )
                    )

            out_of_scope_files: list[str] = []

            if current_task.allowed_files is not None:
                observed_changed_files = getattr(
                    evidence,
                    "changed_files",
                    [],
                )

                out_of_scope_files = sorted(
                    set(observed_changed_files)
                    - set(current_task.allowed_files)
                )

                if out_of_scope_files:
                    audit_events.append(
                        ScopeViolationEvent(
                            kind="scope-violation",
                            attempt=attempt,
                            allowed_files=list(
                                current_task.allowed_files
                            ),
                            out_of_scope_files=list(
                                out_of_scope_files
                            ),
                        )
                    )

            context["stage"] = "verification"
            verification_kwargs = {
                "repo_dir": repo_dir,
                "commands": verification_commands,
            }
            if self.stdin_devnull:
                verification_kwargs["stdin_devnull"] = True
            verification = run_verification(**verification_kwargs)

            record["verification"] = verification
            context["stage"] = "verification-assessment"
            for check in verification.checks:
                if check.expectation_met is False:
                    audit_events.append(
                        VerificationExpectationMismatchEvent(
                            kind=(
                                "verification-expectation-mismatch"
                            ),
                            attempt=attempt,
                            check_id=check.id,
                            expectation=check.expectation,
                            observed_passed=check.passed,
                            expectation_reason=(
                                check.expectation_reason
                            ),
                        )
                    )

            context["stage"] = "review"
            decision: ReviewDecision = (
                self.reviewer.review(
                    task=current_task,
                    worker_result=worker_result,
                    worker_assessment=worker_assessment,
                    evidence=evidence,
                    verification=verification,
                    attempt=attempt,
                )
            )

            context["stage"] = "review-validation"
            # Annotations do not validate plugin output; Pydantic instances can
            # also be mutated or constructed without validation. Rebuild from
            # data before any acceptance or retry branch is authorized.
            if not isinstance(decision, ReviewDecision):
                raise ValueError("reviewer must return a valid ReviewDecision")
            decision = ReviewDecision.model_validate(
                decision.model_dump(mode="python", warnings=False), strict=True,
            )

            record["decision"] = decision
            context["stage"] = "result-or-retry"

            if decision.decision == "accept":
                if worker_assessment.outcome in {
                    "execution-failure",
                    "protocol-failure",
                }:
                    return HarnessRunResult(
                        status="worker-failure",
                        attempts=attempt,
                        audit_events=audit_events,
                    )

                if worker_assessment.outcome in {
                    "blocked",
                    "failed",
                }:
                    audit_events.append(
                        WorkerReviewerDisagreementEvent(
                            kind=(
                                "worker-reviewer-disagreement"
                            ),
                            attempt=attempt,
                            worker_outcome=(
                                worker_assessment.outcome
                            ),
                            reviewer_decision="accept",
                            rationale=decision.rationale,
                        )
                    )

                if out_of_scope_files:
                    return HarnessRunResult(
                        status="scope-violation",
                        attempts=attempt,
                        audit_events=audit_events,
                    )

                if not verification.passed:
                    return HarnessRunResult(
                        status="verification-failure",
                        attempts=attempt,
                        audit_events=audit_events,
                    )

                return HarnessRunResult(
                    status="accepted",
                    attempts=attempt,
                    audit_events=audit_events,
                )

            if decision.decision == "stop":
                return HarnessRunResult(
                    status="stopped",
                    attempts=attempt,
                    audit_events=audit_events,
                )

            if worker_result.process.infrastructure_failed:
                return HarnessRunResult(status="worker-failure", attempts=attempt,
                                        audit_events=audit_events)
            if any(check.infrastructure_failed for check in verification.checks):
                return HarnessRunResult(status="verification-failure", attempts=attempt,
                                        audit_events=audit_events)

            if (
                retry_count
                >= self.max_remediation_attempts
            ):
                return HarnessRunResult(
                    status="remediation-exhausted",
                    attempts=attempt,
                    audit_events=audit_events,
                )

            retry_count += 1

            if decision.decision == "revise-scope":
                scope_revision_count += 1

                previous_allowed_files = (
                    list(current_task.allowed_files)
                    if current_task.allowed_files is not None
                    else None
                )

                revised_allowed_files = list(
                    decision.revised_allowed_files or []
                )

                audit_events.append(
                    ScopeRevisionEvent(
                        kind="scope-revision",
                        attempt=attempt,
                        previous_allowed_files=(
                            previous_allowed_files
                        ),
                        revised_allowed_files=(
                            revised_allowed_files
                        ),
                        rationale=decision.rationale,
                    )
                )

                current_task = TaskPacket(
                    task_id=(
                        f"{original_task.task_id}"
                        f"-scope-revision-"
                        f"{scope_revision_count}"
                    ),
                    goal=(
                        f"Original goal:\n"
                        f"{original_task.goal}\n\n"
                        f"Scope revision rationale:\n"
                        f"{decision.rationale}"
                    ),
                    constraints=list(
                        original_task.constraints
                    ),
                    acceptance=list(
                        original_task.acceptance
                    ),
                    suggested_files=list(
                        original_task.suggested_files
                    ),
                    allowed_files=revised_allowed_files,
                )

                attempt += 1
                continue

            remediation_count += 1

            current_task = TaskPacket(
                task_id=(
                    f"{original_task.task_id}"
                    f"-remediation-{remediation_count}"
                ),
                goal=(
                    f"Original goal:\n"
                    f"{original_task.goal}\n\n"
                    f"Remediation required:\n"
                    f"{decision.remediation}"
                ),
                constraints=list(
                    original_task.constraints
                ),
                acceptance=list(
                    original_task.acceptance
                ),
                suggested_files=list(
                    original_task.suggested_files
                ),
                allowed_files=(
                    list(current_task.allowed_files)
                    if current_task.allowed_files is not None
                    else None
                ),
            )

            attempt += 1