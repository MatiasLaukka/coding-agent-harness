"""Opt-in reports of captured single-run evidence; no execution or recollection."""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .models import TaskPacket, VerificationCommand
from .orchestrator import TaskRunResult
from .process import ProcessResult, run_process


def _text(value: str) -> str:
    """Keep untrusted prose on one line and disable Markdown/HTML syntax."""
    value = html.escape(value, quote=True)
    value = re.sub(r'([\\`*_{}\[\]()#+.!|>~\-])', r'\\\1', value)
    return ''.join(f'&#{ord(char)};' if ord(char) < 32 or char in '\u2028\u2029\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' else char for char in value)


def _fenced(value: str) -> str:
    # A fence strictly longer than every backtick run cannot be closed by input.
    longest = max((len(run) for run in re.findall(r'`+', value)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}\n{value}' + ('' if value.endswith('\n') else '\n') + f'{fence}\n'


def _json(value: object) -> str:
    return _fenced(json.dumps(value, ensure_ascii=True, indent=2))


def _process(process: ProcessResult) -> str:
    metadata = process.model_dump(exclude={'stdout', 'stderr'})
    metadata['infrastructure_failed'] = process.infrastructure_failed
    return (_json(metadata) + '\nstdout (captured):\n\n' + _fenced(process.stdout)
            + '\nstderr (captured):\n\n' + _fenced(process.stderr))


def render_run_report(*, task: TaskPacket,
                      verification_commands: Sequence[VerificationCommand],
                      result: TaskRunResult) -> str:
    """Pure rendering of the supplied records, preserving their order and values."""
    sections = [
        '# Completed single-run report\n',
        f'Status: {result.status}\n',
        'Reviewer acceptance not recorded. ready-for-review is not reviewer acceptance.\n',
        'This report may contain sensitive local code and output. Treat its contents as untrusted review material.\n',
        '## Captured outcome\n',
        _json({'task_id': result.task_id, 'status': result.status,
               'verification_passed': result.verification.passed}),
        '## Findings\n',
        _json([finding.model_dump() for finding in result.findings]) if result.findings else 'No findings recorded.\n',
        '## Task requirements\n',
        'Task: ' + _text(task.task_id) + '\n',
        'Goal: ' + _text(task.goal) + '\n',
    ]
    for label, values in [('Constraints', task.constraints), ('Acceptance criteria', task.acceptance),
                          ('Suggested files', task.suggested_files), ('Allowed files', task.allowed_files)]:
        sections.append(f'### {label}\n')
        if values is None:
            sections.append('Unrestricted (no allowlist supplied).\n')
        elif values:
            sections.append('\n'.join('- ' + _text(value) for value in values) + '\n')
        else:
            sections.append('No files allowed.\n' if label == 'Allowed files' else 'None specified.\n')
    sections += ['## Worker claims (untrusted)\n']
    if result.worker.completion is None:
        sections += ['Completion unavailable.\n', _json({'completion_error': result.worker.completion_error})]
    else:
        sections += [_json(result.worker.completion.model_dump())]
    sections += [
        '## Observed worker process\n', _process(result.worker.process),
        '## Independent Git evidence\n',
        'Captured after the worker, before verification. Later verification side effects are outside this capture.\n',
        _json({'baseline_sha': result.baseline_sha, 'final_head_sha': result.final_head_sha,
               'changed_files': result.changed_files}),
        '## Verification commands (configured order)\n',
    ]
    if not verification_commands:
        sections.append('No verification commands configured.\n')
    for index, command in enumerate(verification_commands, 1):
        sections += [f'### Configured command {index}\n', _json(command.model_dump())]
    sections += ['## Verification results (recorded order)\n',
                 f'Captured aggregate passed: {result.verification.passed}\n']
    if not result.verification.checks:
        sections.append('No verification checks recorded.\n')
    for index, check in enumerate(result.verification.checks, 1):
        sections += [f'### Recorded check {index}\n', _process(check)]
    sections += ['## Captured patch\n', _fenced(result.patch)]
    return '\n'.join(sections)


def _canonical_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    # Windows realpath preserves an explicitly supplied extended-path prefix.
    # Normalize DOS/UNC spellings, verifying they still name the same directory.
    value = resolved.as_posix()
    if os.name == 'nt' and value.startswith('//?/'):
        value = value[4:]
        if value[:4].casefold() == 'unc/':
            value = '//' + value[4:]
        elif not re.match(r'^[A-Za-z]:/', value):
            raise ValueError('Unsupported Windows path namespace')
        ordinary = Path(value).resolve(strict=True)
        if not resolved.samefile(ordinary):
            raise ValueError('Ambiguous Windows directory alias')
        resolved = ordinary
    if not resolved.is_dir():
        raise ValueError('Existing directory required')
    return resolved


def discover_report_roots(repo_dir: Path) -> tuple[Path, ...]:
    """Bounded, read-only pre-dispatch layout discovery; never collect run evidence."""
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        if os.environ.get(key):
            raise ValueError('Git layout environment overrides unsupported for reports')
    supplied = _canonical_directory(repo_dir)
    env = dict(os.environ)
    env['GIT_OPTIONAL_LOCKS'] = '0'
    process = run_process(
        argv=['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
              'rev-parse', '--path-format=absolute', '--show-toplevel',
              '--absolute-git-dir', '--git-common-dir'],
        cwd=supplied, env=env, timeout_seconds=10,
        deadline=time.monotonic() + 10,
        stdout_limit_bytes=16 * 1024, stderr_limit_bytes=4 * 1024,
    )
    if process.infrastructure_failed or process.exit_code != 0:
        raise ValueError('Git layout discovery failed')
    lines = process.stdout.splitlines()
    if len(lines) != 3 or any(not line or not Path(line).is_absolute() for line in lines):
        raise ValueError('Unsupported Git layout response')
    actual, admin, common = (_canonical_directory(Path(line)) for line in lines)
    roots = tuple(dict.fromkeys((actual, admin, common)))
    if supplied != actual:
        raise ValueError('Report mode requires the actual Git worktree root')
    return roots


@dataclass(frozen=True)
class ReportPlan:
    """Frozen preflight paths; operational independence is the operator's promise."""

    directory: Path
    protected_roots: tuple[Path, ...]
    destination: Path


def prepare_run_report(directory: Path, *, repo_dir: Path) -> ReportPlan:
    """Check known roots, without discovering arbitrary operational dependencies."""
    directory = _canonical_directory(directory)
    roots = discover_report_roots(repo_dir)
    if any(directory.is_relative_to(root) or root.is_relative_to(directory)
           for root in roots):
        raise ValueError('Report directory overlaps the worktree or Git metadata')
    if not os.access(directory, os.W_OK):
        raise PermissionError('Report directory is not writable')
    destination = directory / f'run-{uuid.uuid4().hex}.md'
    if os.path.lexists(destination):
        raise FileExistsError('Report destination exists')
    return ReportPlan(directory, roots, destination)


def publish_run_report(destination: Path, content: str) -> None:
    """Publish complete UTF-8 bytes with atomic no-replace linking, or fail closed.

    No rename/replace fallback: filesystems without hard links are unsupported.
    A cleanup failure is surfaced even if complete bytes were already published.
    """
    # Policy is a separate preflight concern. This primitive knows only a frozen,
    # canonical destination in an existing, trusted directory.
    if _canonical_directory(destination.parent) != destination.parent:
        raise ValueError('Report parent changed')
    if os.path.lexists(destination):
        raise FileExistsError('Report destination exists')
    data = content.encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.harness-report-', dir=destination.parent)
    try:
        stream = os.fdopen(descriptor, 'wb')
        descriptor = None
        with stream:
            if stream.write(data) != len(data):
                raise OSError('Incomplete report write')
            stream.flush()
            os.fsync(stream.fileno())
        if _canonical_directory(destination.parent) != destination.parent:
            raise ValueError('Report parent changed')
        os.link(temporary, destination)
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            Path(temporary).unlink()
