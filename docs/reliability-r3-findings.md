# R3: subprocess lifecycle and evidence preservation

## Baseline and isolation

Date: 2026-10-12. Worktree: `D:\Documents\coding-agent-harness-reliability-r3`.
Branch: `codex/reliability-hardening-r3`.

Fetched `origin/main` from `https://github.com/MatiasLaukka/coding-agent-harness.git`.
The starting commit is `faf63f759e0fcff67c50c5eb7a07b406554e71c0`, the merged
R2 PR #4. The requested R2 commit `bb02da1a7dfc84cc0f5df3474e448ed5ddaaa9e8`
is **not an ancestor** because it was squash-merged. Its complete tree and
current main's tree are identical: `13e384ecf89080b0704323bf8a5dcd484eddf2ae`.
`git diff bb02da1 origin/main` was empty. R3 is based on current merged main,
not the earlier `5a5a1b3` checkout. Existing checkout HEADs and files were not
changed; fetching refreshed shared remote metadata and worktree creation added
only the explicitly requested branch/checkout. No push, merge or PR was made.

Testing used `D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe`:
Python 3.12.10, pytest 9.1.1, psutil 7.2.2, Pydantic 2.14.0,
Windows 10 build 19045. No dependencies were installed. No worker sessions,
provider calls, credentials, experiment lanes, holdouts, customer systems, or
code from other projects were used.

## Confirmed findings

| Finding | Severity | Original behavior and impact | Fix |
| --- | --- | --- | --- |
| R3-1: deadline lost after timeout | High | Timeout called tree cleanup then **unbounded** `communicate()`. An exited root with an inherited writer, or unsuccessful termination, waited for that writer indefinitely. | Concurrent pipe readers and a monotonic collection deadline; bounded tree waits and a shared 0.2 s final drain. Never communicate or synchronously close an active reader's pipe on the calling thread. |
| R3-2: cancellation and cleanup evidence absent | High | Cancellation during collection leaked the child and discarded partial evidence. Tree discovery/termination errors were suppressed and survivors from the final wait were ignored. | Report cleanup errors and observed surviving PIDs; preserve original exceptions/cancellation with a `process_result`; cleanup on collection/startup exceptions. Root disappearance explicitly records uncertainty. |
| R3-3: observations discarded by later failure | High | Missing executable raised without structured launch evidence. A later launch, Git patch capture, verification, reviewer, or result-construction exception discarded completed observations and prior attempts. CLI printed only the exception message. | Structured failed launch; retained verification observations and per-stage/attempt exception context; partial Git capture; CLI failure evidence with hashed command identity. Infrastructure failure cannot authorize remediation/scope retry. |
| R3-4: unlimited output buffering | Medium | `communicate()` retained the entire output in memory. Excessive output could exhaust memory before a useful failure result was constructed. | Retain at most 1 MiB of **raw bytes per stream**, continue draining discarded bytes, report truncation separately, reject incomplete/truncated worker completion evidence. |

These four groups were selected for coherent scope. Ordinary nonzero exits and
strict completion parsing already worked; no speculative protocol redesign was
needed. Windows psutil termination worked for a discoverable live parent tree,
as demonstrated by both existing tests and a new finite child/grandchild test.
It does not supply OS-enforced containment.

## Original-code reproduction

The unchanged production package was exported with `git ls-tree` and `git show`
from `faf63f7` into `.r3-evidence/original`. The regression runner prepended that
export to `sys.path` and used pytest's `--import-mode=importlib`. Each outer probe
explicitly imports the same selected package. This avoids the existing venv's
editable installation resolving to the older public checkout when a script is
run from a temporary directory.

Controlled inherited-pipe children slept four seconds; the requested process
budget was one second. Original code returned in **4.375 s**, beyond the test's
2.5 s acceptance bound. A no-op tree terminator likewise returned in
**4.109 s**, waiting for natural process exit. The eight-second outer deadline
was independent of the harness. Both fixtures kill only recorded PID/creation-time
identities in `finally`; children also expire on their own.

The original output test retained **2,097,152 bytes per stream**, exceeding the
1,048,576-byte capture bound. Output-flood children have independent four-second
watchdogs, so even blocked writes cannot live indefinitely. Tests also demonstrate:

- A nonexistent executable raises `FileNotFoundError`, rather than returning
  invoked argv, failed-launch status, unknown exit, elapsed time and failure type.
- A first successful verification check is inaccessible after the next command
  fails to launch; both `expected_failure` and `advisory` require infrastructure
  failure to remain distinguishable from an ordinary command result.
- RuntimeError and KeyboardInterrupt at Git evidence, attempt evidence,
  verification and review stages have no accessible completed context.
- A later authorized attempt and result construction lose prior observations.
- A patch capture exception loses already read Git HEAD/status/changed paths.
- Simulated psutil AccessDenied returns no cleanup report; the survivor is ignored.
- A reader error loses available stdout; cancellation leaves the child running.
- A timeout followed by reviewer remediation invokes another worker.
- A completion record at the beginning of subsequently truncated stdout could
  be accepted without proving that the rest of stdout contains no duplicate.

**27 tests failed and 9 were deselected in 38.73 s** against the immutable
original package at that investigation stage. Four subsequent tests cover
handoff compatibility and normalized provenance, bringing the new total to 40.

The committed [original-code transcript](reliability-r3-evidence/original-code.txt)
contains the exact failing assertions and failure types. Implementation-specific
review guards are excluded from that original-code run, rather than calling a
missing new helper an original defect. The separate CLI red run produced no
`Failure evidence:` line and allowed KeyboardInterrupt to escape.

## Final behavior and evidence

`ProcessResult` retains its original fields and adds defaulted observations:
`started`, `failure_type`, `cancelled`, `output_complete`, stream truncation flags,
`cleanup_attempted`, `cleanup_errors`, and `surviving_pids`. `exit_code=None`
means unknown, never a synthetic successful exit. Failed launch uses
`started=False`, actual sanitized exception class, empty unavailable streams,
and measured elapsed duration. Cleanup errors remain separate from execution
failure and ordinary nonzero exit.

A complete capture means both reader threads saw EOF, closed their streams and
reported no capture error. Truncation remains separately observable even with EOF.
The raw-byte cap precedes UTF-8 decoding with replacement; malformed input may
expand when re-encoded, but the retained raw buffers and decoded string size stay
bounded. No marker is inserted into raw output and no completion is parsed from
incomplete or truncated stdout. Nonzero exit and timeout still retain available
stdout/stderr and actual exit code when observable.

Exceptions retain their original type and identity. Collection/startup exceptions
carry `exc.process_result`; interrupted verification carries
`exc.verification_result` (overall false, completed checks only) and
`exc.failed_command`. Controller and single-run exceptions carry
`exc.harness_evidence`, with stage, sanitized failure type and available native
models. Controller context retains each prior attempt's task, snapshot, worker,
Git evidence, completed verification and validated decision. Absent fields mean
unavailable evidence; they are not filled with fabricated successful models.
Git capture exceptions carry `exc.git_evidence`, a partial dictionary with only
completed fields, forwarded as `partial_git_evidence`. Original Git
CalledProcessError objects retain their command/output/exit information.

The CLI reports an allowlisted failure summary on stderr as
`Failure evidence: {JSON}`. The disclosure correction described below replaces
the initial recursive R3 printer. Raw output, patches, task text, paths, arbitrary
evidence fields and exception messages stay in the native Python evidence.
Ordinary runtime errors still exit 1 and cancellation exits 130. Reporting failure
produces a fixed unavailable message. API callers retain the original exception
and native models and may serialize them with `model_dump`. This is not a durable
execution journal.

Reviewer calls still receive partial work and may explicitly stop. Acceptance
cannot turn execution/protocol failure or failed verification into success.
Remediation and scope revision are blocked after launch, timeout, cleanup,
capture/truncation or unknown-exit infrastructure failures, including verification
infrastructure failures. Ordinary command failures retain existing reviewer and
verification expectation behavior. R2 decision revalidation, retry budgets,
rename endpoints and single-record/unique-key parsing remain unchanged.

Supervisor handoffs project defaulted `worker_infrastructure_failed` and check
`infrastructure_failed` flags, preserving failure classification without exposing
raw arguments/output. Native and loaded provenance checks reject subsequent
attempts after observable infrastructure failure. Timeout and missing exit remain
independently authoritative even when the new flags are omitted.

Measured Windows probes ([transcript](reliability-r3-evidence/deadlines.txt)):

| Case | Budget | Return time | Reported evidence |
| --- | --- | --- | --- |
| Exited parent, inherited writer | 1 s | 1.219 s | root exit 0, incomplete output, `tree-root-unavailable`; no claim that descendants died |
| Deliberately disabled termination | 1 s | 1.203 s | unknown exit, incomplete output, cleanup unavailable/root still running, observed root PID |
| Live parent + child + grandchild | 1.5 s | 1.687 s | root exit 15, complete available output, no cleanup errors or observed survivors |

Tree cleanup requests termination, waits at most 0.5 s, escalates discovered
survivors to kill, then waits at most 1 s. The final reader drain shares one
0.2 s budget. Scheduling and OS calls add overhead; this is a bound on harness
wait/drain logic, not a hard real-time guarantee for process creation or kernel /
psutil calls.

## Validation

Committed transcripts normalize line endings and trailing whitespace; raw local
captures remain under `.r3-evidence`. Failure content and measurements are retained.

The first bare baseline run reached 100% but pytest session cleanup then raised
WinError 5 on an existing `%TEMP%\pytest-of-matia\pytest-current` symlink. No
existing temp tree was altered. Dedicated `--basetemp` directories under R3
removed that environment interference. The unchanged baseline rerun passed
**256 tests in 149.47 s**; a second complete immutable-export baseline passed
**256 tests in 178.51 s** and appears in the committed baseline log.

**Final complete offline suite: 296 passed in 315.47 s (5:15)**, including all
40 new R3 regression cases. See the [final transcript](reliability-r3-evidence/final.txt).
After supervisor alignment, the focused supervisor/R3/R2 suite passed **213 tests
in 175.53 s**, followed by targeted normalized-provenance checks and the full run.
Focused compatibility: **72 passed in 63.69 s** (R3, CLI, R2). Lifecycle/verifier:
**45 passed in 30.24 s**. Real deadline/tree probes: **3 passed in 6.41 s**.

All new real lifecycle probes have an independent eight-second outer deadline;
children have four-second finite lifetimes or watchdogs, and fixture cleanup
checks recorded creation-time identities before killing. In-memory reader probes
have finite waits and join/cleanup in finally. No arbitrary process-name cleanup
or user-process termination is used.

The first complete candidate run yielded **291 passed, 1 failed in 217.21 s**.
It exposed supervisor provenance assuming that an advisory timeout still allowed
acceptance. The single existing advisory-timeout assertion was strengthened from
`accepted` to `verification-failure`, consistent with the requirement that an
unknown outcome cannot be verified. Supervisor validation and projections were
aligned; none of the R2 safety assertions or other existing tests were weakened.
The [initial candidate log](reliability-r3-evidence/initial-final.txt) is retained.

An independent read-only reviewer found two important issues in the initial new
code: cleanup cancellation could be swallowed or replace a previous failure;
and reader ownership was recorded after start. Both received deterministic red
regressions and fixes. Follow-up directly reran all three review guards and
approved those changes with no remaining Important finding in that reviewed
scope. A final review found that loaded handoffs lacked the native infrastructure
retry guard. Two additional deterministic regressions failed before the guard
was added. Independent probes then verified explicit flags, timeout, missing
exit, correct worker-failure summaries, and verification failures. The final
review approved the correction with no remaining Important issue in that scope.


### Reproduction commands

Run from the R3 worktree with the existing venv. To rerun the unchanged complete
baseline after the implementation, export its tests as well as its production
package. This preserves the historical advisory-timeout policy for that run:

```python
# Run with D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe
import io, subprocess, sys, zipfile
from pathlib import Path
import pytest
base = "faf63f759e0fcff67c50c5eb7a07b406554e71c0"
root = Path.cwd()
export = root / ".r3-evidence" / "baseline-reproduction"
archive = subprocess.check_output(["git", "archive", "--format=zip", base,
                                  "coding_agent_harness", "tests", "pyproject.toml"])
with zipfile.ZipFile(io.BytesIO(archive)) as z:
    z.extractall(export)
sys.path.insert(0, str(export))
raise SystemExit(pytest.main([str(export / "tests"), "--import-mode=importlib",
    "--basetemp=" + str(root / ".r3-evidence" / "baseline-reproduction-temp"),
    "-o", "addopts=", "-q"]))
```

For original-code regressions, similarly export only `coding_agent_harness` into
`.r3-evidence/original`, then in a fresh Python interpreter run:

```python
import sys, pytest
sys.path.insert(0, ".r3-evidence/original")
raise SystemExit(pytest.main(["tests/test_reliability_r3.py",
    "--import-mode=importlib", "--basetemp=.r3-evidence/red-reproduction-temp",
    "--tb=short", "-o", "addopts=", "-q", "-k",
    "not verification_infrastructure_failure_blocks_retry and "
    "not cancellation_during_cleanup and not drain_interruption and "
    "not interrupted_reader_start and not discoverable_child and "
    "not cli_reports and not handoff_preserves and not loaded_handoff"]))
```

The selection exercises the same 27 original-code failures; the current suite
also deselects four later handoff regressions. For the final implementation:

```powershell
& 'D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe' -m pytest --basetemp=.r3-evidence/retest-temp -o addopts='' -q
```

## Compatibility and remaining limits

- Public function call signatures, result status literals and reviewer interfaces
  remain unchanged. New process fields are defaulted, and verification checks
  inherit them. Existing Python model construction remains valid. Serialized
  JSON has additive fields; older `extra=forbid` consumers require coordinated
  upgrade. Recompute canonical evidence digests for newly serialized records;
  no old records or frozen documents were rewritten. The two new handoff flags
  also change canonical digests when historical JSON is loaded and serialized;
  old `parent_handoff_sha256` bindings may need regeneration after upgrade.
  Bindings fail closed; historical artifacts were not rewritten.
- Launch OSError now returns structured failure instead of raising. Invalid
  arguments still raise; non-finite/non-positive timeouts are rejected. Runtime
  exceptions and cancellation still propagate with evidence. Callers previously
  catching only launch OSError must inspect `started` / `failure_type` instead.
- Infrastructure failure in an advisory check is now overall verification
  failure. Output truncation also prevents success/retry. Reviewer remediation
  after timeout is intentionally no longer permitted; a new run requires caller
  action. These changes prevent unknown outcomes from being called verified.
- Descendants can exit, reparent, spawn during discovery or escape psutil's
  observed tree. An exited root cannot be used to rediscover every child. There
  is no Windows Job Object / POSIX containment contract, and an empty
  `surviving_pids` list proves only that none were observed, not universal exit.
- If an undiscovered/surviving writer keeps a pipe open, a daemon reader may stay
  blocked until its next read/EOF. Its memory remains capped; the harness returns
  with incomplete evidence and does not retry. It can still retain a thread and
  handle. OS containment or cancellable platform I/O would be needed to remove
  that remaining resource risk. No speculative platform abstraction was added.
- Git helpers still use subprocess.run without a deadline and collect full Git
  output; patch/snapshot processing can be expensive or blocked independently
  of run_process. Partial capture now survives ordinary exceptions/cancellation,
  but bounded Git execution and repository-wide resource limits are a separate
  unresolved case. A wedged process-creation/kernel/psutil call is also outside
  the demonstrated user-space wait bound.
- API exception context and CLI output preserve observed evidence, not crash
  durability. Hard process kill, interpreter crash, repeated fatal interruption,
  output serialization failure, or unrecoverable memory exhaustion cannot be
  guaranteed to produce a complete artifact. No missing checks are marked passed.
- Platform validation was performed on Windows only. Portable tests use ordinary
  pipes/psutil; the actual tree observations do not establish behavior on every OS.

## Review handoff

Production changes are confined to process, worker, verifier, controller, Git
capture, orchestrator, CLI and supervisor modules. Forty new regression cases
are in `tests/test_reliability_r3.py`; `tests/test_supervisor.py` strengthens the
advisory-timeout expectation; `.gitignore` excludes only local R3 export/temp
artifacts. Findings and selected execution logs are committed for review.

Review the exact change with `git diff faf63f759e0fcff67c50c5eb7a07b406554e71c0 HEAD`
or the local `.r3-evidence/review.diff` generated after the commit. The implementation
commit ID is supplied in the engineering handoff; it cannot be embedded in its
own committed contents without a self-reference. Main and existing checkout
files remain unchanged. No push, merge or pull request is authorized or performed.


## CLI disclosure follow-up (pending independent review)

Baseline: local R3 commit `b2d003b26d0ab6b3da2b616bfef53c7149d91062` on the
same isolated branch. Severity: **High**. The recursive evidence printer exposed
worker stdout/stderr, completed verification output, Git patches, cleanup text,
check IDs and unknown dictionary fields. The run-error handler printed raw
exception messages; task validation also exposed rejected input through
Pydantic's exception text. These channels can put repository secrets in terminal
or CI logs even though command arguments were hashed.

Nine offline regressions failed against the original R3 code in **8.01 s**.
They use distinctive fake secrets with real local Git capture, the actual
single-run/controller wrappers and verification collector, and in-memory workers
and subprocess results. Both RuntimeError and KeyboardInterrupt preserve a
successful completed check before a subsequent failure. No worker executable,
model, provider, protected experiment or network is involved.

The correction changes only the CLI presentation boundary. Its explicit
projection includes known stage/type labels, attempted invocation count (null
when unknown), process started/exit/timeout/cancellation and elapsed metadata,
output completeness/truncation, infrastructure-failure flags, cleanup error and
observed survivor counts, completed-check observations, validated Git commit
identities, and SHA-256 identities for commands, captured streams, patches, status,
check IDs and cleanup errors. Unknown exception type names become `other` plus
an identity hash; unknown stages become `unknown`. Unknown evidence fields and
objects are ignored rather than recursively serialized. Cleanup counts do not
assert universal process-tree containment. No missing check is called successful.

The printer neither mutates nor replaces `exc.harness_evidence`, native process
results, captured streams, Git evidence, completed checks or the original
exception/message. The tests inspect them after CLI reporting and prove all
fake-secret markers are absent from stderr (and stdout for these exception
paths). Input/setup failures also have fixed messages and safe summaries.
All existing R2/R3 tests and assertions remain intact.

Focused CLI/integration/R2/R3 suite: **86 passed in 73.21 s**. After the final
unknown-count adjustment, disclosure regressions: **9 passed in 5.25 s**.
**Complete offline suite: 305 passed in 193.62 s (3:13)**. Separate
[red](reliability-r3-evidence/cli-disclosure-red.txt),
[focused](reliability-r3-evidence/cli-disclosure-focused.txt),
[regression](reliability-r3-evidence/cli-disclosure-green.txt) and
[full](reliability-r3-evidence/cli-disclosure-full.txt) transcripts are committed.

Reproduce using the existing venv from the R3 worktree:

```powershell
& 'D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe' -m pytest tests/test_cli_failure_evidence.py tests/test_cli.py tests/test_cli_integration.py tests/test_reliability_r2.py tests/test_reliability_r3.py --basetemp=.r3-evidence/cli-review-focused-temp -o addopts='' -q
& 'D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe' -m pytest --basetemp=.r3-evidence/cli-review-full-temp -o addopts='' -q
```

Compatibility: the `Failure evidence:` line now contains a summary, not a raw
recursive evidence dump. Consumers requiring rich content must inspect the
Python exception evidence in an authorized environment. Python APIs, result
models, exit codes, retry policy and process/Git execution behavior are unchanged.
No opt-in raw CLI dump was added. The original R3 transcripts remain unchanged;
follow-up red/focused/full transcripts are recorded separately.

Remaining risks: plain hashes provide correlation and can permit guessing of
low-entropy content; they are not encryption. Native Python evidence deliberately
retains sensitive content and requires appropriate access control when exported.
The ordinary CLI review surface on stdout still prints task results and patches;
this correction targets exception/input failure stderr, not all CLI output or
argument-parser diagnostics. It does not remove R3's documented containment,
blocked-reader, Git deadline or crash-durability limitations.

The local follow-up diff is `.r3-evidence/cli-disclosure-review.diff`, measured
against the R3 baseline above; its commit ID is supplied in the handoff.
No push, PR, merge or change to main. **Stop for independent review.**


## Invocation-count correction (pending review)

Baseline: `2b10e6618099291bdf5137ae99cb61f3f3582dd4`, the CLI disclosure
follow-up. The controller appends an attempt record before taking its snapshot;
that record does not prove that a worker was invoked. The single-run orchestrator
previously recorded no explicit invocation total. CLI stage/record-length guesses
could therefore overcount snapshot failures, undercount partial records, and
claim a count for delegated execution without sufficient evidence.

Both execution wrappers now initialize `worker_invocation_count` to zero and
increment it immediately before each `worker.run(...)` dispatch, after method
lookup succeeds. The counter survives in the existing exception evidence.
It counts attempted worker dispatches, including launch failures and calls that
raise; it does not count completed results, logical attempt records, successful
subprocess starts or verification commands. `ProcessResult.started` separately
reports launch status. Existing result models and controller execution/retry
semantics are unchanged.

CLI `attempt_count` accepts only the explicit nonnegative integer counter.
Missing/invalid counters produce JSON null, regardless of stage, record length,
or apparent completion evidence. Empty, missing or partial attempt lists cannot
replace or override a recorded total. Task loading and worker construction have
an explicit known zero; after execution is delegated, the CLI requires native
count evidence. It does not assume zero or one for uninstrumented delegates.
The disclosure allowlist remains intact, and native observations are retained.

Deterministic regressions cover zero invocations before baseline/snapshot/method
lookup, failed launch with `started=False`, failure after one completed call,
second-call failure (two dispatches), second-snapshot failure (one dispatch with
two records), partial/missing records, invalid counters, and delegated execution
without authoritative evidence. All 24 tests failed against the baseline in
**8.85 s**. Focused count/CLI/disclosure/integration/R2/R3 suites: **110 passed in
84.36 s**. **Full offline suite: 329 passed in 208.57 s (3:28)**. See the separate
[red](reliability-r3-evidence/invocation-count-red.txt),
[focused](reliability-r3-evidence/invocation-count-focused.txt) and
[full](reliability-r3-evidence/invocation-count-full.txt) transcripts.

```powershell
& 'D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe' -m pytest tests/test_invocation_count.py tests/test_cli_failure_evidence.py tests/test_cli.py tests/test_cli_integration.py tests/test_reliability_r2.py tests/test_reliability_r3.py --basetemp=.r3-evidence/count-review-focused-temp -o addopts='' -q
& 'D:\Documents\coding-agent-harness-public\.venv\Scripts\python.exe' -m pytest --basetemp=.r3-evidence/count-review-full-temp -o addopts='' -q
```

Compatibility is additive for native exception dictionaries. Existing or custom
contexts without the counter now report null instead of an inferred number.
The counter is an in-memory dispatch observation, not a durable journal or proof
of a subprocess/model call; it does not measure retries internal to a custom
worker implementation. Existing R3 lifecycle and disclosure limitations remain.
No live workers/models, services, main changes, push, PR or merge were used.
The exact follow-up diff is `.r3-evidence/invocation-count-review.diff`, against
the baseline above. The commit ID is supplied in the handoff. Stop for review.
