"""Finite, isolated real-SIGINT probe; invoked by test_reliability_r4_ownership."""
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import coding_agent_harness.git as g

root = Path(sys.argv[1])
case = sys.argv[2]
repo = root / "repo"
repo.mkdir()

def git(*args):
    return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.PIPE, timeout=5)

git("init", "-q")
git("config", "user.name", "Offline ownership regression")
git("config", "user.email", "offline@example.test")
(repo / "a").write_text("baseline\n")
git("add", ".")
git("commit", "-qm", "baseline")
index = repo / ".git" / "index"
index_before = index.read_bytes()
records = []
helpers = []
entered = threading.Event()
release = threading.Event()
first = KeyboardInterrupt("original cancellation")
second = KeyboardInterrupt("cleanup cancellation")
fired = []
real_mkstemp = tempfile.mkstemp
real_start = threading.Thread.start
real_init = threading.Thread.__init__
real_snapshot = g.GitWorktreeSnapshot
real_unlink = Path.unlink
real_cleanup = g._cleanup_temporary_index
real_copy = g._copy_index_sync
baseline = g.capture_baseline(repo)
neighbor = root / "unrelated-index"
neighbor.write_bytes(b"unrelated index")
neighbor_lock = Path(str(neighbor)+".lock")
neighbor_lock.write_bytes(b"unrelated lock")
owners = []
real_owner = g._TemporaryIndex

def make_owner(*args, **kwargs):
    owner = real_owner(*args, **kwargs)
    owners.append(owner)
    return owner

def copy(*args, **kwargs):
    if case == "late-copy":
        entered.set()
        assert release.wait(3), "finite copy gate expired"
    return real_copy(*args, **kwargs)
source, base = inspect.getsourcelines(g._temporary_index.__wrapped__)
close_argument = next(i for i, line in enumerate(source) if "lambda: _close_temporary_index_descriptor(owner)" in line)
close_call = next(i for i in range(close_argument-1, -1, -1) if "_bounded_filesystem_call(" in source[i])
close_line = base + (close_call-1 if source[close_call-1].strip() == "try:" else close_call)
yield_index = next(i for i, line in enumerate(source) if line.strip() == "yield owner")
yield_line = base + (yield_index-1 if source[yield_index-1].strip() == "try:" else yield_index)
cleanup_call = next(i for i, line in enumerate(source) if "cleanup_failure = _cleanup_temporary_index(" in line)
cleanup_guard_line = base + next(i for i in range(cleanup_call-1, -1, -1) if source[i].strip() == "try:")
publication_line = base + next(i for i, line in enumerate(source) if line.strip() == "owner.descriptor = fd")

def handler(_s, _f):
    raise first if len(fired) == 1 else second

signal.signal(signal.SIGINT, handler)

def interrupt(label):
    fired.append(label)
    signal.raise_signal(signal.SIGINT)

def mkstemp(prefix):
    value = real_mkstemp(prefix=prefix, dir=root)
    records.append(value)
    if case == "creation-blocked":
        entered.set()
        assert release.wait(3), "finite creation gate expired"
    return value

def cleanup(owner, **kwargs):
    if case == "cleanup-noop":
        return None
    if case in {"cleanup-entry", "repeat-cleanup"}:
        interrupt("cleanup-entry")
    return real_cleanup(owner, **kwargs)

def unlink(path, *args, **kwargs):
    if case == "cleanup-running" and records and path == Path(records[0][1]+".lock"):
        entered.set()
        assert release.wait(3), "finite cleanup gate expired"
    return real_unlink(path, *args, **kwargs)

def init(helper, *args, **kwargs):
    real_init(helper, *args, **kwargs)
    if case == "helper-construction" and records and not fired and helper.name == "harness-git-filesystem":
        interrupt(case)

def snapshot(**kwargs):
    if case == "final-evidence":
        interrupt(case)
    return real_snapshot(**kwargs)

def worker_trace(frame, event, arg):
    if case in {"ownership-publication", "publication-error"} and not fired and event == "line" and frame.f_code.co_name == "create_temporary_index":
        if frame.f_lineno == publication_line:
            if case == "publication-error":
                fired.append(case)
                raise first
            entered.set()
            assert release.wait(3), "finite ownership publication gate expired"
    return worker_trace

def start(helper, *args, **kwargs):
    result = real_start(helper, *args, **kwargs)
    if helper.name == "harness-git-filesystem":
        helpers.append(helper)
        if case == "helper-start" and len(helpers) == 1:
            interrupt("helper-start")
        if case in {"creation-blocked", "ownership-publication"} and len(helpers) == 1:
            assert entered.wait(2)
            interrupt(case)
        if case == "late-copy" and len(helpers) == 3:
            assert entered.wait(2)
            interrupt(case)
        if case == "cleanup-running" and len(helpers) == 4:
            assert entered.wait(2)
            interrupt("cleanup-running")
    return result

def trace(frame, event, arg):
    if fired:
        return trace
    if frame.f_code is g._temporary_index.__wrapped__.__code__ and event == "line":
        if case == "cleanup-guard" and frame.f_lineno == cleanup_guard_line:
            interrupt(case)
        if case == "before-close" and frame.f_lineno == close_line:
            interrupt(case)
        if case == "before-yield" and frame.f_lineno == yield_line:
            interrupt(case)
    if frame.f_code is g._bounded_filesystem_call.__code__ and event == "return":
        if case == "creation-return" and isinstance(arg, tuple) and len(arg) == 2:
            interrupt(case)
        if case == "cleanup-result" and isinstance(arg, tuple) and len(arg) == 3:
            interrupt(case)
    if case == "helper-completion" and event == "line" and frame.f_code is g._bounded_filesystem_call.__code__:
        if frame.f_locals.get("completed_at") is not None:
            interrupt(case)
    return trace

caught = None
started = time.monotonic()
try:
    with patch.object(g.tempfile, "mkstemp", mkstemp), patch.object(threading.Thread, "start", start), patch.object(threading.Thread, "__init__", init), patch.object(g, "_cleanup_temporary_index", cleanup), patch.object(Path, "unlink", unlink), patch.object(g, "GitWorktreeSnapshot", snapshot), patch.object(g, "_copy_index_sync", copy), patch.object(g, "_TemporaryIndex", make_owner):
        threading.settrace(worker_trace)
        sys.settrace(trace)
        try:
            if case == "repeat-cleanup":
                with g._temporary_index(prefix="repeat-", deadline=time.monotonic()+5, init_stage="snapshot-index-init", cleanup_stage="snapshot-cleanup"):
                    interrupt("body")
            elif case == "late-copy":
                g.capture_evidence(repo=repo, baseline=baseline)
            else:
                g.capture_worktree_snapshot(repo)
        except BaseException as exc:
            caught = exc
        finally:
            sys.settrace(None)
        # Capture immutable failure evidence before releasing a possibly blocked helper.
        failures = [getattr(caught, a, None) for a in ("git_failure", "git_cleanup_failure")]
        failures = [f for f in failures if isinstance(f, g.GitFailureMetadata)]
        before = {
            "pending": max((f.filesystem_operation_pending_count for f in failures), default=0),
            "unresolved": max((f.temporary_artifact_unresolved_count for f in failures), default=0),
            "uncertain": any(f.temporary_artifact_cleanup_uncertain for f in failures),
        }
        confirmed_before_release = bool(owners and owners[0].artifact_state == "cleanup-confirmed")
        release.set()
        for helper in helpers:
            helper.join(2)
        fd, name = records[0]
        try:
            os.fstat(fd)
            descriptor_open = True
        except OSError:
            descriptor_open = False
        result = dict(before, fired=fired, identity=caught is first,
                      failed_closed=caught is not None,
                      confirmed_before_release=confirmed_before_release,
                      confirmed_after_release=bool(owners and owners[0].artifact_state == "cleanup-confirmed"),
                      neighbor_unchanged=(neighbor.read_bytes()==b"unrelated index" and neighbor_lock.read_bytes()==b"unrelated lock"),
                      elapsed=time.monotonic()-started,
                      descriptor_open=descriptor_open,
                      artifact_exists=Path(name).exists(),
                      lock_exists=Path(name+".lock").exists(),
                      helpers_alive=sum(h.is_alive() for h in helpers),
                      index_unchanged=index.read_bytes()==index_before,
                      tree_preserved="tree" in getattr(caught, "git_evidence", {}))
finally:
    sys.settrace(None)
    threading.settrace(None)
    release.set()
    for helper in helpers:
        helper.join(2)
    for fd, name in records:
        try:
            os.fstat(fd)
        except OSError:
            pass
        else:
            os.close(fd)
        real_unlink(Path(name+".lock"), missing_ok=True)
        real_unlink(Path(name), missing_ok=True)
print(json.dumps(result))
