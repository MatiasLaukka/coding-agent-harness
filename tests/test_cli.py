from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coding_agent_harness import cli


def write_task_file(
    path: Path,
) -> None:
    path.write_text(
        json.dumps(
            {
                "task": {
                    "task_id": "task-1",
                    "goal": (
                        "Make one bounded change."
                    ),
                    "constraints": [
                        (
                            "Do not make unrelated "
                            "changes."
                        ),
                    ],
                    "acceptance": [
                        (
                            "Independent verification "
                            "passes."
                        ),
                    ],
                    "suggested_files": [
                        "src/example.py",
                    ],
                    "allowed_files": [
                        "src/example.py",
                    ],
                },
                "verification_commands": [
                    {
                        "id": "tests",
                        "argv": [
                            "python",
                            "-m",
                            "pytest",
                            "-q",
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def make_result(
    *,
    status: str = "ready-for-review",
    findings: list | None = None,
):
    return SimpleNamespace(
        task_id="task-1",
        baseline_sha="a" * 40,
        final_head_sha="a" * 40,
        worker=SimpleNamespace(
            process=SimpleNamespace(
                exit_code=0,
                timed_out=False,
            ),
            completion=SimpleNamespace(
                status="completed",
                summary=(
                    "Implemented the requested change."
                ),
            ),
            completion_error=None,
        ),
        changed_files=[
            "src/example.py",
        ],
        patch=(
            "diff --git a/src/example.py "
            "b/src/example.py\n"
        ),
        verification=SimpleNamespace(
            passed=True,
        ),
        findings=(
            findings
            if findings is not None
            else []
        ),
        status=status,
    )


class FakeQwenWorker:
    created: list[
        "FakeQwenWorker"
    ] = []

    def __init__(
        self,
        *,
        command,
        model,
        timeout_seconds,
    ) -> None:
        self.command = command
        self.model = model
        self.timeout_seconds = (
            timeout_seconds
        )

        self.created.append(
            self
        )


def test_run_command_executes_task_and_prints_review_surface(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    task_file = (
        tmp_path
        / "task.json"
    )

    write_task_file(
        task_file
    )

    repo_dir = (
        tmp_path
        / "repo"
    )
    repo_dir.mkdir()

    FakeQwenWorker.created.clear()

    captured = {}

    def fake_run_task(
        *,
        task,
        repo_dir,
        worker,
        verification_commands,
    ):
        captured["task"] = task
        captured["repo_dir"] = repo_dir
        captured["worker"] = worker
        captured[
            "verification_commands"
        ] = verification_commands

        return make_result()

    monkeypatch.setattr(
        cli,
        "QwenWorker",
        FakeQwenWorker,
    )

    monkeypatch.setattr(
        cli,
        "run_task",
        fake_run_task,
    )

    exit_code = cli.app(
        [
            "run",
            str(task_file),
            "--repo",
            str(repo_dir),
            "--model",
            "qwen-test-model",
            "--worker-timeout",
            "123",
        ]
    )

    output = (
        capsys
        .readouterr()
    )

    assert exit_code == 0

    assert len(
        FakeQwenWorker.created
    ) == 1

    worker = (
        FakeQwenWorker.created[0]
    )

    assert worker.command == [
        "qwen",
    ]
    assert worker.model == (
        "qwen-test-model"
    )
    assert worker.timeout_seconds == 123.0

    assert captured[
        "task"
    ].task_id == "task-1"

    assert captured[
        "task"
    ].allowed_files == [
        "src/example.py",
    ]

    assert captured[
        "repo_dir"
    ] == repo_dir.resolve()

    assert len(
        captured[
            "verification_commands"
        ]
    ) == 1

    assert captured[
        "verification_commands"
    ][0].id == "tests"

    assert (
        "Status: ready-for-review"
        in output.out
    )
    assert (
        "Verification: PASS"
        in output.out
    )
    assert (
        "src/example.py"
        in output.out
    )
    assert (
        "Patch:"
        in output.out
    )
    assert (
        "diff --git"
        in output.out
    )

    assert output.err == ""


def test_run_command_returns_two_for_mechanical_findings(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    task_file = (
        tmp_path
        / "task.json"
    )

    write_task_file(
        task_file
    )

    repo_dir = (
        tmp_path
        / "repo"
    )
    repo_dir.mkdir()

    finding = SimpleNamespace(
        code="disallowed-file",
        detail=(
            "Changed file is outside "
            "allowed scope: other.py"
        ),
    )

    monkeypatch.setattr(
        cli,
        "QwenWorker",
        FakeQwenWorker,
    )

    monkeypatch.setattr(
        cli,
        "run_task",
        lambda **kwargs: make_result(
            status="needs-attention",
            findings=[
                finding,
            ],
        ),
    )

    exit_code = cli.app(
        [
            "run",
            str(task_file),
            "--repo",
            str(repo_dir),
            "--model",
            "qwen-test-model",
        ]
    )

    output = (
        capsys
        .readouterr()
    )

    assert exit_code == 2

    assert (
        "Status: needs-attention"
        in output.out
    )
    assert (
        "[disallowed-file]"
        in output.out
    )


def test_run_command_rejects_invalid_task_file_before_worker_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    task_file = (
        tmp_path
        / "invalid.json"
    )

    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "task_id": "task-1",
                }
            }
        ),
        encoding="utf-8",
    )

    repo_dir = (
        tmp_path
        / "repo"
    )
    repo_dir.mkdir()

    FakeQwenWorker.created.clear()

    monkeypatch.setattr(
        cli,
        "QwenWorker",
        FakeQwenWorker,
    )

    exit_code = cli.app(
        [
            "run",
            str(task_file),
            "--repo",
            str(repo_dir),
            "--model",
            "qwen-test-model",
        ]
    )

    output = (
        capsys
        .readouterr()
    )

    assert exit_code == 1
    assert (
        "Task file error:"
        in output.err
    )
    assert (
        FakeQwenWorker.created
        == []
    )

def test_handoff_cli_needs_approval_and_no_output(tmp_path,capsys,monkeypatch):
    from coding_agent_harness.supervisor import (
        SupervisorProposal,RepositoryIdentity,TaskSpecification,ExecutionConfig,canonical_json)
    from coding_agent_harness.models import TaskPacket,VerificationCommand
    def forbid(*args,**kwargs):
        raise AssertionError("No execution allowed")
    monkeypatch.setattr(cli,"QwenWorker",forbid)
    monkeypatch.setattr(cli,"run_task",forbid)
    p=SupervisorProposal(proposal_id="p",repository=RepositoryIdentity(
        repository_sha256="a"*64,baseline_commit="b"*40,baseline_tree="c"*40),
        action="task",rationale="slice",task_spec=TaskSpecification(
            task=TaskPacket(task_id="t",goal="small",acceptance=["pass"],allowed_files=[]),
            verification_commands=[VerificationCommand(id="test",argv=["do-not-run"])],
            execution_config=ExecutionConfig(command_prefix=["do-not-run"],model="local",
                                            worker_timeout_seconds=1.0)))
    proposal=tmp_path/"proposal.json"; proposal.write_bytes(canonical_json(p))
    output=tmp_path/"task.json"
    assert cli.app(["handoff","validate",str(proposal),"--repo",str(tmp_path),
                    "--output",str(output)]) == 2
    feedback=json.loads(capsys.readouterr().out)
    assert feedback["status"]=="needs_operator_approval"
    assert feedback["no_execution_performed"]
    assert not output.exists()


def test_handoff_cli_errors_do_not_echo_input(tmp_path,capsys):
    p=tmp_path/"secret-input.json"
    p.write_text('{"secret":"SECRET-CREDENTIAL"}',encoding="utf8")
    assert cli.app(["handoff","validate",str(p),"--repo",str(tmp_path)])==1
    captured=capsys.readouterr()
    assert "SECRET-CREDENTIAL" not in captured.out+captured.err
    assert json.loads(captured.out)["status"]=="invalid"


def handoff_fixture(tmp_path):
    import subprocess
    from coding_agent_harness.models import TaskPacket,VerificationCommand
    from coding_agent_harness.supervisor import (
        SupervisorProposal,TaskSpecification,ExecutionConfig,OperatorApproval,
        inspect_repository,canonical_json,canonical_digest)
    repo=tmp_path/"target"; repo.mkdir()
    def git(*args):
        subprocess.run(["git",*args],cwd=repo,check=True,capture_output=True)
    git("init","-b","main"); git("config","user.name","Fixture")
    git("config","user.email","fixture@example.invalid")
    (repo/"module.py").write_text("value=1\n")
    git("add","module.py"); git("commit","-m","baseline")
    p=SupervisorProposal(proposal_id="p",repository=inspect_repository(repo),
        action="task",rationale="narrow",task_spec=TaskSpecification(
            task=TaskPacket(task_id="t",goal="small",acceptance=["pass"],allowed_files=["module.py"]),
            verification_commands=[VerificationCommand(id="test",argv=["never-run"])],
            execution_config=ExecutionConfig(command_prefix=["never-run"],model="local",
                                            worker_timeout_seconds=1.0)))
    proposal=tmp_path/"proposal.json"; proposal.write_bytes(canonical_json(p))
    directory=tmp_path/"protected"; directory.mkdir()
    approval=directory/"approved.json"
    spec=p.task_spec
    approval.write_bytes(canonical_json(OperatorApproval(
        proposal_sha256=canonical_digest(p),repository=p.repository,
        task_sha256=canonical_digest(spec.task),allowed_files=spec.task.allowed_files,
        verification_sha256=canonical_digest([c.model_dump(mode="json") for c in spec.verification_commands]),
        execution_config_sha256=canonical_digest(spec.execution_config),
        approve_task=True,approve_scope=True,approve_checks=True,approve_configuration=True)))
    cfg=tmp_path/"config.json"; cfg.write_bytes(canonical_json(spec.execution_config))
    return repo,proposal,approval,directory,cfg


def test_handoff_cli_approved_materialization_and_preflight(tmp_path,capsys,monkeypatch):
    repo,p,approval,directory,cfg=handoff_fixture(tmp_path)
    def forbid(*a,**kw):
        raise AssertionError("No worker invocation")
    monkeypatch.setattr(cli,"QwenWorker",forbid); monkeypatch.setattr(cli,"run_task",forbid)
    task_file=tmp_path/"task.json"
    base=[str(p),"--repo",str(repo),"--approval",str(approval),"--approval-dir",str(directory)]
    assert cli.app(["handoff","validate",*base,"--output",str(task_file)])==0
    assert json.loads(capsys.readouterr().out)["status"]=="approval_validated"
    assert cli._load_task_file(task_file).task.allowed_files==["module.py"]
    check=["handoff","preflight",*base,"--task-file",str(task_file),"--execution-config",str(cfg)]
    assert cli.app(check)==0
    assert json.loads(capsys.readouterr().out)["passed"]
    (repo/"untracked").write_text("dirty")
    assert cli.app(check)==2
    assert not json.loads(capsys.readouterr().out)["passed"]
    original=task_file.read_bytes()
    assert cli.app(["handoff","validate",*base,"--output",str(task_file)])==1
    assert json.loads(capsys.readouterr().out)["errors"]==["output_exists"]
    assert task_file.read_bytes()==original


def test_handoff_cli_paired_approval_flags(tmp_path,capsys):
    repo,p,approval,directory,cfg=handoff_fixture(tmp_path)
    assert cli.app(["handoff","validate",str(p),"--repo",str(repo),
                    "--approval",str(approval)])==1
    assert json.loads(capsys.readouterr().out)["errors"]==["approval_directory_required"]


def test_handoff_cli_malformed_approval_is_input_error(tmp_path,capsys):
    repo,p,approval,directory,cfg=handoff_fixture(tmp_path)
    task_file=tmp_path/"task.json"
    base=[str(p),"--repo",str(repo),"--approval",str(approval),"--approval-dir",str(directory)]
    assert cli.app(["handoff","validate",*base,"--output",str(task_file)])==0
    capsys.readouterr()
    approval.write_text('{"secret":"SECRET-CREDENTIAL"}',encoding="utf8")
    assert cli.app(["handoff","preflight",*base,"--task-file",str(task_file),
                    "--execution-config",str(cfg)])==1
    captured=capsys.readouterr()
    assert "SECRET-CREDENTIAL" not in captured.out+captured.err
    assert json.loads(captured.out)["errors"]==["invalid_document"]
