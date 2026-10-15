# R4 structural ownership and failure reconciliation

Authority: the implementation assignment authorizes investigation, design, implementation,
regressions, offline validation, and one local commit. Execute inline; independent review
is reserved for the next session. No new checkout, provider calls, network tests, or push.

## Root causes and lifetime trace

The current owner is allocated before creation, but its three separate exception guards
leave cancellation windows after creation and after descriptor closure. Helper responsibility
is encoded on an exception rather than retained by the owner. Cleanup's second deadline
check creates a failure with zero counts; attach-if-absent discards the later owned-resource
snapshot. These are ownership and observation publication failures.

Lifecycle: owner allocation -> possible creation -> mkstemp acquisition -> raw fd/name
publication -> derived paths -> descriptor close -> helper completion/acceptance -> yield ->
Git subprocess (possible activity until termination confirmed) -> context exit -> cleanup
start -> exact index/lock deletion and absence verification -> confirmed completion.

## Invariants

1. Acquisition: install one continuous lifetime guard before any creation or helper startup.
   Not-created is distinct from possibly-created and definitely-owned. A helper publishes
   its raw creation tuple before deriving paths; failed publication remains uncertain.
2. Descriptor: retain the creation tuple independently of descriptor publication. Once close
   may have consumed the numeric fd, mark uncertainty before attempting close; never retry
   an uncertain numeric fd. A successful close positively clears descriptor ownership.
3. Helper: register an operation ticket with the owner before startup; distinguish not-started,
   possibly-running, completed, accepted, abandoned, and late-cleanup completion. An exception
   flag is evidence, never the authority for cleanup. Register construction/start under the
   same guard. Caller waits use the existing shared deadline and reserve.
4. Artifacts: only the unique mkstemp index and its exact .lock belong to this owner. Retain
   both potential artifacts while a helper or Git process can still modify them.
5. Cleanup authorization: no concurrent deletion while an abandoned helper or unconfirmed
   Git subprocess can modify paths. Late cleanup runs after operation completion only.
6. Cleanup completion: only confirmed fd closure and verified absence of index and lock,
   with no possible modifier, permit cleanup-confirmed. Deadline expiry, cancellation,
   unknown creation results, and ambiguous close stay unresolved/uncertain.
7. Failure reconciliation: records are cumulative observations of this collection, not
   additive events. Preserve first reason/stage and known exit status; max counts and OR
   flags make repetition idempotent and preserve earlier uncertainty. Preserve completed
   evidence and original exception identity/traceback. No raw resource identifiers enter
   public metadata; distinct owners never share registry state.
8. Deadline/cancellation: cleanup and its reporting are inside the lifetime guard. Preserve
   the original body exception if cleanup also fails/interruption recurs. If no original
   exists, propagate the cleanup exception itself. No successful evidence after uncertainty.

Python cannot cancel blocked OS I/O, and arbitrary repeated asynchronous exceptions can
interrupt exception handling itself. This design does not claim universal async safety;
its tested transitions retain bounded waits, ownership and conservative failure evidence.

## Implementation and validation sequence

1. Add child-process real SIGINT regressions at creation/close/yield/publication/startup/
   completion/cleanup and deterministic cleanup-clock/metadata regressions. Observe red
   against 00e233e before production changes. All fixture writes stay in this worktree.
2. Introduce owner-held operation tickets; collapse temporary-index handling into one
   continuous guard; make descriptor close/recovery coherent; reconcile failure records.
3. Run focused tests, then the requested three-file suite and complete offline suite with
   fresh worktree-local basetemp, plus diff checks. Preserve established expectations.
4. Perform a supplementary adversarial transition examination, add regressions for any
   demonstrated adjacent defects, rerun affected/full checks, document exact evidence.
5. Stage only intended files, inspect staged diff, make one local correction commit;
   verify clean branch, parent and no push. Supply independent-review commands.

## Ledger

- Verified branch/head/origin/ancestry and clean state at 00e233e.
- Supplied probes: before-close leaks fd+path; before-yield leaks path; between-cleanup-check
  expiry keeps tree but reports unresolved=0/uncertain=false with index still present.
- Ruling: user explicitly authorizes design plus implementation; no additional design
  approval gate or reviewer subagent in this implementation session.

- Initial normal validation: focused 17 passed; requested suite 113 passed; full offline
  suite 414 passed, no failures/skips. Supplementary self-check then demonstrated
  SIGINT at cleanup guard entry bypassing local accounting (real signal, owned path
  remains) and fault-injected successful return from a no-op cleanup being trusted.
- Ruling: retain owners in a collection-local registry registered before acquisition.
  Public collection exception handlers reconcile registry snapshots without OS waits;
  final evidence requires terminal confirmed ownership state. This closes the outer
  reporting boundary rather than adding another exception handler around a line.
  The registry uses no threads, callbacks, global state, or controller changes.
  Unresolved resources after cancellation remain explicit; arbitrary further signals
  inside the outer failure-reporting code remain outside a universal guarantee.

- Complete: final focused ownership regressions 22 passed in 20.14 s; required
  Git/R3/R4 suite 113 passed in 68.76 s; full offline suite 419 passed in 229.48 s.
  Every final run: 0 failed, 0 skipped, 0 inconclusive. Commands and reproduction
  comparisons are in docs/reliability-r4-bounded-git.md. Basetemp and final system
  TEMP/TMP are worktree-local. Existing regression expectations remain unchanged.
- Complete: staged and unstaged whitespace checks passed; all five intended files
  inspected. Supplementary author self-check fixes have covering regressions.
  No independent approval is claimed; no remaining demonstrated defect was deferred.
- Ruling costs: a collection-local registry adds explicit internal parameter plumbing
  and an outer evidence barrier; a false positive would fail the collection closed.
  No budget, additional thread layer, controller/supervisor change, or external action
  was needed. Python/OS uncertainty and arbitrary repeated interruption limits remain
  documented rather than claimed solved.
