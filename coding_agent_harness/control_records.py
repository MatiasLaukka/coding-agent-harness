from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import report

MAX_AUDIT_RECORD_BYTES = 64 * 1024 * 1024
_RECORD_KINDS = frozenset({"start", "review", "terminal"})


@dataclass(frozen=True)
class RecordDirectory:
    directory: Path
    protected_roots: tuple[Path, ...]


@dataclass(frozen=True)
class RecordReference:
    record_id: str
    path: Path
    sha256: str

    def model_dump(self) -> dict[str, str]:
        return {"record_id": self.record_id, "path": str(self.path), "sha256": self.sha256}


class RecordPublicationError(RuntimeError):
    def __init__(self, message: str, *, reference: RecordReference, published: bool):
        self.reference = reference
        self.published = published
        super().__init__(message)


def prepare_record_directory(
    path: Path, *, repo_dir: Path, git_roots: tuple[Path, ...] | None = None,
) -> RecordDirectory:
    directory = report._canonical_directory(path)
    protected_roots = git_roots if git_roots is not None else report.discover_report_roots(repo_dir)
    if any(directory.is_relative_to(root) or root.is_relative_to(directory)
           for root in protected_roots):
        raise ValueError("record directory overlaps the repository or Git metadata")
    if not os.access(directory, os.W_OK):
        raise PermissionError("record directory is not writable")
    return RecordDirectory(directory=directory, protected_roots=protected_roots)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json", warnings=False))
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if type(value) is dict:
        return {str(key): _jsonable(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_jsonable(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError("unsupported audit record value")


def encode_record(record: dict[str, Any]) -> bytes:
    text = json.dumps(
        _jsonable(record), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
    ) + "\n"
    data = text.encode("utf-8", errors="strict")
    if len(data) > MAX_AUDIT_RECORD_BYTES:
        raise ValueError("audit record exceeds its size bound")
    return data


def _publish_create_only(destination: Path, data: bytes) -> bool:
    if report._canonical_directory(destination.parent) != destination.parent:
        raise ValueError("record directory changed")
    if os.path.lexists(destination):
        raise FileExistsError("record destination already exists")

    descriptor: int | None = None
    temporary: str | None = None
    linked = False
    primary: BaseException | None = None
    cleanup: BaseException | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".harness-record-", dir=destination.parent,
        )
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            if stream.write(data) != len(data):
                raise OSError("incomplete record write")
            stream.flush()
            os.fsync(stream.fileno())
        if report._canonical_directory(destination.parent) != destination.parent:
            raise ValueError("record directory changed")
        os.link(temporary, destination)
        linked = True
    except BaseException as exc:
        primary = exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as exc:
                cleanup = cleanup or exc
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except BaseException as exc:
                cleanup = cleanup or exc

    if primary is None:
        primary = cleanup
    if primary is not None:
        try:
            primary.record_published = linked
        except Exception:
            pass
        if cleanup is not None and primary is not cleanup:
            try:
                primary.record_cleanup_error = type(cleanup).__name__
            except Exception:
                pass
        if isinstance(primary, Exception):
            raise primary
        raise primary
    return linked


def publish_record(
    directory: RecordDirectory,
    *,
    kind: str,
    record: dict[str, Any],
) -> RecordReference:
    if kind not in _RECORD_KINDS:
        raise ValueError("unsupported record kind")
    record_id = uuid.uuid4().hex
    destination = directory.directory / f"{kind}-{record_id}.json"
    payload = {**record, "record_id": record_id, "record_kind": kind}
    data = encode_record(payload)
    reference = RecordReference(
        record_id=record_id,
        path=destination,
        sha256=hashlib.sha256(data).hexdigest(),
    )
    try:
        published = _publish_create_only(destination, data)
    except BaseException as exc:
        try:
            exc.record_reference = reference
            exc.record_published = bool(getattr(exc, "record_published", False))
        except Exception:
            pass
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, Exception):
            raise RecordPublicationError(
                "audit record publication failed",
                reference=reference,
                published=bool(getattr(exc, "record_published", False)),
            ) from exc
        raise
    if not published:
        raise RecordPublicationError(
            "audit record publication did not complete",
            reference=reference,
            published=False,
        )
    return reference
