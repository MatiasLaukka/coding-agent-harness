"""Opt-in reports of captured single-run evidence; no execution or recollection."""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence

from .models import TaskPacket, VerificationCommand
from .orchestrator import TaskRunResult
from .process import ProcessResult


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


def _read_layout_file(path: Path) -> str:
    with path.open('rb') as stream:
        data = stream.read(4097)
    if len(data) > 4096:
        raise ValueError('Git layout file too large')
    return data.decode('utf-8').strip()


def _protected_roots(repo_dir: Path) -> list[Path]:
    # Inspect only layout files, never run Git or collect additional evidence.
    # Environment overrides can relocate metadata beyond the discovered layout.
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        if os.environ.get(key):
            raise ValueError('Git layout environment overrides unsupported for reports')
    repo = repo_dir.resolve(strict=True)
    if not repo.is_dir():
        raise ValueError('Repository directory required')
    for root in (repo, *repo.parents):
        marker = root / '.git'
        if os.path.lexists(marker):
            if marker.is_dir():
                admin = marker.resolve(strict=True)
            else:
                content = _read_layout_file(marker)
                if not content.startswith('gitdir: ') or not content[8:]:
                    raise ValueError('Invalid Git layout')
                admin = (root / content[8:]).resolve(strict=True)
            if not admin.is_dir():
                raise ValueError('Invalid Git directory')
            common_file = admin / 'commondir'
            common = admin
            if os.path.lexists(common_file):
                value = _read_layout_file(common_file)
                if not value:
                    raise ValueError('Invalid common directory')
                common = (admin / value).resolve(strict=True)
                if not common.is_dir():
                    raise ValueError('Invalid common directory')
            return [root, admin, common]
    raise ValueError('Cannot determine repository layout')


def validate_report_destination(destination: Path, *, repo_dir: Path) -> Path:
    """Point-in-time validation; output parents must be trusted and pre-existing."""
    destination = Path(destination).absolute()
    name = destination.name
    if (not name or ':' in name or name.endswith((' ', '.')) or
            re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.IGNORECASE)):
        raise ValueError('Invalid report filename')
    if os.path.lexists(destination):
        raise FileExistsError('Report destination exists')
    parent = destination.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError('Report parent must be a directory')
    resolved = parent / name
    if '.git' in (part.casefold() for part in resolved.parts):
        raise ValueError('Report destination is in Git metadata')
    if any(resolved.is_relative_to(root) for root in _protected_roots(repo_dir)):
        raise ValueError('Report destination is inside the repository or Git metadata')
    if not os.access(parent, os.W_OK):
        raise PermissionError('Report parent is not writable')
    return resolved


def publish_run_report(destination: Path, content: str, *, repo_dir: Path) -> None:
    """Publish complete UTF-8 bytes with atomic no-replace linking, or fail closed.

    No rename/replace fallback: filesystems without hard links are unsupported.
    A cleanup failure is surfaced even if complete bytes were already published.
    """
    destination = validate_report_destination(destination, repo_dir=repo_dir)
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
        # Recheck layout/parent after writing; os.link handles destination races.
        checked = validate_report_destination(destination, repo_dir=repo_dir)
        if checked != destination:
            raise ValueError('Report parent changed')
        os.link(temporary, destination)
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            Path(temporary).unlink()
