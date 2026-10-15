from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent_harness import cli, orchestrator, report
from coding_agent_harness.worker import QwenWorker
from tests.test_report import layout, sample, task


def inputs(tmp_path):
    repo = layout(tmp_path)
    task_file = tmp_path / 'task.json'
    task_file.write_text(json.dumps({'task': task().model_dump(), 'verification_commands': []}))
    return repo, task_file


def args(repo, task_file, destination=None):
    result = ['run', str(task_file), '--repo', str(repo), '--model', 'offline-test']
    return result + ['--report', str(destination)] if destination is not None else result


@pytest.mark.parametrize('status,expected', [('ready-for-review', 0), ('needs-attention', 2)])
def test_cli_report_retains_console_exit_and_single_dispatch(tmp_path, monkeypatch, capsys, status, expected):
    repo, task_file = inputs(tmp_path)
    destination = tmp_path / 'report.md'
    result = sample(status)
    calls = []
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: calls.append('setup'))
    def run(**kw):
        calls.append('run')
        return result
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(repo, task_file)) == expected
    plain = capsys.readouterr()
    assert cli.app(args(repo, task_file, destination)) == expected
    saved = capsys.readouterr()
    assert saved == plain
    assert calls == ['setup', 'run', 'setup', 'run']
    assert f'Status: {status}' in destination.read_text(encoding='utf-8')


@pytest.mark.parametrize('kind', ['inside', 'exists', 'missing-parent', 'permission', 'git-layout'])
def test_cli_invalid_report_prevents_worker_setup(tmp_path, monkeypatch, capsys, kind):
    from coding_agent_harness import report
    repo, task_file = inputs(tmp_path)
    destination = tmp_path / 'report.md'
    if kind == 'inside': destination = repo / 'report.md'
    if kind == 'exists': destination.write_text('keep')
    if kind == 'missing-parent': destination = tmp_path / 'absent' / 'report.md'
    if kind == 'permission': monkeypatch.setattr(report.os, 'access', lambda *a: False)
    if kind == 'git-layout': (repo / '.git').rename(repo / 'saved-metadata')
    def forbidden(**kwargs): pytest.fail('worker invoked for invalid destination')
    monkeypatch.setattr(cli, 'QwenWorker', forbidden)
    monkeypatch.setattr(cli, 'run_task', forbidden)
    assert cli.app(args(repo, task_file, destination)) == 1
    output = capsys.readouterr()
    assert not output.out and 'Report destination error' in output.err
    if kind == 'exists': assert destination.read_text() == 'keep'


@pytest.mark.parametrize('failure', [RuntimeError('PRIVATE_DETAIL'), KeyboardInterrupt()])
def test_cli_no_completed_report_after_execution_raises(tmp_path, monkeypatch, capsys, failure):
    repo, task_file = inputs(tmp_path)
    destination = tmp_path / 'report.md'
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    def run(**kw): raise failure
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(repo, task_file, destination)) == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    assert not destination.exists()
    assert not list(tmp_path.glob('.harness-report-*'))
    assert 'PRIVATE_DETAIL' not in capsys.readouterr().err


@pytest.mark.parametrize('status', ['ready-for-review', 'needs-attention'])
@pytest.mark.parametrize('failure', ['render', 'publish', 'race'])
def test_cli_export_failure_returns_one_without_rerun(tmp_path, monkeypatch, capsys, status, failure):
    from coding_agent_harness import report
    repo, task_file = inputs(tmp_path)
    destination = tmp_path / 'report.md'
    count = []
    result = sample(status)
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    def run(**kw):
        count.append(1)
        if failure == 'race': destination.write_text('concurrent winner')
        return result
    monkeypatch.setattr(cli, 'run_task', run)
    def fail(*a, **kw): raise OSError('PRIVATE_EXPORT_DETAIL')
    if failure == 'render': monkeypatch.setattr(report, 'render_run_report', fail)
    if failure == 'publish': monkeypatch.setattr(report, 'publish_run_report', fail)
    assert cli.app(args(repo, task_file, destination)) == 1
    output = capsys.readouterr()
    assert count == [1]
    assert f'Status: {status}' in output.out
    assert 'Report publication failed' in output.err
    assert 'PRIVATE_EXPORT_DETAIL' not in output.err
    if failure == 'race': assert destination.read_text() == 'concurrent winner'
    else: assert not destination.exists()


@pytest.mark.parametrize('with_report', [False, True])
@pytest.mark.parametrize('complete', [False, True])
def test_real_offline_run_report_does_not_recollect_or_redispatch(tmp_path, monkeypatch, capsys, with_report, complete):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*argv):
        return subprocess.run(['git', *argv], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (repo / 'observed.py').write_text('before\n')
    git('add', '.')
    git('-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.test', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'baseline')
    baseline = git('rev-parse', 'HEAD')
    worker_script = tmp_path / 'worker.py'
    worker_script.write_text("from pathlib import Path\nPath('observed.py').write_text('after\\n')\n" + ("print('HARNESS_RESULT:' + " + repr(json.dumps({'status': 'completed', 'summary': 'claim', 'changed_files': ['observed.py']})) + ")\n" if complete else "print('no completion')\n"))
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: QwenWorker(command=[sys.executable, str(worker_script)], model='offline', timeout_seconds=30))
    counts = {}
    def track(owner, name):
        original = getattr(owner, name)
        def call(*a, **kw):
            if owner is report:
                assert 'run' not in counts, 'Git layout discovery occurred after worker dispatch'
            counts[name] = counts.get(name, 0) + 1
            return original(*a, **kw)
        monkeypatch.setattr(owner, name, call)
    for name in ('capture_baseline', 'capture_evidence', 'run_verification'):
        track(orchestrator, name)
    track(QwenWorker, 'run')
    track(report, 'run_process')
    spec = tmp_path / 'task.json'
    spec.write_text(json.dumps({'task': task().model_dump(), 'verification_commands': [{'id': 'content', 'argv': [sys.executable, '-c', "from pathlib import Path; assert Path('observed.py').read_text() == 'after\\n'"]}]}))
    destination = tmp_path / 'report.md'
    assert cli.app(args(repo, spec, destination if with_report else None)) == (0 if complete else 2)
    expected_counts = {'capture_baseline': 1, 'capture_evidence': 1, 'run_verification': 1, 'run': 1}
    if with_report: expected_counts['run_process'] = 1
    assert counts == expected_counts
    assert capsys.readouterr().err == ''
    assert destination.exists() == with_report
    if with_report:
        content = destination.read_text(encoding='utf-8')
        assert baseline in content and '+after' in content
        assert 'Completion unavailable' in content if not complete else 'claim' in content
    assert git('rev-parse', 'HEAD') == baseline
    assert git('diff', '--cached', '--name-only') == ''


def test_cli_report_rechecks_layout_after_worker(tmp_path, monkeypatch, capsys):
    repo, task_file = inputs(tmp_path)
    destination = tmp_path / 'report.md'
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    calls = []
    def run(**kw):
        calls.append(1)
        (repo / '.git').rename(repo / 'saved-metadata')
        return sample()
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(repo, task_file, destination)) == 1
    assert calls == [1] and not destination.exists()
    assert 'Report publication failed' in capsys.readouterr().err
