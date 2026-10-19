import subprocess
from pathlib import Path

import pytest

from coding_agent_harness.git import (
    capture_attempt_evidence,
    capture_baseline,
    capture_evidence,
    capture_worktree_snapshot,
)


def git(
    repo: Path,
    *args: str,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    return result.stdout.strip()


def make_repo(
    tmp_path: Path,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    git(
        repo,
        "init",
    )

    git(
        repo,
        "config",
        "user.name",
        "Harness Test",
    )

    git(
        repo,
        "config",
        "user.email",
        "harness@example.test",
    )

    (
        repo / "existing.txt"
    ).write_text(
        "before\n",
        encoding="utf-8",
    )

    git(
        repo,
        "add",
        "existing.txt",
    )

    git(
        repo,
        "commit",
        "-m",
        "baseline",
    )

    return repo


def test_capture_baseline_requires_clean_repo(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path
    )

    (
        repo / "existing.txt"
    ).write_text(
        "dirty\n",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match="working tree must be clean",
    ):
        capture_baseline(
            repo
        )


def test_capture_baseline_records_head(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path
    )

    baseline = capture_baseline(
        repo
    )

    assert baseline.commit == git(
        repo,
        "rev-parse",
        "HEAD",
    )

    assert baseline.status == ""


def test_capture_evidence_finds_tracked_and_untracked_changes(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path
    )

    baseline = capture_baseline(
        repo
    )

    (
        repo / "existing.txt"
    ).write_text(
        "after\n",
        encoding="utf-8",
    )

    (
        repo / "new.txt"
    ).write_text(
        "new file\n",
        encoding="utf-8",
    )

    evidence = capture_evidence(
        repo=repo,
        baseline=baseline,
    )

    assert set(
        evidence.changed_files
    ) == {
        "existing.txt",
        "new.txt",
    }

    assert evidence.final_head == (
        baseline.commit
    )

    assert "existing.txt" in (
        evidence.patch
    )

    assert "new.txt" in (
        evidence.patch
    )

    assert "after" in (
        evidence.patch
    )

    assert "new file" in (
        evidence.patch
    )


def test_capture_evidence_includes_agent_commits(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path
    )

    baseline = capture_baseline(
        repo
    )

    (
        repo / "existing.txt"
    ).write_text(
        "committed change\n",
        encoding="utf-8",
    )

    git(
        repo,
        "add",
        "existing.txt",
    )

    git(
        repo,
        "commit",
        "-m",
        "agent commit",
    )

    evidence = capture_evidence(
        repo=repo,
        baseline=baseline,
    )

    assert evidence.final_head != (
        baseline.commit
    )

    assert len(
        evidence.commits
    ) == 1

    assert "agent commit" in (
        evidence.commits[0]
    )

    assert evidence.changed_files == [
        "existing.txt",
    ]

def test_attempt_evidence_detects_reedit_of_already_dirty_file(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path
    )

    path = repo / "existing.txt"

    # Simulate a change left by attempt 1.
    path.write_text(
        "attempt one\n",
        encoding="utf-8",
    )

    before = capture_worktree_snapshot(
        repo
    )

    # Attempt 2 edits the same already-dirty file.
    path.write_text(
        "attempt two\n",
        encoding="utf-8",
    )

    evidence = capture_attempt_evidence(
        repo=repo,
        before=before,
    )

    assert evidence.before_tree == before.tree
    assert evidence.after_tree != before.tree
    assert evidence.changed_files == [
        "existing.txt",
    ]

def test_index_fingerprint_detects_staged_blob_and_mode_content_changes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    baseline = capture_baseline(repo)
    staged_content = tmp_path / "staged-content.txt"

    staged_content.write_text("staged version one\n", encoding="utf-8")
    first_blob = git(repo, "hash-object", "-w", str(staged_content))
    git(repo, "update-index", "--cacheinfo", f"100644,{first_blob},existing.txt")
    first = capture_evidence(repo=repo, baseline=baseline)

    staged_content.write_text("staged version two\n", encoding="utf-8")
    second_blob = git(repo, "hash-object", "-w", str(staged_content))
    git(repo, "update-index", "--cacheinfo", f"100644,{second_blob},existing.txt")
    second = capture_evidence(repo=repo, baseline=baseline)

    git(repo, "update-index", "--cacheinfo", f"100755,{second_blob},existing.txt")
    third = capture_evidence(repo=repo, baseline=baseline)

    assert first.final_head == second.final_head == third.final_head == baseline.commit
    assert first.final_status == second.final_status == third.final_status != ""
    assert first.patch == second.patch
    assert second.patch != third.patch
    assert first.index_fingerprint != second.index_fingerprint
    assert second.index_fingerprint != third.index_fingerprint


def test_index_fingerprint_is_independent_of_index_file_timestamps(tmp_path: Path) -> None:
    import os

    repo = make_repo(tmp_path)
    baseline = capture_baseline(repo)
    first = capture_evidence(repo=repo, baseline=baseline)
    index_path = repo / git(repo, "rev-parse", "--git-path", "index")
    stat = index_path.stat()
    os.utime(index_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = capture_evidence(repo=repo, baseline=baseline)

    assert first.index_fingerprint == second.index_fingerprint

@pytest.mark.parametrize("failure", ["truncated", "missing-raw-bytes"])
def test_index_fingerprint_fails_closed_on_incomplete_raw_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import coding_agent_harness.git as git_module
    import coding_agent_harness.process as process_module
    from coding_agent_harness.git import GitCollectionError
    from coding_agent_harness.process import ProcessResult

    repo = make_repo(tmp_path)
    baseline = capture_baseline(repo)
    original = process_module.run_process

    def observe(**kwargs):
        if kwargs["argv"][1:] == ["ls-files", "--stage", "-z"]:
            return ProcessResult(
                argv=kwargs["argv"], exit_code=0, stdout="", stderr="",
                timed_out=False, elapsed_seconds=0.01,
                stdout_truncated=failure == "truncated",
                raw_stdout_bytes=(b"incomplete" if failure == "truncated" else None),
            )
        return original(**kwargs)

    monkeypatch.setattr(git_module, "run_process", observe)
    with pytest.raises(GitCollectionError) as caught:
        capture_evidence(repo=repo, baseline=baseline)

    assert caught.value.git_failure.stage == "cumulative-index-entries"
    assert caught.value.git_failure.reason in {"output-budget-exceeded", "incomplete-collection"}
