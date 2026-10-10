import sys
from pathlib import Path

from coding_agent_harness.models import (
    VerificationCommand,
)
from coding_agent_harness.verifier import (
    run_verification,
)


def test_verification_records_passing_command(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="pass",
        argv=[
            sys.executable,
            "-c",
            "print('all good')",
        ],
        timeout_seconds=10,
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    assert result.passed is True
    assert len(result.checks) == 1

    check = result.checks[0]

    assert check.id == "pass"
    assert check.passed is True
    assert check.exit_code == 0
    assert check.timed_out is False
    assert "all good" in check.stdout


def test_verification_records_failure_without_stopping(
    tmp_path: Path,
) -> None:
    commands = [
        VerificationCommand(
            id="fail",
            argv=[
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "print('broken', file=sys.stderr); "
                    "raise SystemExit(3)"
                ),
            ],
            timeout_seconds=10,
        ),
        VerificationCommand(
            id="after",
            argv=[
                sys.executable,
                "-c",
                "print('still ran')",
            ],
            timeout_seconds=10,
        ),
    ]

    result = run_verification(
        repo_dir=tmp_path,
        commands=commands,
    )

    assert result.passed is False
    assert len(result.checks) == 2

    assert result.checks[0].passed is False
    assert result.checks[0].exit_code == 3
    assert "broken" in result.checks[0].stderr

    assert result.checks[1].passed is True
    assert "still ran" in result.checks[1].stdout


def test_verification_records_timeout(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="timeout",
        argv=[
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        timeout_seconds=1,
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    assert result.passed is False
    assert result.checks[0].passed is False
    assert result.checks[0].timed_out is True


def test_verification_runs_from_repository_directory(
    tmp_path: Path,
) -> None:
    (
        tmp_path / "marker.txt"
    ).write_text(
        "repository marker\n",
        encoding="utf-8",
    )

    command = VerificationCommand(
        id="cwd",
        argv=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "print(Path('marker.txt').read_text())"
            ),
        ],
        timeout_seconds=10,
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    assert result.passed is True
    assert "repository marker" in (
        result.checks[0].stdout
    )

def test_must_pass_success_meets_expectation(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="success",
        argv=[
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
        expectation="must_pass",
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    check = result.checks[0]

    assert check.passed is True
    assert check.expectation == "must_pass"
    assert check.expectation_met is True
    assert result.passed is True


def test_must_pass_failure_breaks_verification(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="failure",
        argv=[
            sys.executable,
            "-c",
            "raise SystemExit(1)",
        ],
        expectation="must_pass",
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    check = result.checks[0]

    assert check.passed is False
    assert check.expectation == "must_pass"
    assert check.expectation_met is False
    assert result.passed is False


def test_expected_failure_can_fail_without_breaking_verification(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="expected-red",
        argv=[
            sys.executable,
            "-c",
            "raise SystemExit(1)",
        ],
        expectation="expected_failure",
        expectation_reason=(
            "This slice intentionally leaves "
            "the caller migration incomplete."
        ),
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    check = result.checks[0]

    assert check.passed is False
    assert check.expectation == "expected_failure"
    assert check.expectation_met is True
    assert result.passed is True


def test_unexpected_expected_failure_pass_is_recorded(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="unexpected-green",
        argv=[
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ],
        expectation="expected_failure",
        expectation_reason=(
            "This was expected to remain red "
            "until the next slice."
        ),
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    check = result.checks[0]

    assert check.passed is True
    assert check.expectation == "expected_failure"
    assert check.expectation_met is False

    # A surprising pass is not a hard verification failure.
    assert result.passed is True


def test_advisory_failure_does_not_break_verification(
    tmp_path: Path,
) -> None:
    command = VerificationCommand(
        id="advisory",
        argv=[
            sys.executable,
            "-c",
            "raise SystemExit(1)",
        ],
        expectation="advisory",
    )

    result = run_verification(
        repo_dir=tmp_path,
        commands=[command],
    )

    check = result.checks[0]

    assert check.passed is False
    assert check.expectation == "advisory"
    assert check.expectation_met is None
    assert result.passed is True