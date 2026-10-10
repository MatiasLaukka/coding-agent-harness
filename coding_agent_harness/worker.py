from __future__ import annotations
from typing import Literal

import json
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from coding_agent_harness.models import (
    StrictModel,
    TaskPacket,
    WorkerResult,
)
from coding_agent_harness.process import (
    ProcessResult,
    run_process,
)


COMPLETION_PREFIX = "HARNESS_RESULT:"


class WorkerRunResult(StrictModel):
    process: ProcessResult
    completion: WorkerResult | None
    completion_error: str | None = None

class WorkerRunAssessment(StrictModel):
    outcome: Literal[
        "execution-failure",
        "protocol-failure",
        "completed",
        "blocked",
        "failed",
    ]
    reason: str


def classify_worker_run(
    run: WorkerRunResult,
) -> WorkerRunAssessment:
    if run.process.timed_out:
        return WorkerRunAssessment(
            outcome="execution-failure",
            reason="worker process timed out",
        )

    if run.process.exit_code is None:
        return WorkerRunAssessment(
            outcome="execution-failure",
            reason=(
                "worker process ended without "
                "an exit code"
            ),
        )

    if run.process.exit_code != 0:
        return WorkerRunAssessment(
            outcome="execution-failure",
            reason=(
                "worker process exited with "
                f"code {run.process.exit_code}"
            ),
        )

    if run.completion is None:
        return WorkerRunAssessment(
            outcome="protocol-failure",
            reason=(
                run.completion_error
                or "missing HARNESS_RESULT"
            ),
        )

    return WorkerRunAssessment(
        outcome=run.completion.status,
        reason=run.completion.summary,
    )


def _render_task_prompt(
    task: TaskPacket,
) -> str:
    constraints = (
        "\n".join(
            f"- {item}"
            for item in task.constraints
        )
        if task.constraints
        else "- None specified."
    )

    acceptance = "\n".join(
        f"- {item}"
        for item in task.acceptance
    )

    suggested_files = (
        "\n".join(
            f"- {item}"
            for item in task.suggested_files
        )
        if task.suggested_files
        else "- None specified."
    )

    if task.allowed_files is None:
        allowed_files = (
            "- No machine-enforced file restriction."
        )
        allowed_files_rule = ""
    elif task.allowed_files:
        allowed_files = "\n".join(
            f"- {item}"
            for item in task.allowed_files
        )
        allowed_files_rule = (
            "\nDo not modify any file not listed "
            "under Allowed files."
        )
    else:
        allowed_files = "- None."
        allowed_files_rule = (
            "\nDo not modify any files."
        )

    return f"""You are the implementation worker for one bounded coding task.

Task ID:
{task.task_id}

Goal:
{task.goal}

Constraints:
{constraints}

Acceptance criteria:
{acceptance}

Suggested files:
{suggested_files}

Allowed files:
{allowed_files}
{allowed_files_rule}

Work directly in the current repository.

Inspect the repository as needed, implement only the requested task, and do not
make unrelated changes.

Run whatever focused checks are useful while implementing, but do not claim that
the harness's independent verification has passed. The harness will run its own
verification after you exit.

When you have finished, print exactly one final machine-readable completion line
using this prefix:

{COMPLETION_PREFIX}

The text after the prefix must be one JSON object with this shape:

{{
  "status": "completed" | "blocked" | "failed",
  "summary": "brief factual summary",
  "changed_files": ["relative/path"],
  "remaining_concerns": ["concern"]
}}

changed_files must contain repository-relative paths modified during this worker invocation only.

Do not put Markdown fences around the completion JSON.
"""


def _parse_completion(
    stdout: str,
) -> tuple[
    WorkerResult | None,
    str | None,
]:
    records = [
        line[len(COMPLETION_PREFIX):].strip()
        for line in stdout.splitlines()
        if line.startswith(
            COMPLETION_PREFIX
        )
    ]

    if not records:
        return (
            None,
            (
                "missing HARNESS_RESULT "
                "completion record"
            ),
        )

    raw = records[-1]

    try:
        payload = json.loads(
            raw
        )
    except json.JSONDecodeError as exc:
        return (
            None,
            (
                "invalid HARNESS_RESULT JSON: "
                f"{exc.msg}"
            ),
        )

    try:
        completion = (
            WorkerResult.model_validate(
                payload
            )
        )
    except ValidationError as exc:
        return (
            None,
            (
                "invalid HARNESS_RESULT schema: "
                f"{exc}"
            ),
        )

    return completion, None


class QwenWorker:
    def __init__(
        self,
        *,
        command: Sequence[str],
        model: str,
        timeout_seconds: float,
    ) -> None:
        if not command:
            raise ValueError(
                "command must not be empty"
            )

        if not model:
            raise ValueError(
                "model must not be empty"
            )

        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be > 0"
            )

        self.command = list(
            command
        )
        self.model = model
        self.timeout_seconds = (
            timeout_seconds
        )

    def run(
        self,
        *,
        task: TaskPacket,
        repo_dir: Path,
    ) -> WorkerRunResult:
        prompt = _render_task_prompt(
            task
        )

        argv = [
            *self.command,
            "--model",
            self.model,
            "-p",
            prompt,
        ]

        process = run_process(
            argv=argv,
            cwd=repo_dir,
            timeout_seconds=(
                self.timeout_seconds
            ),
        )

        completion, completion_error = (
            _parse_completion(
                process.stdout
            )
        )

        return WorkerRunResult(
            process=process,
            completion=completion,
            completion_error=(
                completion_error
            ),
        )