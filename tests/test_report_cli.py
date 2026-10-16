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
    return result + ['--report-dir', str(destination)] if destination is not None else result


@pytest.mark.parametrize('status,expected', [('ready-for-review', 0), ('needs-attention', 2)])
def test_cli_report_retains_console_exit_and_single_dispatch(tmp_path, monkeypatch, capsys, status, expected):
    repo, task_file = inputs(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    destination = directory / 'run-fixed.md'
    monkeypatch.setattr(report.uuid, 'uuid4', lambda: type('Name', (), {'hex': 'fixed'})())
    result = sample(status)
    calls = []
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: calls.append('setup'))
    def run(**kw):
        calls.append('run')
        return result
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(repo, task_file)) == expected
    plain = capsys.readouterr()
    assert cli.app(args(repo, task_file, directory)) == expected
    saved = capsys.readouterr()
    assert saved.out == plain.out
    assert saved.err == plain.err + 'Report saved: ' + json.dumps(str(destination.resolve())) + '\n'
    assert list(directory.iterdir()) == [destination]
    assert calls == ['setup', 'run', 'setup', 'run']
    assert f'Status: {status}' in destination.read_text(encoding='utf-8')


@pytest.mark.parametrize('kind', ['inside', 'file', 'missing', 'permission', 'inaccessible', 'git-layout', 'collision'])
def test_cli_invalid_report_prevents_worker_setup(tmp_path, monkeypatch, capsys, kind):
    repo, task_file = inputs(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    if kind == 'inside': directory = repo
    if kind == 'file':
        directory = tmp_path / 'file'
        directory.write_text('keep')
    if kind == 'missing': directory = tmp_path / 'absent'
    if kind == 'permission': monkeypatch.setattr(report.os, 'access', lambda *a: False)
    if kind == 'inaccessible':
        resolve = Path.resolve
        def inaccessible(path, *a, **kw):
            if path == directory: raise PermissionError('PRIVATE_PATH')
            return resolve(path, *a, **kw)
        monkeypatch.setattr(Path, 'resolve', inaccessible)
    if kind == 'git-layout': (repo / '.git').rename(repo / 'saved-metadata')
    if kind == 'collision':
        monkeypatch.setattr(report.uuid, 'uuid4', lambda: type('Name', (), {'hex': 'fixed'})())
        (directory / 'run-fixed.md').write_text('winner')
    def forbidden(**kwargs): pytest.fail('worker setup for invalid report directory')
    monkeypatch.setattr(cli, 'QwenWorker', forbidden)
    monkeypatch.setattr(cli, 'run_task', forbidden)
    assert cli.app(args(repo, task_file, directory)) == 1
    output = capsys.readouterr()
    assert not output.out and 'Report destination error' in output.err
    assert 'PRIVATE_PATH' not in output.err
    assert not list(tmp_path.rglob('.harness-report-*'))
    if kind == 'missing': assert not directory.exists()
    if kind == 'file': assert directory.read_text() == 'keep'
    if kind == 'collision': assert (directory / 'run-fixed.md').read_text() == 'winner'


@pytest.mark.parametrize('failure', [RuntimeError('PRIVATE_DETAIL'), KeyboardInterrupt()])
def test_cli_no_completed_report_after_execution_raises(tmp_path, monkeypatch, capsys, failure):
    repo, task_file = inputs(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    destination = directory / 'run-fixed.md'
    monkeypatch.setattr(report.uuid, 'uuid4', lambda: type('Name', (), {'hex': 'fixed'})())
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    def run(**kw): raise failure
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(repo, task_file, directory)) == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    assert not destination.exists()
    assert not list(directory.glob('.harness-report-*'))
    assert 'PRIVATE_DETAIL' not in capsys.readouterr().err


@pytest.mark.parametrize('status', ['ready-for-review', 'needs-attention'])
@pytest.mark.parametrize('failure', ['render', 'publish', 'race', 'cleanup'])
def test_cli_export_failure_returns_one_without_rerun(tmp_path, monkeypatch, capsys, status, failure):
    from coding_agent_harness import report
    repo, task_file = inputs(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    destination = directory / 'run-fixed.md'
    monkeypatch.setattr(report.uuid, 'uuid4', lambda: type('Name', (), {'hex': 'fixed'})())
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
    if failure == 'cleanup':
        unlink = Path.unlink
        def blocked(path, *a, **kw):
            if path.name.startswith('.harness-report-'): raise PermissionError('PRIVATE_EXPORT_DETAIL')
            return unlink(path, *a, **kw)
        monkeypatch.setattr(Path, 'unlink', blocked)
    assert cli.app(args(repo, task_file, directory)) == 1
    output = capsys.readouterr()
    assert count == [1]
    assert f'Status: {status}' in output.out
    assert 'Report publication failed' in output.err
    assert 'PRIVATE_EXPORT_DETAIL' not in output.err
    if failure == 'race': assert destination.read_text() == 'concurrent winner'
    elif failure == 'cleanup':
        assert destination.read_text(encoding='utf-8').startswith('# Completed single-run report')
        for temporary in directory.glob('.harness-report-*'): unlink(temporary)
    else: assert not destination.exists()
    assert 'Report saved:' not in output.err


@pytest.mark.parametrize('report_mode', ['off', 'save', 'fail'])
@pytest.mark.parametrize('complete', [False, True])
def test_real_offline_run_report_does_not_recollect_or_redispatch(tmp_path, monkeypatch, capsys, report_mode, complete):
    with_report = report_mode != 'off'
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
            key = name
            if owner is report:
                assert kw['argv'][0] == 'git'
                key = 'layout_query'
            counts[key] = counts.get(key, 0) + 1
            return original(*a, **kw)
        monkeypatch.setattr(owner, name, call)
    for name in ('capture_baseline', 'capture_evidence', 'run_verification'):
        track(orchestrator, name)
    track(QwenWorker, 'run')
    track(report, 'run_process')
    spec = tmp_path / 'task.json'
    spec.write_text(json.dumps({'task': task().model_dump(), 'verification_commands': [{'id': 'content', 'argv': [sys.executable, '-c', "from pathlib import Path; assert Path('observed.py').read_text() == 'after\\n'"]}]}))
    directory = tmp_path / 'reports'
    directory.mkdir()
    destination = directory / 'run-fixed.md'
    monkeypatch.setattr(report.uuid, 'uuid4', lambda: type('Name', (), {'hex': 'fixed'})())
    if report_mode == 'fail':
        def fail(*a, **kw): raise OSError('publication unavailable')
        monkeypatch.setattr(report, 'publish_run_report', fail)
    expected_exit = 1 if report_mode == 'fail' else (0 if complete else 2)
    assert cli.app(args(repo, spec, directory if with_report else None)) == expected_exit
    expected_counts = {'capture_baseline': 1, 'capture_evidence': 1, 'run_verification': 1, 'run': 1}
    if with_report: expected_counts.update(layout_query=1)
    assert counts == expected_counts
    stderr = capsys.readouterr().err
    assert ('Report saved:' in stderr) == (report_mode == 'save')
    assert ('Report publication failed' in stderr) == (report_mode == 'fail')
    assert destination.exists() == (report_mode == 'save')
    if report_mode == 'save':
        content = destination.read_text(encoding='utf-8')
        assert baseline in content and '+after' in content
        assert 'Completion unavailable' in content if not complete else 'claim' in content
    assert git('rev-parse', 'HEAD') == baseline
    assert git('diff', '--cached', '--name-only') == ''


def test_nonreporting_run_has_no_report_only_restrictions(tmp_path, monkeypatch, capsys):
    repo, task_file = inputs(tmp_path)
    nested = repo / 'src'
    nested.mkdir()
    monkeypatch.setenv('GIT_OBJECT_DIRECTORY', 'unsupported-for-reporting')
    monkeypatch.setattr(report, 'run_process', lambda **kw: pytest.fail('report layout query'))
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    calls = []
    def run(**kw):
        calls.append(kw['repo_dir'])
        return sample()
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(nested, task_file)) == 0
    assert calls == [nested.resolve()]
    assert 'Report saved:' not in capsys.readouterr().err


def test_lost_output_directory_fails_without_requery(tmp_path, monkeypatch, capsys):
    repo, task_file = inputs(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    calls = []
    def run(**kw):
        calls.append(1)
        directory.rmdir()
        monkeypatch.setattr(report, 'run_process', lambda **kw: pytest.fail('post-dispatch query'))
        return sample()
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(args(repo, task_file, directory)) == 1
    assert calls == [1] and not directory.exists()
    assert 'Report publication failed' in capsys.readouterr().err


def test_generated_names_ignore_untrusted_text_and_use_canonical_stderr(tmp_path, monkeypatch, capsys):
    import re
    repo, task_file = inputs(tmp_path)
    directory = tmp_path / 'reports with spaces π'
    directory.mkdir()
    packet = task()
    packet.task_id = '../CON:attack'
    packet.goal = 'evil.md'
    task_file.write_text(json.dumps({'task': packet.model_dump(), 'verification_commands': []}))
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: object())
    monkeypatch.setattr(cli, 'run_task', lambda **kw: sample())
    for _ in range(2):
        assert cli.app(args(repo, task_file, directory / '.')) == 0
        stderr = capsys.readouterr().err
        saved = Path(json.loads(stderr.removeprefix('Report saved: ').strip()))
        assert saved.parent == directory.resolve()
        assert re.fullmatch(r'run-[0-9a-f]{32}\.md', saved.name, re.ASCII)
        assert saved.is_file()
    assert len(list(directory.iterdir())) == 2
