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
preparation from the real index and are removed on success, failure, and interruption.

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
