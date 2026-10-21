# Engineering findings: failures that changed the design

[README](../README.md) · [Architecture](architecture.md) · [Claim checklist](portfolio-review.md#technical-claim-to-source-checklist)

These cases are supported by committed investigation notes, failing transcripts, current code, and regression tests. Historical measurements below are evidence from those specific investigations, not benchmarks or claims about current throughput. Development and review were extensively AI-assisted under the project owner's direction; these records are not independent human security audits.

## Case study: a rename hid an out-of-scope deletion

**Problem and violated assumption.** The scope gate assumed the name-only diff listed every changed path. With Git rename detection enabled, a staged move from `existing.txt` to allowed `renamed.txt` could omit the deleted source. An accepting fake reviewer then caused the controller to return `accepted`.

**Correction and tradeoff.** [PR #4](https://github.com/MatiasLaukka/coding-agent-harness/pull/4) adds `--no-renames` to the cumulative and per-attempt name-only queries in [git.py](../coding_agent_harness/git.py). Both endpoints count toward scope. Human-readable patch collection remains intact; no new access restriction, index reset, or rollback was introduced.

**Validation.** [R2-1](reliability-r2-findings.md#r2-1-rename-source-omitted-from-scope-evidence--high-severity) records the reproduction and correction. Current [R2 regressions](../tests/test_reliability_r2.py) cover:

- `test_rename_evidence_contains_deleted_source_and_destination`: unstaged, staged, and committed moves expose both paths.
- `test_rename_of_forbidden_source_cannot_be_accepted`: the accepting reviewer cannot override the source violation.
- `test_single_run_rename_scope_gate`: the separate single-run path reports `needs-attention`.
- `test_rename_scope_evidence_survives_handoff`: the source remains visible in exported scope facts.

**Remaining boundary.** This is a Git-visible scope check after execution. The worker has already changed the files. The check cannot prevent writes, observe external effects, or establish semantic task correctness.

## Completion protocol: prompting and enforcement solve different problems

[PR #1](https://github.com/MatiasLaukka/coding-agent-harness/pull/1) reports Qwen sessions writing completion-marker files instead of the required final stdout response. The prompt was clarified without changing the protocol schema or acceptance rules. The PR’s later run report does not establish causation or repeatability.

A separate [R2 parser finding](reliability-r2-findings.md#r2-4-ambiguous-completion-output-silently-overwrites-claims--medium-severity) led to rejecting multiple completion records and duplicate JSON keys. Clear instructions help a cooperative worker; strict parsing rejects ambiguous output. Neither proves the claim true.

## A process timeout did not bound output collection

**Problem.** In the original R3 baseline, timeout handling requested process-tree cleanup and then called unbounded `communicate()`. An already-exited parent could leave a child holding a pipe writer; unsuccessful termination had the same consequence. The harness waited for pipe EOF after its nominal timeout.

**Consequence.** A worker timeout did not reliably return control to the caller. A nominally successful parent exit also could not prove complete output or descendant termination.

**Verified reproduction.** The [R3 investigation](reliability-r3-findings.md#original-code-reproduction) used finite four-second children and an independent outer deadline. With a one-second requested budget, the original implementation returned in 4.375 seconds for an inherited writer and 4.109 seconds with deliberately disabled termination. The committed [original-code transcript](reliability-r3-evidence/original-code.txt) contains the failing assertions. Those measurements describe the historical Windows probe only.

**Correction.** Concurrent readers drain into byte-limited buffers; caller waits, tree cleanup, and final output draining are bounded. Incomplete capture, unknown exit, cleanup errors, and observed survivors are explicit evidence rather than invented success. Infrastructure failure blocks acceptance and further controller remediation.

**Validation.** The historical [deadline transcript](reliability-r3-evidence/deadlines.txt) records three passing finite subprocess probes. Current regressions include `test_parent_exit_with_inherited_pipes_obeys_deadline`, `test_failed_tree_termination_does_not_wait_for_child_exit`, and `test_timeout_never_authorizes_remediation_retry` in [test_reliability_r3.py](../tests/test_reliability_r3.py). [PR #6](https://github.com/MatiasLaukka/coding-agent-harness/pull/6) corrected a distinct race: expiry must win when a delayed monitor observes a successful exit after the deadline. The earlier inherited-pipe fix alone did not establish that ordering. Later R4 changes extend phase deadlines and ownership tracking to [Git collection](reliability-r4-bounded-git.md).

**Remaining boundary.** psutil discovery is not OS containment. Escaped descendants, a blocked OS call, and a daemon reader held by an undiscovered writer remain possible. An empty observed-survivor list does not prove every descendant exited.

## Preserving evidence accidentally widened disclosure

**Problem.** An early R3 failure printer recursively serialized native evidence. It exposed worker/check stdout and stderr, patches, cleanup messages, task content, and unknown dictionary fields. Raw exception and validation messages added another disclosure channel.

**Consequence.** Retaining useful debugging evidence also copied potentially sensitive content into terminal or CI failure logs. Hashing command arguments alone did not close the boundary.

**Verified reproduction.** Nine fake-secret regressions failed against that candidate. The committed [red transcript](reliability-r3-evidence/cli-disclosure-red.txt) identifies disclosed fields for both single-run and controller failures, including cancellation. No real secrets were required.

**Correction.** The CLI failure surface became an explicit allowlisted projection: known stage/type labels, counts, process and verification observations, validated Git identities, and hashes of selected content. Unknown structures are not recursively traversed; exception messages are not echoed there. Rich native evidence remains attached to the original exception for authorized Python callers.

**Validation.** The [green transcript](reliability-r3-evidence/cli-disclosure-green.txt) records nine passing disclosure regressions; the [historical full transcript](reliability-r3-evidence/cli-disclosure-full.txt) records 305 passing tests at that revision. Current coverage is in [test_cli_failure_evidence.py](../tests/test_cli_failure_evidence.py), with implementation in [cli.py](../coding_agent_harness/cli.py). The [investigation](reliability-r3-findings.md#cli-disclosure-follow-up-pending-independent-review) preserves the original review-stage labels; their wording is historical.

**Remaining boundary.** Hashes are not encryption and can reveal low-entropy content through guessing. Ordinary result stdout, optional Markdown reports, and control JSON records deliberately contain source/output. This correction is a failure-summary disclosure boundary, not universal redaction.

## What these findings demonstrate

The useful evidence is the chain from a specific failure to a concrete correction and a regression that exercises the boundary. Passing those regressions does not establish semantic task correctness, malicious-worker containment, production reliability, or comprehensive security.

The repository also retains [R2 findings](reliability-r2-findings.md), [R3 lifecycle/evidence notes](reliability-r3-findings.md), and [R4 bounded Git and ownership notes](reliability-r4-bounded-git.md). Read their stated revision, platform, and validation scope before applying a historical claim to current code.
