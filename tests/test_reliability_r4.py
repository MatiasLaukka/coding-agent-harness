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


def _run_isolated_python_probe(tmp_path: Path, source: str, *, timeout: float = 8.0) -> dict:
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", source, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if child.poll() is None:
            child.kill()
        try:
            stdout, stderr = child.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2.0)
            pytest.fail(
                "isolated cancellation probe did not terminate after killing its "
                f"own child process (pid={child.pid})"
            )
        pytest.fail(
            "isolated cancellation probe exceeded its independent outer deadline; "
            f"child pid={child.pid}, stdout={stdout!r}, stderr={stderr!r}"
        )
    assert child.returncode == 0, f"isolated probe failed: {stderr}"
    return json.loads(stdout.strip().splitlines()[-1])


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


def _configure_slow_git_clean_filter(repo: Path, root: Path) -> tuple[Path, Path]:
    import sys

    marker = root / "slow-filter-marker.json"
    release = root / "slow-filter-release"
    script = root / "slow_clean_filter.py"
    script.write_text(
        "import json, os, time\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'pid': os.getpid(), 'index': os.environ.get('GIT_INDEX_FILE')}}))\n"
        f"release = Path({str(release)!r})\n"
        "while not release.exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable.replace(chr(92), "/")}" "{str(script).replace(chr(92), "/")}"'
    _git_command(repo, "config", "filter.r4.clean", command)
    _git_command(repo, "config", "filter.r4.required", "true")
    (repo / ".gitattributes").write_text("payload.txt filter=r4\n", encoding="utf-8")
    (repo / "payload.txt").write_text("slow input\n", encoding="utf-8")
    return marker, release


def _capture_snapshot_with_outer_deadline(repo: Path, release: Path) -> None:
    import threading

    done = threading.Event()
    result = []

    def capture() -> None:
        try:
            git_module.capture_worktree_snapshot(repo)
        except BaseException as exc:
            result.append(exc)
        else:
            result.append(None)
        finally:
            done.set()

    caller = threading.Thread(target=capture, daemon=True)
    caller.start()
    try:
        if not done.wait(8.0):
            release.touch()
            assert done.wait(2.0), "snapshot exceeded its independent outer safety deadline"
            pytest.fail("snapshot exceeded its independent outer safety deadline")
        assert result and isinstance(result[0], BaseException), "slow filter unexpectedly completed successfully"
        raise result[0]
    finally:
        release.touch()
        caller.join(timeout=1.0)


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


def test_blocked_snapshot_cleanup_returns_by_deadline_with_partial_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    repo = _make_git_repo(tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    index_bytes = real_index.read_bytes()
    monkeypatch.setattr(git_module, "SNAPSHOT_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(git_module, "PROCESS_CLEANUP_RESERVE_SECONDS", 0.05, raising=False)
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)

    def fake_git(repo_path, *args, **kwargs):
        return "a" * 40 + "\n" if args[0] == "write-tree" else ""

    monkeypatch.setattr(git_module, "_git", fake_git)
    original_unlink = Path.unlink
    entered = threading.Event()
    release = threading.Event()
    calls = {}

    def block_final_index_delete(path, *args, **kwargs):
        if path in temporary_indexes:
            calls[path] = calls.get(path, 0) + 1
            if calls[path] == 2:
                entered.set()
                release.wait()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", block_final_index_delete)
    result = []

    def capture():
        try:
            result.append(git_module.capture_worktree_snapshot(repo))
        except BaseException as exc:
            result.append(exc)

    caller = threading.Thread(target=capture, daemon=True)
    caller.start()
    returned_by_outer_deadline = False
    try:
        assert entered.wait(0.5), "snapshot did not reach final index cleanup"
        caller.join(timeout=0.75)
        returned_by_outer_deadline = not caller.is_alive()
    finally:
        release.set()
        caller.join(timeout=1.0)

    assert returned_by_outer_deadline, "blocked unlink held the collection caller past its phase deadline"
    assert isinstance(result[0], git_module.GitCollectionError)
    assert _reason(result[0]) == "deadline-exceeded"
    assert _partial(result[0])["tree"] == "a" * 40
    assert result[0].git_failure.filesystem_operation_pending_count == 1
    assert result[0].git_failure.temporary_artifact_unresolved_count == 2
    assert real_index.read_bytes() == index_bytes


def test_blocked_temporary_index_creation_is_bounded_and_late_creation_cannot_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    repo = _make_git_repo(tmp_path)
    monkeypatch.setattr(git_module, "SNAPSHOT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(git_module, "PROCESS_CLEANUP_RESERVE_SECONDS", 0.05, raising=False)
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []
    entered = threading.Event()
    release = threading.Event()
    late_cleanup_done = threading.Event()
    original_unlink = Path.unlink

    def blocked_mkstemp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        entered.set()
        release.wait()
        return fd, name

    def observe_cleanup(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if temporary_indexes and path == temporary_indexes[0]:
            late_cleanup_done.set()
        return result

    monkeypatch.setattr(git_module.tempfile, "mkstemp", blocked_mkstemp)
    monkeypatch.setattr(Path, "unlink", observe_cleanup)
    result = []

    def capture():
        try:
            result.append(git_module.capture_worktree_snapshot(repo))
        except BaseException as exc:
            result.append(exc)

    caller = threading.Thread(target=capture, daemon=True)
    caller.start()
    returned_by_outer_deadline = False
    try:
        assert entered.wait(0.5), "temporary-index creation did not start"
        assert temporary_indexes[0].exists()
        caller.join(timeout=0.75)
        returned_by_outer_deadline = not caller.is_alive()
    finally:
        release.set()
        caller.join(timeout=1.0)

    assert returned_by_outer_deadline, "blocked creation held the collection caller past its phase deadline"
    assert isinstance(result[0], git_module.GitCollectionError)
    assert _reason(result[0]) == "deadline-exceeded"
    assert result[0].git_failure.filesystem_operation_pending_count == 1
    assert result[0].git_failure.temporary_artifact_cleanup_uncertain is True
    assert late_cleanup_done.wait(1.0)
    assert not temporary_indexes[0].exists()


def test_interrupted_thread_start_keeps_helper_owned_until_late_cleanup(
    tmp_path: Path,
) -> None:
    import textwrap

    probe = textwrap.dedent(
        r"""
        import json, signal, sys, threading, time
        from pathlib import Path
        import coding_agent_harness.git as git_module

        root = Path(sys.argv[1])
        source = root / "source-index"
        target = root / "temporary-index"
        source.write_bytes(b"copied after caller cancellation")
        owner = git_module._TemporaryIndex(
            path=target,
            lock_path=Path(str(target) + ".lock"),
            artifact_state="owned",
        )
        entered = threading.Event()
        release = threading.Event()
        operation_finished = threading.Event()
        cleanup_finished = threading.Event()
        interruption = KeyboardInterrupt("sentinel")
        helpers = []
        real_start = threading.Thread.start
        real_open = Path.open
        real_copy = git_module._copy_index_sync
        real_cleanup = git_module._remove_owned_artifacts

        def signal_handler(_signum, _frame):
            raise interruption

        signal.signal(signal.SIGINT, signal_handler)

        def interrupted_start(self, *args, **kwargs):
            result = real_start(self, *args, **kwargs)
            if self.name == "harness-git-filesystem":
                helpers.append(self)
                if not entered.wait(2.0):
                    raise RuntimeError("copy did not reach its source open")
                signal.raise_signal(signal.SIGINT)
            return result

        threading.Thread.start = interrupted_start

        def blocked_source_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == source and mode == "rb":
                entered.set()
                release.wait(2.0)
            return real_open(path, *args, **kwargs)

        def observed_copy(*args, **kwargs):
            try:
                return real_copy(*args, **kwargs)
            finally:
                operation_finished.set()

        def observed_cleanup(temporary_index):
            result = real_cleanup(temporary_index)
            cleanup_finished.set()
            return result

        Path.open = blocked_source_open
        git_module._copy_index_sync = observed_copy
        git_module._remove_owned_artifacts = observed_cleanup

        try:
            git_module._copy_index(source, owner, deadline=time.monotonic() + 4.0)
            caught = None
        except BaseException as exc:
            caught = exc

        failure = getattr(caught, "git_failure", None)
        alive_at_cancel = any(helper.is_alive() for helper in helpers)
        release.set()
        finished = operation_finished.wait(1.5)
        cleaned = cleanup_finished.wait(1.5)
        print(json.dumps({
            "identity_preserved": caught is interruption,
            "pending": getattr(failure, "filesystem_operation_pending_count", None),
            "unresolved": getattr(failure, "temporary_artifact_unresolved_count", None),
            "uncertain": getattr(failure, "temporary_artifact_cleanup_uncertain", None),
            "helper_alive_at_cancel": alive_at_cancel,
            "operation_finished": finished,
            "cleanup_finished": cleaned,
            "artifact_exists_after_cleanup": target.exists(),
            "helper_alive_after_cleanup": any(helper.is_alive() for helper in helpers),
        }))
        """
    )
    result = _run_isolated_python_probe(tmp_path, probe)

    assert result["identity_preserved"] is True
    assert result["helper_alive_at_cancel"] is True
    assert result["pending"] == 1
    assert result["unresolved"] == 2
    assert result["uncertain"] is True
    assert result["operation_finished"] is True
    assert result["cleanup_finished"] is True
    assert result["artifact_exists_after_cleanup"] is False
    assert result["helper_alive_after_cleanup"] is False


def test_interrupted_temporary_file_creation_retains_descriptor_and_path_ownership(
    tmp_path: Path,
) -> None:
    import textwrap

    probe = textwrap.dedent(
        r"""
        import json, os, signal, sys, tempfile, threading, time
        from pathlib import Path
        import coding_agent_harness.git as git_module

        root = Path(sys.argv[1])
        created = threading.Event()
        cleanup_finished = threading.Event()
        records = []
        interruption = KeyboardInterrupt("creation-handoff")
        real_mkstemp = tempfile.mkstemp
        real_start = threading.Thread.start
        real_cleanup = git_module._remove_owned_artifacts

        def signal_handler(_signum, _frame):
            raise interruption

        signal.signal(signal.SIGINT, signal_handler)

        def owned_mkstemp(prefix):
            fd, name = real_mkstemp(prefix=prefix, dir=root)
            records.append((fd, name))
            created.set()
            return fd, name

        def observe_cleanup(owner):
            result = real_cleanup(owner)
            cleanup_finished.set()
            return result

        git_module.tempfile.mkstemp = owned_mkstemp
        git_module._remove_owned_artifacts = observe_cleanup

        def interrupted_start(self, *args, **kwargs):
            result = real_start(self, *args, **kwargs)
            if self.name == "harness-git-filesystem":
                if not created.wait(2.0):
                    raise RuntimeError("temporary creation did not complete")
                signal.raise_signal(signal.SIGINT)
            return result

        threading.Thread.start = interrupted_start

        try:
            with git_module._temporary_index(
                prefix="ownership-probe-",
                deadline=time.monotonic() + 4.0,
                init_stage="ownership-probe-init",
                cleanup_stage="ownership-probe-cleanup",
            ):
                raise AssertionError("interrupted creation must not yield an index")
            caught = None
        except BaseException as exc:
            caught = exc

        failure = getattr(caught, "git_failure", None)
        cleaned = cleanup_finished.wait(1.5)
        fd, name = records[0]
        try:
            os.fstat(fd)
            descriptor_open = True
        except OSError:
            descriptor_open = False
        print(json.dumps({
            "identity_preserved": caught is interruption,
            "pending": getattr(failure, "filesystem_operation_pending_count", None),
            "unresolved": getattr(failure, "temporary_artifact_unresolved_count", None),
            "uncertain": getattr(failure, "temporary_artifact_cleanup_uncertain", None),
            "cleanup_finished": cleaned,
            "descriptor_open": descriptor_open,
            "temporary_path_exists": Path(name).exists(),
        }))
        """
    )
    result = _run_isolated_python_probe(tmp_path, probe)

    assert result["identity_preserved"] is True
    assert result["pending"] == 1
    assert result["unresolved"] == 2
    assert result["uncertain"] is True
    assert result["cleanup_finished"] is True
    assert result["descriptor_open"] is False
    assert result["temporary_path_exists"] is False


def test_cleanup_deadline_before_helper_start_reports_owned_paths_as_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import types

    temporary_index_path = tmp_path / "owned-index"
    temporary_lock_path = Path(str(temporary_index_path) + ".lock")
    temporary_index_path.write_bytes(b"owned index")
    temporary_lock_path.write_bytes(b"owned lock")
    owner = git_module._TemporaryIndex(
        path=temporary_index_path,
        lock_path=temporary_lock_path,
    )
    starts = []
    real_start = threading.Thread.start

    def observe_start(thread, *args, **kwargs):
        starts.append(thread.name)
        return real_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", observe_start)
    monkeypatch.setattr(
        git_module,
        "time",
        types.SimpleNamespace(monotonic=lambda: 20.0),
    )

    with pytest.raises(git_module.GitCollectionError) as caught:
        git_module._cleanup_temporary_index(
            owner,
            deadline=19.0,
            stage="cleanup-before-launch",
        )

    assert starts == []
    assert caught.value.git_failure.reason == "deadline-exceeded"
    assert caught.value.git_failure.filesystem_operation_pending_count == 0
    assert caught.value.git_failure.temporary_artifact_unresolved_count == 2
    assert caught.value.git_failure.temporary_artifact_cleanup_uncertain is True
    assert temporary_index_path.exists()
    assert temporary_lock_path.exists()


def test_uncertain_descriptor_close_is_not_retried_or_reported_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, name = git_module.tempfile.mkstemp(
        prefix="uncertain-close-",
        dir=tmp_path,
    )
    path = Path(name)
    owner = git_module._TemporaryIndex(
        path=path,
        lock_path=Path(str(path) + ".lock"),
        descriptor=descriptor,
        artifact_state="owned",
    )
    real_close = os.close
    close_calls = []
    close_error = OSError("ambiguous descriptor close")

    def fail_close(actual_descriptor: int) -> None:
        assert actual_descriptor == descriptor
        close_calls.append(actual_descriptor)
        raise close_error

    monkeypatch.setattr(git_module.os, "close", fail_close)
    try:
        errors, unresolved, first_error = git_module._remove_owned_artifacts(owner)
        assert errors >= 1
        assert unresolved >= 1
        assert first_error is close_error
        assert owner.descriptor is None
        assert owner.descriptor_uncertain is True

        _second_errors, second_unresolved, _second_error = (
            git_module._remove_owned_artifacts(owner)
        )
        assert close_calls == [descriptor]
        assert second_unresolved >= 1
    finally:
        monkeypatch.setattr(git_module.os, "close", real_close)
        real_close(descriptor)
        path.unlink(missing_ok=True)
        Path(str(path) + ".lock").unlink(missing_ok=True)


@pytest.mark.parametrize("blocked_operation", ["source-open", "source-read", "target-open", "target-write"])
def test_blocked_temporary_index_copy_io_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_operation: str,
) -> None:
    import threading

    repo = _make_git_repo(tmp_path)
    head = _git_command(repo, "rev-parse", "HEAD")
    source_index = repo / ".git" / "index"
    monkeypatch.setattr(git_module, "CUMULATIVE_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(git_module, "PROCESS_CLEANUP_RESERVE_SECONDS", 0.05, raising=False)
    real_open = Path.open
    entered = threading.Event()
    release = threading.Event()
    late_cleanup_done = threading.Event()
    temporary_indexes = []
    real_mkstemp = git_module.tempfile.mkstemp
    original_unlink = Path.unlink

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    def fake_git(repo_path, *args, **kwargs):
        if args[0] == "rev-parse" and args[1:] == ("HEAD",):
            return head + "\n"
        if args[0] == "rev-parse" and args[1:] == ("--git-path", "index"):
            return ".git/index\n"
        if args[0] == "log":
            return ""
        return ""

    class GateFile:
        def __init__(self, wrapped, method):
            self.wrapped = wrapped
            self.method = method

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def read(self, *args, **kwargs):
            if self.method == "read":
                entered.set()
                release.wait()
            return self.wrapped.read(*args, **kwargs)

        def write(self, *args, **kwargs):
            if self.method == "write":
                entered.set()
                release.wait()
            return self.wrapped.write(*args, **kwargs)

    def gated_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        is_source = path == source_index and mode == "rb"
        is_target = path.name.startswith("coding-agent-harness-index-") and mode == "wb"
        if blocked_operation == "source-open" and is_source:
            entered.set()
            release.wait()
        if blocked_operation == "target-open" and is_target:
            entered.set()
            release.wait()
        wrapped = real_open(path, *args, **kwargs)
        if blocked_operation == "source-read" and is_source:
            return GateFile(wrapped, "read")
        if blocked_operation == "target-write" and is_target:
            return GateFile(wrapped, "write")
        return wrapped

    def observe_cleanup(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if temporary_indexes and path == temporary_indexes[0]:
            late_cleanup_done.set()
        return result

    monkeypatch.setattr(git_module, "_git", fake_git)
    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)
    monkeypatch.setattr(Path, "open", gated_open)
    monkeypatch.setattr(Path, "unlink", observe_cleanup)
    baseline = git_module.GitBaseline(commit=head, status="")
    result = []

    def capture():
        try:
            result.append(git_module.capture_evidence(repo=repo, baseline=baseline))
        except BaseException as exc:
            result.append(exc)

    caller = threading.Thread(target=capture, daemon=True)
    caller.start()
    returned_by_outer_deadline = False
    try:
        assert entered.wait(0.6), f"copy did not reach blocked {blocked_operation}"
        caller.join(timeout=0.8)
        returned_by_outer_deadline = not caller.is_alive()
    finally:
        release.set()
        caller.join(timeout=1.0)

    assert returned_by_outer_deadline, f"blocked {blocked_operation} held the collection caller"
    assert isinstance(result[0], git_module.GitCollectionError)
    assert _reason(result[0]) == "deadline-exceeded"
    assert _partial(result[0])["final_head"] == head
    assert _partial(result[0])["changed_files"] == []
    assert "patch" not in _partial(result[0])
    assert result[0].git_failure.filesystem_operation_pending_count == 1
    assert late_cleanup_done.wait(1.0)
    assert not temporary_indexes[0].exists()


def test_real_timed_out_git_add_removes_owned_index_lock_without_changing_real_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    _, release = _configure_slow_git_clean_filter(repo, tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    real_index_bytes = real_index.read_bytes()
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)
    monkeypatch.setattr(git_module, "SNAPSHOT_TIMEOUT_SECONDS", 4)
    try:
        for _ in range(2):
            release.unlink(missing_ok=True)
            with pytest.raises(git_module.GitCollectionError) as caught:
                _capture_snapshot_with_outer_deadline(repo, release)
            assert _reason(caught.value) == "deadline-exceeded"
            assert caught.value.process_result.cleanup_attempted is True
            assert caught.value.process_result.cleanup_errors == []
            assert caught.value.process_result.surviving_pids == []
            assert caught.value.process_result.exit_code is not None
            temporary_index = temporary_indexes[-1]
            assert not temporary_index.exists()
            assert not Path(str(temporary_index) + ".lock").exists()
            assert not list(tmp_path.glob("coding-agent-harness-snapshot-index-*.lock"))
            assert real_index.read_bytes() == real_index_bytes
    finally:
        release.touch()

    assert len(temporary_indexes) == 2


def test_failed_owned_index_lock_deletion_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    _, release = _configure_slow_git_clean_filter(repo, tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    real_index_bytes = real_index.read_bytes()
    real_mkstemp = git_module.tempfile.mkstemp
    temporary_indexes = []

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)
    monkeypatch.setattr(git_module, "SNAPSHOT_TIMEOUT_SECONDS", 4)
    original_unlink = Path.unlink
    lock_error = OSError("private lock deletion failure")

    def fail_owned_lock(path, *args, **kwargs):
        if temporary_indexes and path == Path(str(temporary_indexes[-1]) + ".lock"):
            raise lock_error
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owned_lock)
    try:
        with pytest.raises(git_module.GitCollectionError) as caught:
            _capture_snapshot_with_outer_deadline(repo, release)
        assert _reason(caught.value) == "deadline-exceeded"
        cleanup = caught.value.git_cleanup_failure
        assert cleanup.reason == "cleanup-failed"
        assert cleanup.cleanup_error_count == 1
        assert cleanup.temporary_artifact_unresolved_count == 1
        assert not temporary_indexes[0].exists()
        assert Path(str(temporary_indexes[0]) + ".lock").exists()
        assert real_index.read_bytes() == real_index_bytes
    finally:
        release.touch()
        monkeypatch.setattr(Path, "unlink", original_unlink)
        if temporary_indexes:
            original_unlink(Path(str(temporary_indexes[0]) + ".lock"), missing_ok=True)


def test_unconfirmed_git_process_preserves_owned_index_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_git_repo(tmp_path)
    real_index = repo / _git_command(repo, "rev-parse", "--git-path", "index")
    real_index_bytes = real_index.read_bytes()
    temporary_indexes = []
    real_mkstemp = git_module.tempfile.mkstemp

    def record_temp(prefix):
        fd, name = real_mkstemp(prefix=prefix, dir=tmp_path)
        temporary_indexes.append(Path(name))
        return fd, name

    monkeypatch.setattr(git_module.tempfile, "mkstemp", record_temp)

    def run(kwargs):
        args = kwargs["argv"][1:]
        temporary_index = Path(kwargs["env"]["GIT_INDEX_FILE"])
        if args[0] == "read-tree":
            temporary_index.write_bytes(b"owned temporary index")
            return _fake_process_result(kwargs["argv"])
        if args[0] == "add":
            Path(str(temporary_index) + ".lock").write_bytes(b"git may still own this lock")
            return _fake_process_result(
                kwargs["argv"],
                exit_code=None,
                timed_out=True,
                output_complete=False,
                cleanup_attempted=True,
                cleanup_errors=["cleanup-deadline"],
                surviving_pids=[4242],
            )
        return _fake_process_result(kwargs["argv"], stdout="b" * 40 + "\n")

    _fake_git_runner(monkeypatch, run)
    with pytest.raises(git_module.GitCollectionError) as caught:
        git_module.capture_worktree_snapshot(repo)

    temporary_index = temporary_indexes[0]
    lock = Path(str(temporary_index) + ".lock")
    assert caught.value.process_result.surviving_pids == [4242]
    assert caught.value.git_cleanup_failure.reason == "termination-unconfirmed"
    assert caught.value.git_cleanup_failure.temporary_artifact_unresolved_count == 2
    assert temporary_index.exists()
    assert lock.exists()
    assert real_index.read_bytes() == real_index_bytes


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
        filesystem_operation_pending_count=1,
        temporary_artifact_unresolved_count=2,
        temporary_artifact_cleanup_uncertain=True,
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
    assert payload["git_failure"]["filesystem_operation_pending_count"] == 1
    assert payload["git_failure"]["temporary_artifact_unresolved_count"] == 2
    assert payload["git_failure"]["temporary_artifact_cleanup_uncertain"] is True
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
