# Portfolio documentation review

[README](../README.md) · [Architecture](architecture.md) · [CLI guide](usage.md)

## Audited revision and scope

Audit date: 2026-10-21. Remote `main` was verified with `git ls-remote` and a fresh separate clone at **`977a764cf399cdebf222b444af5d80d186a80878`**, the merge of R6a PR #9. This documentation branch starts at that commit. The documentation work used an isolated checkout based on that verified remote revision.

The package contains the README, supporting Markdown/SVG, and the supplied case-study presentation. Application code, dependency configuration, tests, CI, and historical investigation artifacts are unchanged. No live provider, Qwen experiment, or benchmark was used. Publication is through a documentation-only branch and review PR; no merge is authorized.

The audit inspected the current README, runtime modules, CLI parser and dispatch, representative protocol/Git/process/controller/supervisor/report/control tests, CI workflow, packaging, merged history, and committed R3/R4 investigations. Previous design notes were used as search pointers; current implementation takes precedence. In particular, current R5 has **no metadata-tree scanner** and explicitly leaves operational-directory independence to the operator.

## Change summary

The opening now explains the problem and system in a short reading pass. A workflow comparison and original SVG make the single-run and human-review paths visible before installation. The safe first exercise is an existing synthetic-worker integration test. Detailed report publication and supervisor material have dedicated guides; key limits remain in the README.

The lifecycle SVG uses explicit backgrounds, high-contrast text, no scripts or external fonts/resources, and an accessible title/description. Two supporting Mermaid diagrams explain decision order and authority. Mermaid renders natively on GitHub; the SVG is linked as a repository asset. A [presentation reference](https://github.com/google-research/harness2) informed the concise proposition, navigation, concepts, and deeper-guide hierarchy. Its design, architecture, and results were not reused as claims about this project.

## Before / after outline

| Before | After | Improvement |
| --- | --- | --- |
| Prototype proposition followed by detailed architecture | Evidence-first proposition, problem, two-path comparison | Purpose and workflow distinction appear immediately |
| One controller-centric diagram | Shared lifecycle SVG plus detailed authority/decision diagrams | Execution, observation, and authorization have separate visual roles |
| Installation followed by a configured-worker template | Offline reproducible test exercise before live-worker instructions | Readers can inspect behavior without model setup |
| Report and human-control contracts embedded in a long usage section | Focused CLI, report, and architecture guides | Operational precision remains accessible without dominating the introduction |
| Long supervisor round trip in README | Short tested trust-boundary summary and full handoff guide | Preserves essential constraints and the existing documentation regression |
| Historical experiments mentioned without linked artifacts | Claims restricted to implementation, tests, and committed evidence | Avoids backend or repeatability implications |
| Generic testing summary | Source checklist and concrete failure/correction cases | Readers can inspect why the engineering choices exist |
| No explicit development attribution | Owner-directed, extensive AI assistance stated plainly | Authorship is confident and transparent |

## Stale or ambiguous statements corrected

- The old component map omitted the provided human-review CLI modules; the new map includes them.
- Blanket statements that verifier/reviewer side effects are never checked did not describe R6a. The docs now distinguish the original capture point, default library/single-run behavior, and control freshness checks.
- “Approval-unaware” is qualified as referring to **sidecar approval files**. The separate foreground control CLI requires its own human authorization.
- The old final remediation limitation mentioned only the controller API; the new comparison describes the provided control CLI and its smaller budget.
- `expected_failure` now explicitly documents that an unexpected ordinary pass is audited but does not fail aggregate verification. It is not equivalent to a required successful assertion.
- R6a freshness documentation includes unsupported index flags and a raw patch digest, not just a staged-entry fingerprint.
- Limited live experiment claims were omitted because they could not be substantiated from the permitted committed artifacts. No raw experiment artifacts were inspected to fill that gap.

## Technical claim-to-source checklist

Each checked item was compared with the audited implementation and regression coverage. A checked source is not independent human approval or proof against every environment.

| Checked assertion | Implementation source | Verification / qualification |
| --- | --- | --- |
| [x] Two installed CLI names expose `run`, `control`, and nonexecuting `handoff` | [pyproject.toml](../pyproject.toml), [cli.py: parser and dispatch](../coding_agent_harness/cli.py) | [CLI tests](../tests/test_cli.py); all five documented help surfaces checked |
| [x] Single-run status is readiness/attention, never acceptance | [orchestrator.py: run_task](../coding_agent_harness/orchestrator.py) | [Integration test](../tests/test_cli_integration.py); real Git and synthetic subprocess |
| [x] Exactly one completion marker record; duplicate keys rejected | [worker.py: _parse_completion](../coding_agent_harness/worker.py) | [Worker outcome](../tests/test_worker_outcome.py), [R2 ambiguity tests](../tests/test_reliability_r2.py) |
| [x] Worker/Git disagreement differs between execution paths | [orchestrator.py](../coding_agent_harness/orchestrator.py), [controller.py](../coding_agent_harness/controller.py) | [Orchestrator](../tests/test_orchestrator.py), [controller tests](../tests/test_controller.py); finding versus audit event |
| [x] Clean baseline, cumulative and per-attempt evidence, both rename endpoints | [git.py](../coding_agent_harness/git.py) | [Git](../tests/test_git.py), [R2](../tests/test_reliability_r2.py); Git-visible state only |
| [x] Temporary indexes preserve the real index but write Git objects | [git.py: snapshot and patch helpers](../coding_agent_harness/git.py) | [R4 ownership](../tests/test_reliability_r4_ownership.py), [offline integration](../tests/test_cli_integration.py) |
| [x] Git phases and capture sizes have finite budgets | [git.py: constants and _git](../coding_agent_harness/git.py), [process.py](../coding_agent_harness/process.py) | [R4 regressions](../tests/test_reliability_r4.py); OS-call/containment limits remain |
| [x] Verification expectations distinguish ordinary and infrastructure failure | [verifier.py](../coding_agent_harness/verifier.py), [process.py: infrastructure_failed](../coding_agent_harness/process.py) | [Verifier](../tests/test_verifier.py), [R3](../tests/test_reliability_r3.py); empty checks pass outside control admission |
| [x] Scope is detection and an acceptance gate, not filesystem prevention | [controller.py](../coding_agent_harness/controller.py), [orchestrator.py](../coding_agent_harness/orchestrator.py) | [Controller](../tests/test_controller.py), [R2 scope tests](../tests/test_reliability_r2.py) |
| [x] Human acceptance cannot override execution/protocol/scope/verification gates | [controller.py: accept branch](../coding_agent_harness/controller.py) | [Controller](../tests/test_controller.py), [worker outcome tests](../tests/test_worker_outcome.py); valid blocked/failed claims may be accepted with disagreement audit |
| [x] Library remediation and scope revision share budget; CLI has 0/1 fixed-scope remediation | [controller.py](../coding_agent_harness/controller.py), [control.py](../coding_agent_harness/control.py), [human_review.py](../coding_agent_harness/human_review.py) | [Controller](../tests/test_controller.py), [control tests](../tests/test_control.py) |
| [x] START binds the exact baseline/configuration and publishes before dispatch | [control.py: run_control](../coding_agent_harness/control.py), [controller.py: expected_baseline](../coding_agent_harness/controller.py) | [Control tests](../tests/test_control.py): start-publication and baseline-mismatch failures |
| [x] Freshness precedes review and follows decision publication | [human_review.py: review and _fresh_observation](../coding_agent_harness/human_review.py) | [Human review](../tests/test_human_review.py), [control drift tests](../tests/test_control.py); not an atomic lock |
| [x] Control rejects hidden Git index flags and compares patch/staged evidence | [git.py: assert_supported_index_flags and capture_evidence](../coding_agent_harness/git.py) | [Git](../tests/test_git.py), [control tests](../tests/test_control.py); enabled by the control wrapper |
| [x] Console input is bounded; subprocess stdin isolation is opt-in outside control | [console.py](../coding_agent_harness/console.py), [control.py](../coding_agent_harness/control.py), [process.py](../coding_agent_harness/process.py) | [Console](../tests/test_control_console.py), [process tests](../tests/test_process.py); genuine terminal still needs manual validation |
| [x] Record publication is create-only; a request is not proof of processing | [control_records.py](../coding_agent_harness/control_records.py), [control.py: terminal projection](../coding_agent_harness/control.py) | [Record](../tests/test_control_records.py), [control audit tests](../tests/test_control.py); no authentication or resume capability |
| [x] Report rendering uses returned evidence and cannot grant acceptance | [report.py: render_run_report](../coding_agent_harness/report.py), [cli.py](../coding_agent_harness/cli.py) | [Report](../tests/test_report.py), [report CLI tests](../tests/test_report_cli.py) |
| [x] Report preflight checks known roots, not complete dependency independence | [report.py: discover_report_roots and prepare_run_report](../coding_agent_harness/report.py) | [Report layout](../tests/test_report_layout.py); operator must maintain independence/stability |
| [x] Hard-link publication does not overwrite; cleanup/publication can fail | [report.py](../coding_agent_harness/report.py), [control_records.py](../coding_agent_harness/control_records.py) | [Report](../tests/test_report.py), [record tests](../tests/test_control_records.py); no crash durability |
| [x] Supervisor validation/preflight do not execute proposals | [supervisor.py: validate_proposal and execution_preflight](../coding_agent_harness/supervisor.py), [cli.py](../coding_agent_harness/cli.py) | [Supervisor tests](../tests/test_supervisor.py); trusted caller can bypass sidecar |
| [x] Handoff privacy/provenance is limited, not authenticated semantic review | [supervisor.py: build_handoff](../coding_agent_harness/supervisor.py) | [Supervisor tests](../tests/test_supervisor.py); semantic scope remains unassessed; writable-root inventory trusted |
| [x] Engineering cases have historical reproductions and current regressions | [Engineering findings](engineering-notes.md) | Links point to R2 findings, committed R3 transcripts and current regressions; historical counts are revision-specific |
| [x] CI configuration runs offline pytest on Ubuntu/Python 3.12 | [ci.yml](../.github/workflows/ci.yml) | Configuration inspected; no claim that Windows console behavior is covered by Ubuntu CI |

## Evidence categories

**Implemented/tested:** the execution paths, parser, model validation, evidence collection, command expectations, controller gates, bounded retry semantics, console adapter logic, and publication boundaries described above.

**Architectural intent:** meaningful checks should be selected separately from a worker's own self-assessment; reviewer authority should be explicit. The code provides the interfaces and gates, but cannot establish that an operator's criteria are sufficient or a reviewer is thoughtful.

**Operational assumptions:** trusted executable/configuration, usable terminal, stable independent record/report directories, meaningful checks, a controlled environment, and an accurate inventory of writable roots. These are not automatically certified.

**Unimplemented/non-goals:** OS sandbox/containment, automatic rollback, cryptographic human authentication, crash-durable journals, executable resume from records, hosted planner/reviewer/supervisor adapters, multi-provider routing, and automatic GitHub publication.

## Initial overhaul validation record

Validated locally on Windows with Python 3.12.14, pytest 9.1.1, Pydantic 2.14.0, and psutil 7.2.2.

| Check | Observed result |
| --- | --- |
| Current remote base | Rechecked `main`: `977a764cf399cdebf222b444af5d80d186a80878` |
| Complete existing offline suite | **575 passed in 383.39 seconds**; no provider calls |
| Focused demonstration/environment check | 24 passed: CLI integration, control suite, and inherited-pipe regression |
| Markdown/link check | 7 documents; 146 relative links and anchors resolved |
| Task JSON | Parsed and admitted by both single-run and control schemas |
| CLI help | Root, run, control, handoff validate, and handoff preflight returned help successfully |
| SVG | Valid XML; no scripts, foreign objects, external references, or embedded images |
| Mermaid | Both diagrams parsed and rendered with Mermaid 11.12.0 |
| Visual review | README and diagrams rendered locally in headless Edge; inspected at 860-pixel content width; no README horizontal overflow |
| Scope / whitespace | Only README and 7 new Markdown/SVG paths; source, dependencies, tests, CI, license, historical findings/transcripts unchanged; whitespace checks passed |
| Detailed contract preservation | Report body preserved exactly; supervisor body preserved with heading and approval-qualification edits |
| Protected local state | Canonical branch, HEAD, dirty/untracked listing, and existing worktree listing match initial observations |
| Fresh AI-assisted static review | No actionable technical findings; ready for local human review, not human security certification |

The complete-suite result above is a historical documentation-validation run against the audited implementation, with imports checked to resolve the intended checkout. It is separate from both PR #9's reported implementation validation and its targeted AI-assisted review. The underlying archived log was inspected; it is not part of the public package.

The installation recipe was checked against packaging and help behavior; a fresh online dependency installation was not performed. Live-worker examples were checked against parser/schema contracts and were not executed.

The local preview approximates GitHub styling; it is not a published GitHub rendering. Genuine Windows terminal behavior, backend compatibility, and semantic adequacy of user-supplied checks remain outside these observations.

## Remaining human judgment

- Review whether the visual tone and opening proposition represent the project owner's preferred portfolio emphasis.
- Validate the genuine Windows console on the intended terminal. Scripted console tests do not establish terminal compatibility.
- Any later live-backend claim needs separately authorized evidence; no such claim was added here.
- Before sharing real run records, inspect them for sensitive content and confirm the output-directory trust contract.

## Visual and narrative refinement

This continuation retains the same branch and implementation base. It uses the local design-history review as context and current source/tests as the behavior contract. A concise [public decision summary](engineering-decisions.md) includes only verified mechanisms and explicit attribution qualifications.

### Visual choices

The [Harness² presentation reference](https://github.com/google-research/harness2) informed the use of a recognizable identity, a concise proposition, and illustrations tied to deeper explanations. No artwork, branding, architecture, benchmark claims, or layout was copied.

The original masthead uses three traces and a decision boundary. The explanatory assets share graphite backgrounds, pale text, teal observation lines, and amber authority lines. A dashed outline marks a claim or a defective earlier assumption; labels and prose carry the meaning as well as color. The rose accent appears only in the historical failure state.

| Asset | Explanatory purpose |
| --- | --- |
| [Identity](assets/identity.svg) | Project name and the execution/evidence/human-authority proposition; an abstract mark, not a flowchart |
| [Execution lifecycle](assets/evidence-lifecycle.svg) | Ordered worker, Git, and check stages; separate single-run and human-control branches |
| [Evidence lineage](assets/evidence-signals.svg) | Three sources feed review; collection outside self-report does not imply OS isolation |
| [Human authorization](assets/human-authorization.svg) | Publication and freshness before action; only authorized, budgeted remediation loops back |
| [Rename case](assets/rename-scope-bypass.svg) | Observed failure, violated path-list assumption, correction, and remaining post-write boundary |

Portrait layouts avoid wide, tiny flowcharts on phones. The SVGs use a 560-unit viewBox, system font fallbacks, explicit backgrounds, and accessible titles/descriptions. No scripts, external fonts, raster images, animation, or embedded HTML are required. The existing detailed Mermaid control flow remains available in an expandable section. Text alternatives and surrounding prose preserve the explanation when images are unavailable.

### Engineering story

The README selects three decisions rather than reciting releases: correct the evidence behind a scope gate; validate decisions before granting another invocation; and narrow filesystem guarantees to an explicit operator contract. The rename diagram uses the actual regression filenames and outcome. Longer completion-protocol, timeout, disclosure, and evidence-ownership history remains in supporting documentation.

The owner-directed investigation and acceptance role remain explicit, alongside substantial AI collaboration. That leadership description is owner-provided background. The documentation does not assign sole intellectual origin, claim every line was handwritten, or label AI reviews as independent human security audits.

### Assertions for final human review

| Assertion / judgment | Current support and boundary |
| --- | --- |
| Both rename endpoints participate in scope | Current `--no-renames` queries and R2 regressions; post-write detection, not prevention |
| `run` and `control` have different authority | Separate dispatch paths and explicit diagram branches; readiness remains nonacceptance |
| A fresh human request still passes controller gates | Human-review publication/freshness ordering and controller revalidation; no atomic repository lock |
| Saved records describe observable progress | Receipt/processing projection distinguishes unknown states; terminal publication may fail after acceptance |
| The report directory requires operator trust | Final R5 contract; known-root checks do not discover every operational dependency |
| Owner leadership and personal credit | Supplied by the owner; specific “I invented/discovered” claims would require additional attribution evidence |

These are review points, not newly discovered implementation defects. Genuine Windows console behavior, live backend behavior, and semantic adequacy of checks still need separately scoped evidence before making stronger public claims.

### Validation of this refinement

- 8 Markdown documents checked, including a private working document; **196 relative links and anchors resolved**. Task JSON admitted by both schemas and all 5 CLI help surfaces returned successfully.
- **7 existing regressions passed; 22 deselected**, in 11.06 seconds: the rename cases and README supervisor trust-boundary assertion. No live worker or provider was invoked. The full 575-test result above belongs to the earlier overhaul, not this refinement.
- All **5 SVGs** parse as XML and have titles/descriptions; no scripts, external references, embedded raster images, or foreign objects. Rendered text stays within each viewBox.
- README previews at **920- and 375-pixel viewports**, in light and dark themes, have no page-level horizontal overflow. Diagram previews were inspected at 640 and 343 pixels wide; the smallest diagram type at the latter width is approximately 12.25 pixels. SVGs were also tested as image resources, matching GitHub embedding more closely than inline SVG.
- The two existing Mermaid diagram bodies remain unchanged from the earlier successful parse/render check; this pass adds an expandable wrapper around the detailed flow. No new Mermaid parser result is claimed.
- Fresh AI-assisted read-only review found an ambiguous single-run connector. It was corrected to branch explicitly to both workflows; the reviewer confirmed resolution and no remaining material findings in the targeted check.
- Branch/base unchanged. Runtime, tests, dependencies, CI, license, and historical investigation files remain unchanged. Hash checks preserved the detailed usage/report/handoff contracts and private working material.

The previews are local GitHub-style approximations, not a published rendering. No claim is made about every browser, screen size, terminal, or assistive-technology combination.

## Publication validation

Validated on 21 October 2026 against the unchanged implementation baseline. These results describe this publication pass; the 575-suite and PR-reported 123-review counts above remain separate historical evidence.

| Publication check | Observed result |
| --- | --- |
| Current default branch | Remote `main` still at `977a764cf399cdebf222b444af5d80d186a80878` |
| Workflow and documentation regressions | **24 passed in 61.97 seconds**: integration, control, and supervisor documentation assertion |
| Illustrated failure regressions | **15 passed, 62 deselected, in 10.32 seconds**: six rename cases and nine deadline cases |
| Public Markdown | **10 documents; 216 local links and anchors resolved** |
| Task and command contracts | One JSON example admitted by both schemas; all five CLI help surfaces passed |
| SVGs and README | Five SVGs valid; desktop/mobile light/dark previews inspected without page overflow or out-of-bounds text |
| Presentation integrity | Eight PDF pages and eight PowerPoint slides; per-page visible text matches; 62 relationship targets resolve; package checks pass |
| Presentation editability and rendering | Native editable shapes retained; final PowerPoint imports successfully; all eight PowerPoint and PDF slide renders inspected |
| Publication-only presentation edits | PDF byte-identical to supplied final; only six PowerPoint notes parts changed for publication references and one slide cross-reference; slide parts unchanged |
| Source evidence | 20 distinct pinned source URLs and three historical commit links resolve locally; cited PRs #1, #4, #6–9 rechecked through GitHub |
| Staged whitespace | `git diff --cached --check` passed after trimming redundant EOF blank lines in three Markdown files |
| Scope and privacy | Explicit 17-file Markdown/SVG/PDF/PPTX package; private working material and validation outputs excluded; all four supplied originals preserved |
| Final accuracy review | AI-assisted read-only review found no material actionable issues; attribution and operational qualifications retained |

No full-suite rerun, live provider experiment, or model-backed benchmark was performed for publication. The 39 newly executed tests are neither an addition to the historical counts nor a claim of complete-suite coverage. Local previews and package validation do not establish every PowerPoint version, browser, genuine Windows terminal, or configured backend.

The package adds a concise public decision summary and case-study links. It preserves application source, tests, dependencies, CI, historical investigation files, and the previously reviewed operational contracts. The private working ledger remains unchanged and excluded; its original contents and local validation archives are not part of this publication.
