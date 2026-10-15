# R4: bounded Git evidence collection

R4 bounds the Git evidence phases used by baseline validation, worktree snapshots,
cumulative evidence, and per-attempt comparison. Each phase creates one monotonic
deadline and passes that absolute deadline through nested helpers. Git stdout and
stderr are drained concurrently into byte-limited buffers; bytes beyond a limit are
discarded while draining continues. For calls using an absolute phase deadline, the
runner reserves the final two seconds for process-tree cleanup and bounded output
draining. The runner stops waiting for a hung cleanup helper at the phase deadline
and marks termination unconfirmed; the daemon helper may continue if an OS
process-tree call remains blocked. Completion at or after the deadline is rejected. Truncation,
incomplete collection, a late exit, launch failure, nonzero exit, or uncertain cleanup
prevents a successful evidence model. Temporary indexes isolate snapshot and patch
preparation from the real index. Their cleanup is bounded; unresolved ownership is
reported and prevents successful collection.

## Budgets

| Phase or stream | Limit |
| --- | ---: |
| Baseline phase | 15 seconds |
| Worktree snapshot phase | 30 seconds |
| Cumulative evidence phase | 45 seconds |
| Attempt snapshot and diff phase | 30 seconds total |
| Process cleanup reserve | 2 seconds within each total deadline |
| Process-tree terminate and kill waits | 0.5 seconds and 1 second |
| Ordinary Git stdout | 1 MiB per command |
| Binary patch stdout | 8 MiB per command |
| Git stderr | 64 KiB per command |

A phase failure keeps observations already completed in that phase on the original
exception. Safe failure metadata contains an allowlisted reason, stage, bounded
numeric fields, and truncation flags. CLI output continues to project only allowlisted
summaries; raw paths, patches, status text, stderr, exception messages, and temporary
paths stay out of failure reporting.

## Remaining boundary

The separate supervisor preflight reader in
`coding_agent_harness/supervisor.py::_inspect_git` remains outside R4. It still uses
`subprocess.run(..., capture_output=True, timeout=...)` without stdout or stderr byte
limits. A large preflight response can therefore consume unbounded memory until its
execution timeout. This is a known remaining risk; the R4 change is limited to the
Git evidence collector in `coding_agent_harness/git.py`.


## Structural ownership correction (2026-10-15)

The correction starts at `00e233ebb8079ba6f2a5da288af2fd38e38ff21e` on
`codex/reliability-r4-bounded-git`. Investigation and implementation used the existing
R4 worktree and Python environment. The design and transition invariants are in
[the implementation plan](superpowers/plans/2026-10-15-r4-structural-ownership.md).
This is an implementation handoff, not independent approval.

### Root causes and structure

Finding A came from three disjoint ownership guards around creation, descriptor
closure and yield. Cancellation could occur after one guard ended and before the
next began. Helper responsibility was also communicated through an exception flag,
which could itself be interrupted before publication. One continuous generator
lifetime guard now spans acquisition through exit. Each index retains a serial
filesystem-operation ticket registered before helper construction/startup. Completed
operations, including errors, transfer responsibility back to the owner. Abandoned
operations retain exclusive cleanup responsibility until their late callback settles.

A collection-local registry retains these owners before acquisition and across
context-manager teardown. Public failure handlers derive conservative, deduplicated
snapshots from that registry without filesystem probes or waits. Final evidence
construction requires confirmed terminal ownership state. This outer ledger covers
cancellation at entry to the cleanup guard itself, which the supplementary self-check
found could otherwise bypass local reporting. No new helper/thread layer was added.

The raw fd/name result remains recoverable before derived path/descriptor publication.
Descriptor close records a possible consume before calling the OS, never retries an
uncertain numeric descriptor, and confirms closure only after return. Cleanup requires
settled filesystem work and confirmed Git termination. It removes/checks only the
unique owned index and its exact `.lock`. Precise cleanup counts are retained, including
when only the lock remains; cleanup return values alone cannot authorize evidence.

Finding B came from attach-if-absent metadata publication. The second cleanup deadline
check could create a zero-count failure and the enriched owned-resource snapshot was
then discarded. Reconciliation now preserves the first reason/stage and known exit
status, takes maximum cumulative counts and ORs flags. It is monotonic and idempotent;
collection snapshots sum distinct registered owners, never repeated attachments. Earlier
uncertainty cannot be cleared by late cleanup. Completed observations are merged without
losing earlier evidence. Exception objects and traceback identity survive metadata
attachment; cleanup failure cannot replace an existing body exception. CLI projection
continues to expose only the existing sanitized allowlist.

### Reproductions and additional examination

| Scenario | Previous implementation | Corrected implementation |
| --- | --- | --- |
| SIGINT before fd close | Open fd and index leak; unresolved=0, uncertain=false | Same cancellation object; fd closed and owned paths absent |
| SIGINT before yield | Index leak; unresolved=0, uncertain=false | Same cancellation object; owned paths absent |
| Helper interrupted during raw ownership publication | Open fd and index leak; unresolved=0, uncertain=false | Raw result recovers descriptor/path; cleanup confirmed |
| Expiry between cleanup deadline checks | Completed tree retained, index remains, unresolved=0, uncertain=false | Tree retained, index remains, unresolved=2, uncertain=true; no success |

The supplied original probes reproduced both findings before production changes.
Comparable child probes loaded the previous Git module read-only from `git show`;
the supplied deadline-expiry probe was rerun with only its fixture directory redirected
inside the R4 worktree. All observed real-index bytes remained unchanged.

New child-process tests cover actual SIGINT before close/yield, creation return,
blocked creation, raw ownership publication, helper construction/start/completion,
cleanup entry/running/guard/result publication, repeated cancellation, final model
construction, and a late copy after caller cancellation. Every signal test has a
finite child safety deadline; no SIGINT is sent into pytest. Neighboring index/lock
bytes remain unchanged. A distinct-owner registry test exercises actual files,
deduplication, separate collections, and cleanup isolation. Metadata tests cover
between-check expiry, repeated enrichment, prior uncertainty, exception/traceback
identity, completed evidence, and completed-helper errors.

After the initial 414-test suite passed, the focused adversarial self-check demonstrated
an unreported resource at cleanup-guard entry. The registry fixes that actual defect.
A separate injected no-op cleanup demonstrated missing final-state validation; the
new evidence barrier rejects it. The self-check also caught completed-error helper
handoff and descriptor-close publication issues during development; failing regressions
preceded their corrections. Cleanup-result interruption retained uncertainty. Late
copying never claimed cleanup confirmation before release and left no owned artifact
after settlement. These are author self-checks, not independent review.

### Limits and review commands

A blocked OS operation cannot be forcibly canceled in Python. Its caller wait remains
bounded and its owner remains uncertain; late cleanup is best effort and cannot revise
returned failure evidence. Cancellation before cleanup starts can leave owned paths,
now reported as unresolved. An ambiguous close can leave a descriptor uncertain and
will not be retried because its number might have been reused. Arbitrarily repeated
signals inside exception handling or outer failure publication have no universal
protection guarantee. Supervisor preflight remains outside scope. No limits, controller,
supervisor or process-execution behavior were changed.

Use the existing worktree and interpreter, and choose unused basetemp directories:

```powershell
Set-Location 'D:\Documents\coding-agent-harness-dev\worktrees\r4-bounded-git'
$env:TEMP = Join-Path (Get-Location) '.pytest_cache\review-system-temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
.\.venv\Scripts\python.exe -m pytest tests/test_reliability_r4_ownership.py -q -o addopts='' --basetemp=.pytest_cache/review-r4-focused
.\.venv\Scripts\python.exe -m pytest tests/test_git.py tests/test_reliability_r3.py tests/test_reliability_r4.py -q -o addopts='' --basetemp=.pytest_cache/review-r4-targeted
.\.venv\Scripts\python.exe -m pytest -q -o addopts='' --basetemp=.pytest_cache/review-r4-full
git diff 00e233ebb8079ba6f2a5da288af2fd38e38ff21e HEAD -- coding_agent_harness/git.py tests/r4_ownership_probe.py tests/test_reliability_r4_ownership.py docs/reliability-r4-bounded-git.md docs/superpowers/plans/2026-10-15-r4-structural-ownership.md
git diff --check
git diff --cached --check
git status --porcelain=v1
```

### Final validation

All final commands used the existing `.venv/Scripts/python.exe`, `-q -o addopts=''`,
and fresh worktree-local basetemp directories. `TEMP` and `TMP` were also confined
to `.pytest_cache/r4-system-temp` for the final runs.

| Run | Passed | Failed | Skipped | Inconclusive | Elapsed |
| --- | ---: | ---: | ---: | ---: | --- |
| New ownership regressions (`tests/test_reliability_r4_ownership.py`) | 22 | 0 | 0 | 0 | 20.14 s |
| Required Git/R3/R4 three-file suite | 113 | 0 | 0 | 0 | 68.76 s |
| Complete offline suite | 419 | 0 | 0 | 0 | 229.48 s |

`git diff --check` and `git diff --cached --check` passed; the staged diff was inspected.
The full suite includes the final probe reporting clarification. Established regression
expectations were unchanged. The initial red ownership suite had 6 passes and 5 failures;
the post-normal-suite adversarial additions had 1 pass and 2 failures (17 deselected).
An intermediate three-file run exposed a widened cleanup count (112 passes, 1 failure);
the precise positive cleanup observation was retained, and the existing assertion passed
unchanged afterward. No final test outcomes were skipped or left inconclusive.

The exact changed files are `coding_agent_harness/git.py`, `tests/r4_ownership_probe.py`,
`tests/test_reliability_r4_ownership.py`, this document, and
`docs/superpowers/plans/2026-10-15-r4-structural-ownership.md`.
No provider calls, experiment execution, network testing, push, PR, or merge occurred.
Independent adversarial review remains the next step.
