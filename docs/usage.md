# CLI and task guide

[README](../README.md) · [Architecture](architecture.md) · [Reports](reports.md) · [Supervisor handoff](supervisor-handoff.md)

## Install and inspect without dispatch

Requires Python 3.12+ and Git on `PATH`. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
coding-harness --help
coding-harness run --help
coding-harness control --help
coding-harness handoff validate --help
coding-harness handoff preflight --help
```

POSIX activation: `source .venv/bin/activate`. Both `coding-harness` and `coding-agent-harness` are installed entry points to the same CLI. Help does not dispatch a worker. The [README demonstration](../README.md#try-it-without-a-model) runs a synthetic worker test without model access.

For real work, install and configure Qwen separately and choose an available model identifier. The harness passes arguments to that executable; it neither provisions a backend nor certifies where inference runs. A `--model` string is not evidence of backend identity.

## Task file

Both execution commands read UTF-8 JSON with `task` and `verification_commands`. Keep this file outside the target worktree, or track and commit it before starting: the target baseline must be clean.

The following is a template for a target project that already has the named source and tests. Adapt paths and criteria before use; it is not an example task targeting this harness repository.

```json
{
  "task": {
    "task_id": "bounded-fix",
    "goal": "Correct the edge case in src/example.py.",
    "constraints": ["Do not change unrelated behavior."],
    "acceptance": ["The existing example tests pass."],
    "suggested_files": ["src/example.py"],
    "allowed_files": ["src/example.py"]
  },
  "verification_commands": [
    {
      "id": "focused-tests",
      "argv": ["python", "-m", "pytest", "tests/test_example.py", "-q"],
      "timeout_seconds": 120,
      "expectation": "must_pass"
    }
  ]
}
```

Verification commands are argument arrays executed in the target directory, not shell command strings. Executables resolve in the harness process environment; activate an appropriate environment or use an absolute executable path. Worker-run tests remain claims until the harness separately executes its configured checks.

### File scope

For `run` and the library, `allowed_files: null` (or omission) imposes no file restriction; `[]` permits no changed files; a list is an exact repository-relative allowlist. Rename sources and destinations both need coverage. Scope checks detect cumulative changes after a worker invocation. They do not prevent writes, contain the worker, assess semantic scope, or restore files.

`control` requires a nonempty explicit allowlist with unique portable paths and keeps it fixed through its optional remediation.

### Verification expectations

| Expectation | Ordinary result | Infrastructure failure |
| --- | --- | --- |
| `must_pass` (default) | Exit 0 passes; nonzero blocks aggregate success | Blocks aggregate success |
| `expected_failure` | Nonzero meets expectation; exit 0 records a mismatch but does **not** block aggregate success | Blocks aggregate success |
| `advisory` | Recorded without changing aggregate success | Blocks aggregate success |

`expected_failure` requires `expectation_reason`. A timeout, failed launch, unknown exit, incomplete/truncated capture, or cleanup uncertainty is not an ordinary expected failure. Infrastructure failures also prevent controller remediation/scope retries. An empty list performs no checks and yields aggregate success in `run` and the library. Supply meaningful checks; a PASS label does not prove coverage.

Control requires 1–8 uniquely identified checks, including at least one non-advisory check. That admission rule allows `expected_failure`; it does not require a successful `must_pass` assertion.

## Single invocation: `run`

The following commands dispatch a real configured worker. Use a dedicated, clean target worktree and trusted commands. `YOUR_CONFIGURED_QWEN_MODEL` is a placeholder.

```powershell
coding-harness run task.json --repo D:\work\target-worktree --model YOUR_CONFIGURED_QWEN_MODEL
```

| Option | Contract |
| --- | --- |
| `--repo` | Required target repository/worktree |
| `--model` | Required identifier passed to the executable |
| `--qwen-command` | Launcher executable; default `qwen` |
| `--worker-timeout` | Worker timeout in seconds; default 600 |
| `--report-dir DIR` | Optional existing dedicated output directory; see [full contract](reports.md) |

Run options cannot be abbreviated. The removed `--report PATH` spelling and its prefixes are usage errors. Reporting requires `--repo` to be the canonical actual worktree root; non-reporting runs retain their existing repository-discovery behavior.

`run` invokes one worker, captures evidence, runs checks, and prints findings and patch. It does not prompt for authorization, call a reviewer, remediate, or record acceptance. Its stdout review surface can contain sensitive raw output; the sanitized failure summary has a narrower disclosure contract.

## Human workflow: `control`

This is a **foreground Windows console** interface. A genuine console input handle and output screen buffer must be available before authorization/dispatch; redirected streams and unsupported terminals may fail admission. Tests with scripted consoles exercise orchestration without demonstrating every real terminal.

```powershell
coding-harness control task.json --repo D:\work\target-worktree --model YOUR_CONFIGURED_QWEN_MODEL --record-dir D:\records\harness --max-remediation-attempts 1
```

The record directory must already exist, be dedicated to records, and remain independent of operational resources. The directory and trusted ancestors must stay stable. Known worktree/admin/common-root overlap is rejected, but being outside those roots does not establish independence. Git layout discovery and unsupported override rules are shared with [report preflight](reports.md).

| Option | Contract |
| --- | --- |
| `--repo`, `--model` | Required actual worktree root and configured worker model |
| `--worker-command` / `--qwen-command` | Aliases for a single launcher executable; default `qwen` |
| `--worker-timeout` | 1–14,400 seconds; default 600 |
| `--review-timeout` | 1–1,800 seconds per START/review transaction; default 300 |
| `--max-remediation-attempts` | Only 0 or 1; default 0 |
| `--record-dir` | Required existing trusted record-only directory |

1. Inspect the displayed task, exact checks, scope, baseline, model/launcher argv, timeouts, budget, and canonical roots. Type the fresh `START <challenge>` to authorize only the first dispatch. A start record must publish, and the controller must confirm the same baseline.
2. After the worker and checks, inspect the complete review packet. The CLI checks freshness first and states that acceptance has not occurred.
3. Type `ACCEPT <challenge>`, `STOP`, or, if enabled and remaining, `REMEDIATE <challenge>`. All decisions need a bounded nonblank rationale; remediation also needs bounded instructions. Human acceptance requests remain subject to controller gates.
4. The decision record publishes before a final freshness check. Only then can the callback return to the controller. A remediation repeats execution and review under the frozen scope.
5. Inspect the terminal record for the actual result or available failure evidence. A saved request alone is not proof of controller receipt or processing; a missing terminal record means the workflow is incomplete.

Input is bounded, typeahead is discarded at transaction boundaries, and invalid input does not extend the transaction deadline. Worker and verification stdin are `DEVNULL`. Task JSON rejects duplicate keys and has a 256 KiB size bound. Additional text/path/argument bounds are in [control.py](../coding_agent_harness/control.py).

The `control` CLI offers no scope expansion, saved-approval replay, resume, or automatic publication to GitHub. An interrupted or failed workflow needs a new operator decision; saved records do not authorize continuation.

## Exit codes

| Command | 0 | 2 | 1 | 130 |
| --- | --- | --- | --- | --- |
| `run` | `ready-for-review` | `needs-attention` | Loading/runtime/report failure | Caught Ctrl+C during worker setup/execution |
| `control` | Accepted controller result and terminal publication | Completed nonaccepted result and terminal publication | Workflow/evidence/input/publication failure | Ctrl+C |
| `handoff validate` / `preflight` | Valid document/preflight outcome | Missing approval or failed preflight | Malformed/contradictory document or output error | No dedicated handoff mapping |

Argument parsing also uses exit 2 for usage errors. Interruptions outside a command's explicit handler follow Python's interruption behavior. In `run`, report-publication interruption returns 1. Cleanup failure can return 1 even after a report was linked. In `control`, terminal publication failure after acceptance reports an error without rewriting the controller's actual outcome. Never infer a specific decision from an exit code without inspecting available result/failure evidence.

## Worker completion protocol

`QwenWorker` appends `--model <model> -p <prompt>` to its configured command. It expects exactly one stdout line prefixed `HARNESS_RESULT:`, followed by a JSON object with `status` (`completed`, `blocked`, or `failed`), `summary`, `changed_files`, and `remaining_concerns`. File claims refer only to that worker invocation.

Multiple records, duplicate JSON keys, malformed payloads, missing completion, and incomplete/truncated stdout cannot establish a valid completion. Raw process evidence is retained separately. See [worker.py](../coding_agent_harness/worker.py) and [protocol tests](../tests/test_worker_outcome.py).

## Supervisor documents

`handoff validate` and `handoff preflight` never execute proposals. They use separate exact operator approval documents; these are not the human console's START/ACCEPT decisions. See the [complete handoff guide](supervisor-handoff.md) for schemas, a manual round trip, trusted caller duties, privacy limits, and nonexecuting recommendations.
