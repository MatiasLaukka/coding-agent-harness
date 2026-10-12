"""Worker dispatch counts are independent of stages and completed records."""
import json

import pytest

from coding_agent_harness import cli, controller, orchestrator
from coding_agent_harness.models import ReviewDecision
from coding_agent_harness.process import run_process
from tests.test_git import make_repo
from tests.test_reliability_r2 import CountingWorker, Reviewer, task

SECRET = 'R3_COUNT_FAKE_SECRET_18b274'


def reported_count(capsys, failure):
    cli._print_failure_evidence(failure)
    stderr = capsys.readouterr().err
    assert SECRET not in stderr
    line = next(line for line in stderr.splitlines() if line.startswith('Failure evidence: '))
    return json.loads(line.removeprefix('Failure evidence: '))['attempt_count']


@pytest.mark.parametrize('mode', ['single', 'controller'])
@pytest.mark.parametrize('fault, expected', [('before', 0), ('lookup', 0), ('launch', 1), ('after', 1)])
def test_native_invocation_count_tracks_dispatch_not_completion(
        tmp_path, monkeypatch, capsys, mode, fault, expected):
    repo = make_repo(tmp_path)
    module = controller if mode == 'controller' else orchestrator
    class Worker(CountingWorker):
        def run(self, **kwargs):
            if fault == 'launch':
                self.calls += 1
                process = run_process(argv=[str(tmp_path / 'missing-executable')],
                                      cwd=repo, timeout_seconds=1)
                failure = FileNotFoundError(SECRET)
                failure.process_result = process
                raise failure
            return super().run(**kwargs)
    class LookupFailure:
        calls = 0
        @property
        def run(self):
            raise RuntimeError(SECRET)
    worker = LookupFailure() if fault == 'lookup' else Worker()
    def broken(*args, **kwargs):
        raise RuntimeError(SECRET)
    if fault == 'before':
        monkeypatch.setattr(module, 'capture_worktree_snapshot' if mode == 'controller'
                            else 'capture_baseline', broken)
    elif fault == 'after':
        monkeypatch.setattr(module, 'run_verification', broken)
    with pytest.raises((RuntimeError, FileNotFoundError)) as caught:
        if mode == 'controller':
            controller.HarnessController(worker=worker, reviewer=Reviewer(
                ReviewDecision(decision='accept', rationale='Offline'))).run(
                    task=task(), repo_dir=repo, verification_commands=[])
        else:
            orchestrator.run_task(task=task(), repo_dir=repo, worker=worker, verification_commands=[])
    assert worker.calls == expected
    assert reported_count(capsys, caught.value) == expected
    evidence = caught.value.harness_evidence
    assert evidence['worker_invocation_count'] == expected
    record = evidence['attempts'][0] if mode == 'controller' else evidence
    if fault == 'launch':
        assert record['failed_process'].started is False
        assert record['failed_process'].exit_code is None
        assert record['failed_process'].failure_type == 'FileNotFoundError'
    elif fault == 'after':
        assert record['worker_result'].process.exit_code == 0
    if mode == 'controller' and fault == 'before':
        assert len(evidence['attempts']) == 1
        assert 'worker_result' not in record


@pytest.mark.parametrize('fault, expected', [('snapshot', 1), ('worker', 2)])
def test_subsequent_dispatch_count_includes_failed_call_but_not_snapshot(
        tmp_path, monkeypatch, capsys, fault, expected):
    repo = make_repo(tmp_path)
    class Worker(CountingWorker):
        def run(self, **kwargs):
            if self.calls == 1 and fault == 'worker':
                self.calls += 1
                raise RuntimeError(SECRET)
            return super().run(**kwargs)
    worker = Worker()
    original = controller.capture_worktree_snapshot
    snapshots = 0
    def snapshot(*args, **kwargs):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2 and fault == 'snapshot':
            raise RuntimeError(SECRET)
        return original(*args, **kwargs)
    monkeypatch.setattr(controller, 'capture_worktree_snapshot', snapshot)
    reviewer = Reviewer(ReviewDecision(decision='remediate', rationale='One authorized retry', remediation='Again'))
    with pytest.raises(RuntimeError) as caught:
        controller.HarnessController(worker=worker, reviewer=reviewer).run(
            task=task(), repo_dir=repo, verification_commands=[])
    assert worker.calls == expected
    assert reported_count(capsys, caught.value) == expected
    evidence = caught.value.harness_evidence
    assert evidence['worker_invocation_count'] == expected
    assert len(evidence['attempts']) == 2
    assert evidence['attempts'][0]['verification'].passed is True
    assert 'worker_result' not in evidence['attempts'][1]


@pytest.mark.parametrize('evidence, expected', [
    ({'stage': 'attempt-snapshot'}, None),
    ({'stage': 'worker', 'attempts': [{}]}, None),
    ({'stage': 'verification', 'attempts': []}, None),
    ({'stage': 'worker', 'attempts': [{'attempt': 2}]}, None),
    ({'stage': 'worker', 'worker_invocation_count': 3}, 3),
    ({'stage': 'attempt-snapshot', 'worker_invocation_count': 0, 'attempts': [{}]}, 0),
    ({'worker_invocation_count': 2, 'attempts': [{}]}, 2),
    ({'worker_invocation_count': 2, 'attempts': []}, 2),
    ({'worker_invocation_count': 2}, 2),
    ({'stage': 'worker', 'worker_invocation_count': True}, None),
    ({'stage': 'worker', 'worker_invocation_count': -1}, None),
    ({'stage': 'worker', 'worker_invocation_count': 1.5}, None),
    ({'stage': 'worker', 'worker_invocation_count': SECRET}, None),
])
def test_missing_or_partial_records_do_not_replace_authoritative_count(capsys, evidence, expected):
    failure = RuntimeError(SECRET)
    failure.harness_evidence = evidence
    assert reported_count(capsys, failure) == expected
    assert failure.harness_evidence is evidence


def test_cli_delegate_without_native_count_reports_unknown(tmp_path, monkeypatch, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    spec = tmp_path / 'task.json'
    spec.write_text(json.dumps({'task': task().model_dump()}))
    worker = CountingWorker()
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kwargs: worker)
    def delegate(**kwargs):
        worker.run(**kwargs)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(cli, 'run_task', delegate)
    assert cli.app(['run', str(spec), '--repo', str(repo), '--model', 'offline']) == 1
    stderr = capsys.readouterr().err
    assert SECRET not in stderr
    assert worker.calls == 1
    payload = json.loads(next(line.removeprefix('Failure evidence: ')
        for line in stderr.splitlines() if line.startswith('Failure evidence: ')))
    assert payload['attempt_count'] is None
