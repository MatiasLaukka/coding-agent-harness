from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from coding_agent_harness.models import StrictModel


class GitBaseline(StrictModel):
    commit: str
    status: str


class GitEvidence(StrictModel):
    baseline_commit: str
    final_head: str
    final_status: str
    changed_files: list[str]
    patch: str
    commits: list[str]

class GitWorktreeSnapshot(StrictModel):
    tree: str


class GitAttemptEvidence(StrictModel):
    before_tree: str
    after_tree: str
    changed_files: list[str]

def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    return result.stdout


def _git_z(
    repo: Path,
    *args: str,
) -> list[str]:
    output = _git(
        repo,
        *args,
    )

    return [
        item
        for item in output.split("\0")
        if item
    ]


def _status(
    repo: Path,
) -> str:
    return _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).rstrip()


def capture_baseline(
    repo: Path,
) -> GitBaseline:
    commit = _git(
        repo,
        "rev-parse",
        "HEAD",
    ).strip()

    status = _status(
        repo
    )

    if status:
        raise RuntimeError(
            "working tree must be clean "
            "before starting a delegated task"
        )

    return GitBaseline(
        commit=commit,
        status=status,
    )


def _capture_patch_with_untracked(
    *,
    repo: Path,
    baseline_commit: str,
    untracked_files: list[str],
) -> str:
    index_path_text = _git(
        repo,
        "rev-parse",
        "--git-path",
        "index",
    ).strip()

    index_path = Path(
        index_path_text
    )

    if not index_path.is_absolute():
        index_path = (
            repo / index_path
        ).resolve()

    fd, temporary_index_text = (
        tempfile.mkstemp(
            prefix="coding-agent-harness-index-",
        )
    )

    os.close(fd)

    temporary_index = Path(
        temporary_index_text
    )

    try:
        if index_path.exists():
            shutil.copyfile(
                index_path,
                temporary_index,
            )
        else:
            temporary_index.write_bytes(
                b""
            )

        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(
            temporary_index
        )

        if untracked_files:
            _git(
                repo,
                "add",
                "-N",
                "--",
                *untracked_files,
                env=env,
            )

        return _git(
            repo,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            baseline_commit,
            "--",
            env=env,
        )

    finally:
        temporary_index.unlink(
            missing_ok=True
        )


def capture_evidence(
    *,
    repo: Path,
    baseline: GitBaseline,
) -> GitEvidence:
    partial = {"baseline_commit": baseline.commit}
    try:
        return _capture_evidence(repo=repo, baseline=baseline, partial=partial)
    except BaseException as exc:
        exc.git_evidence = partial
        raise


def _capture_evidence(*, repo, baseline, partial) -> GitEvidence:
    final_head = _git(
        repo,
        "rev-parse",
        "HEAD",
    ).strip()

    partial["final_head"] = final_head
    final_status = _status(
        repo
    )

    partial["final_status"] = final_status
    tracked_changes = _git_z(
        repo,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        baseline.commit,
        "--",
    )

    partial["tracked_changes"] = tracked_changes
    untracked_files = _git_z(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )

    changed_files = sorted(
        set(
            tracked_changes
            + untracked_files
        )
    )

    partial["changed_files"] = changed_files
    patch = _capture_patch_with_untracked(
        repo=repo,
        baseline_commit=baseline.commit,
        untracked_files=untracked_files,
    )

    partial["patch"] = patch
    commits_output = _git(
        repo,
        "log",
        "--format=%H%x09%s",
        f"{baseline.commit}..{final_head}",
    )

    commits = [
        line
        for line in commits_output.splitlines()
        if line.strip()
    ]

    partial["commits"] = commits
    return GitEvidence(
        baseline_commit=baseline.commit,
        final_head=final_head,
        final_status=final_status,
        changed_files=changed_files,
        patch=patch,
        commits=commits,
    )

def _capture_worktree_tree(
    repo: Path,
) -> str:
    fd, temporary_index_text = tempfile.mkstemp(
        prefix="coding-agent-harness-snapshot-index-",
    )

    os.close(fd)

    temporary_index = Path(
        temporary_index_text
    )

    # Git expects either a valid index or no index at all.
    temporary_index.unlink(
        missing_ok=True
    )

    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(
        temporary_index
    )

    try:
        # Seed the temporary index from the current HEAD,
        # then overlay the current working-tree state.
        _git(
            repo,
            "read-tree",
            "HEAD",
            env=env,
        )

        _git(
            repo,
            "add",
            "-A",
            "--",
            ".",
            env=env,
        )

        return _git(
            repo,
            "write-tree",
            env=env,
        ).strip()

    finally:
        temporary_index.unlink(
            missing_ok=True
        )

def capture_worktree_snapshot(
    repo: Path,
) -> GitWorktreeSnapshot:
    return GitWorktreeSnapshot(
        tree=_capture_worktree_tree(
            repo
        )
    )


def capture_attempt_evidence(
    *,
    repo: Path,
    before: GitWorktreeSnapshot,
) -> GitAttemptEvidence:
    after = capture_worktree_snapshot(
        repo
    )

    changed_files = sorted(
        set(
            _git_z(
                repo,
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                before.tree,
                after.tree,
                "--",
            )
        )
    )

    return GitAttemptEvidence(
        before_tree=before.tree,
        after_tree=after.tree,
        changed_files=changed_files,
    )
