from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from coding_agent_harness.models import TaskPacket, VerificationCommand, WorkerResult
from coding_agent_harness.orchestrator import RunFinding, TaskRunResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.verifier import VerificationCheck, VerificationResult
from coding_agent_harness.worker import WorkerRunResult


def sample(status="ready-for-review", completion=True):
    process = ProcessResult(argv=["offline"], exit_code=0, stdout="worker output", stderr="", timed_out=False, elapsed_seconds=0.1)
    return TaskRunResult(
        task_id="sample", baseline_sha="a" * 40, final_head_sha="b" * 40,
        worker=WorkerRunResult(process=process, completion=WorkerResult(status="completed", summary="worker claim", changed_files=["claimed.py"], remaining_concerns=["claim concern"]) if completion else None, completion_error=None if completion else "missing completion"),
        changed_files=["observed.py"], patch="diff --git a/observed.py b/observed.py\n+captured\n",
        verification=VerificationResult(passed=True, checks=[]),
        findings=[] if status == "ready-for-review" else [RunFinding(code="worker-result-missing", detail="completion unavailable")], status=status,
    )


def task():
    return TaskPacket(task_id="sample", goal="Fix one edge case", constraints=["Keep scope"], acceptance=["Tests pass"], suggested_files=["hint.py"], allowed_files=["observed.py"])


def layout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True, timeout=10)
    return repo


@pytest.mark.parametrize("status", ["ready-for-review", "needs-attention"])
@pytest.mark.parametrize("completion", [True, False])
def test_renderer_captured_evidence_and_purity(status, completion):
    from coding_agent_harness.report import render_run_report
    packet, result = task(), sample(status, completion)
    command = VerificationCommand(id="required", argv=["offline", "test"], expectation="expected_failure", expectation_reason="known failure")
    result.verification = VerificationResult(passed=False, checks=[VerificationCheck(**result.worker.process.model_dump(), id="required", passed=False, expectation="expected_failure", expectation_reason="known failure", expectation_met=False)])
    before = copy.deepcopy((packet.model_dump(), command.model_dump(), result.model_dump()))
    report = render_run_report(task=packet, verification_commands=[command], result=result)
    assert f"Status: {status}" in report
    assert "ready-for-review is not reviewer acceptance" in report
    assert "Reviewer acceptance not recorded" in report
    for value in [packet.goal, "Keep scope", "Tests pass", "hint\\.py", "observed.py", "worker output", "known failure", "expectation_met", result.patch, result.baseline_sha, result.final_head_sha]:
        assert value in report
    assert "Worker claims" in report and "Independent Git evidence" in report
    assert ("worker claim" in report) == completion
    if not completion:
        assert "Completion unavailable" in report and "missing completion" in report
    assert before == (packet.model_dump(), command.model_dump(), result.model_dump())
    assert report == render_run_report(task=packet, verification_commands=[command], result=result)


def test_renderer_zero_checks_and_order():
    from coding_agent_harness.report import render_run_report
    result = sample()
    report = render_run_report(task=task(), verification_commands=[], result=result)
    assert "No verification commands configured" in report
    assert "No verification checks recorded" in report
    commands = [VerificationCommand(id=id, argv=[id]) for id in ["second", "first"]]
    report = render_run_report(task=task(), verification_commands=commands, result=result)
    assert report.index('"second"') < report.index('"first"')


def test_adversarial_markdown_cannot_add_structure():
    from coding_agent_harness.report import render_run_report
    attack = '\n# Forged acceptance\n<script>alert(1)</script>\n[click](https://evil.test)\n```\n````````\n~~~\n'
    packet, result = task(), sample()
    packet.goal = attack
    packet.acceptance = [attack]
    result.worker.completion.summary = attack
    result.changed_files = [attack]
    result.patch = attack
    result.worker.process.stdout = attack
    report = render_run_report(task=packet, verification_commands=[], result=result)
    # A tiny CommonMark fence scanner: every malicious heading/tag stays fenced
    # or escaped, and no embedded run closes a generated fence.
    fence = None
    outside = []
    for line in report.splitlines():
        if fence:
            if line == fence:
                fence = None
        elif line.startswith('```') and set(line) == {'`'}:
            fence = line
        else:
            outside.append(line)
    assert fence is None
    plain = '\n'.join(outside)
    assert not any(line.startswith('# Forged acceptance') for line in outside)
    assert '<script>' not in plain and '[click](' not in plain
    assert attack in report  # exact captured patch, not a normalized replacement


def test_publish_complete_create_only(tmp_path, monkeypatch):
    from coding_agent_harness import report
    destination = tmp_path / 'report.md'
    real_link = os.link
    def observe(source, target):
        assert not destination.exists()
        assert Path(source).read_bytes() == 'complete π\n'.encode()
        real_link(source, target)
    monkeypatch.setattr(report.os, 'link', observe)
    report.publish_run_report(destination, 'complete π\n')
    assert destination.read_bytes() == 'complete π\n'.encode()
    with pytest.raises((OSError, ValueError)):
        report.publish_run_report(destination, 'overwrite')
    assert destination.read_text(encoding='utf-8') == 'complete π\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.md']


@pytest.mark.parametrize('failure', ['race', 'link', 'write', 'fsync'])
def test_publication_failure_never_exposes_partial_file(tmp_path, monkeypatch, failure):
    from coding_agent_harness import report
    destination = tmp_path / 'report.md'
    def fail(*args, **kwargs): raise OSError('private error detail')
    if failure in ('race', 'link'):
        def link(source, target):
            if failure == 'race': destination.write_text('winner')
            else: raise OSError('publication failure')
            os_link(source, target)
        os_link = os.link
        monkeypatch.setattr(report.os, 'link', link)
    elif failure == 'fsync': monkeypatch.setattr(report.os, 'fsync', fail)
    else:
        real_fdopen = os.fdopen
        class Broken:
            def __init__(self, fd): self.file = real_fdopen(fd, 'wb')
            def __enter__(self): return self
            def __exit__(self, *args): self.file.close()
            def write(self, data):
                self.file.write(data[:3])
                raise OSError('partial write')
        monkeypatch.setattr(report.os, 'fdopen', lambda fd, mode: Broken(fd))
    with pytest.raises(OSError): report.publish_run_report(destination, 'complete report')
    assert destination.exists() == (failure == 'race')
    if destination.exists(): assert destination.read_text() == 'winner'
    assert not list(tmp_path.glob('.harness-report-*'))


def test_renderer_does_not_access_filesystem_or_execute(tmp_path, monkeypatch):
    from coding_agent_harness.report import render_run_report
    import subprocess
    packet, result = task(), sample()
    result.patch = ''
    result.worker.process.output_complete = False
    result.worker.process.stdout_truncated = True
    def forbidden(*a, **kw): pytest.fail('renderer attempted I/O')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    content = render_run_report(task=packet, verification_commands=[], result=result)
    assert '"output_complete": false' in content
    assert '"stdout_truncated": true' in content
    assert '## Captured patch' in content


@pytest.mark.parametrize('failure', ['short-write', 'fdopen'])
def test_failed_write_or_stream_acquisition_closes_owned_descriptor(tmp_path, monkeypatch, failure):
    from coding_agent_harness import report
    destination = tmp_path / 'report.md'
    real_fdopen = os.fdopen
    descriptors = []
    class ShortWriter:
        def __init__(self, fd): self.stream = real_fdopen(fd, 'wb')
        def __enter__(self): return self
        def __exit__(self, *a): self.stream.close()
        def write(self, data): return self.stream.write(data[:3])
    def acquire(fd, mode):
        descriptors.append(fd)
        if failure == 'fdopen': raise OSError('acquisition failed')
        return ShortWriter(fd)
    monkeypatch.setattr(report.os, 'fdopen', acquire)
    with pytest.raises(OSError):
        report.publish_run_report(destination, 'complete')
    with pytest.raises(OSError): os.fstat(descriptors[0])
    assert not destination.exists() and not list(tmp_path.glob('.harness-report-*'))


def test_cleanup_failure_reports_error_even_after_complete_publication(tmp_path, monkeypatch):
    from coding_agent_harness import report
    destination = tmp_path / 'report.md'
    real_unlink = Path.unlink
    def fail(path, *a, **kw):
        if path.name.startswith('.harness-report-'): raise PermissionError('cleanup blocked')
        return real_unlink(path, *a, **kw)
    monkeypatch.setattr(Path, 'unlink', fail)
    try:
        with pytest.raises(PermissionError):
            report.publish_run_report(destination, 'complete')
        assert destination.read_text() == 'complete'
    finally:
        for temporary in tmp_path.glob('.harness-report-*'): real_unlink(temporary)


def test_explicit_empty_allowlist_is_not_described_as_unspecified():
    from coding_agent_harness.report import render_run_report
    packet = task()
    packet.allowed_files = []
    content = render_run_report(task=packet, verification_commands=[], result=sample())
    assert 'No files allowed' in content


def test_concurrent_publications_are_complete_and_create_only(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from coding_agent_harness import report
    destination = tmp_path / 'report.md'
    barrier = Barrier(2)
    def publish(content):
        barrier.wait(timeout=10)
        try:
            report.publish_run_report(destination, content)
            return 'published'
        except FileExistsError:
            return 'collision'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ['first' * 1000, 'second' * 1000]))
    assert sorted(results) == ['collision', 'published']
    assert destination.read_text() in ('first' * 1000, 'second' * 1000)
    assert not list(tmp_path.glob('.harness-report-*'))


def test_publisher_has_no_git_dependency(tmp_path, monkeypatch):
    from coding_agent_harness import report
    def forbidden(*a, **kw): pytest.fail('publisher queried Git')
    monkeypatch.setattr(report, 'run_process', forbidden)
    report.publish_run_report(tmp_path / 'report.md', 'captured')
    assert (tmp_path / 'report.md').read_text() == 'captured'


def test_unsupported_hard_links_have_no_overwrite_fallback(tmp_path, monkeypatch):
    import errno
    from coding_agent_harness import report
    def unsupported(*a, **kw): raise OSError(errno.EOPNOTSUPP, 'hard links unsupported')
    def forbidden(*a, **kw): pytest.fail('overwrite fallback')
    monkeypatch.setattr(report.os, 'link', unsupported)
    monkeypatch.setattr(report.os, 'replace', forbidden)
    monkeypatch.setattr(report.os, 'rename', forbidden)
    with pytest.raises(OSError): report.publish_run_report(tmp_path / 'report.md', 'complete')
    assert not list(tmp_path.iterdir())
