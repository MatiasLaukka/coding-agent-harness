from __future__ import annotations
from pydantic import model_validator

from pathlib import Path
from typing import Literal, Sequence

from coding_agent_harness.models import (
    StrictModel,
    VerificationCommand,
)
from coding_agent_harness.process import run_process


class VerificationCheck(StrictModel):
    id: str
    argv: list[str]

    passed: bool

    expectation: Literal[
        "must_pass",
        "expected_failure",
        "advisory",
    ] = "must_pass"

    expectation_reason: str | None = None
    expectation_met: bool | None = None

    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    elapsed_seconds: float

    @model_validator(mode="after")
    def derive_expectation_met(
        self,
    ) -> "VerificationCheck":
        if self.expectation == "advisory":
            self.expectation_met = None
            return self

        if self.expectation_met is not None:
            return self

        if self.expectation == "must_pass":
            self.expectation_met = self.passed
            return self

        self.expectation_met = (
            not self.timed_out
            and self.exit_code is not None
            and self.exit_code != 0
        )

        return self


class VerificationResult(StrictModel):
    passed: bool
    checks: list[VerificationCheck]

def _expectation_met(
    command: VerificationCommand,
    *,
    passed: bool,
    timed_out: bool,
    exit_code: int | None,
) -> bool | None:
    if command.expectation == "advisory":
        return None

    if command.expectation == "must_pass":
        return passed

    # expected_failure means an actual command failure was
    # expected, not an infrastructure timeout.
    return (
        not timed_out
        and exit_code is not None
        and exit_code != 0
    )


def _check_allows_overall_success(
    command: VerificationCommand,
    *,
    passed: bool,
    timed_out: bool,
) -> bool:
    if command.expectation == "must_pass":
        return passed

    if command.expectation == "advisory":
        return True

    # An expected failure or an unexpected pass are both
    # non-fatal to the implementation. A timeout is different:
    # we did not obtain a meaningful verification result.
    return not timed_out


def run_verification(
    *,
    repo_dir: Path,
    commands: Sequence[VerificationCommand],
) -> VerificationResult:
    checks: list[VerificationCheck] = []
    overall_passed = True

    for command in commands:
        process = run_process(
            argv=command.argv,
            cwd=repo_dir,
            timeout_seconds=command.timeout_seconds,
        )

        passed = (
            not process.timed_out
            and process.exit_code == 0
        )

        expectation_met = _expectation_met(
            command,
            passed=passed,
            timed_out=process.timed_out,
            exit_code=process.exit_code,
        )

        check = VerificationCheck(
            id=command.id,
            argv=list(command.argv),
            passed=passed,
            expectation=command.expectation,
            expectation_reason=command.expectation_reason,
            expectation_met=expectation_met,
            exit_code=process.exit_code,
            timed_out=process.timed_out,
            stdout=process.stdout,
            stderr=process.stderr,
            elapsed_seconds=process.elapsed_seconds,
        )

        checks.append(check)

        if not _check_allows_overall_success(
            command,
            passed=passed,
            timed_out=process.timed_out,
        ):
            overall_passed = False

    return VerificationResult(
        passed=overall_passed,
        checks=checks,
    )