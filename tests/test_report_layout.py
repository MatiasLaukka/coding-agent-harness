"""Direct-root mistake prevention, not arbitrary Git dependency discovery."""
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
                    '--report-dir', str(destination)])
    return code, len(setups), int(count.exists())


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
    destination = tmp_path / 'reports'
    destination.mkdir()
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert len(calls) == 1
    output = capsys.readouterr()
    assert 'Report destination error' in output.err
    assert 'PRIVATE_LAYOUT_DETAIL' not in output.err + output.out
    assert not list(destination.iterdir())



@pytest.mark.parametrize('kind', ['equal', 'inside', 'ancestor', 'metadata', 'metadata-child'])
def test_direct_root_overlap_rejected_before_setup(tmp_path, monkeypatch, capsys, kind):
    repo = committed_repo(tmp_path)
    directory = {'equal': repo, 'inside': repo / 'src', 'ancestor': tmp_path,
                 'metadata': repo / '.git', 'metadata-child': repo / '.git' / 'objects'}[kind]
    before = snapshot(repo), (repo / '.git' / 'index').stat().st_mtime_ns
    assert offline_cli(tmp_path, monkeypatch, repo, directory) == (1, 0, 0)
    assert 'Report destination error' in capsys.readouterr().err
    assert (snapshot(repo), (repo / '.git' / 'index').stat().st_mtime_ns) == before
    assert not list(tmp_path.rglob('run-*.md'))
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('misleading', [False, True])
def test_subdirectory_rejected_even_with_external_directory(tmp_path, monkeypatch, misleading):
    repo = committed_repo(tmp_path)
    supplied = repo / 'src'
    if misleading: (supplied / '.git').mkdir()
    assert Path(git(supplied, 'rev-parse', '--show-toplevel')).resolve() == repo.resolve()
    directory = tmp_path / 'reports'
    directory.mkdir()
    before = snapshot(repo)
    assert offline_cli(tmp_path, monkeypatch, supplied, directory) == (1, 0, 0)
    assert snapshot(repo) == before and not list(directory.iterdir())


@pytest.mark.parametrize('layout', ['linked', 'separate', 'linked-separate'])
def test_gitfile_layouts_and_metadata_overlap(tmp_path, monkeypatch, layout):
    repo = committed_repo(tmp_path)
    if 'separate' in layout:
        # A dedicated parent lets us exercise ancestor overlap with external metadata.
        (tmp_path / 'admin-parent').mkdir()
        git(repo, 'init', '--quiet', '--separate-git-dir', str(tmp_path / 'admin-parent' / 'metadata'))
    target = repo
    if layout.startswith('linked'):
        target = tmp_path / 'linked'
        git(repo, 'worktree', 'add', '--quiet', '--detach', str(target), 'HEAD')
    assert (target / '.git').is_file()
    roots = report.discover_report_roots(target)
    admin = Path(git(target, 'rev-parse', '--absolute-git-dir')).resolve()
    common = Path(git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    assert roots == tuple(dict.fromkeys((target.resolve(), admin, common)))
    before = snapshot(repo), snapshot(target), snapshot(common)
    for directory in (admin, common, admin.parent, common.parent, common / 'objects'):
        with pytest.raises(ValueError): report.prepare_run_report(directory, repo_dir=target)
    directory = tmp_path / 'reports'
    directory.mkdir()
    assert offline_cli(tmp_path, monkeypatch, target, directory) == (0, 1, 1)
    assert len(list(directory.glob('run-*.md'))) == 1
    assert (snapshot(repo), snapshot(target), snapshot(common)) == before


@pytest.mark.parametrize('relative', [False, True])
def test_core_worktree_redirect_requires_actual_root(tmp_path, monkeypatch, relative):
    repo = committed_repo(tmp_path)
    metadata = tmp_path / 'metadata'
    git(repo, 'init', '--quiet', '--separate-git-dir', str(metadata))
    redirected = tmp_path / 'redirected'
    (redirected / 'src').mkdir(parents=True)
    (redirected / 'src' / 'observed.py').write_text('before\n')
    if relative:
        (repo / '.git').unlink()  # Git-generated gitfiles are hidden on Windows.
        (repo / '.git').write_text('gitdir: ../metadata\n')
    git(repo, 'config', 'core.worktree', '../redirected' if relative else str(redirected))
    directory = tmp_path / 'reports'
    directory.mkdir()
    before = snapshot(repo), snapshot(metadata), snapshot(redirected)
    assert offline_cli(tmp_path, monkeypatch, repo, directory) == (1, 0, 0)
    assert not list(directory.iterdir())
    # A Git-recognized marker at the actual root makes this a supported target.
    (redirected / '.git').write_text(f'gitdir: {metadata}\n')
    roots = report.discover_report_roots(redirected)
    assert roots[0] == redirected.resolve() and metadata.resolve() in roots
    with pytest.raises(ValueError): report.prepare_run_report(metadata, repo_dir=redirected)
    (redirected / '.git').unlink()
    assert (snapshot(repo), snapshot(metadata), snapshot(redirected)) == before


@pytest.mark.parametrize('kind', ['malformed', 'missing-admin', 'empty-common', 'bare'])
def test_invalid_real_layout_fails_before_setup(tmp_path, monkeypatch, kind):
    repo = committed_repo(tmp_path)
    if kind in ('malformed', 'missing-admin'):
        (repo / '.git').rename(repo / 'saved-metadata')
        (repo / '.git').write_text('invalid' if kind == 'malformed' else 'gitdir: absent\n')
    if kind == 'empty-common': (repo / '.git' / 'commondir').write_text('')
    if kind == 'bare':
        repo = tmp_path / 'bare'
        repo.mkdir()
        git(repo, 'init', '--bare', '-q')
    directory = tmp_path / 'reports'
    directory.mkdir()
    before = snapshot(repo)
    assert offline_cli(tmp_path, monkeypatch, repo, directory) == (1, 0, 0)
    assert snapshot(repo) == before and not list(directory.iterdir())


@pytest.mark.parametrize('variable', ['GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                                      'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'])
def test_nonempty_layout_overrides_fail_before_query(tmp_path, monkeypatch, variable):
    repo = committed_repo(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    before = snapshot(repo)
    monkeypatch.setenv(variable, 'unsupported')
    monkeypatch.setattr(report, 'run_process', lambda **kw: pytest.fail('query with unsupported override'))
    assert offline_cli(tmp_path, monkeypatch, repo, directory) == (1, 0, 0)
    assert snapshot(repo) == before and not list(directory.iterdir())


def test_query_is_read_only_and_fsmonitor_disabled(tmp_path):
    repo = committed_repo(tmp_path)
    marker = tmp_path / 'hook-invoked'
    git(repo, 'config', 'core.fsmonitor', f'echo invoked > "{marker}"')
    index = repo / '.git' / 'index'
    before = snapshot(repo), index.stat().st_mtime_ns
    directory = tmp_path / 'reports'
    directory.mkdir()
    plan = report.prepare_run_report(directory, repo_dir=repo)
    assert plan.directory == directory.resolve() and repo.resolve() in plan.protected_roots
    assert (snapshot(repo), index.stat().st_mtime_ns) == before
    assert not marker.exists() and not list(directory.iterdir())
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError): plan.directory = repo


@pytest.mark.parametrize('kind', ['symlink', 'junction'])
@pytest.mark.parametrize('protected', ['worktree', 'metadata', 'ancestor'])
def test_directory_aliases_to_known_roots_rejected(tmp_path, monkeypatch, kind, protected):
    repo = committed_repo(tmp_path)
    target = {'worktree': repo, 'metadata': repo / '.git', 'ancestor': tmp_path}[protected]
    alias = tmp_path / 'alias'
    if kind == 'symlink':
        try: alias.symlink_to(target, target_is_directory=True)
        except OSError: pytest.skip('directory symlink creation unavailable')
    else:
        if os.name != 'nt': pytest.skip('Windows junction test')
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)],
                       check=True, capture_output=True, timeout=10)
    try:
        assert offline_cli(tmp_path, monkeypatch, repo, alias) == (1, 0, 0)
        assert not list(repo.glob('run-*.md'))
    finally:
        # Remove only the alias, especially for the self-referencing ancestor fixture.
        if kind == 'junction': alias.rmdir()
        else: alias.unlink()


@pytest.mark.skipif(os.name != 'nt', reason='Windows case-insensitive canonical aliases')
def test_windows_case_and_dot_aliases(tmp_path):
    repo = committed_repo(tmp_path)
    for directory in (Path(str(repo).swapcase()), repo / 'src' / '..'):
        with pytest.raises(ValueError): report.prepare_run_report(directory, repo_dir=repo)
    directory = tmp_path / 'reports'
    directory.mkdir()
    plan = report.prepare_run_report(Path(str(directory).swapcase()) / '.', repo_dir=Path(str(repo).swapcase()))
    assert plan.directory == directory.resolve()


def test_canonical_root_and_report_aliases_supported(tmp_path, monkeypatch):
    repo = committed_repo(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    repo_alias, directory_alias = tmp_path / 'repo-alias', tmp_path / 'reports-alias'
    try:
        repo_alias.symlink_to(repo, target_is_directory=True)
        directory_alias.symlink_to(directory, target_is_directory=True)
    except OSError: pytest.skip('directory symlink creation unavailable')
    assert offline_cli(tmp_path, monkeypatch, repo_alias, directory_alias) == (0, 1, 1)
    assert len(list(directory.glob('run-*.md'))) == 1


def test_independent_directory_works_with_external_git_dependencies(tmp_path, monkeypatch):
    # These inputs are operator-owned; preflight deliberately does not scan them.
    seed = committed_repo(tmp_path)
    store = tmp_path / 'object-store.git'
    git(seed, 'clone', '--quiet', '--bare', str(seed), str(store))
    consumer = tmp_path / 'consumer'
    git(seed, 'clone', '--quiet', '--shared', str(store), str(consumer))
    assert (consumer / '.git' / 'objects' / 'info' / 'alternates').is_file()
    include = tmp_path / 'not-yet-created.conf'
    git(consumer, 'config', 'include.path', str(include))
    directory = tmp_path / 'reports'
    directory.mkdir()
    before = snapshot(seed), snapshot(store), snapshot(consumer)
    assert offline_cli(tmp_path, monkeypatch, consumer, directory) == (0, 1, 1)
    assert not include.exists() and len(list(directory.glob('run-*.md'))) == 1
    assert (snapshot(seed), snapshot(store), snapshot(consumer)) == before
    assert git(consumer, 'cat-file', '-t', 'HEAD') == 'commit'


def test_canonical_administrative_directory_alias(tmp_path, monkeypatch):
    repo = committed_repo(tmp_path)
    metadata = tmp_path / 'canonical-admin'
    (repo / '.git').rename(metadata)
    try: (repo / '.git').symlink_to(metadata, target_is_directory=True)
    except OSError: pytest.skip('directory symlink creation unavailable')
    directory = tmp_path / 'reports'
    directory.mkdir()
    roots = report.discover_report_roots(repo)
    assert metadata.resolve() in roots
    before = snapshot(metadata)
    with pytest.raises(ValueError): report.prepare_run_report(metadata, repo_dir=repo)
    assert offline_cli(tmp_path, monkeypatch, repo, directory) == (0, 1, 1)
    assert snapshot(metadata) == before and len(list(directory.glob('run-*.md'))) == 1


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended path aliases')
def test_windows_extended_path_aliases(tmp_path):
    repo = committed_repo(tmp_path)
    directory = tmp_path / 'reports'
    directory.mkdir()
    extended_repo = Path('//?/' + repo.resolve().as_posix())
    extended_directory = Path('//?/' + directory.resolve().as_posix())
    with pytest.raises(ValueError): report.prepare_run_report(extended_repo, repo_dir=repo)
    plan = report.prepare_run_report(extended_directory, repo_dir=extended_repo)
    assert plan.directory == directory.resolve()
    assert plan.protected_roots[0] == repo.resolve()
