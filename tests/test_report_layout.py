"""Real Git layout regressions for saved-report containment."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent_harness import cli, report
from coding_agent_harness.worker import QwenWorker
from tests.test_report import task


def git(repo, *argv):
    return subprocess.run(
        ['git', '--no-optional-locks', *argv], cwd=repo, check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()


def committed_repo(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'src').mkdir(parents=True)
    (repo / 'src' / 'observed.py').write_text('before\n', encoding='utf-8')
    git(repo, 'init', '-q')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.test',
        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'baseline')
    return repo


def snapshot(repo):
    return {str(path.relative_to(repo)): path.read_bytes()
            for path in repo.rglob('*') if path.is_file()}


def offline_cli(tmp_path, monkeypatch, repo, destination):
    spec = tmp_path / 'task.json'
    packet = task()
    packet.allowed_files = ['src/observed.py']
    spec.write_text(json.dumps({'task': packet.model_dump(), 'verification_commands': []}))
    count = tmp_path / 'worker-count.txt'
    script = tmp_path / 'offline_worker.py'
    script.write_text(
        'from pathlib import Path\n'
        f'Path({str(count)!r}).write_text("invoked")\n'
        'print(' + repr('HARNESS_RESULT:' + json.dumps({
            'status': 'completed', 'summary': 'No changes', 'changed_files': [],
        })) + ')\n', encoding='utf-8',
    )
    setups = []
    def worker(**kwargs):
        setups.append(1)
        return QwenWorker(command=[sys.executable, str(script)], model='offline', timeout_seconds=30)
    monkeypatch.setattr(cli, 'QwenWorker', worker)
    code = cli.app(['run', str(spec), '--repo', str(repo), '--model', 'offline',
                    '--report', str(destination)])
    return code, len(setups), int(count.exists())


def test_misleading_nested_git_cli_rejected_before_worker(tmp_path, monkeypatch, capsys):
    repo = committed_repo(tmp_path)
    supplied = repo / 'src'
    (supplied / '.git').mkdir()
    assert Path(git(supplied, 'rev-parse', '--show-toplevel')).resolve() == repo.resolve()
    before = snapshot(repo)
    destination = repo / 'inside-target.md'
    code, setups, invocations = offline_cli(tmp_path, monkeypatch, supplied, destination)
    output = capsys.readouterr()
    print(json.dumps({'exit': code, 'worker_setups': setups, 'worker_invocations': invocations,
                      'report_exists': destination.exists(), 'git_status': git(repo, 'status', '--porcelain')}))
    assert code == 1
    assert setups == invocations == 0
    assert 'Report destination error' in output.err
    assert not destination.exists()
    assert snapshot(repo) == before
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('nested', [False, True])
def test_core_worktree_redirect_protects_both_roots(tmp_path, nested):
    repo = committed_repo(tmp_path)
    redirected = tmp_path / 'redirected'
    (redirected / 'src').mkdir(parents=True)
    (redirected / 'src' / 'observed.py').write_text('before\n')
    git(repo, 'config', 'core.worktree', str(redirected))
    supplied = repo / 'src' if nested else repo
    assert Path(git(supplied, 'rev-parse', '--show-toplevel')).resolve() == redirected.resolve()
    before = snapshot(repo), snapshot(redirected)
    for root in (repo, supplied, redirected, repo / '.git'):
        with pytest.raises((ValueError, OSError)):
            report.validate_report_destination(root / 'inside.md', repo_dir=supplied)
    assert report.validate_report_destination(tmp_path / 'external.md', repo_dir=supplied) == tmp_path / 'external.md'
    assert (snapshot(repo), snapshot(redirected)) == before


def test_real_linked_gitfile_and_common_metadata_exclusions(tmp_path):
    repo = committed_repo(tmp_path)
    linked = tmp_path / 'linked'
    git(repo, 'worktree', 'add', '--quiet', '--detach', str(linked), 'HEAD')
    assert (linked / '.git').is_file()
    admin = Path(git(linked, 'rev-parse', '--absolute-git-dir')).resolve()
    common = Path(git(linked, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    before = snapshot(repo), snapshot(linked)
    for root in (linked, linked / 'src', admin, common):
        with pytest.raises((ValueError, OSError)):
            report.validate_report_destination(root / 'inside.md', repo_dir=linked / 'src')
    assert report.validate_report_destination(tmp_path / 'external.md', repo_dir=linked / 'src') == tmp_path / 'external.md'
    assert (snapshot(repo), snapshot(linked)) == before


@pytest.mark.parametrize('kind', ['normal', 'misleading', 'redirect', 'linked'])
def test_external_layout_validation_does_not_touch_index(tmp_path, kind):
    repo = committed_repo(tmp_path)
    supplied = repo / 'src'
    if kind == 'misleading': (supplied / '.git').mkdir()
    if kind == 'redirect':
        redirected = tmp_path / 'redirected'
        redirected.mkdir()
        git(repo, 'config', 'core.worktree', str(redirected))
    if kind == 'linked':
        linked = tmp_path / 'linked'
        git(repo, 'worktree', 'add', '--quiet', '--detach', str(linked), 'HEAD')
        supplied = linked / 'src'
    index = Path(git(supplied, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    before = index.read_bytes(), index.stat().st_mtime_ns, snapshot(repo)
    destination = tmp_path / 'external.md'
    assert report.validate_report_destination(destination, repo_dir=supplied) == destination
    assert (index.read_bytes(), index.stat().st_mtime_ns, snapshot(repo)) == before
    assert not destination.exists()


def test_configured_fsmonitor_not_executed_by_layout_check(tmp_path):
    repo = committed_repo(tmp_path)
    marker = tmp_path / 'hook-invoked'
    git(repo, 'config', 'core.fsmonitor', f'echo invoked > "{marker}"')
    assert report.validate_report_destination(tmp_path / 'external.md', repo_dir=repo) == tmp_path / 'external.md'
    assert not marker.exists()


@pytest.mark.parametrize('relative', [False, True])
def test_separate_gitfile_core_worktree_redirect(tmp_path, relative):
    repo = committed_repo(tmp_path)
    metadata = tmp_path / 'metadata'
    git(repo, 'init', '--quiet', '--separate-git-dir', str(metadata))
    redirected = tmp_path / 'redirected'
    (redirected / 'src').mkdir(parents=True)
    (redirected / 'src' / 'observed.py').write_text('before\n')
    if relative:
        # Git marks the generated gitfile hidden on Windows; replace this owned
        # fixture file rather than opening that hidden file with CREATE_ALWAYS.
        (repo / '.git').unlink()
        (repo / '.git').write_text('gitdir: ../metadata\n')
    git(repo, 'config', 'core.worktree', '../redirected' if relative else str(redirected))
    supplied = repo / 'src'
    assert Path(git(supplied, 'rev-parse', '--show-toplevel')).resolve() == redirected.resolve()
    before = snapshot(repo), snapshot(metadata), snapshot(redirected)
    for root in (repo, supplied, redirected, metadata):
        with pytest.raises((ValueError, OSError)):
            report.validate_report_destination(root / 'inside.md', repo_dir=supplied)
    assert report.validate_report_destination(tmp_path / 'external.md', repo_dir=supplied) == tmp_path / 'external.md'
    assert (snapshot(repo), snapshot(metadata), snapshot(redirected)) == before


@pytest.mark.parametrize('kind', ['nested-file', 'missing-redirect', 'bare'])
def test_real_invalid_layout_rejected_before_setup(tmp_path, monkeypatch, capsys, kind):
    repo = committed_repo(tmp_path)
    supplied = repo / 'src'
    if kind == 'nested-file': (supplied / '.git').write_text('not a gitfile')
    if kind == 'missing-redirect': git(repo, 'config', 'core.worktree', str(tmp_path / 'absent'))
    if kind == 'bare':
        supplied = tmp_path / 'bare'
        subprocess.run(['git', 'init', '--bare', '-q', str(supplied)], check=True, capture_output=True, timeout=10)
    destination = tmp_path / 'external.md'
    before = snapshot(repo)
    assert offline_cli(tmp_path, monkeypatch, supplied, destination) == (1, 0, 0)
    assert 'Report destination error' in capsys.readouterr().err
    assert not destination.exists() and snapshot(repo) == before
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('failure', ['timeout', 'nonzero', 'truncated', 'incomplete', 'extra-line', 'relative', 'missing-root'])
def test_bounded_query_failures_are_sanitized_and_stop_dispatch(tmp_path, monkeypatch, capsys, failure):
    from coding_agent_harness.process import ProcessResult
    repo = committed_repo(tmp_path)
    admin = repo / '.git'
    stdout = f'{repo}\n{admin}\n{admin}\n'
    changes = {}
    if failure == 'timeout': changes['timed_out'] = True
    if failure == 'nonzero': changes['exit_code'] = 128
    if failure == 'truncated': changes['stdout_truncated'] = True
    if failure == 'incomplete': changes['output_complete'] = False
    if failure == 'extra-line': stdout += 'PRIVATE_LAYOUT_DETAIL\n'
    if failure == 'relative': stdout = f'relative\n{admin}\n{admin}\n'
    if failure == 'missing-root': stdout = f'{tmp_path / "absent"}\n{admin}\n{admin}\n'
    process = ProcessResult(argv=['git'], exit_code=0, stdout=stdout, stderr='PRIVATE_LAYOUT_DETAIL', timed_out=False, elapsed_seconds=0)
    process = process.model_copy(update=changes)
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        assert kwargs['argv'] == ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                                  'rev-parse', '--path-format=absolute', '--show-toplevel',
                                  '--absolute-git-dir', '--git-common-dir']
        assert kwargs['timeout_seconds'] == 10
        assert kwargs['deadline'] > 0
        assert kwargs['stdout_limit_bytes'] == 16 * 1024
        assert kwargs['stderr_limit_bytes'] == 4 * 1024
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        return process
    monkeypatch.setattr(report, 'run_process', query)
    destination = tmp_path / 'external.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert len(calls) == 1
    output = capsys.readouterr()
    assert 'Report destination error' in output.err
    assert 'PRIVATE_LAYOUT_DETAIL' not in output.err + output.out
    assert not destination.exists() and not list(tmp_path.rglob('.harness-report-*'))


def test_redirect_with_nested_metadata_alias_keeps_metadata_owner_root(tmp_path):
    repo = committed_repo(tmp_path)
    redirected = tmp_path / 'redirected'
    redirected.mkdir()
    git(repo, 'config', 'core.worktree', str(redirected))
    supplied = repo / 'src'
    try:
        (supplied / '.git').symlink_to(repo / '.git', target_is_directory=True)
    except OSError:
        pytest.skip('directory symlink creation unavailable')
    assert Path(git(supplied, 'rev-parse', '--show-toplevel')).resolve() == redirected.resolve()
    with pytest.raises(ValueError):
        report.validate_report_destination(repo / 'inside.md', repo_dir=supplied)
