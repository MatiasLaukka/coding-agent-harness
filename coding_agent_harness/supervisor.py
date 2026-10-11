"""Offline supervisor documents and trusted-caller preflight.

This sidecar has no execution capability. Approval validation is not execution
authorization; existing controllers and the run CLI remain approval-unaware.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Literal, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .controller import HarnessRunResult
from .git import GitAttemptEvidence, GitEvidence
from .models import ReviewDecision, StrictModel, TaskPacket, VerificationCommand
from .verifier import VerificationResult
from .worker import WorkerRunAssessment, WorkerRunResult, classify_worker_run

WIRE_LIMIT = 65536
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitObject = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Label = Annotated[str, Field(min_length=1, max_length=256)]
Rationale = Annotated[str, Field(min_length=1, max_length=2000)]


class HandoffError(ValueError):
    """Static, non-echoing boundary error."""


def _json_value(value):
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str:
        value.encode("utf-8")  # Reject lone surrogates.
        return value
    if type(value) is list:
        return [_json_value(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {_json_value(key): _json_value(item) for key, item in value.items()}
    raise HandoffError("invalid_document")


def canonical_json(value: StrictModel | dict | list) -> bytes:
    try:
        return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HandoffError("invalid_document") from None


def canonical_digest(value: StrictModel | dict | list) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def portable_path(value: str) -> str:
    if (not value or len(value) > 256 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
            or any(c in value for c in (chr(92) + ':*?[]<>"|')) or value.startswith("/")):
        raise ValueError("invalid_path")
    for part in value.split("/"):
        stem = part.split(".")[0].rstrip(" .").upper()
        if (part in ("", ".", "..") or part.lower() == ".git"
                or part.endswith((" ", "."))
                or re.fullmatch(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³]", stem)):
            raise ValueError("invalid_path")
    return value


def _paths(paths: list[str]) -> None:
    if len(paths) > 64:
        raise ValueError("too_many_paths")
    for path in paths:
        portable_path(path)
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("duplicate_paths")


def _argv(argv: list[str]) -> None:
    if not 1 <= len(argv) <= 64 or any(not s or len(s) > 4000 for s in argv):
        raise ValueError("invalid_argv")


def _nested(model, value):
    # Existing global models are deliberately unchanged. Revalidate their dumps
    # strictly here, including already constructed objects (not trusted instances).
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    return model.model_validate(value, strict=True)


class Document(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def json_compatible(cls, value):
        _json_value(value)
        if isinstance(value, dict):
            # Pydantic Literal uses Python equality even with strict=True.
            # Reject bool/int lookalikes before Literal normalization.
            for name, exact_type in (
                ("schema_version", int), ("max_remediation_attempts", int),
                ("approve_task", bool), ("approve_scope", bool),
                ("approve_checks", bool), ("approve_configuration", bool),
                ("no_execution_performed", bool),
            ):
                if name in cls.model_fields and name in value and type(value[name]) is not exact_type:
                    raise ValueError("invalid_scalar_type")
        return value


class RepositoryIdentity(Document):
    repository_sha256: Digest
    baseline_commit: GitObject
    baseline_tree: GitObject


class ExecutionConfig(Document):
    command_prefix: list[str]
    model: Label
    worker_timeout_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    max_remediation_attempts: Literal[0] = 0
    turn_limit: int | None = Field(default=None, ge=1, le=1000)
    tool_call_limit: int | None = Field(default=None, ge=1, le=1000)
    wall_limit_seconds: float | None = Field(default=None, gt=0, le=3600, allow_inf_nan=False)
    runtime_configuration_sha256: Digest | None = None

    @model_validator(mode="after")
    def bounds(self):
        _argv(self.command_prefix)
        # Literal[0] otherwise accepts False through Python equality.
        if type(self.max_remediation_attempts) is not int:
            raise ValueError("invalid_budget")
        return self


class TaskSpecification(Document):
    task: TaskPacket
    verification_commands: list[VerificationCommand] = Field(min_length=1, max_length=64)
    execution_config: ExecutionConfig

    @model_validator(mode="before")
    @classmethod
    def nested(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if "task" in value:
                value["task"] = _nested(TaskPacket, value["task"])
            if "execution_config" in value:
                value["execution_config"] = _nested(ExecutionConfig, value["execution_config"])
            if "verification_commands" in value:
                value["verification_commands"] = [
                    _nested(VerificationCommand, c) for c in value["verification_commands"]]
        return value

    @model_validator(mode="after")
    def bounds(self):
        if len(self.task.task_id) > 256 or self.task.allowed_files is None:
            raise ValueError("explicit_scope_required")
        _paths(self.task.allowed_files)
        _paths(self.task.suggested_files)
        for strings in (self.task.constraints, self.task.acceptance):
            if len(strings) > 64 or any(len(s) > 4000 for s in strings):
                raise ValueError("task_bounds")
        ids = []
        for c in self.verification_commands:
            if len(c.id) > 256 or c.timeout_seconds > 3600:
                raise ValueError("check_bounds")
            if c.expectation_reason is not None and len(c.expectation_reason) > 2000:
                raise ValueError("check_bounds")
            _argv(c.argv)
            ids.append(c.id)
        if len(set(ids)) != len(ids) or not any(
                c.expectation == "must_pass" for c in self.verification_commands):
            raise ValueError("required_unique_checks")
        return self


class SupervisorProposal(Document):
    schema_version: Literal[1] = 1
    proposal_id: Label
    parent_handoff_sha256: Digest | None = None
    parent_task_sha256: Digest | None = None
    parent_baseline_sha256: Digest | None = None
    repository: RepositoryIdentity
    action: Literal["task", "review", "escalate", "stop"]
    rationale: Rationale
    task_spec: TaskSpecification | None = None
    review_decision: ReviewDecision | None = None
    follow_up_task: TaskSpecification | None = None
    requested_capability: Rationale | None = None

    @model_validator(mode="before")
    @classmethod
    def nested(cls, value):
        if isinstance(value, dict) and value.get("review_decision") is not None:
            value = dict(value)
            value["review_decision"] = _nested(ReviewDecision, value["review_decision"])
        return value

    @model_validator(mode="after")
    def payload(self):
        if type(self.schema_version) is not int:
            raise ValueError("invalid_version")
        refs = [self.parent_handoff_sha256, self.parent_task_sha256, self.parent_baseline_sha256]
        if any(refs) and not all(refs):
            raise ValueError("parent_references_required")
        if self.action != "task" and not all(refs):
            raise ValueError("parent_references_required")
        if ((self.task_spec is not None) != (self.action == "task")
                or (self.review_decision is not None) != (self.action == "review")
                or (self.requested_capability is not None) != (self.action == "escalate")):
            raise ValueError("action_payload_mismatch")
        if self.follow_up_task is not None and (
                self.action != "review" or self.review_decision.decision not in
                ("remediate", "revise-scope")):
            raise ValueError("action_payload_mismatch")
        if self.review_decision is not None:
            d = self.review_decision
            if len(d.rationale) > 2000 or (d.remediation and len(d.remediation) > 2000):
                raise ValueError("review_bounds")
            if d.revised_allowed_files is not None:
                _paths(d.revised_allowed_files)
            if d.decision != "remediate" and d.remediation is not None:
                raise ValueError("action_payload_mismatch")
            if d.decision != "revise-scope" and d.revised_allowed_files is not None:
                raise ValueError("action_payload_mismatch")
        return self


class OperatorApproval(Document):
    schema_version: Literal[1] = 1
    proposal_sha256: Digest
    repository: RepositoryIdentity
    task_sha256: Digest
    allowed_files: list[str] = Field(max_length=64)
    verification_sha256: Digest
    execution_config_sha256: Digest
    approve_task: Literal[True]
    approve_scope: Literal[True]
    approve_checks: Literal[True]
    approve_configuration: Literal[True]

    @model_validator(mode="after")
    def exact_booleans(self):
        if type(self.schema_version) is not int or any(type(getattr(self, key)) is not bool
                for key in ("approve_task", "approve_scope", "approve_checks",
                            "approve_configuration")):
            raise ValueError("invalid_approval")
        _paths(self.allowed_files)
        return self


T = TypeVar("T", bound=StrictModel)


def _wire(model: type[T], value) -> T:
    try:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        obj = model.model_validate(value, strict=True)
        if len(canonical_json(obj)) > WIRE_LIMIT:
            raise HandoffError("document_too_large")
        return obj
    except HandoffError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HandoffError("invalid_document") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("invalid_document")
        result[key] = value
    return result


def load_document(path: Path, model: type[T]) -> T:
    """Strict bounded loader also used for the CLI's execution configuration."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(WIRE_LIMIT + 1)
        if len(raw) > WIRE_LIMIT:
            raise HandoffError("document_too_large")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               HandoffError("invalid_document")))
        return _wire(model, value)
    except HandoffError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise HandoffError("invalid_document") from None


def load_proposal(path: Path) -> SupervisorProposal:
    return load_document(path, SupervisorProposal)


class ExceptionObservation(Document):
    stage: Literal["reviewer", "controller", "caller"]
    type_name: str
    message_sha256: Digest | None = None


class CallbackRecord(StrictModel):
    attempt: int = Field(ge=1)
    task: TaskPacket
    worker_result: WorkerRunResult
    worker_assessment: WorkerRunAssessment
    evidence: GitEvidence
    verification: VerificationResult
    review_decision: ReviewDecision | None = None
    reviewer_exception: ExceptionObservation | None = None
    per_attempt_evidence: GitAttemptEvidence | None = None

    @model_validator(mode="after")
    def termination(self):
        if (self.review_decision is None) == (self.reviewer_exception is None):
            raise ValueError("callback_termination_required")
        return self


class RecordedRun(StrictModel):
    run_id: Label
    original_task: TaskPacket
    verification_commands: list[VerificationCommand] = Field(max_length=64)
    baseline: RepositoryIdentity | None = None
    baseline_source: Literal["trusted_preflight", "controller_evidence", "missing"]
    controller_max_remediation_attempts: int | None = Field(default=None, ge=0)
    callbacks: list[CallbackRecord] = Field(default_factory=list, max_length=32)
    result: HarnessRunResult | None = None
    exception: ExceptionObservation | None = None

    @model_validator(mode="after")
    def termination(self):
        if (self.result is None) == (self.exception is None):
            raise ValueError("run_termination_required")
        if (self.baseline is None) != (self.baseline_source == "missing"):
            raise ValueError("baseline_source_mismatch")
        return self


class RecordingReviewer:
    """Pass-through recorder; snapshots inputs BEFORE the delegate can mutate them."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.records: list[CallbackRecord] = []

    def review(self, **controller_kwargs) -> ReviewDecision:
        import copy

        snapshot = copy.deepcopy(controller_kwargs)
        try:
            decision = self.delegate.review(**controller_kwargs)
        except Exception as error:
            self.records.append(CallbackRecord(
                **snapshot, reviewer_exception=ExceptionObservation(
                    stage="reviewer", type_name=type(error).__name__,
                    message_sha256=hashlib.sha256(str(error).encode(
                        "utf-8", errors="replace")).hexdigest())))
            raise
        self.records.append(CallbackRecord(
            **snapshot, review_decision=copy.deepcopy(decision)))
        return decision


class TerminationSummary(Document):
    kind: Literal["returned", "exception"]
    status: Literal["accepted", "stopped", "remediation-exhausted", "worker-failure",
                    "verification-failure", "scope-violation"] | None = None
    attempts: int | None = Field(default=None, ge=1, le=32)
    exception_stage: Literal["reviewer", "controller", "caller"] | None = None
    exception_sha256: Digest | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if self.kind == "returned":
            if self.status is None or self.attempts is None or (
                    self.exception_stage is not None or self.exception_sha256 is not None):
                raise ValueError("invalid_termination")
        elif (self.status is not None or self.attempts is not None
              or self.exception_stage is None or self.exception_sha256 is None):
            raise ValueError("invalid_termination")
        return self


class WorkerClaims(Document):
    completion_status: Literal["completed", "blocked", "failed"] | None
    completion_sha256: Digest | None
    changed_files: list[str] = Field(max_length=64)


class AttemptObservation(Document):
    before_tree: GitObject
    after_tree: GitObject
    changed_files: list[str] = Field(max_length=64)
    sha256: Digest


class IndependentObservations(Document):
    baseline_commit: GitObject
    final_head: GitObject
    changed_files: list[str] = Field(max_length=64)
    commits: list[GitObject] = Field(max_length=64)
    file_scope: Literal["within_scope", "out_of_scope", "unspecified"]
    out_of_scope_files: list[str] = Field(max_length=64)
    git_evidence_sha256: Digest
    per_attempt: AttemptObservation | None
    inter_attempt_state_change: bool | None = None


class CheckSummary(Document):
    id: Label
    specification_sha256: Digest
    expectation: Literal["must_pass", "expected_failure", "advisory"]
    passed: bool
    expectation_met: bool | None
    exit_code: int | None
    timed_out: bool
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)


class CallbackSummary(Document):
    attempt: int = Field(ge=1, le=32)
    task_sha256: Digest
    source_sha256: Digest
    worker_exit_code: int | None
    worker_timed_out: bool
    worker_elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    worker_outcome: Literal["execution-failure", "protocol-failure", "completed", "blocked", "failed"]
    worker_claims: WorkerClaims
    independent_observations: IndependentObservations
    verification_passed: bool
    verification_sha256: Digest
    checks: list[CheckSummary] = Field(max_length=64)
    reviewer_decision: Literal["accept", "remediate", "stop", "revise-scope"] | None
    reviewer_record_sha256: Digest

    @model_validator(mode="after")
    def observed_consistency(self):
        claims = self.worker_claims
        if (claims.completion_status is None) != (claims.completion_sha256 is None):
            raise ValueError("invalid_completion_provenance")
        expected = ("execution-failure" if self.worker_timed_out or
                    self.worker_exit_code is None or self.worker_exit_code != 0 else
                    "protocol-failure" if claims.completion_status is None else
                    claims.completion_status)
        if self.worker_outcome != expected:
            raise ValueError("invalid_worker_provenance")
        overall = True
        for check in self.checks:
            passed = check.exit_code == 0 and not check.timed_out
            met = (None if check.expectation == "advisory" else
                   passed if check.expectation == "must_pass" else
                   not check.timed_out and check.exit_code is not None and check.exit_code != 0)
            if check.passed != passed or check.expectation_met != met:
                raise ValueError("invalid_check_provenance")
            overall &= (passed if check.expectation == "must_pass" else
                        not check.timed_out if check.expectation == "expected_failure" else True)
        if self.verification_passed != overall:
            raise ValueError("invalid_check_provenance")
        facts = self.independent_observations
        if (facts.file_scope == "out_of_scope") != bool(facts.out_of_scope_files):
            raise ValueError("invalid_scope_provenance")
        return self


class AuditSummary(Document):
    kind: Literal["worker-git-disagreement", "scope-violation",
                  "verification-expectation-mismatch", "scope-revision",
                  "worker-reviewer-disagreement"]
    attempt: int = Field(ge=1, le=32)
    sha256: Digest
    independently_verified: bool


class HandoffSummary(Document):
    schema_version: Literal[1] = 1
    run_id: Label
    original_task_sha256: Digest
    verification_sha256: Digest
    baseline: RepositoryIdentity | None
    baseline_source: Literal["trusted_preflight", "controller_evidence", "missing"]
    controller_baseline_confirmed: bool
    controller_max_remediation_attempts: int | None = Field(default=None, ge=0)
    termination: TerminationSummary
    controller_result_sha256: Digest | None
    callbacks: list[CallbackSummary] = Field(max_length=32)
    audits: list[AuditSummary] = Field(max_length=64)
    provenance_complete: bool
    missing_evidence: list[Label] = Field(max_length=64)
    optional_missing_evidence: list[Label] = Field(max_length=64)
    semantic_task_scope: Literal["not_assessed_by_handoff"] = "not_assessed_by_handoff"

    @model_validator(mode="after")
    def consistency(self):
        if type(self.schema_version) is not int:
            raise ValueError("invalid_version")
        if self.provenance_complete != (not self.missing_evidence):
            raise ValueError("invalid_provenance")
        if (self.termination.kind == "returned") != (self.controller_result_sha256 is not None):
            raise ValueError("invalid_termination")
        nums = [c.attempt for c in self.callbacks]
        if nums != sorted(set(nums)):
            raise ValueError("invalid_attempts")
        if self.termination.attempts is not None and any(
                n > self.termination.attempts for n in nums + [a.attempt for a in self.audits]):
            raise ValueError("invalid_attempts")
        if self.baseline is None and self.baseline_source != "missing":
            raise ValueError("invalid_baseline")
        required_missing = []
        if self.baseline is None:
            required_missing.append("baseline")
        if self.controller_max_remediation_attempts is None:
            required_missing.append("controller_budget")
        if self.termination.kind == "returned":
            required_missing.extend(f"callback:{n}" for n in
                range(1, self.termination.attempts + 1) if n not in nums)
        else:
            required_missing.append("controller_return_unavailable")
        if not set(required_missing).issubset(self.missing_evidence):
            raise ValueError("missing_required_provenance")
        if self.controller_baseline_confirmed != (self.baseline is not None and bool(self.callbacks)):
            raise ValueError("invalid_baseline")
        for callback in self.callbacks:
            if callback.attempt == 1 and callback.task_sha256 != self.original_task_sha256:
                raise ValueError("invalid_task_provenance")
            if self.baseline is not None and (
                    callback.independent_observations.baseline_commit != self.baseline.baseline_commit):
                raise ValueError("invalid_baseline")
            if (callback.independent_observations.per_attempt is None and
                    f"per_attempt_evidence:{callback.attempt}" not in self.optional_missing_evidence):
                raise ValueError("missing_optional_provenance")
        by_attempt = {c.attempt: c for c in self.callbacks}
        if [a.attempt for a in self.audits] != sorted(a.attempt for a in self.audits):
            raise ValueError("invalid_audit_order")
        for audit in self.audits:
            source = by_attempt.get(audit.attempt)
            if audit.independently_verified and (source is None or
                    (audit.kind == "worker-git-disagreement" and
                     source.independent_observations.per_attempt is None)):
                raise ValueError("unavailable_audit_evidence")
        final = by_attempt.get(self.termination.attempts)
        if final is not None:
            decision = final.reviewer_decision
            if decision is None:
                raise ValueError("exception_is_not_return")
            if decision == "stop":
                expected = "stopped"
            elif decision == "accept":
                expected = ("worker-failure" if final.worker_outcome in
                            ("execution-failure", "protocol-failure") else
                            "scope-violation" if final.independent_observations.file_scope == "out_of_scope"
                            else "verification-failure" if not final.verification_passed else "accepted")
            else:
                expected = "remediation-exhausted"
            if self.termination.status != expected:
                raise ValueError("invalid_controller_provenance")
        if self.termination.attempts is not None and self.controller_max_remediation_attempts is not None:
            if self.termination.attempts > self.controller_max_remediation_attempts + 1:
                raise ValueError("invalid_budget_provenance")
            if (self.termination.status == "remediation-exhausted" and final is not None
                    and self.termination.attempts != self.controller_max_remediation_attempts + 1):
                raise ValueError("invalid_budget_provenance")
        if self.termination.kind == "exception" and self.audits:
            raise ValueError("invented_audits")
        return self


def load_handoff(path: Path) -> HandoffSummary:
    return load_document(path, HandoffSummary)


def _contradiction(condition: bool) -> None:
    if condition:
        raise HandoffError("contradictory_provenance")


def _scope(callback: CallbackRecord) -> list[str]:
    allowed = callback.task.allowed_files
    return [] if allowed is None else sorted(set(callback.evidence.changed_files) - set(allowed))


def _check_provenance(callback: CallbackRecord, commands: Sequence[VerificationCommand]) -> None:
    checks = callback.verification.checks
    _contradiction(len(checks) != len(commands))
    _contradiction(len({c.id for c in commands}) != len(commands))
    overall = True
    for check, command in zip(checks, commands):
        _contradiction(any(getattr(check, key) != getattr(command, key)
                           for key in ("id", "argv", "expectation", "expectation_reason")))
        passed = check.exit_code == 0 and not check.timed_out
        met = (None if command.expectation == "advisory" else
               passed if command.expectation == "must_pass" else
               not check.timed_out and check.exit_code is not None and check.exit_code != 0)
        _contradiction(check.passed != passed or check.expectation_met != met)
        overall &= (passed if command.expectation == "must_pass" else
                    not check.timed_out if command.expectation == "expected_failure" else True)
    _contradiction(callback.verification.passed != overall)


def _expected_status(callback: CallbackRecord, budget: int | None) -> str | None:
    decision = callback.review_decision
    if decision is None:
        return None
    if decision.decision == "stop":
        return "stopped"
    if decision.decision == "accept":
        if callback.worker_assessment.outcome in ("execution-failure", "protocol-failure"):
            return "worker-failure"
        if _scope(callback):
            return "scope-violation"
        return "accepted" if callback.verification.passed else "verification-failure"
    if budget is None:
        return None
    _contradiction(callback.attempt - 1 < budget)
    return "remediation-exhausted"


def _transition(original: TaskPacket, previous: CallbackRecord, current: CallbackRecord,
                history: Sequence[CallbackRecord]) -> None:
    decision = previous.review_decision
    _contradiction(decision is None or decision.decision not in ("remediate", "revise-scope"))
    revision = decision.decision == "revise-scope"
    count = sum(c.review_decision is not None and
                c.review_decision.decision == decision.decision for c in history)
    expected = TaskPacket(
        task_id=f"{original.task_id}-{'scope-revision' if revision else 'remediation'}-{count}",
        goal=(f"Original goal:\n{original.goal}\n\n"
              + (f"Scope revision rationale:\n{decision.rationale}" if revision else
                 f"Remediation required:\n{decision.remediation}")),
        constraints=list(original.constraints), acceptance=list(original.acceptance),
        suggested_files=list(original.suggested_files),
        allowed_files=list(decision.revised_allowed_files) if revision else
                      previous.task.allowed_files)
    _contradiction(canonical_digest(current.task) != canonical_digest(expected))


def _expected_events(c: CallbackRecord, *, continues: bool) -> list[dict]:
    """Calculate expectations for consistency only; never publish invented audits."""
    events = []
    completion = c.worker_result.completion
    if completion is not None and c.per_attempt_evidence is not None:
        claimed = sorted(set(completion.changed_files))
        observed = sorted(set(c.per_attempt_evidence.changed_files))
        if claimed != observed:
            events.append(dict(kind="worker-git-disagreement", attempt=c.attempt,
                worker_claimed_files=claimed, git_observed_files=observed,
                claimed_only_files=sorted(set(claimed)-set(observed)),
                observed_only_files=sorted(set(observed)-set(claimed))))
    outside = _scope(c)
    if outside:
        events.append(dict(kind="scope-violation", attempt=c.attempt,
            allowed_files=list(c.task.allowed_files), out_of_scope_files=outside))
    for check in c.verification.checks:
        if check.expectation_met is False:
            events.append(dict(kind="verification-expectation-mismatch", attempt=c.attempt,
                check_id=check.id, expectation=check.expectation, observed_passed=check.passed,
                expectation_reason=check.expectation_reason))
    decision = c.review_decision
    if decision is not None and decision.decision == "accept" and (
            c.worker_assessment.outcome in ("blocked", "failed")):
        events.append(dict(kind="worker-reviewer-disagreement", attempt=c.attempt,
            worker_outcome=c.worker_assessment.outcome, reviewer_decision="accept",
            rationale=decision.rationale))
    if continues and decision is not None and decision.decision == "revise-scope":
        events.append(dict(kind="scope-revision", attempt=c.attempt,
            previous_allowed_files=c.task.allowed_files,
            revised_allowed_files=decision.revised_allowed_files, rationale=decision.rationale))
    return events


def _safe_label(value: str) -> str:
    # No arbitrary labels/paths are shared: stable, bounded pseudonyms avoid
    # guessing which unusual strings happen to contain secrets.
    if not value or len(value) > 256:
        raise HandoffError("evidence_bounds")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_paths(values: list[str]) -> list[str]:
    if len(values) > 64 or any(len(s) > 256 for s in values):
        raise HandoffError("evidence_bounds")
    return [_safe_label(s) for s in values]


def build_handoff(record: RecordedRun) -> HandoffSummary:
    try:
        # Check the recorded fields BEFORE reused model validators can derive
        # defaults (notably advisory expectation_met). Never normalize away a
        # contradiction in the caller's evidence.
        for callback in record.callbacks:
            _check_provenance(callback, record.verification_commands)
        # Validate even model_copy(update=...) or mutated caller inputs.
        record = RecordedRun.model_validate(record.model_dump(mode="python"), strict=True)
        _contradiction((record.result is None) == (record.exception is None))
        nums = [c.attempt for c in record.callbacks]
        _contradiction(nums != sorted(set(nums)))
        missing = []
        optional = []
        if record.baseline is None:
            missing.append("baseline")
        if record.controller_max_remediation_attempts is None:
            missing.append("controller_budget")
        if record.result is not None:
            if record.result.attempts > 32 or len(record.result.audit_events) > 64:
                raise HandoffError("evidence_bounds")
            _contradiction(any(n > record.result.attempts for n in nums))
            missing.extend(f"callback:{n}" for n in range(1, record.result.attempts+1)
                           if n not in nums)
            _contradiction(record.controller_max_remediation_attempts is not None and
                           record.result.attempts > record.controller_max_remediation_attempts + 1)
        else:
            missing.append("controller_return_unavailable")
        summaries = []
        previous = None
        for c in record.callbacks:
            _contradiction(canonical_digest(c.worker_assessment) !=
                           canonical_digest(classify_worker_run(c.worker_result)))
            if c.attempt == 1:
                _contradiction(canonical_digest(c.task) != canonical_digest(record.original_task))
            elif nums[:nums.index(c.attempt)+1] == list(range(1,c.attempt+1)):
                _transition(record.original_task, previous, c, record.callbacks[:nums.index(c.attempt)])
            else:
                missing.append(f"task_lineage:{c.attempt}")
            if record.baseline is not None:
                _contradiction(c.evidence.baseline_commit != record.baseline.baseline_commit)
            _check_provenance(c, record.verification_commands)
            if record.result is not None:
                _contradiction(c.reviewer_exception is not None)
                if c.attempt < record.result.attempts:
                    _contradiction(c.review_decision.decision in ("accept", "stop"))
                else:
                    status = _expected_status(c, record.controller_max_remediation_attempts)
                    _contradiction(status is not None and status != record.result.status)
            completion = c.worker_result.completion
            process = c.worker_result.process
            attempt = c.per_attempt_evidence
            if attempt is None:
                optional.append(f"per_attempt_evidence:{c.attempt}")
            elif not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", attempt.before_tree) or not (
                    re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", attempt.after_tree)):
                raise HandoffError("contradictory_provenance")
            if attempt is not None and c.attempt == 1 and record.baseline is not None:
                _contradiction(attempt.before_tree != record.baseline.baseline_tree)
            summaries.append(CallbackSummary(
                attempt=c.attempt, task_sha256=canonical_digest(c.task), source_sha256=canonical_digest(c),
                worker_exit_code=process.exit_code, worker_timed_out=process.timed_out,
                worker_elapsed_seconds=process.elapsed_seconds, worker_outcome=c.worker_assessment.outcome,
                worker_claims=WorkerClaims(completion_status=completion.status if completion else None,
                    completion_sha256=canonical_digest(completion) if completion else None,
                    changed_files=_safe_paths(completion.changed_files) if completion else []),
                independent_observations=IndependentObservations(
                    baseline_commit=c.evidence.baseline_commit, final_head=c.evidence.final_head,
                    changed_files=_safe_paths(c.evidence.changed_files), commits=_commit_hashes(c.evidence.commits),
                    file_scope="unspecified" if c.task.allowed_files is None else
                               "out_of_scope" if _scope(c) else "within_scope",
                    out_of_scope_files=_safe_paths(_scope(c)), git_evidence_sha256=canonical_digest(c.evidence),
                    per_attempt=AttemptObservation(before_tree=attempt.before_tree,
                        after_tree=attempt.after_tree, changed_files=_safe_paths(attempt.changed_files),
                        sha256=canonical_digest(attempt)) if attempt else None,
                    inter_attempt_state_change=(previous.per_attempt_evidence.after_tree != attempt.before_tree)
                        if attempt and previous and previous.per_attempt_evidence and
                           previous.attempt+1 == c.attempt else None),
                verification_passed=c.verification.passed, verification_sha256=canonical_digest(c.verification),
                checks=[CheckSummary(id=_safe_label(check.id), specification_sha256=canonical_digest(command),
                    expectation=check.expectation, passed=check.passed, expectation_met=check.expectation_met,
                    exit_code=check.exit_code, timed_out=check.timed_out,
                    elapsed_seconds=check.elapsed_seconds) for check,command in
                    zip(c.verification.checks,record.verification_commands)],
                reviewer_decision=c.review_decision.decision if c.review_decision else None,
                reviewer_record_sha256=canonical_digest(c.review_decision or c.reviewer_exception)))
            previous = c
        audits = []
        if record.result is not None:
            events = record.result.audit_events
            _contradiction([e.attempt for e in events] != sorted(e.attempt for e in events))
            _contradiction(any(e.attempt > record.result.attempts for e in events))
            by_attempt = {c.attempt:c for c in record.callbacks}
            for c in record.callbacks:
                actual = [e.model_dump(mode="json") for e in events if e.attempt == c.attempt]
                # Internal snapshots are optional. Without one, retain the
                # authentic audit, validate its claims/set algebra, but do not
                # assert independent verification of its Git observations.
                if c.per_attempt_evidence is None:
                    disagreements = [e for e in actual if e["kind"] == "worker-git-disagreement"]
                    _contradiction(len(disagreements)>1)
                    for e in disagreements:
                        completion = c.worker_result.completion
                        _contradiction(completion is None)
                        claims = sorted(set(completion.changed_files))
                        observed = e["git_observed_files"]
                        _contradiction(e["worker_claimed_files"] != claims or
                            observed != sorted(set(observed)) or
                            e["claimed_only_files"] != sorted(set(claims)-set(observed)) or
                            e["observed_only_files"] != sorted(set(observed)-set(claims)) or claims == observed)
                        _contradiction(actual[0] != e)
                    actual = [e for e in actual if e["kind"] != "worker-git-disagreement"]
                expected = _expected_events(c, continues=c.attempt < record.result.attempts)
                _contradiction(actual != expected)
            for event in events:
                c = by_attempt.get(event.attempt)
                verified = c is not None and not (
                    event.kind == "worker-git-disagreement" and c.per_attempt_evidence is None)
                if c is None:
                    missing.append(f"audit_callback:{event.attempt}")
                audits.append(AuditSummary(kind=event.kind, attempt=event.attempt,
                                          sha256=canonical_digest(event), independently_verified=verified))
        termination = (TerminationSummary(kind="returned",status=record.result.status,
            attempts=record.result.attempts) if record.result is not None else
            TerminationSummary(kind="exception",exception_stage=record.exception.stage,
                               exception_sha256=canonical_digest(record.exception)))
        return _wire(HandoffSummary, HandoffSummary(
            run_id=_safe_label(record.run_id), original_task_sha256=canonical_digest(record.original_task),
            verification_sha256=canonical_digest([c.model_dump(mode="json") for c in record.verification_commands]),
            baseline=record.baseline, baseline_source=record.baseline_source,
            controller_baseline_confirmed=record.baseline is not None and bool(record.callbacks),
            controller_max_remediation_attempts=record.controller_max_remediation_attempts,
            termination=termination, controller_result_sha256=canonical_digest(record.result)
                if record.result is not None else None,
            callbacks=summaries, audits=audits, provenance_complete=not missing,
            missing_evidence=list(dict.fromkeys(missing)), optional_missing_evidence=optional))
    except HandoffError:
        raise
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise HandoffError("contradictory_provenance") from None


def _commit_hashes(records: list[str]) -> list[str]:
    """Project Git's SHA-tab-subject records without exporting subject text."""
    hashes = []
    for record in records:
        commit, separator, subject = record.partition("\t")
        if not separator or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) or (
                "\r" in subject or "\n" in subject):
            raise HandoffError("invalid_commit_record")
        hashes.append(commit)
    return hashes


def write_handoff(path: Path, summary: HandoffSummary) -> None:
    summary = _wire(HandoffSummary, summary.model_dump(mode="json"))
    _write_output(path, summary)


def _write_output(path: Path, value: Document | dict) -> None:
    raw = canonical_json(value)
    if len(raw) > WIRE_LIMIT:
        raise HandoffError("document_too_large")
    temporary = None
    try:
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", dir=path.parent, prefix=".harness-output-", delete=False) as stream:
                temporary = Path(stream.name)
                if stream.write(raw) != len(raw):
                    raise OSError("incomplete_write")
                stream.flush()
                os.fsync(stream.fileno())
            # A same-filesystem hard link publishes complete bytes atomically
            # and fails if the destination exists; never fall back to overwrite.
            os.link(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except FileExistsError:
        raise HandoffError("output_exists") from None
    except OSError:
        raise HandoffError("output_unavailable") from None


class ValidationFeedback(Document):
    status: Literal["invalid", "needs_operator_approval", "approval_validated",
                    "nonexecuting_recommendation"]
    errors: list[Label] = Field(default_factory=list, max_length=64)
    execution_preflight_required: bool = True
    no_execution_performed: Literal[True] = True


class PreflightResult(Document):
    passed: bool
    errors: list[Label] = Field(default_factory=list, max_length=64)
    proposal_sha256: Digest | None = None
    task_sha256: Digest | None = None
    verification_sha256: Digest | None = None
    execution_config_sha256: Digest | None = None
    repository: RepositoryIdentity | None = None
    no_execution_performed: Literal[True] = True


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def load_operator_approval(path: Path, *, trusted_dir: Path, target_repo: Path,
                           untrusted_workspace_roots: Sequence[Path]) -> OperatorApproval:
    try:
        trusted = trusted_dir.resolve(strict=True)
        actual = path.resolve(strict=True)
        roots = [target_repo.resolve(strict=True)] + [
            root.resolve() for root in untrusted_workspace_roots]
        if not trusted.is_dir() or not actual.is_file() or not _within(actual, trusted):
            raise HandoffError("untrusted_approval_location")
        if any(_within(trusted, root) or _within(root, trusted) or _within(actual, root)
               for root in roots):
            raise HandoffError("untrusted_approval_location")
    except (OSError, RuntimeError):
        raise HandoffError("untrusted_approval_location") from None
    return load_document(actual, OperatorApproval)


def _verification_digest(commands: Sequence[VerificationCommand]) -> str:
    return canonical_digest([c.model_dump(mode="json") for c in commands])


def validate_proposal(proposal: SupervisorProposal, *, handoff: HandoffSummary | None,
                      approval: OperatorApproval | None) -> ValidationFeedback:
    """Pure document checks, without commands or human authentication."""
    try:
        proposal = _wire(SupervisorProposal, proposal.model_dump(mode="json"))
        if handoff is not None:
            handoff = _wire(HandoffSummary, handoff.model_dump(mode="json"))
            if (proposal.parent_handoff_sha256 != canonical_digest(handoff)
                    or proposal.parent_task_sha256 != handoff.original_task_sha256
                    or handoff.baseline is None
                    or proposal.parent_baseline_sha256 != canonical_digest(handoff.baseline)):
                return ValidationFeedback(status="invalid", errors=["parent_binding_mismatch"])
        elif proposal.parent_handoff_sha256 is not None:
            return ValidationFeedback(status="invalid", errors=["parent_handoff_required"])
        if proposal.action != "task":
            if handoff is None:
                return ValidationFeedback(status="invalid", errors=["parent_handoff_required"])
            if proposal.action == "review" and proposal.review_decision.decision == "accept":
                if (handoff.termination.kind != "returned" or handoff.termination.status != "accepted"
                        or not handoff.provenance_complete):
                    return ValidationFeedback(status="invalid", errors=["unsupported_acceptance"])
            return ValidationFeedback(status="nonexecuting_recommendation")
        if approval is None:
            return ValidationFeedback(status="needs_operator_approval",
                                      errors=["operator_approval_required"])
        approval = _wire(OperatorApproval, approval.model_dump(mode="json"))
        spec = proposal.task_spec
        if (approval.proposal_sha256 != canonical_digest(proposal)
                or approval.repository != proposal.repository
                or approval.task_sha256 != canonical_digest(spec.task)
                or approval.allowed_files != spec.task.allowed_files
                or approval.verification_sha256 != _verification_digest(spec.verification_commands)
                or approval.execution_config_sha256 != canonical_digest(spec.execution_config)):
            return ValidationFeedback(status="invalid", errors=["approval_binding_mismatch"])
        return ValidationFeedback(status="approval_validated")
    except (HandoffError, ValueError, TypeError, AttributeError):
        return ValidationFeedback(status="invalid", errors=["invalid_document"])


def materialize_task_file(proposal: SupervisorProposal, *, handoff: HandoffSummary | None,
                          approval: OperatorApproval | None) -> dict[str, object]:
    feedback = validate_proposal(proposal, handoff=handoff, approval=approval)
    if proposal.action != "task" or feedback.status != "approval_validated":
        raise HandoffError("task_not_approved")
    return {"task": proposal.task_spec.task.model_dump(mode="json"),
            "verification_commands": [c.model_dump(mode="json")
                                      for c in proposal.task_spec.verification_commands]}


_GIT_QUERIES = {
    ("rev-parse", "--show-toplevel"),
    ("rev-parse", "--path-format=absolute", "--git-common-dir"),
    ("rev-parse", "--path-format=absolute", "--git-dir"),
    ("rev-parse", "--verify", "HEAD"),
    ("rev-parse", "--verify", "HEAD^{tree}"),
    ("status", "--porcelain=v1", "--untracked-files=all"),
}


def _inspect_git(repo: Path, query: tuple[str, ...]) -> str:
    if query not in _GIT_QUERIES:
        raise HandoffError("unsupported_git_query")
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_CONFIG_PARAMETERS"):
        env.pop(key, None)
    # These restrictions propagate to Git's own submodule inspection.
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="core.fsmonitor", GIT_CONFIG_VALUE_0="false")
    try:
        process = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *query],
            cwd=repo, env=env, capture_output=True, check=True, timeout=10,
            text=True, encoding="utf-8", errors="strict")
        return process.stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise HandoffError("git_inspection_failed") from None


def _repository_state(repo_dir: Path) -> tuple[RepositoryIdentity, Path, Path, Path]:
    try:
        repo = repo_dir.resolve(strict=True)
        root = Path(_inspect_git(repo, ("rev-parse", "--show-toplevel"))).resolve(strict=True)
        if root != repo:
            raise HandoffError("not_worktree_root")
        common = Path(_inspect_git(repo, ("rev-parse", "--path-format=absolute",
                                        "--git-common-dir"))).resolve(strict=True)
        admin = Path(_inspect_git(repo, ("rev-parse", "--path-format=absolute",
                                       "--git-dir"))).resolve(strict=True)
        commit = _inspect_git(repo, ("rev-parse", "--verify", "HEAD"))
        tree = _inspect_git(repo, ("rev-parse", "--verify", "HEAD^{tree}"))
        if _inspect_git(repo, ("status", "--porcelain=v1", "--untracked-files=all")):
            raise HandoffError("dirty_target")
        identity = RepositoryIdentity(repository_sha256=canonical_digest({
            "schema_version": 1, "worktree": os.path.normcase(str(root)),
            "git_common_directory": os.path.normcase(str(common))}),
            baseline_commit=commit, baseline_tree=tree)
        return identity, root, common, admin
    except HandoffError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise HandoffError("repository_unavailable") from None


def inspect_repository(repo_dir: Path) -> RepositoryIdentity:
    """Observe a clean baseline; repository identity binds this worktree."""
    return _repository_state(repo_dir)[0]


def execution_preflight(proposal: SupervisorProposal, *, handoff: HandoffSummary | None,
                        approval_path: Path, trusted_approval_dir: Path,
                        untrusted_workspace_roots: Sequence[Path], repo_dir: Path,
                        task: TaskPacket, verification_commands: Sequence[VerificationCommand],
                        execution_config: ExecutionConfig) -> PreflightResult:
    """Point-in-time trusted-caller check, NOT an execution lock or capability."""
    try:
        approval = load_operator_approval(approval_path, trusted_dir=trusted_approval_dir,
            target_repo=repo_dir, untrusted_workspace_roots=untrusted_workspace_roots)
        feedback = validate_proposal(proposal, handoff=handoff, approval=approval)
        if feedback.status != "approval_validated":
            return PreflightResult(passed=False, errors=feedback.errors or ["task_not_approved"])
        actual = _wire(TaskSpecification, dict(task=task,
            verification_commands=list(verification_commands), execution_config=execution_config))
        if (canonical_digest(actual.task) != approval.task_sha256
                or actual.task.allowed_files != approval.allowed_files
                or _verification_digest(actual.verification_commands) != approval.verification_sha256
                or canonical_digest(actual.execution_config) != approval.execution_config_sha256):
            return PreflightResult(passed=False, errors=["actual_execution_binding_mismatch"])
        identity, root, common, admin = _repository_state(repo_dir)
        if identity != approval.repository or identity != proposal.repository:
            return PreflightResult(passed=False, errors=["repository_baseline_mismatch"])
        for name in actual.task.allowed_files + actual.task.suggested_files:
            resolved = (root / portable_path(name)).resolve()
            if (not _within(resolved, root) or _within(resolved, common)
                    or _within(resolved, admin) or _within(resolved, root / ".git")):
                return PreflightResult(passed=False, errors=["scope_resolution_escape"])
        return PreflightResult(passed=True, proposal_sha256=canonical_digest(proposal),
            task_sha256=canonical_digest(actual.task),
            verification_sha256=_verification_digest(actual.verification_commands),
            execution_config_sha256=canonical_digest(actual.execution_config), repository=identity)
    except HandoffError as error:
        return PreflightResult(passed=False, errors=[str(error)])
    except (OSError, ValueError, TypeError, RuntimeError):
        return PreflightResult(passed=False, errors=["invalid_preflight_input"])
