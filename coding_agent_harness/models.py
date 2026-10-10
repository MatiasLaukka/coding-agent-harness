from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )


class TaskPacket(StrictModel):
    task_id: str = Field(
        min_length=1,
    )
    goal: str = Field(
        min_length=1,
        max_length=4000,
    )
    constraints: list[str] = Field(
        default_factory=list,
    )
    acceptance: list[str] = Field(
        min_length=1,
    )
    suggested_files: list[str] = Field(
        default_factory=list,
    )
    allowed_files: list[str] | None = None


class VerificationCommand(StrictModel):
    id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=120, gt=0)

    expectation: Literal[
        "must_pass",
        "expected_failure",
        "advisory",
    ] = "must_pass"

    expectation_reason: str | None = None

    @model_validator(mode="after")
    def require_expected_failure_reason(
        self,
    ) -> "VerificationCommand":
        if (
            self.expectation == "expected_failure"
            and not self.expectation_reason
        ):
            raise ValueError(
                "expectation_reason is required "
                "when expectation is expected_failure"
            )

        return self


class WorkerResult(StrictModel):
    status: Literal[
        "completed",
        "blocked",
        "failed",
    ]
    summary: str = Field(
        min_length=1,
    )
    changed_files: list[str] = Field(
        default_factory=list,
    )
    remaining_concerns: list[str] = Field(
        default_factory=list,
    )


class ReviewDecision(StrictModel):
    decision: Literal[
        "accept",
        "remediate",
        "stop",
        "revise-scope",
    ]
    rationale: str = Field(min_length=1)
    remediation: str | None = None
    revised_allowed_files: list[str] | None = None

    @model_validator(mode="after")
    def require_decision_payload(
        self,
    ) -> "ReviewDecision":
        if (
            self.decision == "remediate"
            and not self.remediation
        ):
            raise ValueError(
                "remediation is required "
                "when decision is remediate"
            )

        if (
            self.decision == "revise-scope"
            and not self.revised_allowed_files
        ):
            raise ValueError(
                "revised_allowed_files is required "
                "when decision is revise-scope"
            )

        return self