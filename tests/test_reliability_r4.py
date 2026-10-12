import io
import json
from pathlib import Path

import pytest

import coding_agent_harness.process as process_module


@pytest.mark.parametrize(
    "stream_name, payload, limit, expected, truncated",
    [
        ("stdout", b"abcd", 4, "abcd", False),
        ("stdout", b"abcde", 4, "abcd", True),
        ("stderr", b"xyz", 3, "xyz", False),
        ("stderr", b"xyzw", 3, "xyz", True),
    ],
)
def test_process_capture_enforces_per_stream_byte_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_name: str,
    payload: bytes,
    limit: int,
    expected: str,
    truncated: bool,
) -> None:
    class Child:
        pid = 8123

        def __init__(self) -> None:
            self.stdout = io.BytesIO(payload if stream_name == "stdout" else b"")
            self.stderr = io.BytesIO(payload if stream_name == "stderr" else b"")

        def poll(self) -> int:
            return 0

    child = Child()
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *args, **kwargs: child)

    result = process_module.run_process(
        argv=["fake-git"],
        cwd=tmp_path,
        timeout_seconds=1,
        stdout_limit_bytes=limit if stream_name == "stdout" else 4,
        stderr_limit_bytes=limit if stream_name == "stderr" else 4,
    )

    assert getattr(result, stream_name) == expected
    assert getattr(result, f"{stream_name}_truncated") is truncated
    assert result.output_complete is True


@pytest.mark.parametrize("deadline", [10.0, 11.5])
def test_expired_deadline_or_missing_cleanup_reserve_does_not_launch_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deadline: float,
) -> None:
    import types

    launched = []
    monkeypatch.setattr(
        process_module,
        "time",
        types.SimpleNamespace(monotonic=lambda: 10.0, sleep=lambda _: None),
    )
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: launched.append(args),
    )

    result = process_module.run_process(
        argv=["must-not-launch"],
        cwd=tmp_path,
        timeout_seconds=1,
        deadline=deadline,
    )

    assert launched == []
    assert result.timed_out is True
    assert result.started is False
    assert result.exit_code is None
    assert result.infrastructure_failed is True


def test_absolute_deadline_reserves_bounded_cleanup_and_rejects_late_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import types

    clock = [100.0]

    class Stream:
        def close(self) -> None:
            pass

    class Child:
        pid = 8123
        stdout = Stream()
        stderr = Stream()

        def poll(self) -> None:
            return None

    class Capture:
        def __init__(self) -> None:
            self.output_limit_bytes = 1024
            self.data = bytearray()
            self.done = threading.Event()
            self.stop = threading.Event()
            self.eof = False
            self.truncated = False
            self.error = None
            self.thread = None

        def start(self, stream) -> None:
            self.done.set()

        def snapshot(self) -> str:
            return ""

    child = Child()
    monkeypatch.setattr(
        process_module,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(process_module, "_Capture", Capture)

    cleanup_started_at = []

    def late_cleanup(pid: int) -> process_module.CleanupResult:
        assert pid == child.pid
        cleanup_started_at.append(clock[0])
        clock[0] = 110.0
        return process_module.CleanupResult()

    monkeypatch.setattr(process_module, "_kill_process_tree", late_cleanup)

    result = process_module.run_process(
        argv=["fake-git"],
        cwd=tmp_path,
        timeout_seconds=30,
        deadline=110.0,
    )

    assert result.timed_out is True
    assert result.cleanup_attempted is True
    assert "cleanup-deadline" in result.cleanup_errors
    assert "parent-still-running" in result.cleanup_errors
    assert result.surviving_pids == [child.pid]
    assert result.infrastructure_failed is True
    assert cleanup_started_at == [pytest.approx(108.0)]
    assert clock[0] == 110.0


def test_blocked_process_tree_cleanup_returns_with_unconfirmed_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    helper_started = threading.Event()
    release_helper = threading.Event()
    result_holder = []
    cleanup_deadline = []
    pid = 8124

    def blocked_cleanup(actual_pid: int) -> process_module.CleanupResult:
        assert actual_pid == pid
        helper_started.set()
        release_helper.wait()
        return process_module.CleanupResult()

    monkeypatch.setattr(process_module, "_kill_process_tree", blocked_cleanup)

    def invoke_bounded_cleanup() -> None:
        deadline = time.monotonic() + 0.15
        cleanup_deadline.append(deadline)
        result_holder.append(process_module._kill_process_tree_before_deadline(pid, deadline))

    caller = threading.Thread(target=invoke_bounded_cleanup, daemon=True)
    started_at = time.monotonic()
    caller.start()
    try:
        caller.join(timeout=1.0)
        elapsed = time.monotonic() - started_at
        assert not caller.is_alive(), "cleanup call exceeded the independent outer deadline"
        assert helper_started.is_set()
        assert cleanup_deadline
        assert elapsed < 0.9
        assert result_holder[0].errors == ["cleanup-deadline"]
        assert result_holder[0].surviving_pids == [pid]
    finally:
        release_helper.set()
        caller.join(timeout=1.0)


import hashlib
import os
import subprocess
import time
from types import SimpleNamespace

import coding_agent_harness.git as git_module
import coding_agent_harness.process as process_module
from coding_agent_harness.models import ReviewDecision, TaskPacket, WorkerResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.worker import WorkerRunResult


def _git_command(repo: Path, *args: str, timeout: float = 10) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    return result.stdout.strip()


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "git-repo"
    repo.mkdir()
    _git_command(repo, "init", "-q")
    _git_command(repo, "config", "user.name", "R4 Test")
    _git_command(repo, "config", "user.email", "r4@example.test")
    (repo / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
    (repo / "existing.txt").write_text("before\n", encoding="utf-8")
    (repo / "rename source.txt").write_text("rename me\n", encoding="utf-8")
    _git_command(repo, "add", "-A")
    _git_command(repo, "commit", "-m", "baseline")
    return repo


def _fake_process_result(argv, **overrides) -> ProcessResult:
    values = dict(
        argv=list(argv),
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        elapsed_seconds=0.001,
        started=True,
        output_complete=True,
        stdout_truncated=False,
        stderr_truncated=False,
        cleanup_attempted=False,
        cleanup_errors=[],
        surviving_pids=[],
    )
    values.update(overrides)
    return ProcessResult(**values)


def _fake_git_runner(monkeypatch, handler):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return handler(kwargs)

    monkeypatch.setattr(git_module, "run_process", run, raising=False)
    return calls


def _reason(error: BaseException):
    failure = getattr(error, "git_failure", None)
    return getattr(failure, "reason", getattr(error, "reason", None))


def _partial(error: BaseException):
    return getattr(error, "git_evidence", {})


def test_r4_resource_budgets_are_centralized_and_match_the_spec() -> None:
    assert git_module.BASELINE_TIMEOUT_SECONDS == 15
    assert git_module.SNAPSHOT_TIMEOUT_SECONDS == 30
    assert git_module.CUMULATIVE_TIMEOUT_SECONDS == 45
    assert git_module.ATTEMPT_EVIDENCE_TIMEOUT_SECONDS == 30
    assert git_module.GIT_STDOUT_LIMIT_BYTES == 1024 * 1024
    assert git_module.GIT_PATCH_STDOUT_LIMIT_BYTES == 8 * 1024 * 1024
    assert git_module.GIT_STDERR_LIMIT_BYTES == 64 * 1024


def test_baseline_records_clean_state_and_uses_one_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    calls = []
    original = process_module.run_process

    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(git_module, "run_process", observe, raising=False)
    baseline = git_module.capture_baseline(repo)

    assert baseline.commit == _git_command(repo, "rev-parse", "HEAD")
    assert baseline.status == ""
    assert len(calls) == 2
    assert calls[0]["deadline"] == calls[1]["deadline"]
    assert all(0 < call["timeout_seconds"] <= 15 for call in calls)


def test_baseline_preserves_dirty_status_and_rejects_whitespace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    head = "c" * 40

    def fake_git(repo_dir, *args, **kwargs):
        if args == ("rev-parse", "HEAD"):
            return head + "\n"
        if args and args[0] == "status":
            return "?? trailing-space-name \r\n"
        raise AssertionError(args)

    monkeypatch.setattr(git_module, "_git", fake_git)

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_baseline(repo)

    assert _reason(caught.value) == "baseline-not-clean"
    assert _partial(caught.value)["commit"] == head
    assert _partial(caught.value)["status"] == "?? trailing-space-name "


def test_baseline_rejects_invalid_deadline_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    monkeypatch.setattr(git_module, "BASELINE_TIMEOUT_SECONDS", 0, raising=False)

    with pytest.raises(ValueError):
        git_module.capture_baseline(repo)


def test_baseline_timeout_preserves_observed_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    head = "a" * 40

    def run(kwargs):
        args = kwargs["argv"][1:]
        if args == ["rev-parse", "HEAD"]:
            return _fake_process_result(kwargs["argv"], stdout=head + "\n")
        return _fake_process_result(
            kwargs["argv"],
            exit_code=0,
            timed_out=True,
            output_complete=False,
            cleanup_attempted=True,
        )

    _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_baseline(repo)

    assert _reason(caught.value) == "deadline-exceeded"
    assert _partial(caught.value)["commit"] == head
    assert "status" not in _partial(caught.value)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_oversized_baseline_status_is_rejected_after_preserving_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    repo = _make_git_repo(tmp_path)
    head = "b" * 40

    def run(kwargs):
        args = kwargs["argv"][1:]
        if args == ["rev-parse", "HEAD"]:
            return _fake_process_result(kwargs["argv"], stdout=head + "\n")
        return _fake_process_result(
            kwargs["argv"],
            **{f"{stream}_truncated": True},
        )

    _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_baseline(repo)

    assert _reason(caught.value) == "output-budget-exceeded"
    assert _partial(caught.value)["commit"] == head
    assert "status" not in _partial(caught.value)


def test_status_collection_failure_preserves_head_and_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    original = OSError("sensitive temporary path")

    def fail_status(repo_path, *args, **kwargs):
        if args and args[0] == "status":
            raise original
        return original_git(repo_path, *args, **kwargs)

    original_git = git_module._git
    monkeypatch.setattr(git_module, "_git", fail_status)

    with pytest.raises(OSError) as caught:
        git_module.capture_baseline(repo)

    assert caught.value is original
    assert _partial(caught.value)["commit"] == _git_command(repo, "rev-parse", "HEAD")


def test_snapshot_timeout_cleans_temporary_index_and_preserves_real_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    real_index_bytes = real_index.read_bytes()
    temporary_indexes = []
    envs = []
    real_mkstemp = git_module.tempfile.mkstemp

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)

    def run(kwargs):
        envs.append(kwargs.get("env", {}))
        args = kwargs["argv"][1:]
        if args[0] == "add":
            return _fake_process_result(
                kwargs["argv"],
                timed_out=True,
                output_complete=False,
                cleanup_attempted=True,
            )
        return _fake_process_result(kwargs["argv"])

    _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_worktree_snapshot(repo)

    assert _reason(caught.value) == "deadline-exceeded"
    assert len(temporary_indexes) == 1
    assert not temporary_indexes[0].exists()
    assert real_index.read_bytes() == real_index_bytes
    assert envs
    assert all(Path(env["GIT_INDEX_FILE"]) == temporary_indexes[0] for env in envs)


def test_snapshot_commands_share_one_deadline_and_leave_real_index_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    before = hashlib.sha256(real_index.read_bytes()).digest()
    calls = []
    original = process_module.run_process

    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(git_module, "run_process", observe, raising=False)
    snapshot = git_module.capture_worktree_snapshot(repo)

    assert len(snapshot.tree) in (40, 64)
    assert len(calls) == 3
    assert len({call["deadline"] for call in calls}) == 1
    assert hashlib.sha256(real_index.read_bytes()).digest() == before


def test_cumulative_collection_commands_share_one_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    baseline = git_module.capture_baseline(repo)
    calls = []
    original = process_module.run_process

    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(git_module, "run_process", observe, raising=False)
    evidence = git_module.capture_evidence(repo=repo, baseline=baseline)

    assert evidence.changed_files == []
    assert evidence.patch == ""
    assert len(calls) == 7
    assert len({call["deadline"] for call in calls}) == 1
    assert all(call["stdout_limit_bytes"] == git_module.GIT_STDOUT_LIMIT_BYTES
               for call in calls if call["argv"][1:] != ["diff", "--binary", "--no-ext-diff",
                                                        "--no-textconv", baseline.commit, "--"])
    assert any(call["stdout_limit_bytes"] == git_module.GIT_PATCH_STDOUT_LIMIT_BYTES
               for call in calls)


def test_filename_list_over_budget_preserves_only_completed_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    baseline = git_module.capture_baseline(repo)
    head = "c" * 40

    def run(kwargs):
        args = kwargs["argv"][1:]
        if args == ["rev-parse", "HEAD"]:
            return _fake_process_result(kwargs["argv"], stdout=head + "\n")
        if args[0] == "status":
            return _fake_process_result(kwargs["argv"])
        return _fake_process_result(
            kwargs["argv"],
            stdout="truncated-name",
            stdout_truncated=True,
        )

    _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_evidence(repo=repo, baseline=baseline)

    partial = _partial(caught.value)
    assert _reason(caught.value) == "output-budget-exceeded"
    assert partial["final_head"] == head
    assert partial["final_status"] == ""
    assert "tracked_changes" not in partial
    assert "changed_files" not in partial


def test_patch_over_budget_preserves_complete_changed_file_list_without_accepting_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    baseline = git_module.capture_baseline(repo)

    def run(kwargs):
        args = kwargs["argv"][1:]
        if args == ["rev-parse", "HEAD"]:
            return _fake_process_result(kwargs["argv"], stdout=baseline.commit + "\n")
        if args[0] == "status":
            return _fake_process_result(kwargs["argv"])
        if args[:2] == ["rev-parse", "--git-path"]:
            return _fake_process_result(kwargs["argv"], stdout=".git/index\n")
        if args[:2] == ["diff", "--binary"]:
            return _fake_process_result(
                kwargs["argv"],
                stdout="partial patch",
                stdout_truncated=True,
            )
        return _fake_process_result(kwargs["argv"])

    calls = _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_evidence(repo=repo, baseline=baseline)

    partial = _partial(caught.value)
    assert _reason(caught.value) == "output-budget-exceeded"
    assert partial["changed_files"] == []
    assert "patch" not in partial
    assert "commits" not in partial
    patch_call = next(call for call in calls if call["argv"][1:3] == ["diff", "--binary"])
    assert patch_call["stdout_limit_bytes"] == git_module.GIT_PATCH_STDOUT_LIMIT_BYTES


@pytest.mark.parametrize(
    "case, expected_reason",
    [
        ("launch", "git-launch-failed"),
        ("nonzero", "git-nonzero-exit"),
        ("incomplete", "incomplete-collection"),
        ("stdout-over", "output-budget-exceeded"),
        ("stderr-over", "output-budget-exceeded"),
        ("deadline", "deadline-exceeded"),
        ("cleanup", "cleanup-failed"),
        ("survivor", "termination-unconfirmed"),
    ],
)
def test_git_subprocess_failures_have_distinguishable_safe_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_reason: str,
) -> None:
    repo = _make_git_repo(tmp_path)
    overrides = {
        "launch": dict(started=False, exit_code=None, failure_type="FileNotFoundError",
                       output_complete=False),
        "nonzero": dict(exit_code=128, stderr="raw secret"),
        "incomplete": dict(output_complete=False),
        "stdout-over": dict(stdout_truncated=True),
        "stderr-over": dict(stderr_truncated=True),
        "deadline": dict(timed_out=True, output_complete=True, exit_code=0),
        "cleanup": dict(cleanup_errors=["C:\\secret\\path"]),
        "survivor": dict(surviving_pids=[99999]),
    }[case]

    def run(kwargs):
        return _fake_process_result(kwargs["argv"], **overrides)

    _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module._git(
            repo,
            "status",
            deadline=time.monotonic() + 5,
            stage="baseline-status",
        )

    assert _reason(caught.value) == expected_reason
    failure = caught.value.git_failure
    assert failure.stage == "baseline-status"
    assert "secret" not in str(failure)


def test_late_successful_git_exit_is_still_a_deadline_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    clock = [1.0]
    monkeypatch.setattr(git_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def run(kwargs):
        clock[0] = 5.0
        return _fake_process_result(kwargs["argv"], exit_code=0, stdout="complete")

    _fake_git_runner(monkeypatch, run)

    with pytest.raises(RuntimeError) as caught:
        git_module._git(repo, "status", deadline=5.0, stage="baseline-status")

    assert _reason(caught.value) == "deadline-exceeded"
    assert caught.value.process_result.exit_code == 0


def test_attempt_diff_failure_preserves_before_and_after_tree_ids_and_exception_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    before = git_module.GitWorktreeSnapshot(tree="d" * 40)
    after = "e" * 40
    original = OSError("sensitive diff output")

    monkeypatch.setattr(git_module, "_capture_worktree_tree", lambda *args, **kwargs: after)

    def fail_diff(*args, **kwargs):
        raise original

    monkeypatch.setattr(git_module, "_git_z", fail_diff)

    with pytest.raises(OSError) as caught:
        git_module.capture_attempt_evidence(repo=repo, before=before)

    assert caught.value is original
    assert _partial(caught.value)["before_tree"] == before.tree
    assert _partial(caught.value)["after_tree"] == after
import coding_agent_harness.controller as controller_module
import coding_agent_harness.orchestrator as orchestrator_module
import coding_agent_harness.cli as cli_module


def test_attempt_snapshot_and_diff_share_one_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    original = process_module.run_process
    calls = []

    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(git_module, "run_process", observe, raising=False)
    before = git_module.capture_worktree_snapshot(repo)
    calls.clear()

    evidence = git_module.capture_attempt_evidence(repo=repo, before=before)

    assert evidence.before_tree == before.tree
    assert len(calls) == 4
    assert len({call["deadline"] for call in calls}) == 1
    assert all(0 < call["timeout_seconds"] <= 30 for call in calls)


@pytest.mark.parametrize(
    "limit",
    [
        git_module.GIT_STDOUT_LIMIT_BYTES,
        git_module.GIT_PATCH_STDOUT_LIMIT_BYTES,
        git_module.GIT_STDERR_LIMIT_BYTES,
    ],
)
@pytest.mark.parametrize("over_by", [0, 1])
def test_capture_memory_is_bounded_at_each_git_output_budget(
    limit: int,
    over_by: int,
) -> None:
    capture = process_module._Capture()
    capture.output_limit_bytes = limit
    stream = io.BytesIO(b"x" * (limit + over_by))

    capture.start(stream)
    assert capture.done.wait(timeout=5)
    if capture.thread is not None:
        capture.thread.join(timeout=5)

    assert len(capture.data) == limit
    assert capture.truncated is (over_by == 1)
    assert capture.eof is True


def test_snapshot_cleanup_failure_prevents_success_and_preserves_tree_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    index_bytes = real_index.read_bytes()
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)
    original_unlink = Path.unlink
    cleanup_attempts = {}
    cleanup_error = OSError("sensitive cleanup path")

    def fail_final_cleanup(path, *args, **kwargs):
        if path in temporary_indexes:
            cleanup_attempts[path] = cleanup_attempts.get(path, 0) + 1
            if cleanup_attempts[path] == 2:
                raise cleanup_error
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_final_cleanup)

    try:
        with pytest.raises(OSError) as caught:
            git_module.capture_worktree_snapshot(repo)

        assert caught.value is cleanup_error
        assert _reason(caught.value) == "cleanup-failed"
        assert _partial(caught.value)["tree"]
        assert real_index.read_bytes() == index_bytes
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)
        for path in temporary_indexes:
            original_unlink(path, missing_ok=True)


def test_snapshot_cleanup_after_deadline_fails_with_completed_tree_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(git_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)
    original_unlink = Path.unlink
    cleanup_calls = {}

    def expire_during_cleanup(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if path in temporary_indexes:
            cleanup_calls[path] = cleanup_calls.get(path, 0) + 1
            if cleanup_calls[path] == 2:
                clock[0] = 131.0
        return result

    monkeypatch.setattr(Path, "unlink", expire_during_cleanup)
    _fake_git_runner(
        monkeypatch,
        lambda kwargs: _fake_process_result(
            kwargs["argv"], stdout="f" * 40 + "\n" if kwargs["argv"][1] == "write-tree" else "",
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        git_module.capture_worktree_snapshot(repo)

    assert _reason(caught.value) == "deadline-exceeded"
    assert _partial(caught.value)["tree"] == "f" * 40
    assert len(temporary_indexes) == 1
    assert not temporary_indexes[0].exists()


def test_snapshot_interruption_cleans_temporary_index_and_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    index_bytes = real_index.read_bytes()
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []
    interruption = KeyboardInterrupt("private interruption")

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)

    def interrupt(kwargs):
        raise interruption

    _fake_git_runner(monkeypatch, interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        git_module.capture_worktree_snapshot(repo)

    assert caught.value is interruption
    assert len(temporary_indexes) == 1
    assert not temporary_indexes[0].exists()
    assert real_index.read_bytes() == index_bytes


def test_binary_patch_with_rename_staged_unstaged_and_edge_filenames_applies_faithfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    baseline = git_module.capture_baseline(repo)
    (repo / "existing.txt").write_text("staged text\n", encoding="utf-8")
    _git_command(repo, "add", "existing.txt")
    (repo / "existing.txt").write_text("unstaged final text\n", encoding="utf-8")
    _git_command(repo, "mv", "rename source.txt", "rename target.txt")

    binary = bytes(range(256)) + b"\x00R4\xffbinary"
    (repo / "new binary.bin").write_bytes(binary)
    unusual_name = "space [brackets] ☃ file.txt"
    (repo / unusual_name).write_text("special name\n", encoding="utf-8")
    (repo / "ignored.bin").write_bytes(b"ignored")

    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    index_bytes = real_index.read_bytes()
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)
    evidence = git_module.capture_evidence(repo=repo, baseline=baseline)

    expected = {
        "existing.txt",
        "rename source.txt",
        "rename target.txt",
        "new binary.bin",
        unusual_name,
    }
    assert set(evidence.changed_files) == expected
    assert "ignored.bin" not in evidence.changed_files
    assert "GIT binary patch" in evidence.patch
    assert "rename from rename source.txt" in evidence.patch
    assert "rename to rename target.txt" in evidence.patch
    assert "+unstaged final text" in evidence.patch
    assert "ignored.bin" not in evidence.patch
    assert real_index.read_bytes() == index_bytes
    assert temporary_indexes
    assert all(not path.exists() for path in temporary_indexes)

    apply_repo = tmp_path / "patch-apply-repo"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(apply_repo)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=10,
    )
    patch_file = tmp_path / "evidence.patch"
    patch_file.write_text(evidence.patch, encoding="utf-8", newline="")
    subprocess.run(
        ["git", "apply", "--binary", str(patch_file)],
        cwd=apply_repo,
        check=True,
        capture_output=True,
        timeout=10,
    )

    assert (apply_repo / "existing.txt").read_text(encoding="utf-8") == "unstaged final text\n"
    assert not (apply_repo / "rename source.txt").exists()
    assert (apply_repo / "rename target.txt").read_text(encoding="utf-8") == "rename me\n"
    assert (apply_repo / "new binary.bin").read_bytes() == binary
    assert (apply_repo / unusual_name).read_text(encoding="utf-8") == "special name\n"
    assert not (apply_repo / "ignored.bin").exists()


def _offline_task() -> TaskPacket:
    return TaskPacket(
        task_id="r4-offline",
        goal="Exercise bounded Git evidence failure behavior.",
        acceptance=["Git evidence is complete."],
    )


def _offline_worker_result() -> WorkerRunResult:
    process = _fake_process_result(["offline-worker"])
    return WorkerRunResult(
        process=process,
        completion=WorkerResult(status="completed", summary="Offline completion"),
    )


class _CountingOfflineWorker:
    def __init__(self, repo: Path | None = None) -> None:
        self.calls = 0
        self.repo = repo

    def run(self, *, task, repo_dir):
        self.calls += 1
        if self.repo is not None:
            (self.repo / f"worker-{self.calls}.txt").write_text("observed\n", encoding="utf-8")
        return _offline_worker_result()


class _RecordingOfflineReviewer:
    def __init__(self, *decisions: ReviewDecision) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        if self.decisions:
            return self.decisions.pop(0)
        return ReviewDecision(decision="accept", rationale="Offline acceptance")


def _known_git_error() -> RuntimeError:
    failure = git_module.GitFailureMetadata(
        reason="output-budget-exceeded",
        stage="cumulative-patch",
        stdout_limit_bytes=git_module.GIT_PATCH_STDOUT_LIMIT_BYTES,
    )
    return git_module.GitCollectionError(failure)


def test_controller_and_orchestrator_stop_after_git_collection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    worker = _CountingOfflineWorker()
    reviewer = _RecordingOfflineReviewer(
        ReviewDecision(decision="accept", rationale="Should never be reached")
    )
    verification_calls = []

    def fail_collection(**kwargs):
        error = _known_git_error()
        error.git_evidence = {"baseline_commit": "f" * 40, "patch": "PRIVATE_PATCH"}
        raise error

    monkeypatch.setattr(controller_module, "capture_evidence", fail_collection)
    monkeypatch.setattr(orchestrator_module, "capture_evidence", fail_collection)
    monkeypatch.setattr(
        controller_module,
        "run_verification",
        lambda **kwargs: verification_calls.append("controller"),
    )
    monkeypatch.setattr(
        orchestrator_module,
        "run_verification",
        lambda **kwargs: verification_calls.append("orchestrator"),
    )

    with pytest.raises(RuntimeError) as controller_error:
        controller_module.HarnessController(worker=worker, reviewer=reviewer).run(
            task=_offline_task(),
            repo_dir=repo,
            verification_commands=[],
        )

    assert worker.calls == 1
    assert reviewer.calls == 0
    assert verification_calls == []
    assert controller_error.value.harness_evidence["worker_invocation_count"] == 1
    assert controller_error.value.harness_evidence["partial_git_evidence"]["patch"] == "PRIVATE_PATCH"
    assert controller_error.value.harness_evidence["git_failure"].reason == "output-budget-exceeded"

    worker = _CountingOfflineWorker()
    with pytest.raises(RuntimeError) as orchestrator_error:
        orchestrator_module.run_task(
            task=_offline_task(),
            repo_dir=repo,
            worker=worker,
            verification_commands=[],
        )

    assert worker.calls == 1
    assert verification_calls == []
    assert orchestrator_error.value.harness_evidence["worker_invocation_count"] == 1
    assert orchestrator_error.value.harness_evidence["git_failure"].reason == "output-budget-exceeded"


def test_scope_revision_git_failure_prevents_next_worker_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    worker = _CountingOfflineWorker()
    reviewer = _RecordingOfflineReviewer(
        ReviewDecision(
            decision="revise-scope",
            rationale="The authorized file list needs one additional file.",
            revised_allowed_files=["existing.txt", "shared.txt"],
        )
    )
    verification_calls = []
    original_snapshot = controller_module.capture_worktree_snapshot
    snapshot_calls = []

    def fail_second_snapshot(repo_dir):
        snapshot_calls.append(repo_dir)
        if len(snapshot_calls) == 2:
            error = _known_git_error()
            error.git_evidence = {"after_tree": "a" * 40}
            raise error
        return original_snapshot(repo_dir)

    monkeypatch.setattr(controller_module, "capture_worktree_snapshot", fail_second_snapshot)
    monkeypatch.setattr(
        controller_module,
        "run_verification",
        lambda **kwargs: (
            verification_calls.append("verified")
            or SimpleNamespace(passed=True, checks=[])
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        controller_module.HarnessController(worker=worker, reviewer=reviewer).run(
            task=_offline_task(),
            repo_dir=repo,
            verification_commands=[],
        )

    evidence = caught.value.harness_evidence
    assert worker.calls == 1
    assert reviewer.calls == 1
    assert verification_calls == ["verified"]
    assert len(evidence["attempts"]) == 2
    assert evidence["attempts"][0]["decision"].decision == "revise-scope"
    assert "worker_result" not in evidence["attempts"][1]
    assert evidence["partial_git_evidence"]["after_tree"] == "a" * 40
    assert evidence["git_failure"].reason == "output-budget-exceeded"


def test_later_git_failure_preserves_completed_remediation_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    worker = _CountingOfflineWorker(repo)
    reviewer = _RecordingOfflineReviewer(
        ReviewDecision(
            decision="remediate",
            rationale="One offline remediation attempt.",
            remediation="Continue once.",
        )
    )
    original_capture = controller_module.capture_evidence
    calls = []

    def fail_second_capture(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return original_capture(**kwargs)
        error = _known_git_error()
        error.git_evidence = {
            "baseline_commit": kwargs["baseline"].commit,
            "final_head": kwargs["baseline"].commit,
            "tracked_changes": [],
        }
        raise error

    monkeypatch.setattr(controller_module, "capture_evidence", fail_second_capture)

    with pytest.raises(RuntimeError) as caught:
        controller_module.HarnessController(
            worker=worker,
            reviewer=reviewer,
            max_remediation_attempts=2,
        ).run(task=_offline_task(), repo_dir=repo, verification_commands=[])

    evidence = caught.value.harness_evidence
    assert worker.calls == 2
    assert reviewer.calls == 1
    assert len(evidence["attempts"]) == 2
    assert evidence["attempts"][0]["git_evidence"].final_head
    assert evidence["attempts"][0]["verification"].passed is True
    assert evidence["attempts"][0]["decision"].decision == "remediate"
    assert evidence["attempts"][1]["worker_result"].process.exit_code == 0
    assert "git_evidence" not in evidence["attempts"][1]
    assert evidence["partial_git_evidence"]["tracked_changes"] == []


def test_cli_failure_summary_allowlists_git_metadata_without_raw_payloads(
    capsys,
) -> None:
    failure = RuntimeError("PRIVATE_EXCEPTION_MESSAGE")
    failure.git_failure = git_module.GitFailureMetadata(
        reason="output-budget-exceeded",
        stage="cumulative-patch",
        exit_code=0,
        stdout_limit_bytes=git_module.GIT_PATCH_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes=git_module.GIT_STDERR_LIMIT_BYTES,
        stdout_truncated=True,
    )
    failure.git_evidence = {
        "baseline_commit": "a" * 40,
        "final_head": "b" * 40,
        "final_status": "PRIVATE_STATUS",
        "changed_files": ["PRIVATE_FILENAME"],
        "patch": "PRIVATE_PATCH",
        "temporary_index": "D:\\private\\temporary-index",
        "stderr": "PRIVATE_STDERR",
    }
    failure.harness_evidence = {
        "stage": "git-evidence",
        "worker_invocation_count": 1,
        "git_failure": failure.git_failure,
        "partial_git_evidence": failure.git_evidence,
        "attempts": [],
    }

    cli_module._print_failure_evidence(failure, stage="git-evidence")
    line = capsys.readouterr().err.strip()
    prefix = "Failure evidence: "
    assert line.startswith(prefix)
    payload = json.loads(line.removeprefix(prefix))

    assert payload["git_failure"]["reason"] == "output-budget-exceeded"
    assert payload["git_failure"]["stage"] == "cumulative-patch"
    assert payload["git_failure"]["stdout_limit_bytes"] == 8 * 1024 * 1024
    assert payload["git_failure"]["stdout_truncated"] is True
    assert payload["failure_type"] == "RuntimeError"
    for private in (
        "PRIVATE_EXCEPTION_MESSAGE",
        "PRIVATE_STATUS",
        "PRIVATE_FILENAME",
        "PRIVATE_PATCH",
        "PRIVATE_STDERR",
        "D:\\private\\temporary-index",
    ):
        assert private not in line
