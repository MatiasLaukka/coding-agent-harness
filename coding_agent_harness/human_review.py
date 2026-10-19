from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from pydantic import ValidationError

from .console import ConsoleDeadlineExpired, InvalidConsoleLine, ReviewConsole
from .control_records import RecordDirectory, RecordReference, publish_record
from .git import GitBaseline, GitEvidence, capture_evidence
from .models import ReviewDecision, TaskPacket, VerificationCommand

MAX_REVIEW_OUTPUT_CHARS = 32 * 1024 * 1024
MAX_RATIONALE_CHARS = 1024
MAX_REMEDIATION_CHARS = 1000
_CHALLENGE_RE = re.compile(r"[0-9A-F]{8}\Z")


class EvidenceFreshnessError(RuntimeError):
    def __init__(self, *, stage: str, differences: list[str], expected_sha256: str,
                 observed_sha256: str | None = None):
        self.stage = stage
        self.differences = differences
        self.expected_sha256 = expected_sha256
        self.observed_sha256 = observed_sha256
        super().__init__(f"Git evidence changed or could not be confirmed at {stage}")


class HumanReviewError(RuntimeError):
    pass


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", warnings=False)
    if is_dataclass(value) and not isinstance(value, type):
        return _dump(asdict(value))
    if type(value) is dict:
        return {str(key): _dump(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_dump(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    return {"unavailable_type": type(value).__name__}


def _terminal_json(value: Any) -> str:
    # ASCII-only JSON escapes ANSI/OSC, C0/C1, bidi and all other non-ASCII
    # content. Structural newlines are emitted only by this trusted renderer.
    return json.dumps(_dump(value), ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False)


def _evidence_digest(evidence: GitEvidence) -> str:
    encoded = json.dumps(
        evidence.model_dump(mode="json", warnings=False),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_differences(expected: GitEvidence, observed: GitEvidence) -> list[str]:
    fields = (
        "baseline_commit", "final_head", "final_status", "changed_files",
        "patch", "commits", "index_fingerprint",
    )
    return [name for name in fields if getattr(expected, name) != getattr(observed, name)]


def _process_for_review(process) -> dict[str, Any]:
    result = process.model_dump(mode="json", warnings=False, exclude={"argv"})
    result["infrastructure_failed"] = process.infrastructure_failed
    return result


def _verification_for_review(verification) -> dict[str, Any]:
    return {
        "aggregate_passed": verification.passed,
        "checks": [
            {
                **_process_for_review(check),
                "id": check.id,
                "expectation": check.expectation,
                "expectation_reason": check.expectation_reason,
                "passed": check.passed,
                "expectation_met": check.expectation_met,
            }
            for check in verification.checks
        ],
    }


def _review_packet(
    *, task: TaskPacket, verification_commands: Sequence[VerificationCommand],
    worker_result, worker_assessment, evidence: GitEvidence, verification, attempt: int,
    configured_budget: int, remaining_budget: int, challenge: str | None,
) -> dict[str, Any]:
    allowed = task.allowed_files
    out_of_scope = (
        sorted(set(evidence.changed_files) - set(allowed))
        if allowed is not None else []
    )
    completion = getattr(worker_result, "completion", None)
    return {
        "task": task.model_dump(mode="json", warnings=False),
        "frozen_verification_specifications": [
            command.model_dump(mode="json", warnings=False)
            for command in verification_commands
        ],
        "attempt": attempt,
        "remediation_budget": {
            "configured": configured_budget,
            "remaining": remaining_budget,
        },
        "worker_claim_for_this_attempt": _dump(completion) if completion is not None else None,
        "worker_completion_error": getattr(worker_result, "completion_error", None),
        "worker_assessment": _dump(worker_assessment),
        "worker_process": {
            **_process_for_review(worker_result.process),
            "stdout": worker_result.process.stdout,
            "stderr": worker_result.process.stderr,
        },
        "independent_cumulative_git_evidence": {
            **evidence.model_dump(mode="json", warnings=False),
            "changed_paths_label": "cumulative from the approved baseline; not per-attempt changes",
        },
        "cumulative_scope_observation": {
            "allowed_files": allowed,
            "cumulative_out_of_scope_paths": out_of_scope,
            "scope_observation_is_not_an_acceptance_decision": True,
        },
        "verification": _verification_for_review(verification),
        "acceptance_state": "Acceptance has NOT yet occurred.",
        "action_challenge": challenge,
        "actions": (["ACCEPT <fresh challenge>", "STOP"] + (
            ["REMEDIATE <fresh challenge>"] if remaining_budget > 0 else []
        )),
    }


def render_start_authorization(
    *, task: TaskPacket, verification_commands: Sequence[VerificationCommand],
    model: str, worker_command: Sequence[str], launcher_argv: Sequence[str],
    worker_timeout: float, review_timeout: int, max_remediation_attempts: int,
    repository_root: str, git_roots: Sequence[str], record_directory: str,
    baseline: GitBaseline, config_sha256: str, challenge: str,
) -> str:
    payload = {
        "validated_task": task.model_dump(mode="json", warnings=False),
        "exact_verification_commands": [
            command.model_dump(mode="json", warnings=False)
            for command in verification_commands
        ],
        "model": model,
        "worker_command": list(worker_command),
        "exact_first_dispatch_argv": list(launcher_argv),
        "worker_timeout_seconds": worker_timeout,
        "review_timeout_seconds": review_timeout,
        "file_scope": task.allowed_files,
        "max_remediation_attempts": max_remediation_attempts,
        "approved_clean_baseline": {
            "commit": baseline.commit,
            "status": baseline.status,
        },
        "canonical_worktree_root": repository_root,
        "git_metadata_roots": list(git_roots),
        "record_only_directory": record_directory,
        "worker_and_verification_stdin": "DEVNULL",
        "execution_configuration_sha256": config_sha256,
    }
    text = (
        "R6a controller authorization. No worker has been dispatched.\n"
        + _terminal_json(payload)
        + f"\nType exactly START {challenge} to authorize the first dispatch only.\n"
    )
    if len(text) > MAX_REVIEW_OUTPUT_CHARS:
        raise HumanReviewError("authorization display exceeds its output bound")
    return text


def render_review_packet(
    *, task: TaskPacket, verification_commands: Sequence[VerificationCommand],
    worker_result, worker_assessment, evidence: GitEvidence, verification, attempt: int,
    configured_budget: int, remaining_budget: int, challenge: str,
) -> str:
    payload = _review_packet(
        task=task, verification_commands=verification_commands,
        worker_result=worker_result, worker_assessment=worker_assessment,
        evidence=evidence, verification=verification, attempt=attempt,
        configured_budget=configured_budget, remaining_budget=remaining_budget,
        challenge=challenge,
    )
    text = "HUMAN REVIEW PACKET. Acceptance has NOT yet occurred.\n" + _terminal_json(payload)
    if len(text) > MAX_REVIEW_OUTPUT_CHARS:
        raise HumanReviewError("complete review packet exceeds its output bound")
    return text


def _remediation_task(task: TaskPacket, instructions: str) -> TaskPacket:
    candidate = TaskPacket(
        task_id=f"{task.task_id}-remediation-1",
        goal=(
            f"Original goal:\n{task.goal}\n\n"
            f"Remediation required:\n{instructions}"
        ),
        constraints=list(task.constraints),
        acceptance=list(task.acceptance),
        suggested_files=list(task.suggested_files),
        allowed_files=(list(task.allowed_files) if task.allowed_files is not None else None),
    )
    return TaskPacket.model_validate(candidate.model_dump(mode="python", warnings=False), strict=True)


class HumanReviewer:
    """Bounded console adapter for the controller's existing reviewer contract."""

    def __init__(
        self, *, console: ReviewConsole, repo_dir, baseline: GitBaseline,
        verification_commands: Sequence[VerificationCommand], review_timeout: int,
        max_remediation_attempts: int, record_directory: RecordDirectory,
        workflow_id: str, start_record: RecordReference, config_sha256: str,
        challenge_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.console = console
        self.repo_dir = repo_dir
        self.baseline = baseline
        self.verification_commands = tuple(verification_commands)
        self.review_timeout = review_timeout
        self.max_remediation_attempts = max_remediation_attempts
        self.record_directory = record_directory
        self.workflow_id = workflow_id
        self.start_record = start_record
        self.config_sha256 = config_sha256
        self.challenge_factory = challenge_factory or (lambda: secrets.token_hex(4).upper())
        self.clock = clock
        self.review_records: list[RecordReference] = []
        self.callback_returned_record_ids: list[str] = []

    def _deadline(self) -> float:
        now = self.clock()
        deadline = now + self.review_timeout
        if not math.isfinite(now) or not math.isfinite(deadline) or deadline <= now:
            raise ValueError("review deadline is invalid")
        return deadline

    def _check_deadline(self, deadline: float) -> None:
        if self.clock() >= deadline:
            raise ConsoleDeadlineExpired("review deadline expired")

    def _fresh_observation(self, *, expected: GitEvidence, stage: str) -> GitEvidence:
        observed = capture_evidence(repo=self.repo_dir, baseline=self.baseline)
        expected_digest = _evidence_digest(expected)
        observed_digest = _evidence_digest(observed)
        differences = _evidence_differences(expected, observed)
        if expected.index_fingerprint is None or observed.index_fingerprint is None:
            differences = sorted(set(differences + ["index_fingerprint_unavailable"]))
        if differences:
            raise EvidenceFreshnessError(
                stage=stage, differences=differences, expected_sha256=expected_digest,
                observed_sha256=observed_digest,
            )
        return observed

    def _review_evidence(self, *, task, worker_result, worker_assessment, evidence,
                         verification, attempt, remaining_budget) -> dict[str, Any]:
        return _review_packet(
            task=task, verification_commands=self.verification_commands,
            worker_result=worker_result, worker_assessment=worker_assessment,
            evidence=evidence, verification=verification, attempt=attempt,
            configured_budget=self.max_remediation_attempts,
            remaining_budget=remaining_budget, challenge=None,
        )

    def _publish_nondecision(
        self, *, task, worker_result, worker_assessment, evidence, verification,
        attempt: int, remaining_budget: int, stage: str, error: BaseException,
        freshness: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "workflow_id": self.workflow_id,
            "attempt": attempt,
            "start_record": self.start_record.model_dump(),
            "previous_review_record": (
                self.review_records[-1].model_dump() if self.review_records else None
            ),
            "authorization_binding": {"execution_configuration_sha256": self.config_sha256},
            "evidence_provenance": {
                "capture_point": "controller post-worker evidence before verification",
                "evidence_sha256": _evidence_digest(evidence) if isinstance(evidence, GitEvidence) else None,
                "freshness_stage": stage,
                "freshness": freshness,
            },
            "evidence_packet": self._review_evidence(
                task=task, worker_result=worker_result, worker_assessment=worker_assessment,
                evidence=evidence, verification=verification, attempt=attempt,
                remaining_budget=remaining_budget,
            ),
            "human_decision": None,
            "nondecision": {
                "stage": stage,
                "exception_type": type(error).__name__,
                "git_failure": _dump(getattr(error, "git_failure", None)),
            },
            "saved_records_are_not_authorization_or_resume_checkpoints": True,
        }
        reference = publish_record(self.record_directory, kind="review", record=record)
        self.review_records.append(reference)

    def _record_nondecision_preserving(
        self, *, task, worker_result, worker_assessment, evidence, verification,
        attempt: int, remaining_budget: int, stage: str, error: BaseException,
        freshness: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._publish_nondecision(
                task=task, worker_result=worker_result, worker_assessment=worker_assessment,
                evidence=evidence, verification=verification, attempt=attempt,
                remaining_budget=remaining_budget, stage=stage, error=error,
                freshness=freshness,
            )
        except BaseException as publication_error:
            if isinstance(error, KeyboardInterrupt):
                try:
                    error.review_record_failure = type(publication_error).__name__
                except Exception:
                    pass
                return
            raise

    def _read_line(self, *, deadline: float, prompt: str) -> str:
        while True:
            self._check_deadline(deadline)
            self.console.write(prompt)
            try:
                line = self.console.read_line(deadline=deadline)
            except InvalidConsoleLine:
                self.console.write("Input rejected by the bounded console reader. Try again.\n")
                continue
            self._check_deadline(deadline)
            return line

    def _read_rationale(self, *, deadline: float, action: str) -> str:
        while True:
            line = self._read_line(
                deadline=deadline,
                prompt=f"Rationale for {action} (required, at most {MAX_RATIONALE_CHARS} characters): ",
            )
            value = line.strip()
            if not value:
                self.console.write("A nonblank rationale is required.\n")
                continue
            if len(value) > MAX_RATIONALE_CHARS:
                self.console.write("Rationale exceeded its bound. Enter a shorter rationale.\n")
                continue
            return value

    def _read_decision(self, *, task: TaskPacket, attempt: int, remaining_budget: int,
                       challenge: str, deadline: float) -> tuple[ReviewDecision, str, str | None, str | None]:
        action_prompt = (
            f"Action: type ACCEPT {challenge}, STOP, or REMEDIATE {challenge}.\n"
            if remaining_budget > 0 else f"Action: type ACCEPT {challenge} or STOP.\n"
        )
        while True:
            line = self._read_line(deadline=deadline, prompt=action_prompt)
            if line == "STOP":
                rationale = self._read_rationale(deadline=deadline, action="STOP")
                return ReviewDecision(decision="stop", rationale=rationale), "stop", rationale, None
            if line == f"ACCEPT {challenge}":
                rationale = self._read_rationale(deadline=deadline, action="ACCEPT")
                return ReviewDecision(decision="accept", rationale=rationale), "accept", rationale, None
            if line == f"REMEDIATE {challenge}":
                if remaining_budget <= 0:
                    self.console.write("No remediation attempt remains. Choose ACCEPT or STOP.\n")
                    continue
                rationale = self._read_rationale(deadline=deadline, action="REMEDIATE")
                while True:
                    instructions = self._read_line(
                        deadline=deadline,
                        prompt=f"Remediation instructions (required, at most {MAX_REMEDIATION_CHARS} characters): ",
                    ).strip()
                    if not instructions:
                        self.console.write("Remediation instructions cannot be blank.\n")
                        continue
                    if len(instructions) > MAX_REMEDIATION_CHARS:
                        self.console.write("Remediation instructions exceeded their bound. Enter a shorter value.\n")
                        continue
                    try:
                        _remediation_task(task, instructions)
                    except (ValidationError, ValueError):
                        self.console.write("The constructed remediation task exceeds its schema limits. Enter shorter instructions.\n")
                        continue
                    self._check_deadline(deadline)
                    return (
                        ReviewDecision(decision="remediate", rationale=rationale,
                                       remediation=instructions),
                        "remediate", rationale, instructions,
                    )
            self.console.write("Invalid action or stale challenge. Choose one displayed action.\n")

    def review(
        self, *, task: TaskPacket, worker_result, worker_assessment,
        evidence: GitEvidence, verification, attempt: int,
    ) -> ReviewDecision:
        deadline = self._deadline()
        remaining_budget = max(0, self.max_remediation_attempts - (attempt - 1))
        challenge = self.challenge_factory()
        if type(challenge) is not str or not _CHALLENGE_RE.fullmatch(challenge):
            raise HumanReviewError("challenge generation failed")

        try:
            self._check_deadline(deadline)
            entry = self._fresh_observation(expected=evidence, stage="review-entry")
            self._check_deadline(deadline)
        except BaseException as error:
            self._record_nondecision_preserving(
                task=task, worker_result=worker_result, worker_assessment=worker_assessment,
                evidence=evidence, verification=verification, attempt=attempt,
                remaining_budget=remaining_budget, stage="review-entry", error=error,
                freshness={
                    "status": "failed",
                    "expected_sha256": _evidence_digest(evidence),
                    "observed_sha256": getattr(error, "observed_sha256", None),
                    "differences": getattr(error, "differences", []),
                },
            )
            raise

        try:
            self.console.discard_typeahead()
            packet = render_review_packet(
                task=task, verification_commands=self.verification_commands,
                worker_result=worker_result, worker_assessment=worker_assessment,
                evidence=evidence, verification=verification, attempt=attempt,
                configured_budget=self.max_remediation_attempts,
                remaining_budget=remaining_budget, challenge=challenge,
            )
            self.console.write(packet + "\n")
            decision, action, rationale, instructions = self._read_decision(
                task=task, attempt=attempt, remaining_budget=remaining_budget,
                challenge=challenge, deadline=deadline,
            )
            self._check_deadline(deadline)
        except BaseException as error:
            self._record_nondecision_preserving(
                task=task, worker_result=worker_result, worker_assessment=worker_assessment,
                evidence=evidence, verification=verification, attempt=attempt,
                remaining_budget=remaining_budget, stage="review-input", error=error,
                freshness={"status": "confirmed-at-entry", "evidence_sha256": _evidence_digest(entry)},
            )
            raise

        record = {
            "workflow_id": self.workflow_id,
            "attempt": attempt,
            "start_record": self.start_record.model_dump(),
            "previous_review_record": (
                self.review_records[-1].model_dump() if self.review_records else None
            ),
            "authorization_binding": {
                "execution_configuration_sha256": self.config_sha256,
                "challenge_sha256": hashlib.sha256(challenge.encode("ascii")).hexdigest(),
            },
            "evidence_provenance": {
                "capture_point": "controller post-worker evidence before verification",
                "evidence_sha256": _evidence_digest(evidence),
                "review_entry_freshness": "matched",
                "review_entry_sha256": _evidence_digest(entry),
                "final_freshness_check": "required-after-publication",
            },
            "evidence_packet": self._review_evidence(
                task=task, worker_result=worker_result, worker_assessment=worker_assessment,
                evidence=evidence, verification=verification, attempt=attempt,
                remaining_budget=remaining_budget,
            ),
            "human_decision": {
                "requested_action": action,
                "rationale": rationale,
                "remediation_instructions": instructions,
                "requested_at_utc": datetime.now(timezone.utc).isoformat(),
                "controller_callback_return_pending_final_freshness": True,
            },
            "saved_records_are_not_authorization_or_resume_checkpoints": True,
        }
        reference = publish_record(self.record_directory, kind="review", record=record)
        self.review_records.append(reference)

        # Publication precedes this final bounded collection. Any discrepancy
        # aborts the callback; the saved human request is never a resume token.
        self._check_deadline(deadline)
        final = self._fresh_observation(expected=evidence, stage="final-before-controller-return")
        self._check_deadline(deadline)
        self.callback_returned_record_ids.append(reference.record_id)
        return decision
