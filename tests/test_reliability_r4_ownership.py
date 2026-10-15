import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import coding_agent_harness.git as g


@pytest.mark.parametrize("case", [
    "before-close", "before-yield", "creation-return", "creation-blocked",
    "helper-start", "helper-completion", "cleanup-entry", "cleanup-running", "repeat-cleanup",
    "ownership-publication", "publication-error", "helper-construction", "final-evidence",
    "cleanup-guard", "cleanup-result", "late-copy",
])
def test_ownership_lifecycle_real_sigint(tmp_path, case):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("r4_ownership_probe.py")), str(tmp_path), case],
        capture_output=True, text=True, timeout=14,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["fired"]
    assert observed["identity"] is True
    assert observed["elapsed"] < 8
    assert observed["index_unchanged"] is True
    assert observed["neighbor_unchanged"] is True
    assert observed["helpers_alive"] == 0
    if case in {"cleanup-entry", "repeat-cleanup", "cleanup-guard"}:
        assert observed["unresolved"] >= 2
        assert observed["uncertain"] is True
    else:
        assert observed["descriptor_open"] is False
        assert observed["artifact_exists"] is False
        assert observed["lock_exists"] is False
    if case in {"creation-blocked", "cleanup-running", "ownership-publication", "late-copy"}:
        assert observed["pending"] == 1
        assert observed["uncertain"] is True
    if case == "late-copy":
        assert observed["confirmed_before_release"] is False
        assert observed["confirmed_after_release"] is True
    if case.startswith("cleanup") or case == "final-evidence":
        assert observed["tree_preserved"] is True


def test_cleanup_expiry_between_checks_enriches_existing_failure(tmp_path, monkeypatch):
    path = tmp_path / "owned-index"
    path.write_bytes(b"index")
    lock = Path(str(path)+".lock")
    lock.write_bytes(b"lock")
    owner = g._TemporaryIndex(path=path, lock_path=lock, artifact_state="owned")
    clock = iter([10.0, 20.0])
    monkeypatch.setattr(g, "time", SimpleNamespace(monotonic=lambda: next(clock, 20.0)))
    with pytest.raises(g.GitCollectionError) as caught:
        g._cleanup_temporary_index(owner, deadline=15.0, stage="snapshot-cleanup")
    assert caught.value.git_failure.reason == "deadline-exceeded"
    assert caught.value.git_failure.filesystem_operation_pending_count == 0
    assert caught.value.git_failure.temporary_artifact_unresolved_count == 2
    assert caught.value.git_failure.temporary_artifact_cleanup_uncertain is True
    assert path.exists() and lock.exists()


def test_failure_reconciliation_is_monotonic_idempotent_and_preserves_exception():
    original = KeyboardInterrupt("private cancellation")
    try:
        raise original
    except KeyboardInterrupt as exc:
        traceback = exc.__traceback__
        evidence = {"tree": "a"*40}
        g._attach_observations(exc, evidence)
        initial = g._failure_metadata("incomplete-collection", "snapshot-add")
        enriched = g._failure_metadata("cleanup-failed", "snapshot-cleanup",
            cleanup_error_count=1, filesystem_operation_pending_count=1,
            temporary_artifact_unresolved_count=2, temporary_artifact_cleanup_uncertain=True)
        for failure in [initial, enriched, enriched, initial]:
            g._attach_failure(exc, failure)
        assert exc is original and exc.__traceback__ is traceback
        assert exc.git_evidence == evidence
        assert exc.git_failure.reason == initial.reason
        assert exc.git_failure.stage == initial.stage
        assert exc.git_failure.cleanup_error_count == 1
        assert exc.git_failure.filesystem_operation_pending_count == 1
        assert exc.git_failure.temporary_artifact_unresolved_count == 2
        assert exc.git_failure.temporary_artifact_cleanup_uncertain is True
        other = RuntimeError("separate collection")
        g._attach_failure(other, initial)
        assert other.git_failure.temporary_artifact_unresolved_count == 0
        assert other.git_failure.temporary_artifact_cleanup_uncertain is False


def test_completed_helper_error_still_authorizes_owned_cleanup(tmp_path):
    path = tmp_path / "owned-index"
    lock = Path(str(path)+".lock")
    path.write_bytes(b"owned")
    owner = g._TemporaryIndex(path=path, lock_path=lock, artifact_state="owned")
    failure = OSError("operation failed after creating owned bytes")
    def fail():
        raise failure
    import time
    with pytest.raises(OSError) as caught:
        g._bounded_filesystem_call(fail, deadline=time.monotonic()+4,
            stage="snapshot-index-init", owner=owner, pending_artifacts=2)
    assert caught.value is failure
    assert g._cleanup_temporary_index(owner, deadline=time.monotonic()+4,
        stage="snapshot-cleanup") is None
    assert not path.exists() and not lock.exists()


def test_close_started_descriptor_is_never_retried(tmp_path, monkeypatch):
    fd, name = g.tempfile.mkstemp(dir=tmp_path)
    owner = g._TemporaryIndex(path=Path(name), lock_path=Path(name+".lock"),
        descriptor=fd, descriptor_close_started=True, descriptor_uncertain=True,
        artifact_state="owned")
    calls = []
    monkeypatch.setattr(g.os, "close", lambda fd: calls.append(fd))
    try:
        _errors, unresolved, _error = g._remove_owned_artifacts(owner)
        assert calls == []
        assert unresolved >= 1
        assert owner.descriptor_uncertain is True
    finally:
        # The simulated uncertain close did not actually consume this fixture fd.
        monkeypatch.undo()
        os.close(fd)
        Path(name).unlink(missing_ok=True)


def test_final_evidence_requires_positive_owned_cleanup(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("r4_ownership_probe.py")), str(tmp_path), "cleanup-noop"],
        capture_output=True, text=True, timeout=14,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["artifact_exists"] is True
    assert observed["failed_closed"] is True
    assert observed["unresolved"] >= 2
    assert observed["uncertain"] is True
    assert observed["tree_preserved"] is True
    assert observed["index_unchanged"] is True


def test_collection_registry_deduplicates_and_keeps_cleanup_scoped(tmp_path):
    import time
    owners = []
    for number in range(2):
        fd, name = g.tempfile.mkstemp(prefix="unique-index-", dir=tmp_path)
        os.close(fd)
        path = Path(name)
        path.write_bytes(str(number).encode())
        lock = Path(name+".lock")
        lock.write_bytes(b"owned lock")
        owners.append(g._TemporaryIndex(path=path, lock_path=lock, artifact_state="owned"))
    first, second = owners
    assert first.path != second.path
    collection = g._GitCollectionOwnership()
    collection.register(first)
    collection.register(first)
    assert collection.failure("snapshot-cleanup").temporary_artifact_unresolved_count == 2
    separate = g._GitCollectionOwnership()
    separate.register(second)
    assert separate.failure("snapshot-cleanup").temporary_artifact_unresolved_count == 2
    collection.register(second)
    assert collection.failure("snapshot-cleanup").temporary_artifact_unresolved_count == 4
    assert g._cleanup_temporary_index(first, deadline=time.monotonic()+4, stage="snapshot-cleanup") is None
    assert not first.path.exists() and not first.lock_path.exists()
    assert second.path.read_bytes() == b"1"
    assert second.lock_path.read_bytes() == b"owned lock"
    assert collection.failure("snapshot-cleanup").temporary_artifact_unresolved_count == 2
    assert separate.failure("snapshot-cleanup").temporary_artifact_unresolved_count == 2
    assert g._cleanup_temporary_index(second, deadline=time.monotonic()+4, stage="snapshot-cleanup") is None
    collection.require_complete("snapshot-cleanup")
