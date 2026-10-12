# Reliability hardening R2 findings

Date: 2026-10-12 (Europe/Helsinki). This is a bounded offline reliability review,
not a certification of the harness or its execution environment.

## Scope and provenance

- Public repository: `D:\Documents\coding-agent-harness-public`.
- Verified clean public `main` at `5a5a1b3277ca5b16701e327386d437deb240555b` before edits.
- New worktree: `D:\Documents\coding-agent-harness-reliability-r2`.
- New branch: `codex/reliability-hardening-r2`, created directly from that baseline.
- Test interpreter: `D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe`.
  Verified imports resolve to the R2 worktree, not the editable-install checkout.
- Environment: Windows, Python 3.12.10, pytest 9.1.1, Pydantic 2.14.0,
  psutil 7.2.2, Git 2.55.0.windows.2. Existing dependencies only.
- No production checkout edits, existing-branch updates, pushes, merges, PRs,
  live model calls, real Qwen invocations, holdout runs, benchmark changes,
  credential inspection, dependency installation, or system configuration.
  `D:\Documents\coding-harness-sol-qwen-pilot` was not accessed.

## Architecture and investigation priorities

The supervisor sidecar validates data and approvals but has no execution capability.
A trusted caller performs fresh preflight and explicitly invokes the approval-unaware
controller. The controller captures a clean baseline, invokes a worker, captures
cumulative and per-attempt Git evidence, runs independent checks, and obtains a
reviewer decision. Acceptance gates use worker classification, scope evidence and
verification. Remediation and scope revisions share a retry budget. The single-run
orchestrator is a separate consumer of the same Git evidence. RecordingReviewer
snapshots callback evidence; handoff export checks recorded provenance and projects
sanitized observations.

Candidates were prioritized by demonstrable impact:

| Candidate | Result / disposition |
| --- | --- |
| Rename evidence loses a deleted source and defeats scope gating | Confirmed; fixed in both evidence collectors; tested through controller, single-run orchestrator, and handoff. |
| Unexpected reviewer output falls into a retry branch | Confirmed; strict decision revalidation now precedes every branch. |
| Malformed remediation budget disables or alters termination bounds | Confirmed; exact integer validation added. |
| Conflicting completion records or duplicate keys overwrite prior claims | Confirmed; ambiguous protocol input rejected while retaining raw output. |
| Misbound/stale approvals, actual-input mismatch, writable approval roots, link escapes | Existing checks and tests reject these; no new bypass confirmed in this review. |
| Nonzero exits, ordinary timeout, child-tree termination with a live parent | Existing implementation/tests cover these; full offline suite includes them. |
| Launch failure, detached descendants, inherited pipe handles, kill failures | Remaining investigation area; no claim of comprehensive process containment or cleanup guarantees. |

## Confirmed issues and fixes

### R2-1: Rename source omitted from scope evidence — high severity

**Reproduction:** In a clean real Git repository containing `existing.txt`, allow
only `renamed.txt`. Move the tracked file to `renamed.txt` and stage the move.
Explicitly enable Git rename detection. An accepting fake reviewer then causes
baseline controller status `accepted`, despite deletion of a forbidden file.

**Root cause:** `git diff --name-only` can collapse a rename into its destination.
Both cumulative changed-file evidence and tree-to-tree per-attempt evidence used
this output directly. The missing source also propagated into handoff scope facts.

**Fix:** Add `--no-renames` to both name-only diff queries. A rename now contributes
its deletion and addition independently, regardless of repository rename settings.
Patch collection remains intact and can still display a human-readable rename.
No index reset, rollback, or filesystem access restriction was introduced.

**Regression evidence:** Staged and committed moves omitted the source from
cumulative evidence; even an unstaged move omitted it from per-attempt tree evidence.
The real controller integration returned `accepted` instead of `scope-violation`.
All now include both paths, reject a forbidden source, and preserve the observed
source in the exported handoff as an out-of-scope pseudonym and source digest.
The separate single-run path reports `needs-attention` with a disallowed-file finding.

**Compatibility:** Consumers of `changed_files` must expect both rename endpoints.
An exact allowlist must cover both. This implements the existing changed-path/scope
contract rather than adding a new result field.

### R2-2: Malformed reviewer decisions authorize retries — high severity

**Reproduction:** Return a duck-typed decision with `decision="unexpected"`, or a
ReviewDecision with an invalid action introduced through `model_copy`, mutation,
or unvalidated construction. With retries available, baseline invokes the worker
again. A malformed remediation payload or empty revised scope also caused a second
invocation. A mutated empty acceptance rationale was accepted without revalidation.

**Root cause:** A Python annotation does not validate runtime output. After checking
accept, stop, and revise-scope, the controller treated every other value as remediation.
Pydantic model instances can also be modified without assignment validation.

**Fix:** Require a ReviewDecision instance, serialize its data, and rebuild it using
strict validation before any branch. Validation reruns action and required-payload
checks rather than trusting instance identity. Malformed values raise ValueError
(including Pydantic ValidationError) and cannot authorize a retry or acceptance.
Serialization warnings are suppressed during this rebuild; strict validation still
rejects invalid types. Existing well-formed decisions retain their semantics.

**Regression evidence:** Four initial malformed retry cases observed `worker.calls=2`
instead of 1. The expanded suite also covers unvalidated construction and invalid
rationale types. All reject after exactly one initial invocation. A reviewer exception
already propagated without retry on baseline and remains a passing negative control.

**Remaining risk:** Reviewers are still trusted in-process code. This does not isolate
arbitrary Python plugins or prevent their filesystem/configuration side effects.
Invalid-review exceptions do not invent a normal stopped result; callers should
record the exception and preserve available evidence using existing recording APIs.

### R2-3: Retry budget permits nonfinite and fractional values — medium severity

**Reproduction:** Construct a controller with NaN, infinity, 1.5, or True as its
budget. Baseline accepts all four. For NaN, the exhaustion comparison is always false;
for positive infinity no finite retry count reaches the bound. Fractional limits can
permit more attempts than an integer interpretation suggests; booleans are integers
under Python comparisons. A string already failed closed with TypeError.

**Root cause:** Constructor validation only checked `budget < 0`; the annotation
was not enforced. The same value was subsequently used in the exhaustion comparison.

**Fix:** Require exact `int` type and nonnegative value. Legitimate 0, 1 and 2 budgets
still permit exactly 1, 2 and 3 invocations. Invalid types consistently raise ValueError
before execution. No new arbitrary maximum was imposed on legitimate integer budgets.

**Regression evidence:** NaN, infinity, 1.5 and True failed the original rejection
checks; the string case failed the expected exception contract. Negative budget was
already rejected and is a passing control. Valid budget termination is tested using
an always-remediate fake reviewer and real local Git evidence.

**Remaining risk:** The controller's collaborators/configuration remain trusted
mutable Python objects; this is not a capability system or a process resource limiter.

### R2-4: Ambiguous completion output silently overwrites claims — medium severity

**Reproduction:** A deterministic Python worker emits failed then completed records,
identical duplicate records, malformed then valid records, or a single JSON object
with duplicate `status` or `summary` keys. Baseline returns the final successful claim.

**Root cause:** The parser deliberately selected `records[-1]`, while normal JSON
loading silently retained the final value of duplicate keys. Neither ambiguity was
reported as a protocol failure despite the exactly-one-record worker contract.

**Fix:** Require exactly one prefixed record and reject duplicate JSON keys with an
object-pairs hook. Keep ProcessResult stdout/stderr and exit classification unchanged.
A rejected completion is represented by `completion=None` plus an error, and classified
as `protocol-failure` for a zero-exit worker. No valid earlier/later record is selected
as a fallback. Existing single valid, missing and malformed records retain their
established behavior.

**Regression evidence:** All five ambiguity cases originally produced a completion;
all now fail the protocol while preserving every original stdout line. Public worker
and process result schemas/signatures are unchanged.

**Remaining risk:** This is protocol consistency, not authentication of an assistant
response. Worker output remains untrusted. Output size and parser depth are not newly
bounded by R2; the subprocess collector still buffers output in memory.

## Verification evidence

Commands run from the isolated worktree using the existing public interpreter.
No tests required external model services.

| Run | Result |
| --- | --- |
| Initial baseline `python -m pytest -q` | All 228 test bodies reached completion, then pytest session cleanup failed with WinError 5 on the pre-existing global `pytest-current` temp link; process exit 1. Not reported as a successful suite run. |
| Baseline rerun, fresh worktree-local `--basetemp` | 228 original tests completed; exit 0. Test count separately confirmed with collection. |
| Initial new tests, original production source | 19 failed, 1 passed in 17.71s. |
| Expanded R2 tests, exact baseline production source | 23 failed, 5 passed in 27.74s; pytest exit 1 as expected. Source files restored in a finally block within R2 only. |
| Focused R2/controller/Git/worker/outcome suite | 61 passed in 16.09s. |
| Expanded R2 suite after fixes | 28 passed in 24.35s; one serializer warning prompted suppression during strict rebuild. |
| Complete final offline suite | 256 passed in 137.50s (0:02:17); exit 0; no warnings. |
| Independent read-only code review | No actionable correctness/regression findings; suggested constructor/type/zero-budget coverage was added. Reviewer did not independently rerun tests. |
| Diff hygiene | `git diff --check` passes. |

One intermediate handoff test referenced a nonexistent field; it was corrected to
`independent_observations.git_evidence_sha256` and retained literal scope assertions.
No production fix was added to accommodate that test error.

The five expanded original-code passing controls are negative budget, valid budgets
0/1/2, and reviewer exception propagation. Counts do not present them as newly fixed bugs.

Re-run final validation:

```powershell
Set-Location D:\Documents\coding-agent-harness-reliability-r2
& D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe -m pytest `
  -o 'addopts=' -q --basetemp=.pytest_cache/r2-review-NEW-UNIQUE-NAME
```

Use a fresh basetemp name: pytest manages/deletes the specified directory. Keep it
inside this dedicated worktree. Original-code reproductions used `git show` to obtain
only the three baseline production files, temporarily replaced those files in R2,
ran the new tests in a fresh process, and restored saved fixed bytes in a finally block.
The public checkout was never used as an editable reproduction target.

Captured local outputs remain under the ignored `.pytest_cache` directory:
`r2-red-output.txt`, `r2-original-expanded-output.txt`, and `r2-final-output.txt`.
These are supplementary local logs, not committed benchmark artifacts; the committed
tests and this report provide durable reproduction instructions and results.

## Exact change inventory and review

- `D:\Documents\coding-agent-harness-reliability-r2\coding_agent_harness\controller.py`:
  enforce integer budgets and revalidate reviewer decisions.
- `D:\Documents\coding-agent-harness-reliability-r2\coding_agent_harness\git.py`:
  preserve rename source/destination paths in cumulative and per-attempt evidence.
- `D:\Documents\coding-agent-harness-reliability-r2\coding_agent_harness\worker.py`:
  reject multiple completion records and duplicate JSON keys.
- `D:\Documents\coding-agent-harness-reliability-r2\tests\test_reliability_r2.py`:
  28 deterministic regression and control cases, real Git repositories, fake workers.
- `D:\Documents\coding-agent-harness-reliability-r2\README.md`:
  document the stricter public contracts.
- `D:\Documents\coding-agent-harness-reliability-r2\docs\reliability-r2-findings.md`:
  this findings and handoff report.

The six listed files are packaged in one local R2 commit; its exact identity
is supplied in the engineering handoff. A complete review patch is preserved at
`D:\Documents\coding-agent-harness-reliability-r2\.pytest_cache\reliability-r2.patch`. Review with:

```powershell
git log --oneline 5a5a1b3277ca5b16701e327386d437deb240555b..codex/reliability-hardening-r2
git diff --stat 5a5a1b3277ca5b16701e327386d437deb240555b..codex/reliability-hardening-r2
git diff 5a5a1b3277ca5b16701e327386d437deb240555b..codex/reliability-hardening-r2
```

## Outstanding risks and recommended next slice

1. Process cleanup warrants a separate bounded review: after timeout, cleanup catches
   psutil errors and a subsequent `communicate()` has no timeout. Detached descendants
   or inherited pipes may outlive the parent. R2 does not establish reliable cleanup
   in all such cases. Add deterministic finite-lived descendant/pipe reproductions
   before selecting a cross-platform termination strategy; never use live workers.
2. Startup OSError can abort before normal result recording. Investigate preserving
   prior Git evidence and completed verification results without misclassifying a
   failed launch as an expected command failure or authorizing automatic retries.
3. Git evidence excludes ignored/external state and is captured before verifier and
   reviewer side effects. Those commands/plugins can change the target afterward.
   Investigate explicit final-state evidence separately; no semantic-scope claim is made.
4. Approval digests prove binding, not human identity. Trusted directories and supplied
   writable-root inventories are assumptions. Preflight is a point-in-time observation,
   direct controllers remain approval-unaware, and ordinary resolved-path checks do
   not enforce permissions or prevent races. No Windows filesystem isolation or
   OS-enforced sandbox has been established.
5. Output limits/deep malformed completion handling and Git/path edge cases beyond
   tested rename semantics remain useful future probes. Do not infer universal scope
   enforcement or robust termination from the passing offline suite.

Keep this worktree and branch for independent review. No push, integration, or cleanup
of other worktrees is part of this task.
