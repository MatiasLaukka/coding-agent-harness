"""R3 regressions: finite local children and independent outer deadlines."""
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

import coding_agent_harness.process as process_module
import coding_agent_harness.verifier as verifier_module
import coding_agent_harness.controller as controller_module
from coding_agent_harness.process import run_process
from coding_agent_harness.models import VerificationCommand, ReviewDecision
from coding_agent_harness.verifier import run_verification
from coding_agent_harness.controller import HarnessController
from coding_agent_harness.worker import QwenWorker, classify_worker_run
from tests.test_git import make_repo
from tests.test_reliability_r2 import CountingWorker, Reviewer, task


def isolated_probe(tmp_path, body):
    """Do not let code under test define the test's own safety deadline."""
    script = tmp_path / 'probe.py'
    source_root = str(Path(process_module.__file__).resolve().parents[1])
    script.write_text('import sys\nsys.path.insert(0, ' + repr(source_root) + ')\n' + body, encoding='utf-8')
    runner = subprocess.Popen([sys.executable, str(script)], cwd=Path.cwd(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = runner.communicate(timeout=8)
        assert runner.returncode == 0, stderr
        return json.loads(stdout)
    finally:
        # Only identities created by this probe; every descendant also self-expires.
        for pid_file in tmp_path.glob('*.pid'):
            try:
                identity = json.loads(pid_file.read_text())
                child = psutil.Process(identity[0])
                if identity[1] is not None and child.create_time() == identity[1]:
                    child.kill()
                    child.wait(timeout=2)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
        if runner.poll() is None:
            runner.kill()
        runner.wait(timeout=2)


def test_parent_exit_with_inherited_pipes_obeys_deadline(tmp_path):
    child = tmp_path / 'child.py'
    child.write_text("import time; time.sleep(4)\n", encoding='utf-8')
    parent = tmp_path / 'parent.py'
    parent.write_text(
        'import subprocess, sys, json, psutil\nfrom pathlib import Path\n'
        f'p = subprocess.Popen([sys.executable, {str(child)!r}])\n'
        f'Path({str(tmp_path / "child.pid")!r}).write_text(json.dumps([p.pid, psutil.Process(p.pid).create_time()]))\n'
        "print('partial-before-exit', flush=True)\n", encoding='utf-8')
    result = isolated_probe(tmp_path, f'''
import json, sys
from pathlib import Path
from coding_agent_harness.process import run_process
r = run_process(argv=[sys.executable, {str(parent)!r}], cwd=Path({str(tmp_path)!r}), timeout_seconds=1)
print(r.model_dump_json())
''')
    assert result['elapsed_seconds'] < 2.5
    assert result['timed_out'] is True
    assert 'partial-before-exit' in result['stdout']
    assert result['output_complete'] is False or result['cleanup_errors']
    print('inherited pipes:', json.dumps({key: result[key] for key in ('elapsed_seconds', 'exit_code', 'output_complete', 'cleanup_errors', 'surviving_pids')}))


def test_failed_tree_termination_does_not_wait_for_child_exit(tmp_path):
    result = isolated_probe(tmp_path, f'''
import json, sys
from pathlib import Path
import coding_agent_harness.process as m
original = m.subprocess.Popen
def launch(*args, **kwargs):
    import psutil
    p = original(*args, **kwargs)
    Path({str(tmp_path / 'child.pid')!r}).write_text(json.dumps([p.pid, psutil.Process(p.pid).create_time()]))
    return p
m.subprocess.Popen = launch
m._kill_process_tree = lambda *args, **kwargs: None
r = m.run_process(argv=[sys.executable, '-c', "import time; print('before timeout', flush=True); time.sleep(4)"], cwd=Path({str(tmp_path)!r}), timeout_seconds=1)
print(r.model_dump_json())
''')
    assert result['elapsed_seconds'] < 2.5
    assert result['timed_out'] is True
    assert 'before timeout' in result['stdout']
    assert result['cleanup_errors']
    assert result['exit_code'] is None
    print('failed cleanup:', json.dumps({key: result[key] for key in ('elapsed_seconds', 'exit_code', 'output_complete', 'cleanup_errors', 'surviving_pids')}))


def test_large_output_is_bounded_with_truncation_evidence(tmp_path):
    result = isolated_probe(tmp_path, f"""
import sys
from pathlib import Path
from coding_agent_harness.process import run_process
r = run_process(argv=[sys.executable, '-c', "import sys, threading, os; timer = threading.Timer(4, lambda: os._exit(99)); timer.start(); sys.stdout.write('x' * (2 * 1024 * 1024)); sys.stderr.write('e' * (2 * 1024 * 1024)); timer.cancel()"], cwd=Path({str(tmp_path)!r}), timeout_seconds=5)
print(r.model_dump_json())
""")
    assert len(result['stdout'].encode()) <= 1024 * 1024
    assert len(result['stderr'].encode()) <= 1024 * 1024
    assert result['stdout_truncated'] and result['stderr_truncated']
    assert result['exit_code'] == 0


def test_launch_failure_is_structured_execution_evidence(tmp_path):
    argv = [str(tmp_path / 'definitely-missing-executable')]
    result = run_process(argv=argv, cwd=tmp_path, timeout_seconds=1)
    assert result.argv == argv
    assert result.started is False
    assert result.exit_code is None
    assert result.failure_type == 'FileNotFoundError'
    assert result.stdout == result.stderr == ''
    assert result.elapsed_seconds >= 0


@pytest.mark.parametrize('expectation', ['must_pass', 'expected_failure', 'advisory'])
def test_late_launch_failure_preserves_completed_checks(tmp_path, expectation):
    result = run_verification(repo_dir=tmp_path, commands=[
        VerificationCommand(id='first', argv=[sys.executable, '-c', "print('verified first')"]),
        VerificationCommand(id='missing', argv=[str(tmp_path / 'missing')], expectation=expectation,
                            expectation_reason='controlled failure' if expectation == 'expected_failure' else None),
    ])
    assert result.passed is False
    assert result.checks[0].passed is True
    assert 'verified first' in result.checks[0].stdout
    assert result.checks[1].exit_code is None
    assert result.checks[1].failure_type == 'FileNotFoundError'
    assert result.checks[1].passed is False


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_verification_interruption_keeps_prior_observations(tmp_path, monkeypatch, error):
    real_run = verifier_module.run_process
    def interrupted(**kwargs):
        if kwargs['argv'][0] == 'interrupt':
            raise error('controlled interruption')
        return real_run(**kwargs)
    monkeypatch.setattr(verifier_module, 'run_process', interrupted)
    with pytest.raises(error) as caught:
        run_verification(repo_dir=tmp_path, commands=[
            VerificationCommand(id='first', argv=[sys.executable, '-c', "print('preserved')"]),
            VerificationCommand(id='interrupt', argv=['interrupt']),
        ])
    observations = caught.value.verification_result
    assert observations.passed is False
    assert observations.checks[0].passed is True
    assert 'preserved' in observations.checks[0].stdout
    assert caught.value.failed_command.id == 'interrupt'


@pytest.mark.parametrize('stage', ['capture_evidence', 'capture_attempt_evidence', 'run_verification', 'review'])
@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_controller_failure_keeps_completed_evidence(tmp_path, monkeypatch, stage, error):
    repo = make_repo(tmp_path)
    worker = CountingWorker()
    reviewer = Reviewer(ReviewDecision(decision='accept', rationale='Offline accept'))
    def broken(**kwargs):
        raise error('controlled evidence failure')
    if stage == 'review':
        reviewer.review = broken
    else:
        monkeypatch.setattr(controller_module, stage, broken)
    with pytest.raises(error) as caught:
        HarnessController(worker=worker, reviewer=reviewer).run(
            task=task(), repo_dir=repo, verification_commands=[])
    preserved = caught.value.harness_evidence
    assert preserved['baseline'].commit
    record = preserved['attempts'][0]
    assert record['worker_result'].process.exit_code == 0
    if stage != 'capture_evidence':
        assert record['git_evidence'].final_head
    if stage == 'review':
        assert record['verification'].passed is True
    assert worker.calls == 1


def test_timeout_never_authorizes_remediation_retry(tmp_path):
    repo = make_repo(tmp_path)
    class TimeoutWorker(CountingWorker):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            result.process.timed_out = True
            return result
    worker = TimeoutWorker()
    reviewer = Reviewer(ReviewDecision(decision='remediate', rationale='Retry', remediation='Try again'))
    result = HarnessController(worker=worker, reviewer=reviewer).run(
        task=task(), repo_dir=repo, verification_commands=[])
    assert result.status == 'worker-failure'
    assert worker.calls == 1

def test_cancellation_preserves_process_evidence_and_attempts_cleanup(tmp_path):
    result = isolated_probe(tmp_path, f'''
import json, sys, psutil
from pathlib import Path
import coding_agent_harness.process as m
original = m.subprocess.Popen
created = []
def launch(*args, **kwargs):
    p = original(*args, **kwargs)
    created.append(p)
    identity = psutil.Process(p.pid)
    Path({str(tmp_path / 'child.pid')!r}).write_text(json.dumps([p.pid, identity.create_time()]))
    return p
m.subprocess.Popen = launch
def interrupt(seconds):
    raise KeyboardInterrupt('controlled cancellation')
m.time.sleep = interrupt
try:
    m.run_process(argv=[sys.executable, '-c', 'import time; time.sleep(4)'], cwd=Path({str(tmp_path)!r}), timeout_seconds=1)
except KeyboardInterrupt as exc:
    r = getattr(exc, 'process_result', None)
    print(json.dumps({{'evidence': r.model_dump() if r else None, 'alive': created[0].poll() is None}}))
''')
    assert result['evidence'] is not None
    assert result['evidence']['cancelled'] is True
    assert result['evidence']['failure_type'] == 'KeyboardInterrupt'
    assert result['evidence']['cleanup_attempted'] is True
    assert result['alive'] is False


def test_termination_denial_reports_errors_and_survivors(monkeypatch):
    class DeniedProcess:
        pid = 123
        def children(self, recursive):
            raise psutil.AccessDenied(self.pid)
        def terminate(self):
            raise psutil.AccessDenied(self.pid)
        def kill(self):
            raise psutil.AccessDenied(self.pid)
    monkeypatch.setattr(process_module.psutil, 'Process', lambda pid: DeniedProcess())
    monkeypatch.setattr(process_module.psutil, 'wait_procs', lambda processes, timeout: ([], processes))
    result = process_module._kill_process_tree(123)
    assert result is not None
    assert result.surviving_pids == [123]
    assert any('AccessDenied' in error for error in result.errors)


def test_output_capture_error_keeps_available_output_and_exit(tmp_path):
    result = isolated_probe(tmp_path, f'''
import sys, json, psutil
from pathlib import Path
import coding_agent_harness.process as m
original = m.subprocess.Popen
class BrokenRead:
    def __init__(self, stream):
        self.stream = stream
        self.reads = 0
    def read1(self, size):
        self.reads += 1
        if self.reads > 1:
            raise OSError('controlled capture failure')
        return self.stream.read1(size)
    def read(self, *args):
        raise OSError('controlled capture failure')
    def close(self):
        self.stream.close()
def launch(*args, **kwargs):
    p = original(*args, **kwargs)
    identity = psutil.Process(p.pid)
    Path({str(tmp_path / 'child.pid')!r}).write_text(json.dumps([p.pid, identity.create_time()]))
    p.stdout = BrokenRead(p.stdout)
    return p
m.subprocess.Popen = launch
try:
    r = m.run_process(argv=[sys.executable, '-c', "import time; print('available', flush=True); time.sleep(4)"], cwd=Path({str(tmp_path)!r}), timeout_seconds=1)
    print(r.model_dump_json())
except Exception as exc:
    print(json.dumps({{'failure_type': type(exc).__name__, 'stdout': '', 'exit_code': None}}))
''')
    assert 'available' in result['stdout']
    assert result['failure_type'] == 'OSError'
    assert result['output_complete'] is False
    assert result['exit_code'] is not None


def test_single_run_failure_keeps_worker_and_git_evidence(tmp_path, monkeypatch):
    import coding_agent_harness.orchestrator as orchestrator
    repo = make_repo(tmp_path)
    def broken(**kwargs):
        raise RuntimeError('controlled verification failure')
    monkeypatch.setattr(orchestrator, 'run_verification', broken)
    with pytest.raises(RuntimeError) as caught:
        orchestrator.run_task(task=task(), repo_dir=repo, worker=CountingWorker(), verification_commands=[])
    preserved = caught.value.harness_evidence
    assert preserved['worker_result'].process.exit_code == 0
    assert preserved['git_evidence'].final_head


def test_truncated_worker_completion_cannot_be_accepted(tmp_path):
    result = isolated_probe(tmp_path, f"""
import sys, json
from pathlib import Path
from coding_agent_harness.models import TaskPacket
from coding_agent_harness.worker import QwenWorker, classify_worker_run
script = "import threading, os; timer = threading.Timer(4, lambda: os._exit(99)); timer.start(); print('HARNESS_RESULT: {{\\"status\\":\\"completed\\",\\"summary\\":\\"Done\\"}}'); print('x' * (2 * 1024 * 1024)); timer.cancel()"
r = QwenWorker(command=[sys.executable, '-c', script], model='offline', timeout_seconds=5).run(task=TaskPacket(task_id='r3', goal='Offline', acceptance=['Reviewed']), repo_dir=Path({str(tmp_path)!r}))
print(json.dumps({{'truncated': r.process.stdout_truncated, 'completion': r.completion.model_dump() if r.completion else None, 'outcome': classify_worker_run(r).outcome}}))
""")
    assert result['truncated']
    assert result['completion'] is None
    assert result['outcome'] == 'execution-failure'


@pytest.mark.parametrize('fault', ['timed_out', 'output_complete', 'cleanup_errors'])
def test_verification_infrastructure_failure_blocks_retry(tmp_path, fault):
    repo = make_repo(tmp_path)
    worker = CountingWorker()
    reviewer = Reviewer(ReviewDecision(decision='remediate', rationale='Retry', remediation='Again'))
    original = controller_module.run_verification
    def infrastructure(**kwargs):
        result = original(repo_dir=kwargs['repo_dir'], commands=[VerificationCommand(
            id='local', argv=[sys.executable, '-c', "print('done')"], expectation='advisory')])
        value = False if fault == 'output_complete' else ['denied'] if fault == 'cleanup_errors' else True
        setattr(result.checks[0], fault, value)
        result.passed = False
        return result
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(controller_module, 'run_verification', infrastructure)
        result = HarnessController(worker=worker, reviewer=reviewer).run(
            task=task(), repo_dir=repo, verification_commands=[])
    assert result.status == 'verification-failure'
    assert worker.calls == 1

def test_later_attempt_exception_retains_prior_successful_observations(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    worker = CountingWorker()
    reviewer = Reviewer(ReviewDecision(decision='remediate', rationale='One authorized retry', remediation='Again'))
    original = controller_module.capture_evidence
    captures = 0
    def capture(**kwargs):
        nonlocal captures
        captures += 1
        if captures == 2:
            raise RuntimeError('second attempt capture failed')
        return original(**kwargs)
    monkeypatch.setattr(controller_module, 'capture_evidence', capture)
    with pytest.raises(RuntimeError) as caught:
        HarnessController(worker=worker, reviewer=reviewer).run(
            task=task(), repo_dir=repo, verification_commands=[])
    attempts = caught.value.harness_evidence['attempts']
    assert len(attempts) == 2
    assert attempts[0]['verification'].passed is True
    assert attempts[0]['git_evidence'].final_head
    assert attempts[0]['decision'].decision == 'remediate'
    assert attempts[1]['worker_result'].process.exit_code == 0
    assert 'git_evidence' not in attempts[1]
    assert worker.calls == 2


def test_result_construction_error_keeps_completed_observations(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    def broken(**kwargs):
        raise RuntimeError('result construction failed')
    monkeypatch.setattr(controller_module, 'HarnessRunResult', broken)
    with pytest.raises(RuntimeError) as caught:
        HarnessController(worker=CountingWorker(), reviewer=Reviewer(
            ReviewDecision(decision='accept', rationale='Offline'))).run(
                task=task(), repo_dir=repo, verification_commands=[])
    record = caught.value.harness_evidence['attempts'][0]
    assert record['worker_result'].process.exit_code == 0
    assert record['verification'].passed is True
    assert record['decision'].decision == 'accept'


def test_error_immediately_after_launch_cleans_up_and_preserves_identity(tmp_path):
    result = isolated_probe(tmp_path, f'''
import json, sys, psutil
from pathlib import Path
import coding_agent_harness.process as m
original = m.subprocess.Popen
created = []
def launch(*args, **kwargs):
    p = original(*args, **kwargs)
    created.append(p)
    Path({str(tmp_path / 'child.pid')!r}).write_text(json.dumps([p.pid, psutil.Process(p.pid).create_time()]))
    return p
m.subprocess.Popen = launch
def broken_start(self):
    raise RuntimeError('reader startup failed')
import threading
threading.Thread.start = broken_start
try:
    m.run_process(argv=[sys.executable, '-c', 'import time; time.sleep(4)'], cwd=Path({str(tmp_path)!r}), timeout_seconds=1)
except RuntimeError as exc:
    print(json.dumps({{'evidence': exc.process_result.model_dump(), 'alive': created[0].poll() is None}}))
''')
    assert result['alive'] is False
    assert result['evidence']['started'] is True
    assert result['evidence']['failure_type'] == 'RuntimeError'
    assert result['evidence']['cleanup_attempted'] is True
    assert result['evidence']['output_complete'] is False

def test_partial_git_capture_preserves_completed_fields(tmp_path, monkeypatch):
    import coding_agent_harness.git as git_module
    repo = make_repo(tmp_path)
    baseline = git_module.capture_baseline(repo)
    (repo / 'existing.txt').write_text('changed\n')
    def broken(**kwargs):
        raise OSError('controlled patch collection failure')
    monkeypatch.setattr(git_module, '_capture_patch_with_untracked', broken)
    with pytest.raises(OSError) as caught:
        git_module.capture_evidence(repo=repo, baseline=baseline)
    partial = caught.value.git_evidence
    assert partial['final_head'] == baseline.commit
    assert partial['changed_files'] == ['existing.txt']
    assert 'patch' not in partial
    assert 'commits' not in partial

def test_cancellation_during_cleanup_is_preserved(tmp_path, monkeypatch):
    class Process:
        pid = 123
        stdout = stderr = None
        def poll(self):
            return None
    class Capture:
        def __init__(self):
            import threading
            self.done = threading.Event()
            self.done.set()
            self.stop = threading.Event()
            self.eof = True
            self.error = None
            self.truncated = False
            self.thread = object()
        def start(self, stream):
            pass
        def snapshot(self):
            return 'available'
    interruption = KeyboardInterrupt('during cleanup')
    def interrupted(pid):
        raise interruption
    monkeypatch.setattr(process_module.subprocess, 'Popen', lambda *args, **kwargs: Process())
    monkeypatch.setattr(process_module, '_Capture', Capture)
    monkeypatch.setattr(process_module, '_kill_process_tree', interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_process(argv=['fake'], cwd=tmp_path, timeout_seconds=.001)
    assert caught.value is interruption
    assert caught.value.process_result.cancelled is True
    assert caught.value.process_result.stdout == 'available'


def test_drain_interruption_keeps_original_failure_and_evidence(tmp_path, monkeypatch):
    class Stream:
        def close(self):
            pass
    class Process:
        pid = 123
        stdout = Stream()
        stderr = Stream()
        def poll(self):
            return None
    failure = RuntimeError('reader start failed')
    def broken_start(self, stream):
        raise failure
    def interrupted_wait(self, timeout=None):
        raise KeyboardInterrupt('during drain')
    monkeypatch.setattr(process_module.subprocess, 'Popen', lambda *args, **kwargs: Process())
    monkeypatch.setattr(process_module._Capture, 'start', broken_start)
    monkeypatch.setattr(process_module, '_kill_process_tree', lambda pid: process_module.CleanupResult())
    monkeypatch.setattr(process_module.threading.Event, 'wait', interrupted_wait)
    with pytest.raises(RuntimeError) as caught:
        run_process(argv=['fake'], cwd=tmp_path, timeout_seconds=1)
    assert caught.value is failure
    assert caught.value.process_result.failure_type == 'RuntimeError'
    assert 'drain:KeyboardInterrupt' in caught.value.process_result.cleanup_errors

def test_interrupted_reader_start_never_closes_active_pipe_on_main_thread(tmp_path, monkeypatch):
    import threading
    entered, finish = threading.Event(), threading.Event()
    main_closed_active = []
    class Stream:
        def __init__(self, active):
            self.active = active
        def read1(self, size):
            entered.set()
            finish.wait(1)
            return b''
        def close(self):
            if self.active and threading.current_thread() is threading.main_thread() and entered.is_set() and not finish.is_set():
                main_closed_active.append(True)
    class Process:
        pid = 123
        stdout = Stream(True)
        stderr = Stream(False)
        def poll(self):
            return 0
    original = threading.Thread.start
    threads = []
    def interrupted_start(thread):
        original(thread)
        threads.append(thread)
        assert entered.wait(.5)
        raise KeyboardInterrupt('after reader start')
    monkeypatch.setattr(process_module.subprocess, 'Popen', lambda *args, **kwargs: Process())
    monkeypatch.setattr(process_module, '_kill_process_tree', lambda pid: process_module.CleanupResult())
    monkeypatch.setattr(threading.Thread, 'start', interrupted_start)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_process(argv=['fake'], cwd=tmp_path, timeout_seconds=1)
        assert not main_closed_active
    finally:
        finish.set()
        for thread in threads:
            thread.join(timeout=2)

def test_timeout_cleans_up_discoverable_child_and_grandchild(tmp_path):
    family = tmp_path / 'family.py'
    family.write_text('''import sys, subprocess, time, os, json, psutil
from pathlib import Path
role = sys.argv[1]
Path(role + '.pid').write_text(json.dumps([os.getpid(), psutil.Process().create_time()]))
if role != 'grandchild':
    subprocess.Popen([sys.executable, __file__, 'child' if role == 'root' else 'grandchild'])
print(role + ' output', flush=True)
time.sleep(4)
''')
    result = isolated_probe(tmp_path, f'''
import sys, json, psutil
from pathlib import Path
from coding_agent_harness.process import run_process
r = run_process(argv=[sys.executable, {str(family)!r}, 'root'], cwd=Path({str(tmp_path)!r}), timeout_seconds=1.5)
alive = []
for file in Path({str(tmp_path)!r}).glob('*.pid'):
    pid, created = json.loads(file.read_text())
    try:
        p = psutil.Process(pid)
        if p.create_time() == created and p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
            alive.append(pid)
    except psutil.NoSuchProcess:
        pass
print(json.dumps({{'process': r.model_dump(), 'alive': alive}}))
''')
    process = result['process']
    assert process['timed_out'] is True
    assert process['elapsed_seconds'] < 3.5
    assert 'grandchild output' in process['stdout']
    assert process['cleanup_attempted'] is True
    assert not process['cleanup_errors']
    assert not process['surviving_pids']
    assert not result['alive']
    print('discoverable tree:', json.dumps({key: process[key] for key in (
        'elapsed_seconds', 'exit_code', 'output_complete', 'cleanup_errors', 'surviving_pids')}))

@pytest.mark.parametrize('error, exit_code', [(RuntimeError, 1), (KeyboardInterrupt, 130)])
def test_cli_reports_preserved_failure_evidence(tmp_path, monkeypatch, capsys, error, exit_code):
    import coding_agent_harness.cli as cli
    import coding_agent_harness.orchestrator as orchestrator
    repo = make_repo(tmp_path)
    spec = tmp_path / 'task.json'
    spec.write_text(json.dumps({'task': task().model_dump(), 'verification_commands': []}))
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kwargs: CountingWorker())
    def broken(**kwargs):
        raise error('controlled verification interruption')
    monkeypatch.setattr(orchestrator, 'run_verification', broken)
    assert cli.app(['run', str(spec), '--repo', str(repo), '--model', 'offline']) == exit_code
    stderr = capsys.readouterr().err
    payload = json.loads(next(line.removeprefix('Failure evidence: ') for line in stderr.splitlines()
                              if line.startswith('Failure evidence: ')))
    assert payload['worker_result']['process']['exit_code'] == 0
    assert 'argv_sha256' in payload['worker_result']['process']
    assert 'argv' not in payload['worker_result']['process']
    assert payload['git_evidence']['final_head']
    assert payload['failure_type'] == error.__name__

@pytest.mark.parametrize('location', ['worker', 'verification'])
def test_handoff_preserves_infrastructure_failure_without_retry(tmp_path, monkeypatch, location):
    from coding_agent_harness.supervisor import RecordingReviewer, RecordedRun, build_handoff, inspect_repository
    from coding_agent_harness.verifier import VerificationCheck, VerificationResult
    repo = make_repo(tmp_path)
    baseline = inspect_repository(repo)
    original_task = task()
    class CleanupWorker(CountingWorker):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            if location == 'worker':
                result.process.cleanup_errors = ['terminate:AccessDenied']
            return result
    worker = CleanupWorker()
    recorder = RecordingReviewer(Reviewer(ReviewDecision(decision='remediate', rationale='Retry', remediation='Again')))
    commands = []
    if location == 'verification':
        commands = [VerificationCommand(id='local', argv=['fake'], expectation='advisory')]
        check = VerificationCheck(id='local', argv=['fake'], expectation='advisory', passed=False,
            exit_code=0, timed_out=False, stdout='available', stderr='', elapsed_seconds=0,
            cleanup_errors=['terminate:AccessDenied'])
        monkeypatch.setattr(controller_module, 'run_verification', lambda **kwargs: VerificationResult(passed=False, checks=[check]))
    result = HarnessController(worker=worker, reviewer=recorder, max_remediation_attempts=2).run(
        task=original_task, repo_dir=repo, verification_commands=commands)
    summary = build_handoff(RecordedRun(run_id='r3', original_task=original_task,
        verification_commands=commands, baseline=baseline, baseline_source='controller_evidence',
        controller_max_remediation_attempts=2, callbacks=recorder.records, result=result))
    assert summary.termination.status == ('worker-failure' if location == 'worker' else 'verification-failure')
    assert worker.calls == 1
    if location == 'worker':
        assert summary.callbacks[0].worker_infrastructure_failed
    else:
        assert summary.callbacks[0].checks[0].infrastructure_failed

@pytest.mark.parametrize('case', ['retry', 'exhaustion'])
def test_loaded_handoff_rejects_infrastructure_retry_provenance(tmp_path, case):
    from pydantic import ValidationError
    from coding_agent_harness.supervisor import (
        RecordingReviewer, RecordedRun, build_handoff, inspect_repository, HandoffSummary)
    repo = make_repo(tmp_path)
    baseline = inspect_repository(repo)
    recorder = RecordingReviewer(Reviewer(ReviewDecision(decision='remediate', rationale='One retry', remediation='Again')))
    budget = 1 if case == 'retry' else 0
    result = HarnessController(worker=CountingWorker(), reviewer=recorder, max_remediation_attempts=budget).run(
        task=task(), repo_dir=repo, verification_commands=[])
    summary = build_handoff(RecordedRun(run_id='r3', original_task=task(), verification_commands=[],
        baseline=baseline, baseline_source='controller_evidence', controller_max_remediation_attempts=budget,
        callbacks=recorder.records, result=result))
    payload = summary.model_dump(mode='json')
    callback = payload['callbacks'][0]
    callback['worker_timed_out'] = True
    callback['worker_outcome'] = 'execution-failure'
    # Omitted new metadata must not override independently observable timeout.
    callback.pop('worker_infrastructure_failed')
    with pytest.raises(ValidationError):
        HandoffSummary.model_validate(payload)


def install_controlled_process(
    monkeypatch,
    *,
    completion_after=0.12,
    monitor_sleep_advance=0.4,
    advance_on_first_poll=0,
):
    """Install a finite fake child and a clock that advances without wall time."""
    from types import SimpleNamespace

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += monitor_sleep_advance

    clock = Clock()

    class Pipe:
        def __init__(self, text):
            self.text = text

    completion = (
        'HARNESS_RESULT: '
        '{"status":"completed","summary":"Finished late",'
        '"changed_files":[],"remaining_concerns":[]}\n'
    )

    class Child:
        pid = 424242

        def __init__(self):
            self.completed = False
            self.poll_count = 0
            self.stdout = Pipe(completion)
            self.stderr = Pipe('captured stderr\n')

        def poll(self):
            if self.poll_count == 0:
                clock.now += advance_on_first_poll
            self.poll_count += 1
            if clock.now >= completion_after:
                self.completed = True
            return 0 if self.completed else None

    child = Child()

    class Done:
        def is_set(self):
            return child.completed

        def wait(self, timeout=None):
            return child.completed

    class Stop:
        def set(self):
            pass

    class Capture:
        def __init__(self):
            self.done = Done()
            self.stop = Stop()
            self.error = None
            self.truncated = False
            self.thread = None
            self.stream = None

        @property
        def eof(self):
            return child.completed

        def start(self, stream):
            self.stream = stream
            self.thread = object()

        def snapshot(self):
            return self.stream.text

    launches = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return child

    cleanup_calls = []
    monkeypatch.setattr(
        process_module,
        'time',
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        process_module,
        'subprocess',
        SimpleNamespace(Popen=launch, PIPE=object()),
    )
    monkeypatch.setattr(process_module, '_Capture', Capture)
    monkeypatch.setattr(
        process_module,
        '_kill_process_tree',
        lambda pid: (cleanup_calls.append(pid) or process_module.CleanupResult()),
    )
    return child, clock, launches, cleanup_calls


def test_delayed_monitor_classifies_late_child_completion_as_timeout(tmp_path, monkeypatch):
    child, clock, _, cleanup_calls = install_controlled_process(monkeypatch)

    result = run_process(argv=['controlled-child'], cwd=tmp_path, timeout_seconds=0.05)

    assert clock.now == pytest.approx(0.4)
    assert result.elapsed_seconds > 0.05
    assert result.timed_out is True
    assert result.infrastructure_failed is True
    assert result.exit_code == 0
    assert result.stdout.startswith('HARNESS_RESULT: ')
    assert result.stderr == 'captured stderr\n'
    assert result.output_complete is True
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False
    assert result.cleanup_attempted is True
    assert cleanup_calls == [child.pid]


def test_monitor_rechecks_deadline_after_completion_observations(tmp_path, monkeypatch):
    child, clock, _, cleanup_calls = install_controlled_process(
        monkeypatch,
        completion_after=0,
        monitor_sleep_advance=0.01,
        advance_on_first_poll=0.06,
    )

    result = run_process(argv=['controlled-child'], cwd=tmp_path, timeout_seconds=0.05)

    assert clock.now == pytest.approx(0.06)
    assert result.timed_out is True
    assert result.exit_code == 0
    assert result.output_complete is True
    assert result.cleanup_attempted is True
    assert cleanup_calls == [child.pid]


def test_safe_completion_within_budget_skips_cleanup(tmp_path, monkeypatch):
    child, clock, _, cleanup_calls = install_controlled_process(
        monkeypatch,
        completion_after=0.01,
        monitor_sleep_advance=0.01,
    )

    result = run_process(argv=['controlled-child'], cwd=tmp_path, timeout_seconds=0.05)

    assert clock.now == pytest.approx(0.01)
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.output_complete is True
    assert result.cleanup_attempted is False
    assert cleanup_calls == []
    assert child.completed is True


@pytest.mark.parametrize('action', ['accept', 'remediate', 'revise-scope'])
def test_late_worker_completion_cannot_authorize_controller_followup(tmp_path, monkeypatch, action):
    repo = make_repo(tmp_path)
    child, _, launches, _ = install_controlled_process(monkeypatch)

    class CountingQwenWorker(QwenWorker):
        def __init__(self):
            super().__init__(command=['offline-child'], model='offline', timeout_seconds=0.05)
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return super().run(**kwargs)

    class ChosenReviewer:
        def __init__(self, decision):
            self.decision = decision
            self.calls = 0
            self.observed = None

        def review(self, **kwargs):
            self.calls += 1
            self.observed = kwargs
            return self.decision

    decision = {
        'accept': ReviewDecision(decision='accept', rationale='Accept the completion'),
        'remediate': ReviewDecision(
            decision='remediate', rationale='Try again', remediation='Run again',
        ),
        'revise-scope': ReviewDecision(
            decision='revise-scope', rationale='Broaden scope', revised_allowed_files=['safe.py'],
        ),
    }[action]
    worker = CountingQwenWorker()
    reviewer = ChosenReviewer(decision)

    result = HarnessController(
        worker=worker,
        reviewer=reviewer,
        max_remediation_attempts=2,
    ).run(task=task(), repo_dir=repo, verification_commands=[])

    assert result.status == 'worker-failure'
    assert result.attempts == 1
    assert worker.calls == 1
    assert len(launches) == 1
    assert reviewer.calls == 1
    assert reviewer.observed['worker_assessment'].outcome == 'execution-failure'
    assert reviewer.observed['worker_result'].completion.status == 'completed'
    assert reviewer.observed['worker_result'].process.timed_out is True
    assert reviewer.observed['worker_result'].process.exit_code == 0
    assert child.completed is True


@pytest.mark.parametrize('expectation', ['must_pass', 'expected_failure', 'advisory'])
def test_late_verification_completion_is_never_reported_as_passing(tmp_path, monkeypatch, expectation):
    install_controlled_process(monkeypatch, completion_after=1.2, monitor_sleep_advance=1.4)
    reason = 'Expected local command failure' if expectation == 'expected_failure' else None

    result = run_verification(
        repo_dir=tmp_path,
        commands=[VerificationCommand(
            id='late',
            argv=['controlled-child'],
            timeout_seconds=1,
            expectation=expectation,
            expectation_reason=reason,
        )],
    )

    check = result.checks[0]
    assert result.passed is False
    assert check.passed is False
    assert check.timed_out is True
    assert check.infrastructure_failed is True
    assert check.exit_code == 0
    assert check.stdout.startswith('HARNESS_RESULT: ')
    if expectation != 'advisory':
        assert check.expectation_met is False
