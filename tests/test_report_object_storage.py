"""External object-storage policy, exercised with real local Git clones."""
from __future__ import annotations

import builtins
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from coding_agent_harness import _report_metadata, orchestrator, report
from tests.test_report_layout import committed_repo, git, offline_cli, snapshot


def clones(tmp_path, kind='shared', relative=False):
    seed = committed_repo(tmp_path)
    store = tmp_path / 'object-store.git'
    git(seed, 'clone', '--quiet', '--bare', '--no-hardlinks', str(seed), str(store))
    consumer = tmp_path / 'consumer'
    options = ['--shared', str(store)] if kind == 'shared' else ['--reference', str(store), '--no-hardlinks', str(seed)]
    git(seed, 'clone', '--quiet', *options, str(consumer))
    alternates = consumer / '.git' / 'objects' / 'info' / 'alternates'
    assert alternates.is_file() and not alternates.is_symlink()
    if relative:
        alternates.write_text(os.path.relpath(store / 'objects', alternates.parent.parent).replace('\\', '/') + '\n', encoding='utf-8', newline='\n')
    probe = subprocess.run(['git', '--no-optional-locks', 'cat-file', '-t', 'HEAD'],
                           cwd=consumer, check=True, capture_output=True, text=True, timeout=10)
    assert probe.stdout.strip() == 'commit' and probe.stderr == ''
    return seed, store, consumer


def execution_counts(monkeypatch):
    counts = {}
    for name in ('capture_baseline', 'capture_evidence', 'run_verification'):
        original = getattr(orchestrator, name)
        def track(*args, _name=name, _original=original, **kwargs):
            counts[_name] = counts.get(_name, 0) + 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(orchestrator, name, track)
    return counts


def state(*roots):
    return tuple(snapshot(root) for root in roots)


@pytest.mark.parametrize('kind', ['shared', 'reference'])
@pytest.mark.parametrize('relative', [False, True])
def test_external_store_destination_rejected_before_dispatch(tmp_path, monkeypatch, capsys, kind, relative):
    seed, store, consumer = clones(tmp_path, kind, relative)
    destination = store / 'objects' / 'info' / 'alternates'
    assert not destination.exists()  # The unsafe destination is new, not a collision.
    status = git(consumer, 'status', '--porcelain')
    index = consumer / '.git' / 'index'
    before = state(seed, store, consumer), index.stat().st_mtime_ns
    counts = execution_counts(monkeypatch)
    result = offline_cli(tmp_path, monkeypatch, consumer, destination)
    output = capsys.readouterr()
    probe = subprocess.run(['git', '--no-optional-locks', 'fsck', '--full'], cwd=consumer,
                           capture_output=True, text=True, timeout=10)
    print(json.dumps({'clone': kind, 'relative': relative, 'exit': result[0], 'setups': result[1],
                      'workers': result[2], 'execution_calls': counts,
                      'report_exists': destination.exists(), 'object_probe_exit': probe.returncode,
                      'object_probe_clean': probe.returncode == 0 and probe.stderr == '',
                      'metadata_unchanged': (state(seed, store, consumer), index.stat().st_mtime_ns) == before}))
    assert result == (1, 0, 0)
    assert counts == {}
    assert 'Report destination error' in output.err
    assert not destination.exists()
    assert (state(seed, store, consumer), index.stat().st_mtime_ns) == before
    assert git(consumer, 'status', '--porcelain') == status
    assert probe.returncode == 0 and probe.stderr == ''
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('layout', ['linked', 'separate', 'linked-separate'])
def test_alternates_in_actual_common_metadata_rejected(tmp_path, monkeypatch, capsys, layout):
    seed, store, consumer = clones(tmp_path)
    metadata = consumer / '.git'
    if 'separate' in layout:
        metadata = tmp_path / 'separate-admin'
        git(consumer, 'init', '--quiet', '--separate-git-dir', str(metadata))
    target = consumer
    if layout.startswith('linked'):
        target = tmp_path / 'linked'
        git(consumer, 'worktree', 'add', '--quiet', '--detach', str(target), 'HEAD')
    common = Path(git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    assert common == metadata.resolve()
    assert (common / 'objects' / 'info' / 'alternates').is_file()
    index = Path(git(target, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    status = git(target, 'status', '--porcelain')
    roots = tuple(dict.fromkeys([seed, store, consumer, target, metadata]))
    before = state(*roots), index.read_bytes(), index.stat().st_mtime_ns
    counts = execution_counts(monkeypatch)
    destination = tmp_path / 'external-report.md'
    assert offline_cli(tmp_path, monkeypatch, target, destination) == (1, 0, 0)
    assert counts == {} and not destination.exists()
    assert (state(*roots), index.read_bytes(), index.stat().st_mtime_ns) == before
    assert git(target, 'status', '--porcelain') == status
    assert 'Report destination error' in capsys.readouterr().err
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('variable', ['GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'])
def test_nonempty_object_storage_environment_rejected_before_inspection(tmp_path, monkeypatch, capsys, variable):
    seed = committed_repo(tmp_path)
    store = tmp_path / 'object-store.git'
    git(seed, 'clone', '--quiet', '--bare', '--no-hardlinks', str(seed), str(store))
    destination = store / 'objects' / 'info' / 'alternates'
    assert not destination.exists()
    before = state(seed, store), (seed / '.git' / 'index').stat().st_mtime_ns
    counts = execution_counts(monkeypatch)
    def forbidden(**kwargs): pytest.fail('object override reached layout inspection')
    with monkeypatch.context() as context:
        context.setenv(variable, str(store / 'objects'))
        context.setattr(report, 'run_process', forbidden)
        assert offline_cli(tmp_path, context, seed, destination) == (1, 0, 0)
    assert counts == {} and not destination.exists()
    assert (state(seed, store), (seed / '.git' / 'index').stat().st_mtime_ns) == before
    assert git(seed, 'status', '--porcelain') == ''
    assert 'Report destination error' in capsys.readouterr().err
    assert not list(tmp_path.rglob('.harness-report-*'))


@pytest.mark.parametrize('contents', ['', 'not a parsed path\n', '../internal-store\n'])
def test_alternates_presence_rejected_without_reading_contents(tmp_path, monkeypatch, contents):
    repo = committed_repo(tmp_path)
    metadata = repo / '.git'
    (metadata / 'objects' / 'info' / 'alternates').write_text(contents)
    def forbidden(*args, **kwargs): pytest.fail('metadata inspector opened file content')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(io, 'open', forbidden)
    monkeypatch.setattr(os, 'open', forbidden)
    with pytest.raises(ValueError):
        _report_metadata.inspect_metadata([str(metadata)])


def test_http_alternates_presence_rejected(tmp_path, monkeypatch, capsys):
    repo = committed_repo(tmp_path)
    (repo / '.git' / 'objects' / 'info' / 'http-alternates').write_text('')
    before = snapshot(repo)
    counts = execution_counts(monkeypatch)
    destination = tmp_path / 'external-report.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (1, 0, 0)
    assert counts == {} and not destination.exists() and snapshot(repo) == before
    assert 'Report destination error' in capsys.readouterr().err


@pytest.mark.parametrize('kind', ['ordinary', 'dissociated', 'unrelated-name'])
def test_self_contained_object_store_remains_supported(tmp_path, monkeypatch, kind):
    repo = committed_repo(tmp_path)
    if kind == 'dissociated':
        store = tmp_path / 'object-store.git'
        git(repo, 'clone', '--quiet', '--bare', '--no-hardlinks', str(repo), str(store))
        consumer = tmp_path / 'consumer'
        git(repo, 'clone', '--quiet', '--reference', str(store), '--dissociate', '--no-hardlinks', str(repo), str(consumer))
        repo = consumer
    if kind == 'unrelated-name':
        (repo / '.git' / 'info' / 'alternates').write_text('not Git object metadata\n')
    assert not (repo / '.git' / 'objects' / 'info' / 'alternates').exists()
    before = snapshot(repo)
    counts = execution_counts(monkeypatch)
    destination = tmp_path / 'external-report.md'
    assert offline_cli(tmp_path, monkeypatch, repo, destination) == (0, 1, 1)
    assert counts == {'capture_baseline': 1, 'capture_evidence': 1, 'run_verification': 1}
    assert destination.is_file() and snapshot(repo) == before
