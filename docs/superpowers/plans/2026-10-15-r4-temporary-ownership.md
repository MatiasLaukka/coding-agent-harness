# R4 Temporary Resource Ownership Correction

Date: 2026-10-15

## Objective

Correct helper cancellation/startup and temporary-index cleanup-expiry ownership gaps in R4 without changing evidence collection semantics or broadening into supervisor preflight.

## Lifecycle model

Track filesystem helper state from pre-start through possible-running, completion, and abandonment. Track the temporary index independently as not created, possibly created, owned with its descriptor and index/lock paths, cleanup confirmed, or cleanup uncertain. On interruption, preserve the original exception and report pending work and unresolved artifacts; defer cleanup until the modifying helper has completed. If cleanup cannot start before its deadline, report known owned artifacts as unresolved without filesystem probes.

## Steps

1. Add deterministic red regressions for interrupted real `Thread.start()` handoff, late helper completion, interrupted temporary-file creation/descriptor transfer, and cleanup expiry before helper launch. Run the interrupt scenarios in isolated child processes with outer deadlines and exact child cleanup.
2. Make helper startup and completion handoff interruption-safe; ensure abandoned operations remain pending and only late-clean after completion. Preserve observed process results, captured output, completed Git observations, and original exceptions.
3. Make temporary-index ownership explicit before creation handoff and retain index/lock uncertainty if cleanup is skipped or cannot start. Keep existing bounded cleanup and termination gates.
4. Verify worker/controller fail-closed behavior, shared verification behavior, confidentiality, invocation counts, reviewer validation, retry budgets, and supervisor provenance through existing targeted coverage.
5. Run focused regressions, the requested three-file suite, the full offline suite, and `git diff --check` with a fresh dedicated pytest temporary directory; prepare a local review commit only.

## Constraints

No live providers, Qwen, network, frozen experiment access, other checkout changes, push, PR, or merge. Preserve all existing assertions and the two earlier R4 corrections.
