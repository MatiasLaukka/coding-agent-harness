"""Static metadata aliases and bounded, pre-dispatch inspection."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent_harness import report
from tests.test_report_layout import committed_repo, git, offline_cli, snapshot


def directory_alias(source, target):
    source.rename(target)
    try:
        source.symlink_to(target, target_is_directory=True)
    except OSError:
        target.rename(source)
        pytest.skip('directory symlink creation unavailable; alias security unverified')


@pytest.mark.parametrize('kind', ['objects', 'refs', 'nested', 'linked-admin', 'linked-common', 'linked-separate-admin'])
def test_metadata_alias_rejected_before_dispatch(tmp_path, monkeypatch, capsys, kind):
    repo = committed_repo(tmp_path)
    supplied = repo / 'src'
    linked = tmp_path / 'linked'
    if kind.startswith('linked'):
        git(repo, 'worktree', 'add', '--quiet', '--detach', str(linked), 'HEAD')
        supplied = linked / 'src'
    admin = Path(git(supplied, 'rev-parse', '--absolute-git-dir')).resolve()
    common = Path(git(supplied, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if kind == 'linked-separate-admin':
        moved = tmp_path / 'separate-admin'
        admin.rename(moved)
        (moved / 'commondir').write_text(str(common) + '\n')
        (linked / '.git').unlink()  # Git marks this fixture file hidden on Windows.
        (linked / '.git').write_text(f'gitdir: {moved}\n')
        admin = Path(git(supplied, 'rev-parse', '--absolute-git-dir')).resolve()
        assert admin == moved and not admin.is_relative_to(common)
    source = {
        'objects': common / 'objects',
        'refs': common / 'refs',
        'nested': common / 'logs' / 'refs' / 'heads',
        'linked-admin': admin / 'logs',
        'linked-common': common / 'refs',
        'linked-separate-admin': admin / 'logs',
    }[kind]
    external = tmp_path / 'external-metadata'
    directory_alias(source, external)
    destination = (external / 'info' / 'alternates' if kind == 'objects' else
                   external / 'heads' / 'report.md' if kind in ('refs', 'linked-common') else
                   external / 'report.md')
    assert git(supplied, 'cat-file', '-t', 'HEAD') == 'commit'
    assert git(supplied, 'log', '--all', '--oneline')
    status = git(supplied, 'status', '--porcelain')
    index = Path(git(supplied, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    before = (snapshot(repo), snapshot(external), snapshot(admin), snapshot(linked) if linked.exists() else {},
              index.read_bytes(), index.stat().st_mtime_ns)
    result = offline_cli(tmp_path, monkeypatch, supplied, destination)
    output = capsys.readouterr()
    print(json.dumps({'layout': kind, 'exit': result[0], 'setups': result[1],
                      'invocations': result[2], 'report_exists': destination.exists()}))
    assert result == (1, 0, 0)
    assert not destination.exists()
    assert 'Report destination error' in output.err
    assert (snapshot(repo), snapshot(external), snapshot(admin), snapshot(linked) if linked.exists() else {},
            index.read_bytes(), index.stat().st_mtime_ns) == before
    assert git(supplied, 'status', '--porcelain') == status
    assert git(supplied, 'log', '--all', '--oneline')
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('kind', ['internal', 'dangling', 'file'])
def test_other_metadata_symlinks_fail_closed(tmp_path, monkeypatch, kind):
    repo = committed_repo(tmp_path)
    metadata = repo / '.git'
    alias = metadata / 'alias'
    target = metadata / 'objects' if kind == 'internal' else tmp_path / 'absent'
    if kind == 'file':
        target = tmp_path / 'config-target'
        target.write_text('# external metadata file\n')
    try:
        alias.symlink_to(target, target_is_directory=kind != 'file')
    except OSError:
        pytest.skip('symlink creation unavailable; alias security unverified')
    destination = tmp_path / 'external.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert not destination.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory junction test')
def test_metadata_directory_junction_rejected(tmp_path, monkeypatch):
    repo = committed_repo(tmp_path)
    target = tmp_path / 'junction-target'
    (repo / '.git' / 'refs').rename(target)
    source = repo / '.git' / 'refs'
    # Fixed shell purpose; all paths are disposable fixture paths passed as argv.
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(source), str(target)],
                   check=True, capture_output=True, timeout=10)
    destination = target / 'heads' / 'report.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert not destination.exists()


@pytest.mark.parametrize('failure', ['entries', 'depth', 'deadline', 'scandir', 'stat'])
def test_incomplete_metadata_inspection_stops_dispatch(tmp_path, monkeypatch, capsys, failure):
    repo = committed_repo(tmp_path)
    real_run = report.run_process
    calls = []
    def limited(**kwargs):
        if kwargs['argv'][0] == 'git':
            return real_run(**kwargs)
        calls.append(kwargs)
        assert kwargs['argv'][:2] == [sys.executable, '-I']
        assert kwargs['timeout_seconds'] == 10
        assert kwargs['deadline'] > 0
        assert kwargs['stdout_limit_bytes'] == 64
        assert kwargs['stderr_limit_bytes'] == 1024
        helper = kwargs['argv'][2]
        roots = kwargs['argv'][3:]
        code = f"import runpy; m=runpy.run_path({helper!r}); "
        if failure in ('scandir', 'stat'):
            code += "\n"
            code += "def fail(*a, **k): raise PermissionError('PRIVATE_INSPECTION_DETAIL')\n"
            code += f"m['os'].{failure}=fail\n"
        options = {'entries': ', max_entries=1', 'depth': ', max_depth=0',
                   'deadline': ', deadline=0'}.get(failure, '')
        code += (f"\ntry: m['inspect_metadata']({roots!r}{options})\n"
                 "except (ValueError, PermissionError) as exc:\n"
                 "    print(type(exc).__name__ + ':' + str(exc))\n"
                 "    raise SystemExit(7)\n")
        result = real_run(**{**kwargs, 'argv': [sys.executable, '-I', '-c', code]})
        expected = {'entries': 'ValueError:Metadata inspection entry limit',
                    'depth': 'ValueError:Metadata inspection depth',
                    'deadline': 'ValueError:Metadata inspection deadline',
                    'scandir': 'PermissionError:PRIVATE_INSPECTION_DETAIL',
                    'stat': 'PermissionError:PRIVATE_INSPECTION_DETAIL'}[failure]
        assert result.exit_code == 7 and result.stdout.strip() == expected
        return result
    monkeypatch.setattr(report, 'run_process', limited)
    index = repo / '.git' / 'index'
    before = snapshot(repo), index.read_bytes(), index.stat().st_mtime_ns
    destination = tmp_path / 'external.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert len(calls) == 1
    output = capsys.readouterr()
    assert 'Report destination error' in output.err
    assert 'PRIVATE_INSPECTION_DETAIL' not in output.out + output.err
    assert not destination.exists()
    assert (snapshot(repo), index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize('failure', ['timeout', 'truncated', 'incomplete', 'nonzero', 'malformed', 'cleanup'])
def test_metadata_helper_result_must_be_complete(tmp_path, monkeypatch, failure):
    from coding_agent_harness.process import ProcessResult
    repo = committed_repo(tmp_path)
    real_run = report.run_process
    changes = {
        'timeout': {'timed_out': True},
        'truncated': {'stdout_truncated': True},
        'incomplete': {'output_complete': False},
        'nonzero': {'exit_code': 1},
        'malformed': {'stdout': 'partial'},
        'cleanup': {'cleanup_errors': ['unconfirmed']},
    }[failure]
    def fail(**kwargs):
        if kwargs['argv'][0] == 'git':
            return real_run(**kwargs)
        return ProcessResult(argv=kwargs['argv'], exit_code=0, stdout='metadata-layout-ok\n',
                             stderr='', timed_out=False, elapsed_seconds=0).model_copy(update=changes)
    monkeypatch.setattr(report, 'run_process', fail)
    destination = tmp_path / 'external.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert not destination.exists()


def test_normal_external_report_and_frozen_preflight(tmp_path, monkeypatch):
    repo = committed_repo(tmp_path)
    real_run = report.run_process
    calls = []
    def track(**kwargs):
        calls.append(kwargs['argv'])
        return real_run(**kwargs)
    monkeypatch.setattr(report, 'run_process', track)
    destination = tmp_path / 'external.md'
    before = snapshot(repo)
    assert offline_cli(tmp_path, monkeypatch, repo / 'src', destination) == (0, 1, 1)
    assert destination.exists()
    assert len(calls) == 2
    assert calls[0][0] == 'git' and calls[1][:2] == [sys.executable, '-I']
    assert snapshot(repo) == before



def test_blocked_metadata_filesystem_call_is_bounded(tmp_path, monkeypatch, capsys):
    import time
    repo = committed_repo(tmp_path)
    real_run = report.run_process
    results = []
    def blocked(**kwargs):
        if kwargs['argv'][0] == 'git':
            return real_run(**kwargs)
        helper = kwargs['argv'][2]
        roots = kwargs['argv'][3:]
        code = (f"import runpy,time; m=runpy.run_path({helper!r}); real=m['os'].scandir\n"
                "def block(*a, **k):\n    time.sleep(20)\n    return real(*a, **k)\n"
                f"m['os'].scandir=block\nm['inspect_metadata']({roots!r})")
        result = real_run(**{**kwargs, 'argv': [sys.executable, '-I', '-c', code]})
        results.append(result)
        return result
    monkeypatch.setattr(report, 'run_process', blocked)
    destination = tmp_path / 'external.md'
    started = time.monotonic()
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert time.monotonic() - started < 15
    assert len(results) == 1 and results[0].timed_out
    assert results[0].cleanup_attempted
    assert not results[0].surviving_pids and not results[0].cleanup_errors
    assert not destination.exists()
    assert 'Report destination error' in capsys.readouterr().err



def test_canonical_metadata_root_alias_remains_supported(tmp_path, monkeypatch):
    repo = committed_repo(tmp_path)
    external = tmp_path / 'canonical-admin'
    directory_alias(repo / '.git', external)
    before = snapshot(repo), snapshot(external)
    destination = tmp_path / 'external.md'
    assert offline_cli(tmp_path, monkeypatch, repo / 'src', destination) == (0, 1, 1)
    assert destination.exists()
    assert (snapshot(repo), snapshot(external)) == before
