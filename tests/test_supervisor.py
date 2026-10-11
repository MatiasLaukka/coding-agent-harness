import json
from pathlib import Path

import pytest

from coding_agent_harness.models import TaskPacket, VerificationCommand
from coding_agent_harness.supervisor import (
    ExecutionConfig, RepositoryIdentity, SupervisorProposal, TaskSpecification,
    HandoffError, canonical_json, canonical_digest, load_proposal, portable_path,
)


def task():
    return TaskPacket(task_id="slice", goal="Implement eligibility",
                      acceptance=["Tests pass"], allowed_files=["module.py"])


def config():
    return ExecutionConfig(command_prefix=["local-worker"], model="operator-model",
                           worker_timeout_seconds=300.0)


def proposal():
    return SupervisorProposal(
        proposal_id="p1", repository=RepositoryIdentity(
            repository_sha256="a"*64, baseline_commit="b"*40, baseline_tree="c"*40),
        action="task", rationale="Narrow slice",
        task_spec=TaskSpecification(task=task(), verification_commands=[
            VerificationCommand(id="required", argv=["python", "-m", "pytest"])
        ], execution_config=config()))


def test_canonical_fixed_vector():
    value = {"b": [True, None], "a": "é"}
    assert canonical_json(value) == b'{"a":"\xc3\xa9","b":[true,null]}'
    assert canonical_digest(value) == "c5cc0d1b9005cced90abb4178e4d502f70ee99f99e158b1841f82ab812241f3f"
    assert canonical_digest(value) == canonical_digest({"a":"é", "b":[True,None]})
    assert canonical_digest(value) != canonical_digest({"a":"é", "b":[None,True]})
    assert proposal().model_dump()["parent_handoff_sha256"] is None


def test_valid_proposal_roundtrip(tmp_path):
    path=tmp_path/"proposal.json"
    path.write_bytes(canonical_json(proposal()))
    assert load_proposal(path) == proposal()


@pytest.mark.parametrize("raw", [
    b'{"action":"stop","action":"task"}',
    b'{"task_spec":{"x":1,"x":2}}', b'{"x":NaN}', b'{"x":Infinity}',
    b"\xff", b'{"proposal_id":"\\ud800"}', b'[]',
])
def test_malformed_wire_non_echoing(tmp_path, raw):
    path=tmp_path/"bad.json"; path.write_bytes(raw)
    with pytest.raises(HandoffError) as error:
        load_proposal(path)
    assert str(error.value) in {"invalid_document", "document_too_large"}


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version=2),
    lambda d: d.update(approved=True),
    lambda d: d.update(action="execute"),
    lambda d: d.update(action="stop"),
    lambda d: d["task_spec"]["execution_config"].update(worker_timeout_seconds="300"),
    lambda d: d["task_spec"]["execution_config"].update(max_remediation_attempts=True),
    lambda d: d["task_spec"]["task"].update(allowed_files=None),
    lambda d: d["task_spec"]["task"].update(allowed_files=["A.py","a.py"]),
    lambda d: d["task_spec"].update(verification_commands=[]),
    lambda d: d["task_spec"]["verification_commands"][0].update(expectation="advisory"),
    lambda d: d["task_spec"]["verification_commands"][0].update(timeout_seconds=3601),
    lambda d: d["task_spec"]["task"].update(task_id="x"*257),
    lambda d: d["task_spec"]["task"].update(suggested_files=["x"]*65),
    lambda d: d["task_spec"]["verification_commands"][0].update(argv=["x"]*65),
    lambda d: d["task_spec"]["verification_commands"][0].update(argv=["x"*4001]),
    lambda d: d["task_spec"]["execution_config"].update(turn_limit=1001),
    lambda d: d["task_spec"]["execution_config"].update(tool_call_limit=0),
    lambda d: d["task_spec"]["execution_config"].update(wall_limit_seconds=float("inf")),
])
def test_strict_proposal_admission(tmp_path, mutation):
    d=proposal().model_dump(mode="json"); mutation(d)
    path=tmp_path/"p.json"; path.write_text(json.dumps(d), encoding="utf8")
    with pytest.raises(HandoffError):
        load_proposal(path)


@pytest.mark.parametrize("path", [
    "", ".", "../x", "a/../b", "/x", "C:/x", "C:x", "\\\\server\\x",
    "a\\b", "a//b", "x:stream", "a/.git/config", ".GIT", "NUL.txt",
    "com1", "LPT².py", "x.", "x ", "a/*", "a/?", "a/[x]", "a/\x01x",
])
def test_portable_paths(path):
    with pytest.raises(ValueError):
        portable_path(path)


def test_no_edit_scope_and_required_checks():
    d=proposal().model_dump(mode="json")
    d["task_spec"]["task"]["allowed_files"]=[]
    assert SupervisorProposal.model_validate(d).task_spec.task.allowed_files == []
    assert portable_path("src/example.py") == "src/example.py"
    d["task_spec"]["verification_commands"] *= 2
    with pytest.raises(ValueError):
        SupervisorProposal.model_validate(d)


def test_wire_bound(tmp_path):
    p=tmp_path/"large.json"; p.write_bytes(b" "*65537)
    with pytest.raises(HandoffError, match="document_too_large"):
        load_proposal(p)


import subprocess
import sys

from coding_agent_harness.controller import HarnessController
from coding_agent_harness.git import GitAttemptEvidence
from coding_agent_harness.models import ReviewDecision, WorkerResult
from coding_agent_harness.process import ProcessResult
from coding_agent_harness.worker import WorkerRunResult
from coding_agent_harness.supervisor import (
    RecordingReviewer, RecordedRun, ExceptionObservation, build_handoff,
    write_handoff, load_handoff,
)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root=tmp_path/"target"; root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Offline Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root/"module.py").write_text("value = 1\n", encoding="utf8")
    git(root, "add", "module.py"); git(root, "commit", "-m", "baseline")
    return root


class SyntheticWorker:
    def __init__(self, *, exit_code=0, timed_out=False, completion="completed",
                 filename="module.py", claim=None, raises=None):
        self.exit_code=exit_code; self.timed_out=timed_out
        self.completion=completion; self.filename=filename; self.claim=claim
        self.raises=raises; self.calls=0

    def run(self, **kwargs):
        self.calls += 1
        if self.raises:
            raise self.raises
        (kwargs["repo_dir"]/self.filename).write_text("value = 2\n", encoding="utf8")
        return WorkerRunResult(process=ProcessResult(
            argv=["secret-command"], exit_code=self.exit_code,
            stdout="SECRET stdout", stderr="SECRET stderr",
            timed_out=self.timed_out, elapsed_seconds=0.2),
            completion=WorkerResult(status=self.completion, summary="SECRET summary",
                                    changed_files=self.claim or [self.filename])
                if self.completion else None,
            completion_error=None if self.completion else "SECRET missing")


class Reviewer:
    def __init__(self, decision="accept", raises=None):
        self.decision=decision; self.raises=raises

    def review(self, **kwargs):
        if self.raises:
            raise self.raises
        return ReviewDecision(decision=self.decision, rationale="SECRET rationale")


def recorded(repo, worker=None, reviewer=None, commands=None):
    baseline=RepositoryIdentity(repository_sha256="a"*64,
        baseline_commit=git(repo,"rev-parse","HEAD"),
        baseline_tree=git(repo,"rev-parse","HEAD^{tree}"))
    checks=commands or [VerificationCommand(id="check", argv=[sys.executable,"-c","pass"])]
    recorder=RecordingReviewer(reviewer or Reviewer())
    controller=HarnessController(worker=worker or SyntheticWorker(),
                                 reviewer=recorder, max_remediation_attempts=0)
    result=None; exception=None
    try:
        result=controller.run(task=task(), repo_dir=repo, verification_commands=checks)
    except Exception as error:
        exception=ExceptionObservation(stage="controller", type_name=type(error).__name__)
    return RecordedRun(run_id="run", original_task=task(), verification_commands=checks,
                       baseline=baseline, baseline_source="trusted_preflight",
                       controller_max_remediation_attempts=0, callbacks=recorder.records,
                       result=result, exception=exception)


def test_accepted_optional_detail_is_not_required(repo):
    record=recorded(repo)
    summary=build_handoff(record)
    assert summary.termination.status == "accepted"
    assert summary.provenance_complete
    assert summary.missing_evidence == []
    assert summary.optional_missing_evidence == ["per_attempt_evidence:1"]
    assert summary.callbacks[0].independent_observations.per_attempt is None
    assert summary.controller_result_sha256 == canonical_digest(record.result)
    assert summary.semantic_task_scope == "not_assessed_by_handoff"


def test_claims_distinguished_from_git_and_audit_preserved(repo):
    record=recorded(repo, SyntheticWorker(claim=["fiction.py"]), Reviewer("stop"))
    before=canonical_json(record)
    summary=build_handoff(record)
    assert summary.termination.status == "stopped"
    assert summary.callbacks[0].worker_claims.changed_files != (
        summary.callbacks[0].independent_observations.changed_files)
    assert summary.audits[0].kind == "worker-git-disagreement"
    assert not summary.audits[0].independently_verified
    assert summary.provenance_complete  # Optional snapshot absent, real audit preserved.
    assert summary.audits[0].sha256 == canonical_digest(record.result.audit_events[0])
    assert canonical_json(record) == before


@pytest.mark.parametrize("kwargs,status", [
    ({"exit_code":55},"worker-failure"), ({"timed_out":True},"worker-failure"),
    ({"completion":None},"worker-failure"), ({"filename":"unauthorized.txt"},"scope-violation"),
    ({"completion":"blocked"},"accepted"), ({"completion":"failed"},"accepted"),
])
def test_controller_original_gates(repo, kwargs, status):
    record=recorded(repo, SyntheticWorker(**kwargs))
    summary=build_handoff(record)
    assert summary.termination.status == record.result.status == status
    assert summary.provenance_complete


@pytest.mark.parametrize("expectation,exit_code,expected", [
    ("must_pass",1,"verification-failure"), ("advisory",1,"accepted"),
    ("expected_failure",1,"accepted"), ("expected_failure",0,"accepted"),
])
def test_verification_semantics(repo, expectation, exit_code, expected):
    check=VerificationCommand(id="check",argv=[sys.executable,"-c",f"exit({exit_code})"],
        expectation=expectation, expectation_reason="reason" if expectation=="expected_failure" else None)
    record=recorded(repo, commands=[check])
    assert build_handoff(record).termination.status == expected


@pytest.mark.parametrize("mutate", [
    lambda r: setattr(r.callbacks[0].task,"goal","contradiction"),
    lambda r: setattr(r.callbacks[0].evidence,"baseline_commit","b"*40),
    lambda r: setattr(r.callbacks[0],"attempt",2),
    lambda r: r.callbacks.append(r.callbacks[0].model_copy(deep=True)),
    lambda r: setattr(r.callbacks[0].worker_assessment,"outcome","failed"),
    lambda r: setattr(r.callbacks[0].verification.checks[0],"argv",["wrong"]),
    lambda r: setattr(r.callbacks[0].verification.checks[0],"passed",False),
    lambda r: setattr(r.callbacks[0].verification.checks[0],"expectation_met",False),
    lambda r: setattr(r.callbacks[0].verification,"passed",False),
    lambda r: setattr(r.result,"status","stopped"),
    lambda r: setattr(r.result,"attempts",2),
])
def test_contradictions_reject_without_repair(repo, mutate):
    record=recorded(repo); mutate(record); before=canonical_json(record)
    with pytest.raises(HandoffError, match="contradictory_provenance"):
        build_handoff(record)
    assert canonical_json(record) == before


def test_missing_required_records_explicit(repo):
    record=recorded(repo)
    record.callbacks=[]
    record.baseline=None; record.baseline_source="missing"
    record.controller_max_remediation_attempts=None
    summary=build_handoff(record)
    assert summary.termination.status == "accepted"
    assert not summary.provenance_complete
    assert set(summary.missing_evidence) == {"baseline","controller_budget","callback:1"}
    assert summary.callbacks == []


@pytest.mark.parametrize("stage",["worker","reviewer"])
def test_exceptions_are_not_controller_returns(repo, stage):
    error=RuntimeError("SECRET unexpected")
    recorder=RecordingReviewer(Reviewer(raises=error))
    if stage=="reviewer":
        # Prove the original exception object is re-raised.
        record=recorded(repo, reviewer=Reviewer(raises=error))
        assert record.callbacks[0].reviewer_exception is not None
    else:
        record=recorded(repo,SyntheticWorker(raises=error))
        assert record.callbacks == []
    summary=build_handoff(record)
    assert record.result is None
    assert summary.termination.kind == "exception"
    assert summary.termination.status is None
    assert summary.termination.attempts is None
    assert summary.audits == []
    assert b"SECRET" not in canonical_json(summary)


def test_privacy_and_create_only(repo,tmp_path):
    record=recorded(repo, SyntheticWorker(claim=["SECRET-key.txt"]), Reviewer("stop"))
    summary=build_handoff(record)
    raw=canonical_json(summary)
    assert b"SECRET" not in raw
    assert b"secret-command" not in raw
    target=tmp_path/"summary.json"
    write_handoff(target,summary)
    assert load_handoff(target) == summary
    with pytest.raises(HandoffError,match="output_exists"):
        write_handoff(target,summary)
    assert target.read_bytes() == raw


def test_missing_observable_audit_is_contradiction(repo):
    record=recorded(repo, SyntheticWorker(filename="unauthorized.txt"))
    record.result.audit_events=[]
    with pytest.raises(HandoffError):
        build_handoff(record)


def test_optional_attempt_evidence_checks_disagreement(repo):
    record=recorded(repo,SyntheticWorker(claim=["fiction.py"]),Reviewer("stop"))
    record.callbacks[0].per_attempt_evidence=GitAttemptEvidence(
        before_tree=record.baseline.baseline_tree,after_tree="c"*40,changed_files=["module.py"])
    summary=build_handoff(record)
    assert summary.audits[0].independently_verified
    assert summary.optional_missing_evidence == []
    record.callbacks[0].per_attempt_evidence.changed_files=["different.py"]
    with pytest.raises(HandoffError):
        build_handoff(record)


@pytest.mark.parametrize("expectation,status", [
    ("must_pass","verification-failure"), ("expected_failure","verification-failure"),
    ("advisory","accepted")])
def test_verification_timeout_policy(repo,expectation,status):
    checks=[VerificationCommand(id="timeout",argv=[sys.executable,"-c",
        "import time; time.sleep(5)"],timeout_seconds=1,expectation=expectation,
        expectation_reason="expected" if expectation=="expected_failure" else None)]
    record=recorded(repo,commands=checks)
    assert record.callbacks[0].verification.checks[0].timed_out
    assert build_handoff(record).termination.status == status


@pytest.mark.parametrize("decision",["remediate","revise-scope"])
def test_retry_lineage_and_inter_attempt_detail(repo,decision):
    class RetryReviewer:
        def review(self, **kwargs):
            if kwargs["attempt"] == 1:
                return ReviewDecision(decision=decision,rationale="new scope",
                    remediation="try again" if decision=="remediate" else None,
                    revised_allowed_files=["module.py"] if decision=="revise-scope" else None)
            return ReviewDecision(decision="accept",rationale="done")
    recorder=RecordingReviewer(RetryReviewer())
    worker=SyntheticWorker()
    baseline=RepositoryIdentity(repository_sha256="a"*64,
        baseline_commit=git(repo,"rev-parse","HEAD"), baseline_tree=git(repo,"rev-parse","HEAD^{tree}"))
    commands=[VerificationCommand(id="check",argv=[sys.executable,"-c","pass"])]
    result=HarnessController(worker=worker,reviewer=recorder,max_remediation_attempts=1).run(
        task=task(),repo_dir=repo,verification_commands=commands)
    record=RecordedRun(run_id="retry",original_task=task(),verification_commands=commands,
        baseline=baseline,baseline_source="trusted_preflight",
        controller_max_remediation_attempts=1,callbacks=recorder.records,result=result)
    summary=build_handoff(record)
    assert summary.termination.status == "accepted"
    assert summary.provenance_complete
    # State changes between captures are observable, not guessed contradictions.
    record.callbacks[0].per_attempt_evidence=GitAttemptEvidence(
        before_tree=record.baseline.baseline_tree,after_tree="c"*40,changed_files=["module.py"])
    record.callbacks[1].per_attempt_evidence=GitAttemptEvidence(
        before_tree="d"*40,after_tree="e"*40,changed_files=[])
    summary=build_handoff(record)
    assert summary.callbacks[1].independent_observations.inter_attempt_state_change is True
    record.callbacks[1].task.goal="wrong transition"
    with pytest.raises(HandoffError):
        build_handoff(record)


def test_recorder_rethrows_original_and_copies_inputs(repo):
    record=recorded(repo)
    source=record.callbacks[0]
    kwargs={name:getattr(source,name) for name in (
        "attempt","task","worker_result","worker_assessment","evidence","verification")}
    error=RuntimeError("secret")
    class MutatingReviewer:
        def review(self, **values):
            values["task"].goal="changed"
            raise error
    recorder=RecordingReviewer(MutatingReviewer())
    with pytest.raises(RuntimeError) as caught:
        recorder.review(**kwargs)
    assert caught.value is error
    assert recorder.records[0].task.goal == "Implement eligibility"
    assert recorder.records[0].review_decision is None


def test_handoff_loader_rejects_claimed_complete_missing_history(repo,tmp_path):
    summary=build_handoff(recorded(repo)).model_dump(mode="json")
    summary["callbacks"]=[]
    p=tmp_path/"contradiction.json"; p.write_text(json.dumps(summary),encoding="utf8")
    with pytest.raises(HandoffError):
        load_handoff(p)


from coding_agent_harness.supervisor import (
    OperatorApproval, load_operator_approval, validate_proposal, materialize_task_file,
    inspect_repository, execution_preflight,
)


def bound_proposal(repo):
    return proposal().model_copy(update={"repository":inspect_repository(repo)})


def approval_for(p):
    spec=p.task_spec
    return OperatorApproval(proposal_sha256=canonical_digest(p),repository=p.repository,
        task_sha256=canonical_digest(spec.task),allowed_files=list(spec.task.allowed_files),
        verification_sha256=canonical_digest([c.model_dump(mode="json") for c in spec.verification_commands]),
        execution_config_sha256=canonical_digest(spec.execution_config),approve_task=True,
        approve_scope=True,approve_checks=True,approve_configuration=True)


def save_approval(tmp_path,p):
    directory=tmp_path/"operator"; directory.mkdir(exist_ok=True)
    file=directory/"approved.json"; file.write_bytes(canonical_json(approval_for(p)))
    return file,directory


def preflight(p, repo, path, directory, **changes):
    values=dict(handoff=None,approval_path=path,trusted_approval_dir=directory,
        untrusted_workspace_roots=[],repo_dir=repo,task=p.task_spec.task,
        verification_commands=p.task_spec.verification_commands,
        execution_config=p.task_spec.execution_config)
    values.update(changes)
    return execution_preflight(p,**values)


def test_missing_approval_is_not_authorization(repo):
    p=bound_proposal(repo)
    feedback=validate_proposal(p,handoff=None,approval=None)
    assert feedback.status == "needs_operator_approval"
    assert feedback.no_execution_performed
    assert feedback.execution_preflight_required
    with pytest.raises(HandoffError):
        materialize_task_file(p,handoff=None,approval=None)


@pytest.mark.parametrize("mutation",[
    lambda p: setattr(p,"rationale","changed"),
    lambda p: setattr(p.task_spec.task,"goal","different"),
    lambda p: p.task_spec.task.allowed_files.append("other.py"),
    lambda p: setattr(p.task_spec.verification_commands[0],"argv",["untrusted","command"]),
    lambda p: setattr(p.task_spec.verification_commands[0],"timeout_seconds",301),
    lambda p: setattr(p.task_spec.execution_config,"model","different"),
    lambda p: setattr(p.task_spec.execution_config,"tool_call_limit",20),
    lambda p: setattr(p.repository,"baseline_commit","b"*40),
])
def test_approval_bindings(repo,mutation):
    p=bound_proposal(repo); approval=approval_for(p)
    mutation(p)
    assert validate_proposal(p,handoff=None,approval=approval).status == "invalid"


def test_protected_approval_directory(repo,tmp_path):
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    assert load_operator_approval(path,trusted_dir=directory,target_repo=repo,
                                  untrusted_workspace_roots=[]) == approval_for(p)
    for trusted,roots in [(tmp_path,[]),(directory,[directory]),(directory,[tmp_path])]:
        with pytest.raises(HandoffError):
            load_operator_approval(path,trusted_dir=trusted,target_repo=repo,
                                   untrusted_workspace_roots=roots)
    in_target=repo/"approval.json"; in_target.write_bytes(path.read_bytes())
    with pytest.raises(HandoffError):
        load_operator_approval(in_target,trusted_dir=repo,target_repo=repo,
                               untrusted_workspace_roots=[])
    with pytest.raises(HandoffError):
        load_operator_approval(path,trusted_dir=tmp_path/"elsewhere",target_repo=repo,
                               untrusted_workspace_roots=[])


def test_protected_directory_link_escape(repo,tmp_path):
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    linked=tmp_path/"exchange-link"
    if sys.platform=="win32":
        # Junctions require no symlink privilege on Windows.
        subprocess.run(["cmd","/c","mklink","/J",str(linked),str(directory)],
                       check=True,capture_output=True)
    else:
        linked.symlink_to(directory,target_is_directory=True)
    with pytest.raises(HandoffError):
        load_operator_approval(path,trusted_dir=directory,target_repo=repo,
                               untrusted_workspace_roots=[linked])


def test_materialization_is_data_only(repo):
    p=bound_proposal(repo); approval=approval_for(p)
    result=materialize_task_file(p,handoff=None,approval=approval)
    from coding_agent_harness.cli import TaskFile
    spec=TaskFile.model_validate(result)
    assert spec.task == p.task_spec.task
    assert spec.verification_commands == p.task_spec.verification_commands
    assert set(result) == {"task","verification_commands"}


def test_preflight_is_fresh_and_clean(repo,tmp_path):
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    assert preflight(p,repo,path,directory).passed
    (repo/"untracked.txt").write_text("dirty")
    result=preflight(p,repo,path,directory)
    assert not result.passed and "dirty_target" in result.errors
    (repo/"untracked.txt").unlink()
    git(repo,"commit","--allow-empty","-m","new head")
    assert not preflight(p,repo,path,directory).passed


@pytest.mark.parametrize("field",["task","verification_commands","execution_config"])
def test_preflight_exact_actual_inputs(repo,tmp_path,field):
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    actual=getattr(p.task_spec,field)
    if field=="task":
        actual=actual.model_copy(update={"goal":"different"})
    elif field=="verification_commands":
        actual=[actual[0].model_copy(update={"argv":["never","execute"]})]
    else:
        actual=actual.model_copy(update={"worker_timeout_seconds":301.0})
    assert not preflight(p,repo,path,directory,**{field:actual}).passed


def test_preflight_reloads_approval(repo,tmp_path):
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    approval=approval_for(p)
    assert validate_proposal(p,handoff=None,approval=approval).status=="approval_validated"
    approval.task_sha256="b"*64; path.write_bytes(canonical_json(approval))
    assert not preflight(p,repo,path,directory).passed


def test_preflight_fixed_queries_no_optional_lock_or_fsmonitor(repo,tmp_path,monkeypatch):
    import coding_agent_harness.supervisor as sidecar
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    calls=[]; actual=subprocess.run
    def spy(argv,**kwargs):
        calls.append((argv,kwargs))
        return actual(argv,**kwargs)
    monkeypatch.setattr(sidecar.subprocess,"run",spy)
    assert validate_proposal(p,handoff=None,approval=approval_for(p)).status=="approval_validated"
    assert calls == []
    assert preflight(p,repo,path,directory).passed
    assert calls
    for argv,kwargs in calls:
        assert argv[:4] == ["git","--no-optional-locks","-c","core.fsmonitor=false"]
        assert argv[4] in ("rev-parse","status")
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_CONFIG_VALUE_0"] == "false"
        assert "local-worker" not in argv and "pytest" not in argv


def test_preflight_rejects_repo_subdirectory(repo):
    sub=repo/"sub"; sub.mkdir()
    with pytest.raises(HandoffError,match="not_worktree_root"):
        inspect_repository(sub)


@pytest.mark.parametrize("destination",["outside","git"])
def test_preflight_scope_link_escape(repo,tmp_path,destination):
    target=tmp_path/"outside" if destination=="outside" else repo/".git"
    target.mkdir(exist_ok=True)
    link=repo/"linked"
    if sys.platform=="win32":
        subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)],
                       check=True,capture_output=True)
    else:
        link.symlink_to(target,target_is_directory=True)
    # Ignore the junction itself so scope resolution, not dirt, is under test.
    (repo/".git"/"info"/"exclude").write_text("linked/\n")
    p=bound_proposal(repo)
    p.task_spec.task.allowed_files=["linked/file.py"]
    path,directory=save_approval(tmp_path,p)
    assert not preflight(p,repo,path,directory).passed


@pytest.mark.parametrize("decision",["accept","remediate","stop","revise-scope"])
def test_supervisor_review_does_not_execute_or_reinterpret(repo,decision):
    worker=SyntheticWorker()
    record=recorded(repo,worker,Reviewer("stop"))
    summary=build_handoff(record)
    d=ReviewDecision(decision=decision,rationale="review",
        remediation="narrow task" if decision=="remediate" else None,
        revised_allowed_files=["module.py"] if decision=="revise-scope" else None)
    p=SupervisorProposal(proposal_id="next",action="review",rationale="recommendation",
        repository=record.baseline,parent_handoff_sha256=canonical_digest(summary),
        parent_task_sha256=summary.original_task_sha256,
        parent_baseline_sha256=canonical_digest(summary.baseline),review_decision=d)
    original=canonical_json(record.result)
    feedback=validate_proposal(p,handoff=summary,approval=None)
    assert feedback.status == ("invalid" if decision=="accept" else "nonexecuting_recommendation")
    assert feedback.no_execution_performed
    with pytest.raises(HandoffError):
        materialize_task_file(p,handoff=summary,approval=None)
    assert worker.calls == 1
    assert canonical_json(record.result) == original


def test_accept_acknowledges_original_with_optional_detail_missing(repo):
    summary=build_handoff(recorded(repo))
    p=SupervisorProposal(proposal_id="ack",action="review",rationale="acknowledge",
        repository=summary.baseline,parent_handoff_sha256=canonical_digest(summary),
        parent_task_sha256=summary.original_task_sha256,
        parent_baseline_sha256=canonical_digest(summary.baseline),
        review_decision=ReviewDecision(decision="accept",rationale="original accepted"))
    assert validate_proposal(p,handoff=summary,approval=None).status=="nonexecuting_recommendation"
    summary.provenance_complete=False; summary.missing_evidence=["controller_budget"]
    p.parent_handoff_sha256=canonical_digest(summary)
    assert validate_proposal(p,handoff=summary,approval=None).status=="invalid"


@pytest.mark.parametrize("field,value", [
    ("schema_version",True),
    ("budget",False),
    ("approve_task",1),
])
def test_literals_reject_boolean_integer_coercion(tmp_path,field,value):
    if field=="approve_task":
        d=approval_for(proposal()).model_dump(mode="json")
        d[field]=value
        from coding_agent_harness.supervisor import load_document
        model=OperatorApproval
        loader=lambda p:load_document(p,model)
    else:
        d=proposal().model_dump(mode="json")
        if field=="budget":
            d["task_spec"]["execution_config"]["max_remediation_attempts"]=value
        else:
            d[field]=value
        loader=load_proposal
    p=tmp_path/"literal.json"; p.write_text(json.dumps(d),encoding="utf8")
    with pytest.raises(HandoffError):
        loader(p)


def test_escalation_and_followup_are_nonexecuting(repo):
    summary=build_handoff(recorded(repo))
    refs=dict(repository=summary.baseline,parent_handoff_sha256=canonical_digest(summary),
        parent_task_sha256=summary.original_task_sha256,
        parent_baseline_sha256=canonical_digest(summary.baseline))
    for action in ("stop","escalate","review"):
        p=SupervisorProposal(proposal_id="next",action=action,rationale="next",**refs,
            requested_capability="stronger reasoning" if action=="escalate" else None,
            review_decision=ReviewDecision(decision="remediate",rationale="slice",
                remediation="do less") if action=="review" else None,
            follow_up_task=proposal().task_spec if action=="review" else None)
        assert validate_proposal(p,handoff=summary,approval=None).status=="nonexecuting_recommendation"
        with pytest.raises(HandoffError):
            materialize_task_file(p,handoff=summary,approval=None)


def test_preflight_different_worktree_is_not_same_repository(repo,tmp_path):
    p=bound_proposal(repo); path,directory=save_approval(tmp_path,p)
    other=tmp_path/"other-worktree"
    git(repo,"worktree","add","-b","other",str(other))
    assert inspect_repository(other).baseline_commit == p.repository.baseline_commit
    assert inspect_repository(other).repository_sha256 != p.repository.repository_sha256
    assert not preflight(p,other,path,directory).passed


def test_readonly_inspection_does_not_refresh_index(repo):
    before=(repo/".git"/"index").read_bytes()
    inspect_repository(repo)
    assert (repo/".git"/"index").read_bytes()==before


def test_offline_supervised_roundtrip(repo,tmp_path):
    p=bound_proposal(repo)
    p.task_spec.task.goal="Set module.value to 2; preserve the interface."
    p.task_spec.verification_commands=[VerificationCommand(id="value",argv=[
        sys.executable,"-B","-c","import module; assert module.value == 2"])]
    exchange=tmp_path/"exchange"; exchange.mkdir()
    proposal_path=exchange/"proposal.json"; proposal_path.write_bytes(canonical_json(p))
    approved_path,directory=save_approval(tmp_path,p)  # Trusted OPERATOR fixture.
    loaded=load_proposal(proposal_path)
    approval=load_operator_approval(approved_path,trusted_dir=directory,target_repo=repo,
                                   untrusted_workspace_roots=[exchange])
    assert validate_proposal(loaded,handoff=None,approval=approval).status=="approval_validated"
    materialized=materialize_task_file(loaded,handoff=None,approval=approval)
    from coding_agent_harness.cli import TaskFile
    actual=TaskFile.model_validate(materialized)
    pre=execution_preflight(loaded,handoff=None,approval_path=approved_path,
        trusted_approval_dir=directory,untrusted_workspace_roots=[exchange],repo_dir=repo,
        task=actual.task,verification_commands=actual.verification_commands,
        execution_config=loaded.task_spec.execution_config)
    assert pre.passed
    worker=SyntheticWorker()
    class IndependentReviewer:
        def review(self,**values):
            assert values["worker_assessment"].outcome=="completed"
            assert values["verification"].passed
            assert values["evidence"].changed_files==["module.py"]
            return ReviewDecision(decision="accept",rationale="Independent check passed")
    recorder=RecordingReviewer(IndependentReviewer())
    controller=HarnessController(worker=worker,reviewer=recorder,max_remediation_attempts=0)
    # Explicit trusted-caller invocation, not a supervisor/preflight action.
    result=controller.run(task=actual.task,repo_dir=repo,
                          verification_commands=actual.verification_commands)
    assert result.status=="accepted"
    record=RecordedRun(run_id="roundtrip",original_task=actual.task,
        verification_commands=actual.verification_commands,baseline=pre.repository,
        baseline_source="trusted_preflight",controller_max_remediation_attempts=0,
        callbacks=recorder.records,result=result)
    summary=build_handoff(record)
    summary_path=exchange/"summary.json"; write_handoff(summary_path,summary)
    summary=load_handoff(summary_path)
    next_proposal=SupervisorProposal(proposal_id="next",action="review",
        rationale="Consider another narrow slice",repository=summary.baseline,
        parent_handoff_sha256=canonical_digest(summary),
        parent_task_sha256=summary.original_task_sha256,
        parent_baseline_sha256=canonical_digest(summary.baseline),
        review_decision=ReviewDecision(decision="remediate",rationale="recommend only",
                                      remediation="A separately approved task"))
    before=canonical_json(result)
    assert validate_proposal(next_proposal,handoff=summary,approval=None).status=="nonexecuting_recommendation"
    assert worker.calls==1 and canonical_json(result)==before


def test_documentation_describes_manual_trust_boundary():
    readme=(Path(__file__).parents[1]/"README.md").read_text(encoding="utf8")
    assert "## Offline supervisor handoff" in readme
    assert "approval-unaware" in readme
    assert "hashes do not authenticate a human" in readme
    assert "optional per-attempt" in readme
    assert "execution_preflight" in readme


@pytest.mark.parametrize("mutation", [
    lambda d:d["termination"].update(status="accepted"),
    lambda d:d["callbacks"][0]["checks"][0].update(passed=False),
    lambda d:d["callbacks"][0].update(worker_outcome="execution-failure"),
    lambda d:d["audits"][0].update(independently_verified=True),
])
def test_loaded_handoff_rejects_projected_contradictions(repo,tmp_path,mutation):
    summary=build_handoff(recorded(repo,SyntheticWorker(claim=["fiction.py"]),Reviewer("stop")))
    d=summary.model_dump(mode="json"); mutation(d)
    path=tmp_path/"contradictory.json"; path.write_text(json.dumps(d),encoding="utf8")
    with pytest.raises(HandoffError):
        load_handoff(path)


@pytest.mark.parametrize("path",['src/<bad>.py','src/"bad".py',"src/a|b","CON .txt"])
def test_additional_windows_ambiguity(path):
    with pytest.raises(ValueError):
        portable_path(path)


def test_deeply_nested_wire_is_static_error(tmp_path):
    path=tmp_path/"nested.json"
    path.write_bytes(b'['*1500+b'0'+b']'*1500)
    with pytest.raises(HandoffError,match="invalid_document"):
        load_proposal(path)


def test_supplied_first_snapshot_contradicts_baseline(repo):
    record=recorded(repo)
    record.callbacks[0].per_attempt_evidence=GitAttemptEvidence(
        before_tree="f"*40,after_tree="c"*40,changed_files=["module.py"])
    with pytest.raises(HandoffError,match="contradictory_provenance"):
        build_handoff(record)


def test_mutated_nested_config_revalidated():
    cfg=config()
    cfg.turn_limit=-1
    with pytest.raises(ValueError):
        TaskSpecification(task=task(),verification_commands=[
            VerificationCommand(id="t",argv=["check"])],execution_config=cfg)


def test_missing_and_reordered_audits_are_not_reconstructed(repo):
    record=recorded(repo,SyntheticWorker(filename="outside.py"),
        commands=[VerificationCommand(id="t",argv=[sys.executable,"-c","exit(1)"])])
    before=canonical_json(record.result)
    record.result.audit_events.reverse()
    with pytest.raises(HandoffError):
        build_handoff(record)
    assert canonical_json(record.result) != before  # Caller reversal remains, no repair.


def test_recorded_advisory_contradiction_not_normalized_away(repo):
    record=recorded(repo,commands=[VerificationCommand(
        id="advisory",argv=[sys.executable,"-c","pass"],expectation="advisory")])
    record.callbacks[0].verification.checks[0].expectation_met=False
    before=canonical_json(record)
    with pytest.raises(HandoffError,match="contradictory_provenance"):
        build_handoff(record)
    assert canonical_json(record)==before


def test_portable_path_rejects_c1_control():
    with pytest.raises(ValueError):
        portable_path("src/\x85hidden.py")


class CommittingWorker(SyntheticWorker):
    def run(self, **kwargs):
        result = super().run(**kwargs)
        git(kwargs["repo_dir"], "add", "module.py")
        git(kwargs["repo_dir"], "commit", "-m", "PRIVATE commit subject")
        return result


def test_worker_commit_exports_hash_preserves_complete_evidence(repo):
    record = recorded(repo, CommittingWorker())
    commit = git(repo, "rev-parse", "HEAD")
    evidence = record.callbacks[0].evidence
    assert evidence.commits == [commit + "\tPRIVATE commit subject"]
    assert record.result.status == "accepted"
    before = canonical_json(record)
    summary = build_handoff(record)
    observation = summary.callbacks[0].independent_observations
    assert observation.commits == [commit]
    assert observation.git_evidence_sha256 == canonical_digest(evidence)
    assert b"PRIVATE commit subject" not in canonical_json(summary)
    assert summary.termination.status == record.result.status
    assert summary.controller_result_sha256 == canonical_digest(record.result)
    assert [audit.sha256 for audit in summary.audits] == [
        canonical_digest(audit) for audit in record.result.audit_events]
    assert canonical_json(record) == before


@pytest.mark.parametrize("commit_record", [
    "a"*40, "bad\tPRIVATE subject", "A"*40 + "\tPRIVATE subject",
    "a"*39 + "\tPRIVATE subject", "a"*40 + "\n\tPRIVATE subject",
    "a"*40 + "\tPRIVATE\nsubject",
])
def test_malformed_commit_record_rejected_without_mutation(repo, commit_record):
    record = recorded(repo)
    record.callbacks[0].evidence.commits = [commit_record]
    before = canonical_json(record)
    with pytest.raises(HandoffError):
        build_handoff(record)
    assert canonical_json(record) == before


def test_failed_output_write_leaves_no_destination_or_temporary_file(tmp_path, monkeypatch):
    import tempfile
    from coding_agent_harness.supervisor import _write_output

    original_open = Path.open
    original_temporary = tempfile.NamedTemporaryFile

    class FailingWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, raw):
            self.stream.write(raw[:3])
            raise OSError("PRIVATE failure detail")

    def failing_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return FailingWrite(stream) if mode == "xb" else stream

    monkeypatch.setattr(Path, "open", failing_open)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile",
                        lambda *args, **kwargs: FailingWrite(
                            original_temporary(*args, **kwargs)))
    destination = tmp_path / "summary.json"
    with pytest.raises(HandoffError, match="^output_unavailable$"):
        _write_output(destination, {"complete": True})
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_output_published_only_after_complete_write(tmp_path, monkeypatch):
    import os
    from coding_agent_harness.supervisor import _write_output

    destination = tmp_path / "summary.json"
    value = {"complete": True}
    original_link = os.link
    publications = []

    def inspect_publication(source, target):
        assert Path(source).parent == destination.parent
        assert Path(source).read_bytes() == canonical_json(value)
        assert not destination.exists()
        publications.append(target)
        return original_link(source, target)

    monkeypatch.setattr(os, "link", inspect_publication)
    _write_output(destination, value)
    assert publications == [destination]
    assert destination.read_bytes() == canonical_json(value)
    assert list(tmp_path.iterdir()) == [destination]


def test_output_concurrent_creation_is_not_overwritten(tmp_path, monkeypatch):
    import os
    from coding_agent_harness.supervisor import _write_output

    destination = tmp_path / "summary.json"
    original_link = os.link

    def competing_publication(source, target):
        destination.write_bytes(b"existing operator artifact")
        return original_link(source, target)

    monkeypatch.setattr(os, "link", competing_publication)
    with pytest.raises(HandoffError, match="^output_exists$"):
        _write_output(destination, {"complete": True})
    assert destination.read_bytes() == b"existing operator artifact"
    assert list(tmp_path.iterdir()) == [destination]


def test_output_publication_failure_cleans_temporary_file(tmp_path, monkeypatch):
    import os
    from coding_agent_harness.supervisor import _write_output

    def failed_publication(*args, **kwargs):
        raise OSError("PRIVATE filesystem detail")

    monkeypatch.setattr(os, "link", failed_publication)
    with pytest.raises(HandoffError, match="^output_unavailable$"):
        _write_output(tmp_path / "summary.json", {"complete": True})
    assert list(tmp_path.iterdir()) == []


def test_oversized_output_creates_no_artifacts(tmp_path):
    from coding_agent_harness.supervisor import _write_output, WIRE_LIMIT

    with pytest.raises(HandoffError, match="^document_too_large$"):
        _write_output(tmp_path / "summary.json", {"value": "x" * WIRE_LIMIT})
    assert list(tmp_path.iterdir()) == []
