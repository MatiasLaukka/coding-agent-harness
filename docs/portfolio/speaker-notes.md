# Coding Agent Harness — speaker notes

Target duration: 5 minutes. Timings total 300 seconds. Source URLs are also embedded in the PowerPoint notes. Evidence labels: V = verified implementation; O = owner-reported leadership; C = substantial AI collaboration; U = uncertain individual origin.

## 1. Don’t trust completion. Verify it. (30 seconds)

A convincing completion message cannot establish that the right files changed, meaningful checks passed, or someone authorized acceptance. Coding Agent Harness investigates that gap. It separates the worker’s account, independently collected observations, and the acceptance decision. Matias directed the project with substantial AI collaboration. The engineering story is how concrete failures sharpened those boundaries.

Sources and qualifications

The quoted worker claim is illustrative, not a real run quotation. Leadership is owner-reported (O); implementation behavior is verified (V). Independent collection means outside the worker self-report, not isolated permissions.

- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/controller.py
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/orchestrator.py
- Portfolio documentation: [README](../../README.md). The implementation links above remain pinned to the audited revision.
- Attribution source: owner-provided background. Leadership is owner-reported; individual mechanism origin remains uncertain.

## 2. Architecture and trust boundaries (35 seconds)

Both paths execute a worker, capture Git evidence after it exits, and run caller-supplied checks. Collection is independent of the worker’s self-report, but processes share user permissions. The run command returns mechanical findings and readiness. The separate control workflow combines human requests, freshness checks, controller gates, and records. Direct library use also supports caller-supplied reviewers and scope revision. The human CLI is narrower.

Sources and qualifications

The common upper sequence is a conceptual summary implemented separately by run and controller paths. Control requires START and start-record publication before dispatch (shown on slide 6). Git capture precedes verification; control additionally checks freshness. Ready-for-review is not acceptance. There is no sandbox. Library audit events and control JSON records are distinct.

- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/orchestrator.py#L81
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/control.py
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/human_review.py#L434
- Portfolio documentation: [Architecture](../architecture.md).

## 3. A reproducible offline workflow (40 seconds)

Here is a reproducible example from the existing integration test. A task allows changes to existing.txt and new.txt. A synthetic Python subprocess edits the tracked file and creates the new one through the real worker adapter. The harness collects both paths and their patches. A separate verification process checks the exact contents. The test confirms ready-for-review, passing verification, no findings, unchanged HEAD, and an unstaged index. The diagram summarizes those assertions. This single test passed again at the verified baseline. There is no model call or reviewer acceptance.

Sources and qualifications

Actual test assertions were inspected and rerun successfully. Diagram is explanatory, not a terminal screenshot. The real worker adapter runs a synthetic Python executable. Reproduce in a clone of the pinned revision with Python 3.12+, Git, and project development dependencies installed. No acceptance event is claimed.

- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_cli_integration.py#L11
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/orchestrator.py#L81
- Portfolio documentation: [README: offline workflow](../../README.md#try-it-without-a-model).
- Presentation preparation recorded one selected integration test passing on Windows/Python 3.12.14 on 21 October 2026 against a separate extraction of the pinned revision. This is separate from the historical suite counts; no model/provider calls.

## 4. The first real problems (40 seconds)

PR 1 reports Qwen writing completion files instead of emitting the required final response on stdout. That prompted a clearer output-channel contract. Later parser tests separately rejected ambiguous records. The reproduced rename case challenged evidence semantics: name-only output could omit a deleted source, allowing an out-of-scope deletion when only the destination was allowed. Disabling rename collapsing exposed both paths. Regressions cover staged, unstaged, and committed moves, plus controller, single-run, and handoff behavior.

Sources and qualifications

PR #1 circumstances are documented reports, not raw-run reinspection. Public PR text was rechecked on 21 October 2026. Rename source/destination assertions are in the pinned tests. Scope observes after writes and does not prevent them.

- https://github.com/MatiasLaukka/coding-agent-harness/pull/1
- https://github.com/MatiasLaukka/coding-agent-harness/pull/4
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/worker.py
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/git.py#L1230
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/git.py#L1431
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_reliability_r2.py#L81
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/docs/reliability-r2-findings.md

## 5. Adversarial scrutiny: success after expiry (45 seconds)

An earlier timeout fix bounded pipe collection and cleanup. Adversarial follow-up found another race: a delayed monitor could observe successful completion after expiry and accept it before checking time. This controlled-clock regression uses a 50-millisecond budget and a 400-millisecond observation. Exit zero and complete output remain true observations, while the result must still be timeout. The correction checks expiry before and after completion observations. Nine cases cover timing, controller actions, and verification expectations. The lesson is to test ordering at the failure boundary.

Sources and qualifications

The diagram uses synthetic clock values asserted in the regression, not a performance benchmark or real process timing. Nine cases = three monitor cases + three controller actions + three verification expectations. Current source/tests were inspected; those tests were not rerun during presentation creation. PR 6 reports the historical suite passed. Bounded waits do not establish OS containment.

- https://github.com/MatiasLaukka/coding-agent-harness/pull/6
- https://github.com/MatiasLaukka/coding-agent-harness/commit/90bbe08481ad331fca0775f4de3fc4394201c09a
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/process.py#L263
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_reliability_r3.py#L739
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_reliability_r3.py#L758
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_reliability_r3.py#L776
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_reliability_r3.py#L795
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/tests/test_reliability_r3.py#L884

## 6. Human authority and truthful evidence (40 seconds)

Control requires a fresh START, a published start record, and the approved clean baseline before dispatch. Acceptance and remediation require fresh challenges and rationales. The CLI allows zero or one enabled remediation with fixed scope. After publishing the human request, final freshness must pass before the controller evaluates it. Records distinguish publication, observed receipt, and evidenced processing. A terminal-record failure cannot undo acceptance already returned. These are explicit authority boundaries, not authenticated or tamper-proof records.

Sources and qualifications

Control CLI default remediation budget is zero, maximum one. Library default is two additional invocations shared with scope revision. Review-entry freshness occurs before human review. STOP needs no fresh challenge. Saved records are not resume authority. Point-in-time checks are not an atomic lock, and all processes share user permissions.

- https://github.com/MatiasLaukka/coding-agent-harness/pull/9
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/control.py#L243
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/human_review.py#L434
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/human_review.py#L506
- Portfolio documentation: [Architecture](../architecture.md).

## 7. What the evidence actually establishes (40 seconds)

The two counts describe different evidence. An inspected archived log records 575 offline tests passing on Windows. PR 9 separately reports 123 tests and probes passing in a targeted AI-assisted review. Do not add them. The smaller historical note records one reported Qwen eligibility acceptance, with original feature tests still failing in advisory mode. It carries no repeatability claim. The offline demonstration just shown was rerun separately. None of this establishes production readiness, universal backend or console support, or semantic correctness beyond the configured checks.

Sources and qualifications

Counts are historical and not additive. The fresh single-test demonstration is separate from both counts. PR #9 distinguishes implementation validation from AI-assisted review; neither is an external human audit. PR #1 retains 18 original feature tests as advisory failures. Raw experiment artifacts were not inspected.

- https://github.com/MatiasLaukka/coding-agent-harness/pull/9
- https://github.com/MatiasLaukka/coding-agent-harness/pull/1
- Historical evidence: the documentation review inspected a Windows full-suite log, 575 passed. Historical run, not rerun for this presentation; see [documentation review](../portfolio-review.md#initial-overhaul-validation-record).
- Portfolio documentation: [Documentation review](../portfolio-review.md#initial-overhaul-validation-record).

## 8. Engineering contribution and lessons (30 seconds)

Matias’s role was to frame the investigation, set requirements and review priorities, and decide what evidence supported acceptance. Architecture, implementation, investigation, and review involved extensive AI collaboration. Individual mechanism origin remains uncertain. The lessons are to inspect the evidence behind a gate, retain counterexamples as regressions, and narrow guarantees when necessary. Repeatable console and backend validation is a possible next step.

Sources and qualifications

Leadership is owner-reported (O), authorized by the current brief. Mechanism origin remains uncertain (U); do not say Matias personally invented or discovered each correction. AI collaboration is substantial (C), not a formal external human audit. Future validation is a possibility, not a committed roadmap.

- https://github.com/MatiasLaukka/coding-agent-harness/pull/8
- https://github.com/MatiasLaukka/coding-agent-harness/blob/977a764cf399cdebf222b444af5d80d186a80878/coding_agent_harness/report.py
- Portfolio documentation: [README](../../README.md) and [report contract](../reports.md).
- Attribution source: owner-provided background. Leadership is owner-reported; sole invention is not established.
