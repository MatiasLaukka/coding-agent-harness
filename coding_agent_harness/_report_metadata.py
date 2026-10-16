"""Read-only metadata tree inspection, run as an isolated bounded subprocess."""
from __future__ import annotations

import os
import stat
import sys
import time


def inspect_metadata(roots: list[str], *, max_entries: int = 100_000,
                     max_depth: int = 64, deadline: float | None = None) -> None:
    """Reject aliases, alternate object references, and special files or raise.

    Depth-first scandir retains at most max_depth + 1 open iterators and never
    reads file contents or follows a symlink/reparse point. The parent process
    bounds blocking filesystem calls as well as this cooperative time budget.
    """
    if not roots:
        raise ValueError('Metadata roots required')
    if deadline is None:
        deadline = time.monotonic() + 5
    # These regular files are Git logical references, not filesystem aliases.
    # Reject presence without opening content or following alternate-store chains.
    object_references = {
        os.path.normcase(os.path.join(root, 'objects', 'info', name))
        for root in roots for name in ('alternates', 'http-alternates')
    }
    entries_seen = 0

    def check_time() -> None:
        if time.monotonic() >= deadline:
            raise ValueError('Metadata inspection deadline')

    def inspect(path: str, depth: int) -> None:
        nonlocal entries_seen
        check_time()
        metadata = os.stat(path, follow_symlinks=False)
        if (stat.S_ISLNK(metadata.st_mode) or
                getattr(metadata, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('Metadata aliases unsupported')
        if os.path.normcase(path) in object_references:
            raise ValueError('Alternate object storage unsupported')
        if stat.S_ISDIR(metadata.st_mode):
            if depth > max_depth:
                raise ValueError('Metadata inspection depth')
            with os.scandir(path) as entries:
                for entry in entries:
                    check_time()
                    entries_seen += 1
                    if entries_seen > max_entries:
                        raise ValueError('Metadata inspection entry limit')
                    inspect(entry.path, depth + 1)
        elif depth == 0 or not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Unsupported metadata entry')
        check_time()

    for root in dict.fromkeys(roots):
        inspect(root, 0)


if __name__ == '__main__':
    try:
        inspect_metadata(sys.argv[1:])
    except (OSError, ValueError, RuntimeError):
        sys.exit(1)
    sys.stdout.buffer.write(b'metadata-layout-ok\n')
