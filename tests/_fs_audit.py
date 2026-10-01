"""One audit hook for the test suite: record or refuse file and process access.

``tests/_bootstrap.py`` installs the hook (``sys.addaudithook``) before
anything imports py_apple_books. It does nothing unless a test turns it
on, so the rest of the suite only pays a set lookup per audit event:

    with _fs_audit.record() as rec:
        api.list_books()
    assert not rec.of("subprocess.Popen")

    with _fs_audit.block(_fs_audit.Policy.for_library(home, books=[bundle])):
        api.get_annotations_by_color("yellow")   # a read of the bundle raises PermissionError

``record()`` collects the events in ``EVENTS``; ``block(policy)`` also
raises ``PermissionError`` from the hook for an event the policy refuses
(the operation then fails as if the OS had refused it), and keeps it in
``Recording.refused``.

Scope: a ContextVar, so events count only in the thread (or asyncio
task) that entered ``record()``/``block()``, plus whatever runs in a
context copied from it. Pass ``all_threads=True`` to see every thread
for the duration (a test of thread pools); nesting is allowed.

Stdlib only, and it never imports py_apple_books.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, List, NamedTuple, Optional, Tuple

# File reads and listings: the first argument is the path (or an fd).
PATH_EVENTS = frozenset({"open", "os.listdir", "os.scandir", "shutil.copyfile", "sqlite3.connect"})
# Starting another program or process.
PROCESS_EVENTS = frozenset({
    "subprocess.Popen", "os.posix_spawn", "os.system", "os.exec", "os.fork",
    "os.forkpty", "os.spawn",
})
DLOPEN_EVENTS = frozenset({"ctypes.dlopen"})
EVENTS = PATH_EVENTS | PROCESS_EVENTS | DLOPEN_EVENTS

DOCUMENTS = ("Library", "Containers", "com.apple.iBooksX", "Data", "Documents")
STORE_FOLDERS = ("BKLibrary", "AEAnnotation")
MOBILE_DOCUMENTS = "/library/mobile documents/"


class AuditEvent(NamedTuple):
    event: str
    # The path for PATH_EVENTS (absolute, symlinks resolved), the library
    # name for ctypes.dlopen, the program for process events; None for an
    # fd or an event without one.
    path: Optional[str]
    args: tuple
    thread: str


def _normalize(value) -> Optional[str]:
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            text = os.fsdecode(value)
        except (TypeError, ValueError):
            return None
        if text.startswith("file:"):  # an SQLite URI
            text = urllib.parse.unquote(text[5:].split("?", 1)[0].split("#", 1)[0])
            if text.startswith("//"):  # file://[authority]/path
                text = "/" + text[2:].partition("/")[2]
        if text in ("", ":memory:"):
            return text or None
        return os.path.realpath(os.path.abspath(text))
    return None


def _subject(event: str, args: tuple) -> Optional[str]:
    if not args:
        return None
    if event in PATH_EVENTS:
        return _normalize(args[0])
    if event == "subprocess.Popen" and len(args) > 1:
        program = args[0] if args[0] is not None else (
            args[1][0] if isinstance(args[1], (list, tuple)) and args[1] else args[1])
        return os.fsdecode(program) if isinstance(program, (str, bytes, os.PathLike)) else None
    if isinstance(args[0], (str, bytes, os.PathLike)):
        return os.fsdecode(args[0])
    return None


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class Policy:
    """What ``block()`` lets through. A path event is refused when its
    path is under ``deny``, under any ``Library/Mobile Documents``, or
    (unless ``containers``) under ``<home>/Library/Containers`` or
    ``Group Containers`` outside ``allow``; ``allow`` wins over the
    container rule but not over ``deny``. Process events are refused
    unless ``subprocess``; ``ctypes.dlopen`` unless ``dlopen``.
    """

    home: Optional[str] = None
    allow: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()
    subprocess: bool = False
    dlopen: bool = True
    containers: bool = False
    mobile_documents: bool = False
    # Extra rule: return a reason string to refuse an event, None to let it be.
    extra: Optional[Callable[[AuditEvent], Optional[str]]] = None
    _containers: Tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self):
        home = _normalize(self.home or os.path.expanduser("~"))
        object.__setattr__(self, "home", home)
        object.__setattr__(self, "allow", tuple(p for p in map(_normalize, self.allow) if p))
        object.__setattr__(self, "deny", tuple(p for p in map(_normalize, self.deny) if p))
        object.__setattr__(self, "_containers", tuple(
            os.path.join(home, "Library", name) for name in ("Containers", "Group Containers")))

    @classmethod
    def for_library(cls, home, *, books: Iterable = (), allow: Iterable = (), **kwargs) -> "Policy":
        """The read policy for a library under ``home``: its two store
        folders allowed, ``books`` (bundle or file paths) refused,
        everything else in the containers, iCloud Drive and processes
        refused."""
        docs = os.path.join(os.fspath(home), *DOCUMENTS)
        stores = tuple(os.path.join(docs, name) for name in STORE_FOLDERS)
        return cls(home=os.fspath(home), allow=stores + tuple(map(os.fspath, allow)),
                   deny=tuple(map(os.fspath, books)), **kwargs)

    def refusal(self, ev: AuditEvent) -> Optional[str]:
        """Why ``ev`` is refused, or None."""
        if ev.event in PROCESS_EVENTS:
            if not self.subprocess:
                return "process creation"
        elif ev.event in DLOPEN_EVENTS:
            if not self.dlopen:
                return "ctypes.dlopen"
        elif ev.path:
            if any(_under(ev.path, p) for p in self.deny):
                return "a denied path"
            if not self.mobile_documents and MOBILE_DOCUMENTS in (ev.path + "/").casefold():
                return "iCloud Drive (Mobile Documents)"
            if (not self.containers and any(_under(ev.path, c) for c in self._containers)
                    and not any(_under(ev.path, p) for p in self.allow)):
                return "a container file outside the allowed stores"
        if self.extra is not None:
            return self.extra(ev)
        return None


class Recording:
    """Events seen while a ``record()``/``block()`` was active."""

    def __init__(self, policy: Optional[Policy] = None):
        self.policy = policy
        self.events: List[AuditEvent] = []
        self.refused: List[Tuple[AuditEvent, str]] = []
        self._lock = threading.Lock()

    def _add(self, ev: AuditEvent) -> Optional[str]:
        reason = self.policy.refusal(ev) if self.policy is not None else None
        with self._lock:
            self.events.append(ev)
            if reason is not None:
                self.refused.append((ev, reason))
        return reason

    def of(self, *names: str) -> List[AuditEvent]:
        """The events named ``names`` (all events if none given)."""
        with self._lock:
            return [e for e in self.events if not names or e.event in names]

    def paths(self, *names: str) -> List[str]:
        return [e.path for e in self.of(*names) if e.path]

    def under(self, prefix, *names: str) -> List[AuditEvent]:
        """Path events at or below ``prefix``."""
        root = _normalize(prefix)
        return [e for e in self.of(*(names or PATH_EVENTS)) if e.path and _under(e.path, root)]


_ACTIVE: contextvars.ContextVar[Tuple[Recording, ...]] = contextvars.ContextVar("_fs_audit_active", default=())
_GLOBAL: List[Recording] = []  # all_threads recordings
_GLOBAL_LOCK = threading.Lock()
_BUSY = threading.local()  # re-entrancy guard: the hook's own work raises events too
_installed = False
# Whether py_apple_books was already imported when the hook went in.
package_imported_before_install: Optional[bool] = None


def _hook(event: str, args: tuple) -> None:
    if event not in EVENTS:
        return
    sinks = _ACTIVE.get()
    if _GLOBAL:
        with _GLOBAL_LOCK:
            sinks = sinks + tuple(r for r in _GLOBAL if r not in sinks)
    if not sinks or getattr(_BUSY, "on", False):
        return
    _BUSY.on = True
    try:
        ev = AuditEvent(event, _subject(event, args), args, threading.current_thread().name)
        reasons = [r for r in (sink._add(ev) for sink in sinks) if r is not None]
    except Exception:  # an exception here would fail the audited call itself
        return
    finally:
        _BUSY.on = False
    if reasons:
        raise PermissionError(f"_fs_audit refused {event} of {ev.path!r}: {reasons[0]}")


def install() -> None:
    """Add the hook once (audit hooks can't be removed)."""
    global _installed, package_imported_before_install
    if _installed:
        return
    package_imported_before_install = "py_apple_books" in sys.modules
    sys.addaudithook(_hook)
    _installed = True


def installed() -> bool:
    return _installed


@contextlib.contextmanager
def _activate(rec: Recording, all_threads: bool) -> Iterator[Recording]:
    if not _installed:
        install()
    token = _ACTIVE.set(_ACTIVE.get() + (rec,))
    if all_threads:
        with _GLOBAL_LOCK:
            _GLOBAL.append(rec)
    try:
        yield rec
    finally:
        if all_threads:
            with _GLOBAL_LOCK:
                _GLOBAL.remove(rec)
        _ACTIVE.reset(token)


def record(*, all_threads: bool = False):
    """Collect the audit events in ``EVENTS`` until the block exits."""
    return _activate(Recording(), all_threads)


def block(policy: Policy, *, all_threads: bool = False):
    """Like ``record()``, and refuse what ``policy`` refuses."""
    return _activate(Recording(policy), all_threads)
