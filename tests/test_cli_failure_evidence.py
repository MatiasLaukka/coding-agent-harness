"""Offline disclosure regressions at the CLI/Python evidence boundary."""
import hashlib
import json

import pytest

from coding_agent_harness import cli, orchestrator, verifier
from coding_agent_harness.controller import HarnessController
from coding_agent_harness.models import ReviewDecision, VerificationCommand
from coding_agent_harness.process import ProcessResult
from tests.test_git import make_repo
from tests.test_reliability_r2 import CountingWorker, Reviewer, task

MARKERS = {
    name: 'R3_FAKE_SECRET_' + name.upper() + '_98c427'
    for name in ('worker_stdout', 'worker_stderr', 'check_stdout', 'check_stderr',
                 'patch', 'exception', 'cleanup', 'task', 'arbitrary')
}


def summary(stderr):
    return json.loads(next(line.removeprefix('Failure evidence: ')
        for line in stderr.splitlines() if line.startswith('Failure evidence: ')))


@pytest.mark.parametrize('mode', ['single', 'controller'])
@pytest.mark.parametrize('error, code', [(RuntimeError, 1), (KeyboardInterrupt, 130)])
def test_cli_failure_hides_content_but_preserves_python_evidence(
        tmp_path, monkeypatch, capsys, mode, error, code):
    repo = make_repo(tmp_path)
    spec = tmp_path / 'task.json'
    commands = [VerificationCommand(id=MARKERS['task'], argv=['offline-first']),
                VerificationCommand(id='second', argv=['offline-second'])]
    spec.write_text(json.dumps({'task': task().model_dump(),
                               'verification_commands': [c.model_dump() for c in commands]}))
    class Worker(CountingWorker):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            (repo / 'existing.txt').write_text(MARKERS['patch'])
            result.process.stdout = MARKERS['worker_stdout']
            result.process.stderr = MARKERS['worker_stderr']
            result.process.output_complete = False
            result.process.stdout_truncated = True
            result.process.cleanup_attempted = True
            result.process.cleanup_errors = [MARKERS['cleanup']]
            return result
    worker = Worker()
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kwargs: worker)
    failure = error(MARKERS['exception'])
    calls = 0
    def verification_process(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return ProcessResult(argv=kwargs['argv'], exit_code=0, timed_out=False,
            stdout=MARKERS['check_stdout'], stderr=MARKERS['check_stderr'], elapsed_seconds=.01)
    monkeypatch.setattr(verifier, 'run_process', verification_process)
    observed = {}
    def run(**kwargs):
        try:
            if mode == 'controller':
                reviewer = Reviewer(ReviewDecision(decision='remediate', rationale='Offline', remediation='Again'))
                return HarnessController(worker=worker, reviewer=reviewer).run(
                    task=kwargs['task'], repo_dir=repo, verification_commands=commands)
            return orchestrator.run_task(**kwargs)
        except BaseException as exc:
            observed['exception'] = exc
            # Future or unrecognized evidence must never become public by recursion.
            exc.harness_evidence['arbitrary'] = {'secret': MARKERS['arbitrary']}
            raise
    monkeypatch.setattr(cli, 'run_task', run)
    assert cli.app(['run', str(spec), '--repo', str(repo), '--model', 'offline']) == code
    output = capsys.readouterr()
    leaked = [name for name, marker in MARKERS.items() if marker in output.err + output.out]
    assert not leaked, 'raw content disclosed: ' + ', '.join(leaked)
    evidence = observed['exception'].harness_evidence
    record = evidence['attempts'][0] if mode == 'controller' else evidence
    # Inspect the original native evidence after CLI reporting, including the same exception.
    assert observed['exception'] is failure
    assert str(failure) == MARKERS['exception']
    assert record['worker_result'].process.stdout == MARKERS['worker_stdout']
    assert record['worker_result'].process.stderr == MARKERS['worker_stderr']
    assert record['worker_result'].process.infrastructure_failed
    assert record['worker_result'].process.cleanup_errors == [MARKERS['cleanup']]
    assert MARKERS['patch'] in record['git_evidence'].patch
    assert record['verification'].passed is False
    assert len(record['verification'].checks) == 1
    check = record['verification'].checks[0]
    assert check.stdout == MARKERS['check_stdout']
    assert check.stderr == MARKERS['check_stderr']
    assert worker.calls == 1
    payload = summary(output.err)
    assert payload['stage'] == 'verification'
    assert payload['failure_type'] == error.__name__
    assert payload['attempt_count'] == 1
    projected = payload['attempts'][0] if mode == 'controller' else payload
    process = projected['worker_result']['process']
    assert process['started'] is True and process['exit_code'] == 0
    assert process['timed_out'] is False and process['infrastructure_failed'] is True
    assert process['output_complete'] is False and process['stdout_truncated'] is True
    assert process['cleanup_attempted'] is True and process['cleanup_error_count'] == 1
    assert process['stdout_sha256'] == hashlib.sha256(MARKERS['worker_stdout'].encode()).hexdigest()
    assert projected['verification']['passed'] is False
    assert projected['verification']['checks'][0]['stdout_sha256'] == hashlib.sha256(check.stdout.encode()).hexdigest()
    assert projected['git_evidence']['patch_sha256'] == hashlib.sha256(record['git_evidence'].patch.encode()).hexdigest()


@pytest.mark.parametrize('location', ['task-file', 'worker-setup'])
def test_cli_input_and_setup_exceptions_do_not_echo_sensitive_messages(
        tmp_path, monkeypatch, capsys, location):
    repo = tmp_path / 'repo'
    repo.mkdir()
    spec = tmp_path / 'task.json'
    if location == 'task-file':
        spec.write_text(json.dumps({'task': {'task_id': 'local',
            'goal': {'sensitive': MARKERS['exception']}, 'acceptance': ['checked']}}))
        def forbidden(**kwargs):
            raise AssertionError('worker must not be constructed')
        monkeypatch.setattr(cli, 'QwenWorker', forbidden)
    else:
        spec.write_text(json.dumps({'task': task().model_dump()}))
        def broken(**kwargs):
            raise RuntimeError(MARKERS['exception'])
        monkeypatch.setattr(cli, 'QwenWorker', broken)
    assert cli.app(['run', str(spec), '--repo', str(repo), '--model', 'offline']) == 1
    stderr = capsys.readouterr().err
    assert MARKERS['exception'] not in stderr
    payload = summary(stderr)
    assert payload['stage'] == location
    assert payload['attempt_count'] == 0


def test_cli_failure_allowlist_hides_unknown_labels_and_exception_type(capsys):
    secret = MARKERS['arbitrary']
    error_type = type(secret, (RuntimeError,), {})
    failure = error_type(MARKERS['exception'])
    process = ProcessResult(argv=[secret], exit_code=None, timed_out=True,
        stdout=secret, stderr=secret, elapsed_seconds=.1, failure_type=secret,
        cleanup_attempted=True, cleanup_errors=[secret], surviving_pids=[123])
    failure.harness_evidence = {'stage': secret, 'failure_type': secret,
        'failed_process': process, 'future_field': {'secret': secret}}
    cli._print_failure_evidence(failure)
    stderr = capsys.readouterr().err
    assert secret not in stderr and MARKERS['exception'] not in stderr
    payload = summary(stderr)
    assert payload['stage'] == 'unknown'
    assert payload['failure_type'] == 'other'
    assert payload['failed_process']['failure_type'] == 'other'
    assert payload['failed_process']['infrastructure_failed'] is True
    assert payload['failed_process']['surviving_pid_count'] == 1
    assert failure.harness_evidence['future_field']['secret'] == secret


@pytest.mark.parametrize('started, timed_out', [(False, False), (True, True)])
def test_cli_failure_summary_keeps_unknown_exit_and_launch_timeout_state(
        capsys, started, timed_out):
    failure = RuntimeError(MARKERS['exception'])
    process = ProcessResult(argv=[MARKERS['task']], started=started, exit_code=None,
        timed_out=timed_out, stdout=MARKERS['worker_stdout'], stderr=MARKERS['worker_stderr'],
        elapsed_seconds=.2, output_complete=False, stderr_truncated=True,
        failure_type='FileNotFoundError' if not started else None)
    failure.harness_evidence = {'stage': 'worker', 'failed_process': process}
    cli._print_failure_evidence(failure)
    stderr = capsys.readouterr().err
    assert all(marker not in stderr for marker in MARKERS.values())
    projected = summary(stderr)['failed_process']
    assert projected['started'] is started
    assert projected['exit_code'] is None
    assert projected['timed_out'] is timed_out
    assert projected['output_complete'] is False
    assert projected['stderr_truncated'] is True
    assert projected['infrastructure_failed'] is True
