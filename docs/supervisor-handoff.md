# Offline supervisor handoff

[README](../README.md) · [Architecture](architecture.md) · [CLI guide](usage.md)


The sidecar in `coding_agent_harness.supervisor` exchanges three strict JSON
documents: `SupervisorProposal`, `OperatorApproval`, and `HandoffSummary`.
It makes no model calls and never executes a proposal. An external supervisor
can propose a task, review, escalation, or stop. A review is a recommendation;
it is never substituted for a controller decision. An acceptance recommendation
may only acknowledge an already accepted, consistently recorded controller run.

Proposals reuse `TaskPacket`, ordered `VerificationCommand` records and an
`ExecutionConfig`. They require an explicit exact file allowlist (an empty list
permits no edits), unique checks including a required check, finite timeouts,
and zero remediation retries. Worker argv, model and optional runtime limits
are operator-selected data, not executed during validation. CLI limit support
and actual launcher/provider settings remain the operator's responsibility.

## One manually supervised round trip

Use a clean disposable target, an exchange directory, and a **protected operator
directory outside every worker- and supervisor-writable workspace**. Inspect
the complete proposed task, file scope, checks and execution settings first.
The following is an operator-side example for an initial task proposal.
`proposal.json` is written by the supervisor into the exchange directory;
the operator supplies the paths and a trusted existing reviewer.

```python
from pathlib import Path
from coding_agent_harness.supervisor import (
    OperatorApproval, canonical_json, canonical_digest, inspect_repository,
    load_proposal,
)

repo = Path("target-worktree").resolve()
exchange = Path("supervisor-exchange").resolve()
approval_dir = Path("operator-controlled-approvals").resolve()
proposal = load_proposal(exchange / "proposal.json")
spec = proposal.task_spec
assert proposal.action == "task"
assert inspect_repository(repo) == proposal.repository

# HUMAN CHECKPOINT: inspect spec.task, spec.verification_commands and
# spec.execution_config before creating this exact approval.
approval = OperatorApproval(
    proposal_sha256=canonical_digest(proposal),
    repository=proposal.repository,
    task_sha256=canonical_digest(spec.task),
    allowed_files=list(spec.task.allowed_files),
    verification_sha256=canonical_digest([
        check.model_dump(mode="json") for check in spec.verification_commands
    ]),
    execution_config_sha256=canonical_digest(spec.execution_config),
    approve_task=True, approve_scope=True,
    approve_checks=True, approve_configuration=True,
)
# Operator creates this file; never expose approval_dir to the supervisor.
approval_path = approval_dir / "approved.json"
with approval_path.open("xb") as stream:
    stream.write(canonical_json(approval))
```

Document validation may create an existing-compatible task file. It reports
`approval_validated`, which is distinct from a fresh execution preflight:

```powershell
coding-harness handoff validate supervisor-exchange/proposal.json `
  --repo target-worktree --approval operator-controlled-approvals/approved.json `
  --approval-dir operator-controlled-approvals --untrusted-root supervisor-exchange `
  --output task.json

coding-harness handoff preflight supervisor-exchange/proposal.json `
  --repo target-worktree --approval operator-controlled-approvals/approved.json `
  --approval-dir operator-controlled-approvals --untrusted-root supervisor-exchange `
  --task-file task.json --execution-config execution-config.json
```

`execution-config.json` must contain the exact approved `ExecutionConfig`.
`--untrusted-root` is repeatable: supply every additional worker/supervisor
writable root; the target is always included. Use `--handoff summary.json` for
proposals referring to a prior run. Validation/preflight return 0 on success,
2 for missing approval or failed preflight, and 1 for malformed/contradictory
documents or output errors. Outputs are create-only and errors do not echo
untrusted input. No supervisor execute or approval-writing CLI exists.

Immediately before an explicit controller call, the **trusted caller** must
reread approval and check the actual objects it will execute:

```python
from coding_agent_harness.controller import HarnessController
from coding_agent_harness.worker import QwenWorker
from coding_agent_harness.supervisor import (
    ExceptionObservation, RecordedRun, RecordingReviewer, build_handoff,
    execution_preflight, write_handoff,
)

cfg = spec.execution_config
# Operator has separately verified argv, CLI limits and local runtime settings.
worker = QwenWorker(command=cfg.command_prefix, model=cfg.model,
                    timeout_seconds=cfg.worker_timeout_seconds)
recorder = RecordingReviewer(trusted_reviewer)  # Supplied by the operator.
controller = HarnessController(worker=worker, reviewer=recorder,
                               max_remediation_attempts=cfg.max_remediation_attempts)
preflight = execution_preflight(
    proposal, handoff=None, approval_path=approval_path,
    trusted_approval_dir=approval_dir,
    untrusted_workspace_roots=[exchange], repo_dir=repo,
    task=spec.task, verification_commands=spec.verification_commands,
    execution_config=cfg,
)
if not preflight.passed:
    raise RuntimeError("Preflight failed; do not invoke the worker")

result = None
exception = None
observed_error = None
try:
    result = controller.run(task=spec.task, repo_dir=repo,
                            verification_commands=spec.verification_commands)
except Exception as error:
    observed_error = error
    # An exception is not an invented stopped/accepted HarnessRunResult.
    exception = ExceptionObservation(stage="controller",
                                     type_name=type(error).__name__)
record = RecordedRun(
    run_id="supervised-run", original_task=spec.task,
    verification_commands=spec.verification_commands,
    baseline=preflight.repository, baseline_source="trusted_preflight",
    controller_max_remediation_attempts=cfg.max_remediation_attempts,
    callbacks=recorder.records, result=result, exception=exception,
)
write_handoff(exchange / "summary.json", build_handoff(record))
if observed_error is not None:
    raise observed_error
# Inspect the summary before sharing it.
```

The supervisor can now read the summary and propose another narrow task or a
nonexecuting remediation/escalation/stop recommendation. The operator decides
whether to continue. A nested follow-up task is context only: execution needs
a separately submitted task proposal, exact new approval and a clean approved
baseline. The sidecar never commits, cleans or promotes a stopped run's state.

## Evidence and trust limitations

`RecordingReviewer` copies callback inputs, returns the delegate's exact
decision, and re-raises reviewer exceptions. Export checks original task and
baseline identities, ordered checks and verifier semantics, retry lineage,
final controller branches and observable audit payloads/order. Contradictory
provenance is rejected; original source records and controller results are
never repaired or replaced. Missing required callbacks/baseline/budget are
explicit. Unavailable **optional per-attempt** Git snapshots are separate:
an actual accepted result with complete required callback history remains
reportable. Dependent audit details are preserved but not claimed independently
verified; missing snapshots or audits are never reconstructed.

Summaries separate worker completion claims from Git observations, process
status, verification outcomes and recorded reviewer decisions. File-scope
compliance does not prove semantic task-scope compliance:
`semantic_task_scope` is always `not_assessed_by_handoff`. Valid blocked/failed
worker acceptance retains existing controller semantics and disagreement audits.

Wire JSON is limited to 64 KiB, 32 attempts and 64 checks/paths/audits; excess
is rejected rather than truncated. Duplicate keys, scalar coercion, unsupported
versions/actions and ambiguous Windows/Git-internal proposal paths are rejected.
Hashes use UTF-8 sorted-key compact JSON with defaults/nulls and ordered arrays,
without Unicode normalization; this is not RFC 8785. Default summaries omit
logs, diffs, argv, free-form summaries/rationales and status text. Retained labels
and paths are SHA-256 pseudonyms; original source digests and counts are retained.
These summaries can ground a next-step request but cannot alone prove semantic
correctness. Inspect them before sharing; metadata/digests are not a secrecy
guarantee, and additional evidence needs separate operator review.

**Integrity hashes do not authenticate a human.** The operator directory and
caller inventory of writable roots are trusted; resolved-path checks are not
ACL enforcement. Same-user workers may otherwise write anywhere. Filesystem
compromise or forged approvals within the trusted directory are outside this
prototype's guarantees. There is no signature/authentication service or sandbox.

The single-run CLI and bare library controller do not consume sidecar approval
files. A direct caller can bypass the sidecar. The separate control CLI uses
fresh foreground human authorization and review; it does not consume these
approval files either. Preflight uses fixed read-only Git commands with optional
locking and filesystem monitoring disabled; it does not run approved argv,
workers or checks. A pass is a point-in-time observation, not a lock or execution
capability. Repeat it if documents, Git state or runtime configuration change.
Ignored files, hidden settings, concurrent modifications and verifier/reviewer
side effects remain limitations. No live hosted supervisor integration exists;
a future adapter reads summaries and writes proposals, without receiving an
approval writer or process/controller capability.
