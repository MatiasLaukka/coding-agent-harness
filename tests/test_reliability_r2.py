"""Deterministic regressions for R2; no model services or real workers."""
import sys
import hashlib
from types import SimpleNamespace

import pytest

from coding_agent_harness.controller import HarnessController
from coding_agent_harness.git import (
    capture_baseline, capture_evidence, capture_worktree_snapshot,
    capture_attempt_evidence,
)
from coding_agent_harness.models import ReviewDecision, TaskPacket
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.worker import QwenWorker, WorkerRunResult, classify_worker_run
from tests.test_git import make_repo, git


def task():
    return TaskPacket(task_id="r2", goal="Inspect the target", acceptance=["Reviewed"],
                      allowed_files=["renamed.txt"])


class CountingWorker:
    def __init__(self):
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        from coding_agent_harness.models import WorkerResult
        return WorkerRunResult(
            process=ProcessResult(argv=["fake"], exit_code=0, stdout="", stderr="",
                                  timed_out=False, elapsed_seconds=0),
            completion=WorkerResult(status="completed", summary="Done"))


class Reviewer:
    def __init__(self, first):
        self.first = first
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.first
        return ReviewDecision(decision="stop", rationale="Unexpected second call")


@pytest.mark.parametrize("bad", [
    SimpleNamespace(decision="unexpected", rationale="No retry approved", remediation=None),
    ReviewDecision(decision="stop", rationale="Stop").model_copy(update={"decision": "typo"}),
    ReviewDecision(decision="remediate", rationale="Fix", remediation="Repair").model_copy(
        update={"remediation": None}),
    ReviewDecision(decision="revise-scope", rationale="Scope", revised_allowed_files=["a"]).model_copy(
        update={"revised_allowed_files": []}),
    ReviewDecision(decision="accept", rationale="OK").model_copy(update={"rationale": ""}),
    ReviewDecision.model_construct(decision="unknown", rationale="No authorization"),
    ReviewDecision(decision="stop", rationale="Stop").model_copy(update={"rationale": 3}),
])
def test_invalid_review_never_authorizes_another_invocation(tmp_path, bad):
    repo = make_repo(tmp_path)
    worker = CountingWorker()
    controller = HarnessController(worker=worker, reviewer=Reviewer(bad))
    rejected = False
    try:
        controller.run(task=task(), repo_dir=repo, verification_commands=[])
    except ValueError:
        rejected = True
    assert worker.calls == 1
    assert rejected, "malformed decisions must fail closed"


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), 1.5, True, "2", -1])
def test_invalid_budget_rejected_before_execution(budget):
    worker = CountingWorker()
    with pytest.raises(ValueError):
        HarnessController(worker=worker, reviewer=Reviewer(None), max_remediation_attempts=budget)
    assert worker.calls == 0


@pytest.mark.parametrize("mode", ["unstaged", "staged", "committed"])
def test_rename_evidence_contains_deleted_source_and_destination(tmp_path, mode):
    repo = make_repo(tmp_path)
    git(repo, "config", "diff.renames", "true")
    baseline = capture_baseline(repo)
    before = capture_worktree_snapshot(repo)
    (repo / "existing.txt").rename(repo / "renamed.txt")
    if mode != "unstaged":
        git(repo, "add", "-A")
    if mode == "committed":
        git(repo, "commit", "-m", "worker rename")
    evidence = capture_evidence(repo=repo, baseline=baseline)
    attempt = capture_attempt_evidence(repo=repo, before=before)
    assert evidence.changed_files == ["existing.txt", "renamed.txt"]
    assert attempt.changed_files == ["existing.txt", "renamed.txt"]
    assert "existing.txt" in evidence.patch and "renamed.txt" in evidence.patch


def test_rename_of_forbidden_source_cannot_be_accepted(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "config", "diff.renames", "true")

    class RenamingWorker(CountingWorker):
        def run(self, **kwargs):
            (repo / "existing.txt").rename(repo / "renamed.txt")
            git(repo, "add", "-A")
            return super().run(**kwargs)

    worker = RenamingWorker()
    result = HarnessController(worker=worker, reviewer=Reviewer(
        ReviewDecision(decision="accept", rationale="Accept destination"))).run(
            task=task(), repo_dir=repo, verification_commands=[])
    assert result.status == "scope-violation"
    violation = next(e for e in result.audit_events if e.kind == "scope-violation")
    assert violation.out_of_scope_files == ["existing.txt"]
    assert worker.calls == 1


@pytest.mark.parametrize("records", [
    ['{"status":"completed","summary":"done"}', '{"status":"completed","summary":"done"}'],
    ['{"status":"failed","summary":"failed"}', '{"status":"completed","summary":"done"}'],
    ['{not-json}', '{"status":"completed","summary":"done"}'],
    ['{"status":"failed","status":"completed","summary":"done"}'],
    ['{"status":"completed","summary":"first","summary":"second"}'],
])
def test_ambiguous_completion_is_protocol_failure_with_raw_evidence(tmp_path, records):
    output = "\n".join("HARNESS_RESULT: " + r for r in records)
    script = tmp_path / "fake_worker.py"
    script.write_text("print(" + repr(output) + ")\n", encoding="utf-8")
    result = QwenWorker(command=[sys.executable, str(script)], model="offline-fake",
                        timeout_seconds=10).run(task=task(), repo_dir=tmp_path)
    assert result.process.exit_code == 0
    assert result.process.stdout.splitlines() == output.splitlines()
    assert result.completion is None
    assert result.completion_error
    assert classify_worker_run(result).outcome == "protocol-failure"


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_valid_budget_bounds_explicit_remediation(tmp_path, budget):
    repo = make_repo(tmp_path)
    worker = CountingWorker()

    class RemediationReviewer:
        def review(self, **kwargs):
            return ReviewDecision(decision="remediate", rationale="Retry required",
                                  remediation="Inspect again")

    result = HarnessController(worker=worker, reviewer=RemediationReviewer(),
                               max_remediation_attempts=budget).run(
        task=task(), repo_dir=repo, verification_commands=[])
    assert result.status == "remediation-exhausted"
    assert result.attempts == budget + 1
    assert worker.calls == budget + 1


def test_reviewer_exception_does_not_retry(tmp_path):
    repo = make_repo(tmp_path)
    worker = CountingWorker()

    class BrokenReviewer:
        def review(self, **kwargs):
            raise RuntimeError("offline reviewer failure")

    with pytest.raises(RuntimeError, match="offline reviewer failure"):
        HarnessController(worker=worker, reviewer=BrokenReviewer()).run(
            task=task(), repo_dir=repo, verification_commands=[])
    assert worker.calls == 1


def test_single_run_rename_scope_gate(tmp_path):
    from coding_agent_harness.orchestrator import run_task
    repo = make_repo(tmp_path)
    git(repo, "config", "diff.renames", "true")

    class RenamingWorker(CountingWorker):
        def run(self, **kwargs):
            (repo / "existing.txt").rename(repo / "renamed.txt")
            git(repo, "add", "-A")
            return super().run(**kwargs)

    result = run_task(task=task(), repo_dir=repo, worker=RenamingWorker(),
                      verification_commands=[])
    assert result.status == "needs-attention"
    assert result.changed_files == ["existing.txt", "renamed.txt"]
    assert any(f.code == "disallowed-file" and "existing.txt" in f.detail
               for f in result.findings)


def test_rename_scope_evidence_survives_handoff(tmp_path):
    from coding_agent_harness.supervisor import (
        RecordingReviewer, RecordedRun, build_handoff, canonical_digest, inspect_repository,
    )
    repo = make_repo(tmp_path)
    git(repo, "config", "diff.renames", "true")
    baseline = inspect_repository(repo)
    original = task()
    recorder = RecordingReviewer(Reviewer(ReviewDecision(decision="accept", rationale="Accept")))

    class RenamingWorker(CountingWorker):
        def run(self, **kwargs):
            (repo / "existing.txt").rename(repo / "renamed.txt")
            git(repo, "add", "-A")
            return super().run(**kwargs)

    result = HarnessController(worker=RenamingWorker(), reviewer=recorder,
                               max_remediation_attempts=0).run(
        task=original, repo_dir=repo, verification_commands=[])
    summary = build_handoff(RecordedRun(
        run_id="r2-rename", original_task=original, verification_commands=[],
        baseline=baseline, baseline_source="controller_evidence",
        controller_max_remediation_attempts=0, callbacks=recorder.records, result=result))
    assert summary.termination.status == "scope-violation"
    assert recorder.records[0].evidence.changed_files == ["existing.txt", "renamed.txt"]
    observations = summary.callbacks[0].independent_observations
    assert observations.file_scope == "out_of_scope"
    assert observations.out_of_scope_files == ["sha256:" + hashlib.sha256(
        b"existing.txt").hexdigest()]
    assert observations.git_evidence_sha256 == canonical_digest(recorder.records[0].evidence)
