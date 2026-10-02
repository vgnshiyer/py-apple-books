"""Worker processes for ``tests/test_backup_concurrency.py`` (run as a
file, never imported by the suite).

``burst DB BACKUP_DIR SLEEP``
    Slows ``write_safety._take_backup`` by ``SLEEP`` seconds, prints
    ``ready``, waits for a line on stdin, then runs
    ``backup_library(DB, BACKUP_DIR, min_interval=300)`` and prints the
    backup's file name (or ``busy`` on ``LibraryBusyError``).

``fork FOLDER OTHER``
    Takes the backup lock of ``FOLDER`` and forks inside it. The child
    checks that it starts with a fresh registry and without the
    inherited descriptor, and that the lock is still held (by the
    parent); it then locks ``OTHER`` (likely on the inherited
    descriptor's number) and checks that leaving the inherited hold
    doesn't close that. The parent checks that, once it releases the
    lock, another descriptor gets it while the child is still alive and
    inside the hold it inherited. Prints one JSON object.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import sys
import time


def burst(db: str, backup_dir: str, sleep: float) -> None:
    from py_apple_books import write_safety
    from py_apple_books.exceptions import LibraryBusyError

    take = write_safety._take_backup

    def slow_take(*args, **kwargs):
        time.sleep(sleep)
        return take(*args, **kwargs)

    write_safety._take_backup = slow_take
    print("ready", flush=True)
    sys.stdin.readline()
    try:
        path = write_safety.backup_library(db, backup_dir, min_interval=300)
    except LibraryBusyError:
        print("busy", flush=True)
        return
    print(os.path.basename(path), flush=True)


def _fd_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError as e:
        if e.errno == errno.EBADF:
            return False
        raise
    return True


def _probe_free(folder: str) -> bool:
    """Whether a new descriptor (a new open file description) gets the
    folder's flock right now."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def fork(folder: str, other: str) -> None:
    from py_apple_books import write_safety as ws
    from py_apple_books.exceptions import LibraryBusyError

    to_parent_r, to_parent_w = os.pipe()
    to_child_r, to_child_w = os.pipe()
    child = {}
    parent = {}
    with ws._backup_folder_lock(folder):
        [held] = ws._held_fds
        pid = os.fork()
        if pid == 0:
            try:
                child["inherited_fd_open"] = _fd_open(held)
                child["registry_fresh"] = not ws._registry and not ws._held_fds
                try:
                    with ws._backup_folder_lock(folder, timeout=0):
                        child["lock_taken"] = True
                except LibraryBusyError:
                    child["lock_taken"] = False
                # Likely on the inherited descriptor's number: leaving
                # the inherited hold must not close it.
                other_hold = ws._backup_folder_lock(other)
                other_hold.__enter__()
                [child["other_fd"]] = ws._held_fds
                child["other_fd_is_inherited_number"] = child["other_fd"] == held
                os.close(to_parent_r)
                os.close(to_child_w)
                os.write(to_parent_w, b"1")  # the parent may release now
                os.read(to_child_r, 1)  # still inside the inherited hold
            except BaseException as e:  # never return into the parent's code
                os.write(to_parent_w, b"1" + json.dumps({"error": repr(e)}).encode() + b"\n")
                os._exit(1)
        else:
            # Hold the lock until the child has checked that it is held.
            os.read(to_parent_r, 1)
    if pid == 0:
        try:
            child["other_fd_open_after_leaving"] = _fd_open(child["other_fd"])
            child["other_fd_held_after_leaving"] = sorted(ws._held_fds) == [child["other_fd"]]
            child["other_locked_after_leaving"] = not _probe_free(other)
            other_hold.__exit__(None, None, None)
            child["other_free_after_release"] = _probe_free(other)
            os.write(to_parent_w, json.dumps(child).encode() + b"\n")
            os.read(to_child_r, 1)  # stay alive until the parent is done
        finally:
            os._exit(0)

    # The parent has released the lock; the child is alive and still
    # inside the hold it inherited.
    parent["child_alive"] = os.waitpid(pid, os.WNOHANG) == (0, 0)
    parent["free_after_release"] = _probe_free(folder)
    parent["parent_fds"] = sorted(ws._held_fds)
    try:
        with ws._backup_folder_lock(folder, timeout=0):
            parent["relocked"] = True
    except LibraryBusyError:
        parent["relocked"] = False
    os.write(to_child_w, b"2")  # the child leaves its inherited hold
    os.close(to_parent_w)
    with os.fdopen(to_parent_r) as reader:
        report = json.loads(reader.readline())
    os.close(to_child_w)
    os.waitpid(pid, 0)
    print(json.dumps({"child": report, "parent": parent}), flush=True)


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "burst":
        burst(args[0], args[1], float(args[2]))
    elif mode == "fork":
        fork(args[0], args[1])
    else:
        raise SystemExit(f"unknown mode {mode!r}")
