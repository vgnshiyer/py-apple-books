"""Ranked annotation search (new in 1.11).

:meth:`PyAppleBooks.search_annotations
<py_apple_books.PyAppleBooks.search_annotations>` ranks highlights and
notes against a query, best first, and returns :class:`AnnotationHit`
objects. This module holds its public types and the private index it
runs on.

The index is a SQLite FTS5 table in a private in-memory database, one
per :class:`~py_apple_books.db.LibraryDB`, built on first use from the
annotation store's highlighted text, notes and surrounding text, each
folded with :func:`py_apple_books.text.fold_for_match` (so are queries).
It is never written to disk, never shown (hits carry the annotations
themselves), and rebuilt when the annotations change: that is checked at
most once a second. ``LibraryDB.close()`` (``PyAppleBooks.close()``)
drops it.

Public: :class:`AnnotationHit`, :class:`MatchMethod`,
:func:`fts5_available`, :data:`MAX_QUERY_LENGTH`. Importing this module
does no I/O; FTS5 is probed on first use.
"""

from __future__ import annotations

import functools
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple, TypeVar

from py_apple_books.db.clause import _text
from py_apple_books.db.client import ANNOTATION_SCHEMA, ANNOTATIONS_NOT_FOUND, _identity, use_library
from py_apple_books.exceptions import (
    AnnotationStoreNotFoundError,
    DBQueryError,
    InvalidArgumentError,
    QueryTimeoutError,
)
from py_apple_books.models.annotation import _ALL_ANNOTATIONS, _LIVE_ANNOTATIONS, Annotation
from py_apple_books.text import fold_for_match

if TYPE_CHECKING:
    from py_apple_books.db.client import LibraryDB

__all__ = ["AnnotationHit", "MatchMethod", "fts5_available", "MAX_QUERY_LENGTH"]

_T = TypeVar("_T")

#: The longest query :meth:`PyAppleBooks.search_annotations` accepts, in
#: characters; a longer one raises :class:`InvalidArgumentError`.
MAX_QUERY_LENGTH = 10_000

# At most this many distinct words (and quoted phrases) of a query are
# searched for; the whole query is still matched as one substring.
_MAX_TERMS = 32

# The indexed columns: highlighted text, note, surrounding text; and the
# bm25 weight of each, so a word in the highlight counts most and one
# only in the surrounding paragraph (which always holds the highlight)
# least. The substring tiers score a hit with the same weights.
_COLUMNS = ("sel", "note", "rep")
_WEIGHTS = (10.0, 5.0, 1.0)

# FTS5 tokenizers, best first: SQLite before 3.27 has no
# remove_diacritics 2.
_TOKENIZERS = ("porter unicode61 remove_diacritics 2", "porter unicode61 remove_diacritics 1",
               "porter unicode61")

# Freshness: the annotation store is fingerprinted at most once per
# _RECHECK seconds; without a Z_OPT column (or when fingerprinting
# fails) the index is also rebuilt every _TTL_WITHOUT_ZOPT seconds.
_RECHECK = 1.0
_TTL_WITHOUT_ZOPT = 60.0

# Rows read from the annotation store per build statement, and
# annotations loaded per statement for a page of hits.
_CHUNK = 2000
_PAGE_CHUNK = 500

# SQLite VM instructions between deadline checks on the private database.
_PROGRESS_OPCODES = 1000

# The key of the index in LibraryDB._derived_cache.
_INDEX_KEY = "annotation_index"

# Fixed message of every failure of the private index: SQLite's own
# messages can quote the query (FTS5 echoes its terms).
_FAILED = "Ranked annotation search failed."

# English function words left out of a multi-word query (unless every
# word is one, when the words are searched for as a phrase). Stemming is
# English-only as well.
_STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from
further had has have having he her here hers herself him himself his how i if in into is it
its itself just me more most my myself no nor not of off on once only or other our ours
ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were
what when where which while who whom why will with would you your yours yourself yourselves
""".split())

# Scripts written without spaces between words (unicode61 makes one
# token of a whole run of them), and Hangul (particles attach to the
# word): a query holding any of them is matched as substrings. Kana,
# CJK ideographs (unified, extension A, compatibility, extensions B-H),
# Hangul syllables and jamo, Thai and Lao, Khmer, Myanmar.
_NO_SPACE_SCRIPT = re.compile(
    "[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\U00020000-\U000323af\uac00-\ud7af\u1100-\u11ff\u3130-\u318f"
    "\u0e00-\u0eff\u1780-\u17ff\u1000-\u109f]")


class MatchMethod(str, Enum):
    """How an :class:`AnnotationHit` matched: ``FTS``, the full-text
    index (words, English stemming); ``SUBSTRING``, folded text
    containing the query or its words. Compares equal to its value."""

    FTS = "fts"
    SUBSTRING = "substring"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, eq=False)
class AnnotationHit:
    """One result of :meth:`PyAppleBooks.search_annotations`.

    Compared and hashed by identity.

    :ivar annotation: the :class:`~py_apple_books.models.Annotation`,
        read from the instance's library (its relations work).
    :ivar score: higher is better. ``-bm25`` for ``FTS`` hits, a weighted
        count of the matching fields for ``SUBSTRING`` hits, so compare
        scores only between hits of the same method in one result.
    :ivar matched_all: whether every word of the query matched (hits
        with ``matched_all`` come first).
    :ivar method: the :class:`MatchMethod`.
    """

    annotation: Annotation
    score: float
    matched_all: bool
    method: MatchMethod


# -- FTS5 -------------------------------------------------------------------

_fts5: Optional[bool] = None
_fts5_lock = threading.Lock()


def _new_fts5_lock() -> None:
    # A child forked while another thread held the lock (probing) would
    # otherwise wait for it forever.
    global _fts5_lock
    _fts5_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_fts5_lock)


def _probe_fts5() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def fts5_available() -> bool:
    """Whether this Python's SQLite has FTS5 (probed once, in memory, on
    the first call). Without it :meth:`PyAppleBooks.search_annotations`
    still works, matching substrings only (every hit ``SUBSTRING``)."""
    global _fts5
    found = _fts5
    if found is None:
        with _fts5_lock:
            if _fts5 is None:
                _fts5 = _probe_fts5()
            found = _fts5
    return found


# -- queries ----------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """What a query searches for."""

    #: FTS5 strings (quoted) for ``items``, in order.
    terms: Tuple[str, ...]
    #: The distinct words and quoted phrases, folded (stopwords left out).
    items: Tuple[str, ...]
    #: Match ``items`` as substrings, not with FTS5: the query holds a
    #: script written without spaces, or no word at all (punctuation).
    substring: bool
    #: The whole folded query as a substring, the tier that makes results
    #: a superset of ``search_annotation_by_text`` for every folded query
    #: of 3 or more characters: stripped when that leaves 3 or more
    #: characters (it then finds more), else as folded (' ab' keeps its
    #: space, exactly the substring search's needle); None when shorter.
    needle: Optional[str]


def _token_char(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category[0] in "LNM" or category == "Co"


def _split_words(text: str, limit: int) -> List[str]:
    """The runs of letters, digits and marks in ``text`` (an apostrophe
    between two of them stays in the word), at most ``limit``."""
    words: List[str] = []
    cur: List[str] = []
    for i, ch in enumerate(text):
        if _token_char(ch):
            cur.append(ch)
        elif ch == "'" and cur and i + 1 < len(text) and _token_char(text[i + 1]):
            cur.append(ch)
        elif cur:
            words.append("".join(cur))
            cur = []
            if len(words) >= limit:
                return words
    if cur:
        words.append("".join(cur))
    return words


def _quote(item: str) -> str:
    """``item`` as an FTS5 string: operators and column filters in it
    are plain text."""
    return '"' + item.replace('"', '""') + '"'


def _query_text(query) -> Optional[str]:
    """``query`` as text, as the ``__search`` lookup converts it
    (``str()``; None for an int too long to convert).

    :raises InvalidArgumentError: longer than :data:`MAX_QUERY_LENGTH`.
    """
    if isinstance(query, str) and len(query) > MAX_QUERY_LENGTH:
        raise _too_long()
    text = _text(query)
    if text is not None and len(text) > MAX_QUERY_LENGTH:
        raise _too_long()
    return text


def _too_long() -> InvalidArgumentError:
    return InvalidArgumentError(f"The query is too long (at most {MAX_QUERY_LENGTH} characters).")


def _plan(query) -> Optional[_Plan]:
    """The plan for ``query``, or None when it can match nothing ('',
    whitespace, characters that all fold away).

    Folded once. Double quotes group words into a phrase. English
    stopwords are dropped unless every word is one (a multi-word query
    of stopwords is searched for as a phrase). At most
    :data:`_MAX_TERMS` distinct items are kept.

    :raises InvalidArgumentError: longer than :data:`MAX_QUERY_LENGTH`.
    """
    folded = fold_for_match(_query_text(query))
    if folded is None:
        return None
    stripped = folded.strip()
    if not stripped:
        return None
    # Folding leaves at most one space at each end. A stripped needle is
    # broader, so it is used when it is still selective; a shorter one
    # keeps its spaces (as the substring search matches it).
    needle = stripped if len(stripped) >= 3 else folded if len(folded) >= 3 else None
    phrases: List[str] = []
    words: List[str] = []
    # Enough raw words to find _MAX_TERMS distinct non-stopwords in any
    # sane query; the planning stops there.
    budget = 8 * _MAX_TERMS
    parts = folded.split('"')
    for i, part in enumerate(parts):
        quoted = i % 2 == 1 and i < len(parts) - 1
        found = _split_words(part, budget)
        if quoted and len(found) > 1:
            phrases.append(" ".join(found))
        else:
            words.extend(found)
        if len(words) + len(phrases) >= budget:
            break
    content = [w for w in words if w not in _STOPWORDS]
    if content or phrases:
        words = content
    elif len(words) > 1:
        phrases, words = [" ".join(words)], []
    items = tuple(dict.fromkeys(phrases + words))[:_MAX_TERMS]
    if not items:
        # Punctuation only: the stripped query, as a substring.
        return _Plan(terms=(), items=(stripped,), substring=True, needle=needle)
    return _Plan(terms=tuple(_quote(item) for item in items), items=items,
                 substring=_NO_SPACE_SCRIPT.search(folded) is not None, needle=needle)


# -- the private database -----------------------------------------------------


class _Dead(Exception):
    """Internal: the index belongs to another process; get a new one."""


def _timeout(what: str, limit: Optional[float]) -> QueryTimeoutError:
    return QueryTimeoutError(f"{what} (limit {limit:g} s).", timeout=limit)


def _wait(lock: threading.Lock, deadline: Optional[float], limit: Optional[float]) -> None:
    """Acquire ``lock`` by ``deadline`` (None: however long it takes).

    :raises QueryTimeoutError: the deadline came first.
    """
    if deadline is None:
        lock.acquire()
    # Clamped as the pool's waits are: a huge (valid) timeout would
    # otherwise overflow acquire().
    elif not lock.acquire(timeout=min(max(0.0, deadline - time.monotonic()), threading.TIMEOUT_MAX)):
        raise _timeout("Timed out waiting for the annotation search index", limit)


def _run(conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], _T],
         deadline: Optional[float], limit: Optional[float]) -> _T:
    """``fn(conn)`` on a private connection, stopped at ``deadline``.

    An interrupted statement raises :class:`QueryTimeoutError`, any other
    ``sqlite3.Error`` ``DBQueryError('Ranked annotation search failed.')``.
    Both are raised outside the ``except`` block, so neither their cause
    nor their context holds SQLite's message (which can quote the query).
    """
    failure = None
    if deadline is not None:
        conn.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_OPCODES)
    try:
        return fn(conn)
    except sqlite3.OperationalError as e:
        failure = "timeout" if deadline is not None and "interrupted" in str(e) else "error"
    except sqlite3.Error:
        failure = "error"
    finally:
        if deadline is not None:
            try:
                conn.set_progress_handler(None, 0)
            except sqlite3.Error:
                pass
    if failure == "timeout":
        raise _timeout("Query took too long and was stopped", limit)
    raise DBQueryError(_FAILED)


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _new_database() -> Tuple[sqlite3.Connection, Optional[str]]:
    """A private in-memory database with an empty ``ann`` table, and the
    FTS5 tokenizer it uses (None: a plain table, no FTS5).

    :raises DBQueryError: SQLite failed (fixed text).
    """
    conn = None
    try:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        # Sorts and temporary results stay in memory too.
        conn.execute("PRAGMA temp_store=MEMORY")
        if fts5_available():
            for tokenizer in _TOKENIZERS:
                try:
                    conn.execute(f"CREATE VIRTUAL TABLE ann USING fts5({', '.join(_COLUMNS)}, "
                                 f"asset UNINDEXED, live UNINDEXED, tokenize='{tokenizer}')")
                    return conn, tokenizer
                except sqlite3.OperationalError:
                    continue
        conn.execute(f"CREATE TABLE ann (rowid INTEGER PRIMARY KEY, {', '.join(_COLUMNS)}, asset, live)")
        return conn, None
    except sqlite3.Error:
        pass
    _close_quietly(conn)
    raise DBQueryError(_FAILED)


@functools.lru_cache(maxsize=1)
def _row_index() -> Dict[str, int]:
    """Positions of the fields the build reads in an Annotation row."""
    fields = list(Annotation._get_mappings("Annotation"))
    return {name: fields.index(name)
            for name in ("id", "asset_id", "selected_text", "note", "representative_text")}


class _Gen:
    """One built index (a private in-memory database), pinned by the
    searches using it; a retired one is closed by its last user."""

    __slots__ = ("conn", "fts", "key", "checked", "ttl_at", "users", "retired", "lock")

    def __init__(self, conn, fts, key, checked, ttl_at):
        self.conn, self.fts, self.key, self.checked, self.ttl_at = conn, fts, key, checked, ttl_at
        self.users = 0
        self.retired = False
        # Searches on one database run one at a time.
        self.lock = threading.Lock()

    def expired(self, now: float) -> bool:
        return self.ttl_at is not None and now >= self.ttl_at


class _Pending:
    """A build in progress: rows up to ``last`` (a Z_PK) are in ``conn``."""

    __slots__ = ("conn", "fts", "key", "checked", "ttl_at", "last")

    def __init__(self, conn, fts, key, checked, ttl_at):
        self.conn, self.fts, self.key, self.checked, self.ttl_at = conn, fts, key, checked, ttl_at
        self.last: Optional[int] = None


# Clock of the freshness checks (tests replace it).
_clock = time.monotonic


class AnnotationIndex:
    """The ranked-search index of one library, held in
    ``LibraryDB._derived_cache`` (private; see its contract).

    Construction is O(1) and does no I/O. The index keeps no reference
    to its library: the ``LibraryDB`` is passed to each call.

    Locks, in this order only: ``_build_lock`` (one builder),
    ``_check_lock`` (one freshness check), a generation's ``lock`` (one
    search per database), ``_state`` (the fields, held only around
    reading and writing them), then the library's own lock and
    connections (pooled reads). No thread holds two of the first three
    at once; waits for them end at the statement deadline.

    Freshness: one thread at a time fingerprints the store (see
    :meth:`_check`), at most once per ``_RECHECK`` seconds; the others
    use its result.

    Building is resumable: rows are read in Z_PK chunks, and a build
    that a caller's deadline stops is continued by the next caller.

    ``discard()`` never blocks: it stops new use, and the last search
    inside closes the databases. In a forked child the index is never
    used (``dead``); the library makes a new one.
    """

    def __init__(self):
        self._pid = os.getpid()
        self._state = threading.Lock()
        self._build_lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._ready: Optional[_Gen] = None
        self._pending: Optional[_Pending] = None
        # The latest fingerprint: (clock before it was taken, key, ttl).
        self._last_check: Optional[Tuple[float, tuple, bool]] = None
        self._dead = False
        self._inside = 0
        #: Completed builds, and rows read from the annotation store (tests).
        self.builds = 0
        self.rows_fetched = 0

    # -- lifecycle ----------------------------------------------------------

    @property
    def dead(self) -> bool:
        return self._dead or os.getpid() != self._pid

    def _detach_all(self) -> List[sqlite3.Connection]:
        """With ``_state`` held and no search inside: forget both
        databases and return their connections, to close."""
        conns = [x.conn for x in (self._ready, self._pending) if x is not None]
        self._ready = self._pending = None
        return conns

    def _retire(self, gen: Optional[_Gen]) -> Optional[sqlite3.Connection]:
        """With ``_state`` held: retire ``gen``; its connection, to close
        now, if nothing uses it."""
        if gen is None:
            return None
        gen.retired = True
        return gen.conn if gen.users == 0 else None

    def _unpin(self, gen: _Gen) -> None:
        with self._state:
            gen.users -= 1
            last = gen.retired and gen.users == 0
        if last:
            _close_quietly(gen.conn)

    def discard(self) -> None:
        """Stop new searches from entering; close the databases now if no
        search is inside, else when the last one leaves. Never blocks."""
        with self._state:
            self._dead = True
            conns = self._detach_all() if self._inside == 0 else []
        for conn in conns:
            _close_quietly(conn)

    def mark_stale(self) -> None:
        """Fingerprint the store again on the next search."""
        with self._state:
            self._last_check = None
            if self._ready is not None:
                self._ready.checked = float("-inf")

    def __del__(self):
        # Unreferenced: no search is inside. sqlite3 connections can wait
        # for the cyclic garbage collector, so close them now, but only in
        # the process that opened them.
        try:
            if os.getpid() == self._pid:
                for x in (self._ready, self._pending):
                    if x is not None:
                        x.conn.close()
        except Exception:
            pass

    # -- freshness ----------------------------------------------------------

    def _fingerprint(self, db: "LibraryDB") -> Tuple[tuple, bool]:
        """``(key, ttl)``: what identifies the annotations the index was
        built from, and whether a TTL applies too (no ``Z_OPT``, or the
        fingerprint failed and the key only names the store)."""
        paths = db.paths()
        identity = _identity(paths.annotations)
        columns = {c.upper() for c in db.schema().get(f"{ANNOTATION_SCHEMA}.ZAEANNOTATION", ())}
        has_opt = "Z_OPT" in columns
        select = ["count(*)", "max(Z_PK)", "total(Z_OPT)" if has_opt else "NULL",
                  "total(ZANNOTATIONMODIFICATIONDATE)" if "ZANNOTATIONMODIFICATIONDATE" in columns
                  else "NULL"]
        sql = (f"SELECT {', '.join(select)} FROM {ANNOTATION_SCHEMA}.ZAEANNOTATION "
               "WHERE ZANNOTATIONTYPE IS NOT 3")
        try:
            row = tuple(db.execute(sql)[0])
        except QueryTimeoutError:
            raise
        except DBQueryError:
            # A column gone under a running process (or the cached schema
            # is out of date): read it again next time, and rely on the
            # TTL meanwhile rather than fail the search (see _matches).
            db.invalidate_schema()
            return (paths, identity, None), True
        return (paths, identity, row), not has_opt

    def _pin_if_fresh(self, now: float) -> Optional[_Gen]:
        """With ``_state`` held: the ready generation, pinned, if it was
        checked less than ``_RECHECK`` seconds before ``now`` and has not
        expired."""
        gen = self._ready
        if gen is not None and now - gen.checked < _RECHECK and not gen.expired(now):
            gen.users += 1
            return gen
        return None

    @staticmethod
    def _matches(gen: _Gen, key: tuple, ttl: bool, checked: float, now: float) -> bool:
        """With ``_state`` held: whether ``gen`` still serves the store
        fingerprinted as ``key`` at ``checked``.

        A failed fingerprint (no row in ``key``) keeps an index of the
        same store file, for at most ``_TTL_WITHOUT_ZOPT`` seconds from
        the first failure, rather than rebuild it at once (and again when
        the fingerprint works again). A matching fingerprint with
        ``Z_OPT`` ends such a TTL.
        """
        if key[2] is None:
            if gen.key[:2] != key[:2]:
                return False
            limit = checked + _TTL_WITHOUT_ZOPT
            if gen.ttl_at is None or gen.ttl_at > limit:
                gen.ttl_at = limit
            return not gen.expired(now)
        if gen.key != key:
            return False
        if not ttl:
            gen.ttl_at = None
        return not gen.expired(now)

    def _check(self, db: "LibraryDB") -> Tuple[Optional[_Gen], tuple, bool, float]:
        """``(gen, key, ttl, checked)``: the store's fingerprint ``key``
        (and whether a TTL applies), taken at ``checked`` (by this thread,
        or by another less than ``_RECHECK`` seconds ago), and the ready
        generation pinned if it still matches (else None).

        One thread at a time: a thread that waited finds the generation
        checked by the one before it, or reuses its fingerprint. The clock
        is read before the statement, under the lock, so a later
        ``checked`` always comes with a fingerprint taken later.
        """
        deadline, limit = db._statement_deadline()
        _wait(self._check_lock, deadline, limit)
        try:
            now = _clock()
            with self._state:
                gen = self._pin_if_fresh(now)
                last = self._last_check
            if gen is not None:
                return gen, gen.key, False, gen.checked
            if last is not None and now - last[0] < _RECHECK:
                checked, key, ttl = last
            else:
                checked = now
                key, ttl = self._fingerprint(db)
                with self._state:
                    self._last_check = (checked, key, ttl)
            with self._state:
                gen = self._ready
                if gen is not None and self._matches(gen, key, ttl, checked, now):
                    if key[2] is not None:
                        gen.checked = max(gen.checked, checked)
                    gen.users += 1
                    return gen, key, ttl, checked
            return None, key, ttl, checked
        finally:
            self._check_lock.release()

    def _pin_fresh(self, db: "LibraryDB") -> _Gen:
        """A generation built from the store as it is now (as of the last
        check, at most ``_RECHECK`` seconds ago), pinned."""
        while True:
            with self._state:
                gen = self._pin_if_fresh(_clock())
            if gen is not None:
                return gen
            gen, key, ttl, checked = self._check(db)
            if gen is not None:
                return gen
            gen = self._build(db, key, ttl, checked)
            with self._state:
                # What this call built (or found built for its key) is
                # used even if already due for a check: progress.
                if gen is self._ready:
                    gen.users += 1
                    return gen

    # -- build ----------------------------------------------------------------

    def _build(self, db: "LibraryDB", key: tuple, ttl: bool, checked: float) -> _Gen:
        """The generation for ``key`` (fingerprinted at ``checked``): built,
        or the build in progress continued. A generation or build from a
        fingerprint taken since is used instead: never an older one in
        place of a newer one."""
        deadline, limit = db._statement_deadline()
        _wait(self._build_lock, deadline, limit)
        try:
            with self._state:
                ready = self._ready
                if (ready is not None and not ready.expired(_clock())
                        and (ready.key == key or ready.checked >= checked)):
                    return ready
                pending = self._pending
            if pending is None or (pending.key != key and pending.checked < checked):
                # A new build: the old generation is retired first (closed
                # now, unless searches still use it; the last one closes
                # it), so the index keeps one database besides those that
                # running searches hold.
                with self._state:
                    stale = [self._retire(self._ready), pending and pending.conn]
                    self._ready = self._pending = None
                for conn in stale:
                    _close_quietly(conn)
                conn, fts = _new_database()
                pending = _Pending(conn, fts, key, checked,
                                   checked + _TTL_WITHOUT_ZOPT if ttl else None)
                with self._state:
                    self._pending = pending
            self._fill(db, pending)
            gen = _Gen(pending.conn, pending.fts, pending.key, pending.checked, pending.ttl_at)
            with self._state:
                self._pending = None
                self._ready = gen
                self.builds += 1
            return gen
        finally:
            self._build_lock.release()

    def _fill(self, db: "LibraryDB", pending: _Pending) -> None:
        """Read the rest of the store into ``pending``, chunk by chunk
        (a chunk read but not inserted is read again next time)."""
        ix = _row_index()
        read = list(ix)
        manager = Annotation.manager
        with use_library(db):
            while True:
                after = {} if pending.last is None else {"id__gt": pending.last}
                rows = manager.filter(only=read, order_by="id", limit=_CHUNK, **after,
                                      **_ALL_ANNOTATIONS).run_query()
                self.rows_fetched += len(rows)
                if not rows:
                    return
                first, last = rows[0][ix["id"]], rows[-1][ix["id"]]
                live = {row[ix["id"]] for row in manager.filter(
                    only=["id"], id__gte=first, id__lte=last, **_LIVE_ANNOTATIONS).run_query()}
                data = []
                for row in rows:
                    texts = [fold_for_match(row[ix[f]]) for f in ("selected_text", "note",
                                                                  "representative_text")]
                    if any(texts):
                        pk = row[ix["id"]]
                        data.append((pk, *(t or "" for t in texts), row[ix["asset_id"]],
                                     1 if pk in live else 0))

                def insert(conn, data=data):
                    with conn:
                        conn.executemany("INSERT INTO ann (rowid, sel, note, rep, asset, live) "
                                         "VALUES (?, ?, ?, ?, ?, ?)", data)

                _run(pending.conn, insert, *db._statement_deadline())
                pending.last = last

    # -- search ---------------------------------------------------------------

    def search(self, db: "LibraryDB", plan: _Plan, *, asset_id=None, include_deleted: bool = False,
               require_all: bool = False) -> List[tuple]:
        """``[(annotation id, score, matched_all, method)]``, best first.

        :raises _Dead: the index belongs to another process.
        """
        if os.getpid() != self._pid:
            raise _Dead
        with self._state:
            self._inside += 1
        try:
            gen = self._pin_fresh(db)
            try:
                deadline, limit = db._statement_deadline()
                _wait(gen.lock, deadline, limit)
                try:
                    return _run(gen.conn, lambda conn: _query(
                        conn, gen.fts, plan, asset_id, include_deleted, require_all), deadline, limit)
                finally:
                    gen.lock.release()
            finally:
                self._unpin(gen)
        finally:
            with self._state:
                self._inside -= 1
                conns = self._detach_all() if self._dead and self._inside == 0 else []
            for conn in conns:
                _close_quietly(conn)


def _query(conn: sqlite3.Connection, fts: Optional[str], plan: _Plan, asset_id,
           include_deleted: bool, require_all: bool) -> List[tuple]:
    """The hits of ``plan`` in the private database, in tier order:

    1. every item (FTS5 AND, best bm25 first; substrings for a
       substring plan);
    2. the whole query as a substring (``plan.needle``);
    3. unless ``require_all``, some of the items (OR);
    4. only when nothing matched, every item of 3 or more characters as
       a substring (an attached article or affix).

    Ties go to the higher annotation id; a row is listed once, in the
    first tier that finds it.
    """
    scope, params = [], []
    if not include_deleted:
        scope.append("live = 1")
    if asset_id is not None:
        scope.append("asset = ?")
        params.append(asset_id)
    extra = "".join(f" AND {s}" for s in scope)
    hits: List[tuple] = []
    seen = set()

    def add(rows, matched_all: bool, method: MatchMethod) -> None:
        for rowid, score in rows:
            if rowid not in seen:
                seen.add(rowid)
                hits.append((rowid, float(score), matched_all, method))

    def substring(items, connector: str, matched_all: bool) -> None:
        conditions, score_terms = [], []
        for _ in items:
            conditions.append("(" + " OR ".join(f"instr({c}, ?) > 0" for c in _COLUMNS) + ")")
            score_terms.append(" + ".join(f"(instr({c}, ?) > 0) * {w}" for c, w in zip(_COLUMNS, _WEIGHTS)))
        per_item = [item for item in items for _ in _COLUMNS]
        sql = (f"SELECT rowid, {' + '.join(score_terms)} AS s FROM ann "
               f"WHERE ({f' {connector} '.join(conditions)}){extra} ORDER BY s DESC, rowid DESC")
        add(conn.execute(sql, per_item + per_item + params).fetchall(), matched_all, MatchMethod.SUBSTRING)

    use_fts = fts is not None and not plan.substring
    weights = ", ".join(str(w) for w in _WEIGHTS)
    fts_sql = (f"SELECT rowid, -bm25(ann, {weights}) FROM ann WHERE ann MATCH ?{extra} "
               f"ORDER BY bm25(ann, {weights}), rowid DESC")
    if use_fts:
        add(conn.execute(fts_sql, [" AND ".join(plan.terms)] + params).fetchall(), True, MatchMethod.FTS)
    else:
        substring(plan.items, "AND", True)
    if plan.needle is not None:
        substring((plan.needle,), "AND", True)
    if not require_all and len(plan.items) > 1:
        if use_fts:
            add(conn.execute(fts_sql, [" OR ".join(plan.terms)] + params).fetchall(), False, MatchMethod.FTS)
        else:
            substring(plan.items, "OR", False)
    if not hits and use_fts:
        long_items = tuple(item for item in plan.items if len(item) >= 3)
        if long_items:
            substring(long_items, "AND", True)
    return hits


# -- the facade's call --------------------------------------------------------


def _index_hits(db: "LibraryDB", plan: _Plan, **kwargs) -> Tuple[AnnotationIndex, List[tuple]]:
    for _ in range(3):
        index = db._derived_cache(_INDEX_KEY, AnnotationIndex)
        try:
            return index, index.search(db, plan, **kwargs)
        except _Dead:
            continue
    raise DBQueryError(_FAILED)


def _search_annotations(db: "LibraryDB", plan: _Plan, *, limit: Optional[int], offset: int,
                        asset_id, require_all: bool, include_deleted: bool) -> List[AnnotationHit]:
    """``search_annotations`` once its arguments are checked: the hits of
    ``plan`` in ``db``, ``[offset:offset + limit]`` of the ranked list.

    Each page's annotations are read again in the requested scope; one
    gone since the index was checked (deleted, say) is skipped, the page
    is filled from the next hits, and the index is fingerprinted again on
    the next search.
    """
    if not db.has_annotations():
        raise AnnotationStoreNotFoundError(ANNOTATIONS_NOT_FOUND)
    index, raw = _index_hits(db, plan, asset_id=asset_id, include_deleted=include_deleted,
                             require_all=require_all)
    scope = dict(_ALL_ANNOTATIONS if include_deleted else _LIVE_ANNOTATIONS)
    if asset_id is not None:
        scope["asset_id"] = asset_id
    out: List[AnnotationHit] = []
    dropped = False
    i = offset
    with use_library(db):
        while i < len(raw) and (limit is None or len(out) < limit):
            want = len(raw) - i if limit is None else limit - len(out)
            batch = raw[i:i + want]
            i += len(batch)
            for j in range(0, len(batch), _PAGE_CHUNK):
                part = batch[j:j + _PAGE_CHUNK]
                found = {a.id: a for a in Annotation.manager.filter(id__in=[h[0] for h in part], **scope)}
                for pk, score, matched_all, method in part:
                    annotation = found.get(pk)
                    if annotation is None:
                        dropped = True
                    else:
                        out.append(AnnotationHit(annotation, score, matched_all, method))
    if dropped:
        index.mark_stale()
    return out
