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
