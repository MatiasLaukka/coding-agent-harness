import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent_harness import control_records
from coding_agent_harness.control_records import (
    RecordPublicationError,
    prepare_record_directory,
    publish_record,
)


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def test_record_directory_must_be_existing_and_outside_git_resources(tmp_path):
    repo = init_repo(tmp_path / "repo")
    outside = tmp_path / "record-only"
    outside.mkdir()

    plan = prepare_record_directory(outside, repo_dir=repo)
    assert plan.directory == outside.resolve()

    with pytest.raises(ValueError, match="overlaps"):
        prepare_record_directory(repo, repo_dir=repo)
    with pytest.raises(FileNotFoundError):
        prepare_record_directory(tmp_path / "missing", repo_dir=repo)


def test_create_only_collision_never_overwrites_existing_record(tmp_path, monkeypatch):
    directory = tmp_path / "records"
    directory.mkdir()
    plan = control_records.RecordDirectory(directory.resolve(), ())
    record_id = "a" * 32
    destination = directory / f"start-{record_id}.json"
    destination.write_text("original", encoding="utf-8")
    monkeypatch.setattr(control_records.uuid, "uuid4", lambda: SimpleNamespace(hex=record_id))

    with pytest.raises(RecordPublicationError) as caught:
        publish_record(plan, kind="start", record={"workflow_id": "run"})

    assert caught.value.published is False
    assert destination.read_text(encoding="utf-8") == "original"
    assert list(directory.iterdir()) == [destination]


def test_link_failure_has_no_overwrite_fallback_or_worker_authorization(tmp_path, monkeypatch):
    directory = tmp_path / "records"
    directory.mkdir()
    plan = control_records.RecordDirectory(directory.resolve(), ())
    monkeypatch.setattr(control_records.os, "link", lambda *args: (_ for _ in ()).throw(OSError("link blocked")))
    monkeypatch.setattr(control_records.os, "replace", lambda *args: pytest.fail("replace fallback"))
    monkeypatch.setattr(control_records.os, "rename", lambda *args: pytest.fail("rename fallback"))

    with pytest.raises(RecordPublicationError) as caught:
        publish_record(plan, kind="start", record={"workflow_id": "run"})

    assert caught.value.published is False
    assert not list(directory.glob("start-*.json"))
    assert not list(directory.glob(".harness-record-*"))


def test_cleanup_failure_after_link_is_reported_with_published_reference(tmp_path, monkeypatch):
    directory = tmp_path / "records"
    directory.mkdir()
    plan = control_records.RecordDirectory(directory.resolve(), ())
    real_unlink = Path.unlink

    def fail_temp(path, *args, **kwargs):
        if path.name.startswith(".harness-record-"):
            raise PermissionError("cleanup blocked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp)
    with pytest.raises(RecordPublicationError) as caught:
        publish_record(plan, kind="review", record={"human_decision": {"requested_action": "accept"}})

    assert caught.value.published is True
    assert caught.value.reference.path.exists()
    assert caught.value.reference.path.read_text(encoding="utf-8").find('"requested_action": "accept"') >= 0
    for temporary in directory.glob(".harness-record-*"):
        real_unlink(temporary)
