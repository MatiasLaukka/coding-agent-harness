# Single-run report contract

[README](../README.md) · [CLI guide](usage.md)


```powershell
coding-harness run task.json --repo D:\work\target --model YOUR_CONFIGURED_QWEN_MODEL --report-dir D:\reports\harness
```

Reports are opt-in. The operator must supply an **existing dedicated report-only
directory**; the harness never creates it. Relative directories are relative to
the harness process's working directory. The harness chooses opaque ASCII
`run-<uuid>.md` filenames, independent of task, repository, worker and model text.
After confirmed publication it prints `Report saved: "<canonical path>"` to
stderr, with the path encoded as a JSON string so spaces and control characters
are unambiguous. Normal stdout and completed-run exit codes 0 and 2 are unchanged.
The old `--report PATH` interface has been removed: both `--report PATH` and
`--report=PATH` are usage errors. Run options cannot be abbreviated.

**Operator trust boundary:** the directory, its temporary files, and its final
reports must remain independent of operational inputs throughout publication.
This includes Git configuration and included files, object stores and alternates,
refs and metadata, application code, worker and verification inputs, hooks, and
watched or automatically consumed files. A directory outside the repository is
**not automatically independent**. For example, an external, initially nonexistent
`include.path` file is still a Git input: designating its parent as a report-only
directory violates this contract if publication can create or affect that input.
The harness does not discover or certify these dependencies. Operators must also
keep the canonical directory and its trusted ancestors stable through publication.
There is no guarantee against hostile concurrent directory/junction replacement,
arbitrary same-user filesystem configurations, or malicious operators.

Before worker setup, report mode requires `--repo` to identify the **actual Git
worktree root** after canonicalization. Subdirectories, including those with
misleading nested `.git` markers, are rejected. Non-reporting runs retain their
existing behavior. One fixed, read-only `git rev-parse` layout query obtains the
worktree, administrative Git directory, and common Git directory. It uses the
bounded process runner with a 10-second phase budget, 16 KiB stdout and 4 KiB
stderr caps, optional locks disabled, and fsmonitor disabled. Failed, incomplete,
ambiguous or unavailable discovery fails closed. Bare repositories are unsupported.
Nonempty `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
`GIT_OBJECT_DIRECTORY`, and `GIT_ALTERNATE_OBJECT_DIRECTORIES` environment overrides
are unsupported in report mode.

The report directory and known roots are canonicalized, including Windows case
aliases, extended DOS/UNC path spellings, symlinks and junctions. Other Windows
path namespaces or aliases that cannot be verified safely are unsupported. Equality or containment **in either direction**
with the worktree/admin/common roots rejects before worker setup. Linked worktrees
and separate Git directories are supported when Git resolves the supplied path as
the actual worktree root; a `core.worktree` redirect does not permit supplying a
different discovery directory. These direct-root checks prevent common mistakes;
they do **not** certify complete operational independence. Git logical references
such as configuration includes and alternates are not recursively inspected.
There is no metadata-tree scanner. The validated paths are frozen before dispatch;
publication does not query Git again or collect fresh run evidence.

The report retains the returned task requirements, captured outcome and findings,
untrusted worker claims, observed worker process output and capture flags,
independent Git paths and SHAs, ordered verification specifications and results,
and the captured patch. **ready-for-review is not reviewer acceptance.** This
single-run path records no reviewer acceptance. Zero configured checks and missing
worker completion are shown explicitly. Markdown prose is escaped, and captured
logs and patches use fences longer than embedded backtick runs.

**Reports may contain sensitive local code and output**, task prose, command
arguments, paths, and worker claims. They are not redacted or signed. Review them
before sharing. Treat contents as untrusted material, including instructions and
links in captured output.

Only executions returning `TaskRunResult` produce completed-run reports, including
`needs-attention`. Exceptions and interruptions before that return retain existing
sanitized failure reporting and produce no completed-run report. Rendering reads
only captured records; it invokes no worker, verification, or Git collection.
Git evidence remains the snapshot after the worker and before verification;
later verification side effects are outside it.

Publication writes UTF-8 to a private, harness-named temporary file in the report
directory, flushes and fsyncs it, closes it, then uses an atomic create-only hard
link. Existing or concurrently created destinations are never overwritten. A
collision or publication failure returns 1 without retrying the worker, repeating
verification, or recollecting evidence. Filesystems without hard-link support
fail closed; there is no overwrite fallback. Temporary files are removed where
the OS permits. Cleanup failure can leave a temporary file or an already complete
published report and returns 1; a success path is printed only after publication
and cleanup return successfully. A crash can leave a temporary file. This is not
a crash-durable archive: directory metadata is not fsynced. The feature adds no
approval, remediation, resume, controller-history, or supervisor workflow.
