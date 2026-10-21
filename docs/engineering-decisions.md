# Engineering decisions

[README](../README.md) · [Architecture](architecture.md) · [Case-study evidence](portfolio/evidence-checklist.md)

These decisions explain the implemented system at revision `977a764cf399cdebf222b444af5d80d186a80878`. They draw on public PRs, committed findings, source, and tests. A test's existence is distinct from a newly executed result; [validation records](portfolio-review.md) identify what was run.

The owner reports initiating the investigation, setting requirements and priorities, reviewing results, and making acceptance decisions. Development involved extensive AI collaboration. Repository attribution does not establish who first conceived each mechanism; individual invention and discovery credit remain uncertain. AI-assisted adversarial reviews are not formal external human security audits.

## 1. Keep claims, observations, and acceptance separate

A valid worker message cannot establish correct work or permission to accept it. The single-run orchestrator collects Git evidence and configured check outcomes, then returns mechanical readiness or attention findings. The controller instead asks a reviewer and applies acceptance gates.

The original project already made this distinction; R6a added a foreground human interface. The [supervisor sidecar](supervisor-handoff.md) validates proposals and approvals without executing them.

**Evidence:** [initial snapshot](https://github.com/MatiasLaukka/coding-agent-harness/commit/70b1a21), [orchestrator](../coding_agent_harness/orchestrator.py), [controller](../coding_agent_harness/controller.py), [integration test](../tests/test_cli_integration.py).

**Tradeoff and limit:** observations are independently collected from self-report, but share user permissions. Caller-selected checks provide evidence only for what they test. `ready-for-review` is never controller acceptance.

## 2. Validate both the evidence and the decision at a gate

[R2 adversarial probes](reliability-r2-findings.md) exposed two different assumptions. Rename collapsing could omit a forbidden deleted source from path evidence; malformed reviewer output could fall through into remediation.

[PR #4](https://github.com/MatiasLaukka/coding-agent-harness/pull/4) disabled rename collapsing in both name-only collectors and rebuilt reviewer decisions with strict runtime validation before branching. Retry budgets must be exact nonnegative integers.

**Evidence:** [Git collectors](../coding_agent_harness/git.py), [controller](../coding_agent_harness/controller.py), [R2 regressions](../tests/test_reliability_r2.py). The tests cover both rename endpoints, controller scope rejection, malformed model instances, and bounded valid retries.

**Tradeoff and limit:** patches may still present a human-readable rename. Scope detects Git-visible writes after execution; it cannot prevent or undo them. Runtime validation does not isolate an in-process reviewer.

## 3. Preserve uncertainty when execution or collection fails

A process timeout once led to unbounded pipe collection. A later delayed-monitor counterexample showed that successful completion observed after expiry could still bypass the deadline. These were distinct failures.

R3 bounded output collection and cleanup; [PR #6](https://github.com/MatiasLaukka/coding-agent-harness/pull/6) checked expiry before and after completion observations. [PR #7](https://github.com/MatiasLaukka/coding-agent-harness/pull/7) extended deadlines, byte caps, and continuous temporary-resource ownership to Git evidence.

**Evidence:** [failure/correction cases](engineering-notes.md), [process runner](../coding_agent_harness/process.py), [R3 regressions](../tests/test_reliability_r3.py), [R4 ownership probes](../tests/test_reliability_r4_ownership.py).

**Tradeoff and limit:** exit/output observations remain useful even when the overall result is failure. Incomplete capture and cleanup uncertainty cannot become successful evidence. Bounded caller waits do not establish OS containment or cancellation of every blocked OS call.

## 4. Narrow report publication to an explicit operator contract

An external directory can still supply a Git include, object store, hook input, or watched resource. Root checks cannot discover every operational dependency.

The final [PR #8 redesign](https://github.com/MatiasLaukka/coding-agent-harness/pull/8) removed recursive metadata scanning. It requires an existing operator-trusted `--report-dir`, opaque generated names, known-root overlap checks, and create-only hard-link publication from captured evidence.

**Evidence:** [final correction](https://github.com/MatiasLaukka/coding-agent-harness/commit/9f227a5), [report contract](reports.md), [report implementation](../coding_agent_harness/report.py), [layout](../tests/test_report_layout.py) and [publication tests](../tests/test_report.py).

**Tradeoff and limit:** the operator must maintain independence and stable ancestors. Reports remain sensitive and unauthenticated. Cleanup may fail after publication; crash durability is not guaranteed.

## 5. Bind human authority to current evidence and report only observable progress

[PR #9](https://github.com/MatiasLaukka/coding-agent-harness/pull/9) added `control`: fresh `START`, start-record publication, and baseline matching before first dispatch. Human `ACCEPT` and `REMEDIATE` require fresh challenges and rationales. Review publication and final freshness precede callback return; the controller still validates the request and applies its gates.

**Evidence:** [human review](../coding_agent_harness/human_review.py), [control and terminal projection](../coding_agent_harness/control.py), [control tests](../tests/test_control.py), [authority diagram](architecture.md#human-authority-in-control).

**Tradeoff and limit:** the CLI freezes scope and permits zero or one enabled remediation. The library separately shares a finite budget across remediation and explicit scope revision. Freshness is point-in-time, not a lock. Saved requests do not prove controller processing or grant resume authority; terminal publication failure cannot undo acceptance already returned.
