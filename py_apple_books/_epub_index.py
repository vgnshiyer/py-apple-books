"""The EPUB book index (private, 1.11).

What the content APIs know about a book before they read any of its
chapters: the chapter list (with spine indexes), the spine, the manifest
and the table-of-contents pages, from ``META-INF/container.xml``, the
package document (OPF) and the navigation files (EPUB 3 nav document,
NCX) alone.

* :class:`_IndexReader` loads a book with ebooklib, as 1.10 did, but
  reads only those files: every other manifest item gets a
  :class:`_Deferred` stand-in that remembers the entry name ebooklib
  asked for, so the file is read (through
  ``content._read_entry_bytes``) when, and only when, its text is
  wanted. The chapter list is computed by 1.10's own code on this book.
* :class:`_IndexCache` keeps indexes, per-file anchor tables and
  ``encryption.xml`` verdicts process-wide: a weighted LRU (32 MiB
  estimated, 4,096 entries), single-flight builds (threads that ask
  while one builds share its value, stored or not), emptied by
  ``content.clear_content_cache()``, reset for builds in progress in a
  forked child. Keys come from file metadata only:
  the bundle's device, inode and modification time, and the inode,
  modification time and size of every file an index was read from (an
  anchor table: of its file). Nothing is written to disk or logged.
* :func:`_gate_path` and :func:`_book_gate` are the checks every new
  content API runs before reading a book: the database state (books
  Apple Books keeps only in iCloud, unowned Store items), iCloud
  placeholders on the way to every file read, DRM. No ``du``, no walk of
  the bundle.

Lock order: the cache's one lock is held only around dict operations;
builds run outside it, and no other lock is taken while it is held.
Stored values are never changed after they are stored.
"""

from __future__ import annotations

import os
import pathlib
import posixpath
import re
import stat as _stat
import threading
import urllib.parse
import zipfile
from array import array
from collections import OrderedDict
from collections.abc import Mapping as _MappingABC
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, NamedTuple, Optional, Set, Tuple

from bs4 import BeautifulSoup, Tag
from ebooklib import epub
from ebooklib.utils import parse_html_string

from py_apple_books import _icloud
from py_apple_books import content as _content
from py_apple_books._messages import detail, quote_name, quote_title
from py_apple_books.content import Chapter, SpineItem
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    DRMProtectedError,
    NotEpubError,
)
from py_apple_books.positions import UnavailableReason

_OPF = "{http://www.idpf.org/2007/opf}"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: Cache bounds (R4): estimated bytes, and entries of every kind.
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_CACHE_ENTRIES = 4096
#: Largest content document an anchor table is built for.
MAX_ANCHOR_BYTES = 16 * 1024 * 1024

_MISSING_MESSAGE = (
    "This book's files are not on this Mac. Open it in Apple Books to "
    "download it, then try again."
)
_FAIRPLAY_MESSAGE = (
    "This book is a DRM-protected Apple Books Store purchase (FairPlay). "
    "Its text content cannot be read directly; only imported EPUBs and "
    "PDFs are readable."
)
_ENCRYPTED_MESSAGE = (
    "This book is an encrypted EPUB (DRM). Its text content cannot be "
    "read directly; only DRM-free EPUBs are readable."
)


# ---------------------------------------------------------------------------
# The index-only reader
# ---------------------------------------------------------------------------


class _Deferred(bytes):
    """Empty content standing in for a manifest item that was not read.
    :attr:`entry` is the entry name ebooklib asked for: reading it gives
    exactly the bytes a full load would have stored."""

    def __new__(cls, entry: str) -> "_Deferred":
        obj = super().__new__(cls, b"")
        obj.entry = entry
        return obj

    def __reduce__(self):
        return (_Deferred, (self.entry,))

    def __repr__(self) -> str:
        return "<unread EPUB entry>"


def _local(tag: Any) -> str:
    """An element's tag without its ``{namespace}``; '' for a comment or
    processing instruction (lxml gives those a non-str tag)."""
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _wanted_names(opf: Any, opf_dir: str) -> FrozenSet[str]:
    """Entry names (normalized) of the manifest items the index reads:
    NCX files (by media type), navigation documents (``nav`` in
    ``properties``) and the item the spine's ``toc`` names. Both the
    href as written and percent-decoded, as ebooklib asks for either."""
    spine = opf.find(f"{_OPF}spine")
    toc_id = spine.get("toc", "") if spine is not None else ""
    manifest = opf.find(f"{_OPF}manifest")
    names: Set[str] = set()
    for item in manifest if manifest is not None else ():
        if item.tag != f"{_OPF}item":
            continue
        properties = (item.get("properties") or "").split()
        if (item.get("media-type") == _NCX_MEDIA_TYPE or "nav" in properties
                or (toc_id and item.get("id") == toc_id)):
            href = item.get("href")
            if href:
                for name in {href, urllib.parse.unquote(href)}:
                    names.add(posixpath.normpath(posixpath.join(opf_dir, name)))
    return frozenset(names)


class _IndexReader(_content._ContainedEpubReader):
    """:class:`~py_apple_books.content._ContainedEpubReader` that reads
    only ``container.xml``, the package document and the navigation files
    (see :func:`_wanted_names`), each through
    ``content._read_entry`` (the guarded per-file read); every other
    manifest item gets :class:`_Deferred` content and isn't touched at
    all (not even stat'ed).

    Records the ``fstat`` identity of each file read (:attr:`keyed`) and
    whether every file was unchanged across its read (:attr:`stable`).
    """

    def __init__(self, epub_file_name):
        super().__init__(epub_file_name)
        self.bytes_read = 0
        self.keyed: Dict[str, Tuple[int, int, int, int]] = {}
        self.stable = True
        self._wanted: Optional[FrozenSet[str]] = None

    def read_file(self, name):
        if isinstance(self.zf, zipfile.ZipFile):
            return super().read_file(name)
        norm = posixpath.normpath(name)
        # ebooklib keeps the parsed package document as `container`, set
        # once it has read it: from then on only the wanted files are read.
        opf = getattr(self, "container", None)
        if opf is not None and norm != posixpath.normpath(self.opf_file or ""):
            if self._wanted is None:
                self._wanted = _wanted_names(opf, self.opf_dir)
            if norm not in self._wanted:
                return _Deferred(name)
        try:
            data, st, stable = _content._read_entry(
                pathlib.Path(self.file_name), name, _content._MAX_ENTRY_BYTES)
        except OSError as e:
            err = _icloud.not_downloaded_error(e)
            if err is not None:
                raise err from None
            # Name the entry, not the absolute path (as the full load).
            raise AppleBooksError(
                f"Could not read EPUB entry {quote_name(name)}: {e.strerror}"
            ) from e
        self.bytes_read += len(data)
        ident = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if not stable or self.keyed.setdefault(norm, ident) != ident:
            self.stable = False
        if self.container_bytes is None and norm == "META-INF/container.xml":
            self.container_bytes = data
        return data


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _BookIndex:
    """What the index knows about one book (immutable once built).

    :attr key: Its cache key (see :func:`_current_key`).
    :attr chapters: 1.10's chapter list, with :attr:`Chapter.spine_index`.
    :attr spine: The spine, one :class:`SpineItem` per ``<spine>`` element.
    :attr book: ebooklib's book from the index-only load, keeping what
        text extraction needs: its text documents (those not read have
        :class:`_Deferred` content), ebooklib's own spine view, language
        and templates. Binary items, the parsed ToC and page lists are
        dropped (see :attr:`manifest` and :attr:`chapters`).
    :attr opf_dir: The package document's folder in the bundle (as
        ``BookContent`` derives it).
    :attr manifest: manifest id -> ``(href, media type, entry name)``:
        ``href`` relative to the bundle (like :attr:`Chapter.href`),
        ``entry`` the name a read of the item uses (relative to the
        bundle), for the first ``<item>`` with that id.
    :attr first_spine_index: normalized bundle-relative href -> the first
        spine index of that file.
    :attr toc_orders: normalized bundle-relative href -> the ascending
        :attr:`Chapter.order` of the ToC entries in that file.
    :attr toc_page_ids: manifest ids of table-of-contents pages.
    :attr keyed_files: the files the index was read from (bundle-relative,
        normalized), in key order.
    :attr bytes_read: bytes read to build it.
    :attr weight: its estimated size in memory, in bytes.
    :attr stable: every file read was unchanged across its read.
    """

    key: tuple
    chapters: Tuple[Chapter, ...]
    spine: Tuple[SpineItem, ...]
    book: Any
    opf_dir: pathlib.PurePosixPath
    manifest: Mapping[str, Tuple[Optional[str], Optional[str], Optional[str]]]
    first_spine_index: Mapping[str, int]
    toc_orders: Mapping[str, Tuple[int, ...]]
    toc_page_ids: FrozenSet[str]
    keyed_files: Tuple[str, ...]
    bytes_read: int
    weight: int
    stable: bool


def _target(href: Optional[str], base: str) -> Optional[str]:
    """``href`` (relative to the folder ``base`` of the bundle) as a
    normalized bundle-relative path, without its fragment; None for an
    empty, absolute, scheme or out-of-bundle reference."""
    if not isinstance(href, str):
        return None
    path = href.split("#", 1)[0].strip()
    if not path or path.startswith("/") or _SCHEME.match(path):
        return None
    norm = posixpath.normpath(posixpath.join(base, urllib.parse.unquote(path)))
    if norm in (".", "..") or norm.startswith(("../", "/")):
        return None
    return norm


def _type_tokens(element: Any) -> Set[str]:
    """The tokens of an element's ``epub:type`` (any attribute named
    ``type``, with or without a prefix)."""
    tokens: Set[str] = set()
    for name, value in element.attrib.items():
        if name.rpartition("}")[2].rpartition(":")[2] == "type" and isinstance(value, str):
            tokens.update(value.split())
    return tokens


def _landmark_toc_targets(content: bytes, nav_dir: str) -> Set[str]:
    """Targets of the ``epub:type="toc"`` links in a navigation document's
    landmarks nav."""
    try:
        tree = parse_html_string(content)
    except Exception:  # noqa: BLE001 (an unparsable nav names no ToC page)
        return set()
    found: Set[str] = set()
    for nav in tree.iter("nav"):
        if "landmarks" not in _type_tokens(nav):
            continue
        for link in nav.iter("a"):
            if "toc" in _type_tokens(link):
                target = _target(link.get("href"), nav_dir)
                if target is not None:
                    found.add(target)
    return found


def _toc_page_targets(opf: Any, book: Any, opf_dir: str, rel: Callable[[str], str]) -> Set[str]:
    """Normalized bundle-relative paths of the files the package's guide
    (``type="toc"``) or a navigation document's landmarks
    (``epub:type="toc"``) name as the table of contents."""
    found: Set[str] = set()
    guide = opf.find(f"{_OPF}guide")
    for ref in guide if guide is not None else ():
        if _local(ref.tag) == "reference" and (ref.get("type") or "").strip().lower() == "toc":
            target = _target(ref.get("href"), opf_dir)
            if target is not None:
                found.add(target)
    for item in book.get_items():
        content = getattr(item, "content", None)
        if isinstance(item, epub.EpubNav) and content and not isinstance(content, _Deferred):
            nav_dir = posixpath.dirname(rel(item.file_name))
            found |= _landmark_toc_targets(content, nav_dir)
    return found


def _manifest(opf: Any, ebooklib_dir: str,
              rel: Callable[[str], str]) -> Dict[str, Tuple[Optional[str], Optional[str], Optional[str], Tuple[str, ...]]]:
    """manifest id -> ``(href, media type, entry, properties)`` for the
    first ``<item>`` with each id: ``href`` relative to the bundle (via
    ``rel``), ``entry`` the bundle path ebooklib reads it from (relative to
    its own package folder, ``ebooklib_dir``)."""
    found: Dict[str, Tuple[Optional[str], Optional[str], Optional[str], Tuple[str, ...]]] = {}
    manifest = opf.find(f"{_OPF}manifest")
    for item in manifest if manifest is not None else ():
        item_id = item.get("id") if item.tag == f"{_OPF}item" else None
        if item_id is None or item_id in found:
            continue
        raw = item.get("href")
        name = urllib.parse.unquote(raw) if raw else ""
        href = rel(name) if name else None
        entry = posixpath.normpath(posixpath.join(ebooklib_dir, name)) if name else None
        properties = tuple((item.get("properties") or "").split())
        found[item_id] = (href, item.get("media-type"), entry, properties)
    return found


def _spine(opf: Any, manifest: Mapping, toc_targets: Set[str],
           toc_orders: Mapping[str, Tuple[int, ...]]) -> Tuple[SpineItem, ...]:
    """One :class:`SpineItem` per element child of ``<spine>``."""
    spine = opf.find(f"{_OPF}spine")
    items: List[SpineItem] = []
    for element in spine if spine is not None else ():
        if not isinstance(element.tag, str):
            continue  # a comment or processing instruction
        index = len(items)
        linear = (element.get("linear") or "yes").strip().lower() != "no"
        idref = element.get("idref") if _local(element.tag) == "itemref" else None
        if not idref:
            items.append(SpineItem(index, None, None, None, linear, False, False, ()))
            continue
        found = manifest.get(idref)
        if found is None:
            items.append(SpineItem(index, idref, None, None, linear, False, False, ()))
            continue
        href, media_type, entry, properties = found
        norm = posixpath.normpath(href) if href else None
        inside = bool(href) and not href.startswith("/") and not _content._escapes_bundle(href) \
            and entry is not None and not _content._escapes_bundle(entry)
        items.append(SpineItem(
            index=index,
            item_id=idref,
            href=href,
            media_type=media_type,
            linear=linear,
            is_toc_page="nav" in properties or (norm is not None and norm in toc_targets),
            readable=inside and _content._is_text_media_type(media_type),
            toc_orders=toc_orders.get(norm, ()) if norm else (),
        ))
    return tuple(items)


def _first_spine_index(spine: Iterable[SpineItem]) -> Dict[str, int]:
    """normalized bundle-relative href -> the first spine index of that
    file."""
    first: Dict[str, int] = {}
    for item in spine:
        if item.href:
            first.setdefault(posixpath.normpath(item.href), item.index)
    return first


def _with_spine_indexes(chapters: Iterable[Chapter], first: Mapping[str, int]) -> Tuple[Chapter, ...]:
    """``chapters`` with :attr:`Chapter.spine_index` set from ``first``
    (see :func:`_first_spine_index`)."""
    return tuple(
        replace(c, spine_index=first.get(posixpath.normpath(c.href))) if c.href else c
        for c in chapters
    )


def _package_spine_indexes(opf: Any, ebooklib_dir: str, rel: Callable[[str], str]) -> Dict[str, int]:
    """:func:`_first_spine_index` of a package document ebooklib parsed
    (``EpubReader.container``, its folder ``EpubReader.opf_dir``): the
    rule the index uses, for chapters read without it (see
    ``BookContent._chapter_list``)."""
    return _first_spine_index(_spine(opf, _manifest(opf, ebooklib_dir, rel), set(), {}))


def _build_index(root: pathlib.Path, root_st: os.stat_result) -> _BookIndex:
    """Read the book's index (container, package and navigation files
    only) and build a :class:`_BookIndex`. Errors as a full load raises
    them: :class:`BookNotDownloadedError` for an iCloud placeholder,
    :class:`UnsafeEpubEntryError` for an unsafe entry,
    :class:`AppleBooksError` otherwise."""
    reader = _IndexReader(str(root))
    try:
        with _icloud.no_materialize():
            book = reader.load()
            reader.process()
    except AppleBooksError:
        raise
    except Exception as e:
        err = _icloud.not_downloaded_error(e)
        if err is not None:
            raise err from None
        raise AppleBooksError(
            f"Could not read EPUB {quote_title(root.name)}: {detail(e)}"
        ) from e
    opf = reader.container
    container = reader.container_bytes
    opf_dir = (_content._opf_dir_from_container_bytes(container) if container is not None
               else pathlib.PurePosixPath())

    def rel(name: str) -> str:
        return _content._to_bundle_relative(opf_dir, name)

    chapters = _content._book_chapters(book, rel)
    orders: Dict[str, List[int]] = {}
    for chapter in chapters:
        if chapter.href:
            orders.setdefault(posixpath.normpath(chapter.href), []).append(chapter.order)
    toc_orders = {href: tuple(sorted(found)) for href, found in orders.items()}
    opf_dir_str = "" if str(opf_dir) in ("", ".") else str(opf_dir)
    manifest = _manifest(opf, reader.opf_dir or "", rel)
    toc_targets = _toc_page_targets(opf, book, opf_dir_str, rel)
    spine = _spine(opf, manifest, toc_targets, toc_orders)
    first = _first_spine_index(spine)
    chapters = _with_spine_indexes(chapters, first)
    toc_page_ids = frozenset(
        [i for i, (_h, _m, _e, props) in manifest.items() if "nav" in props]
        + [item.item_id for item in spine if item.is_toc_page and item.item_id])
    keyed_files = tuple(sorted(reader.keyed))
    key = _key(root_st, ((name, *reader.keyed[name]) for name in keyed_files))
    # Keep only what text extraction needs: the text documents (those not
    # read stay deferred), ebooklib's spine view, the book's language and
    # templates. Binary items (their text is refused anyway), the NCX and
    # ebooklib's parsed ToC and page lists (the chapter list is computed)
    # are dropped.
    # An id is looked up as ebooklib does (its first item), so an id whose
    # first item is binary keeps no item at all (refused, as it was).
    first_is_text: Dict[Any, bool] = {}
    for item in book.items:
        first_is_text.setdefault(item.id, _content._is_text_media_type(getattr(item, "media_type", None)))
    book.items = [item for item in book.items
                  if first_is_text[item.id]
                  and _content._is_text_media_type(getattr(item, "media_type", None))]
    book.toc = []
    book.pages = []
    for item in book.items:
        if getattr(item, "pages", None):
            item.pages = []
    kept_bytes = sum(len(item.content) for item in book.items
                     if isinstance(item.content, bytes) and not isinstance(item.content, _Deferred))
    # Calibrated with tracemalloc on synthetic books: about 1 KB
    # per kept item and 0.5 KB per ToC entry, spine entry and manifest map
    # entry, plus the navigation documents kept as read.
    weight = (8192 + 2 * kept_bytes + 1024 * len(book.items)
              + 512 * (len(chapters) + len(spine) + len(manifest))
              + sum(len(c.id) + len(c.title) + len(c.href) + len(c.fragment) for c in chapters))
    return _BookIndex(
        key=key,
        chapters=chapters,
        spine=spine,
        book=book,
        opf_dir=opf_dir,
        manifest=MappingProxyType({i: v[:3] for i, v in manifest.items()}),
        first_spine_index=MappingProxyType(first),
        toc_orders=MappingProxyType(toc_orders),
        toc_page_ids=toc_page_ids,
        keyed_files=keyed_files,
        bytes_read=reader.bytes_read,
        weight=weight,
        stable=reader.stable,
    )


def _key(root_st: os.stat_result, files: Iterable[tuple]) -> tuple:
    """An index's cache key: the bundle's device, inode and modification
    time, then ``(name, device, inode, mtime_ns, size)`` of each file it
    was read from."""
    return (root_st.st_dev, root_st.st_ino, root_st.st_mtime_ns, tuple(files))


def _current_key(root: pathlib.Path, root_st: os.stat_result,
                 keyed_files: Iterable[str]) -> Optional[tuple]:
    """The key an index of the bundle read now would have, from file
    metadata alone (every folder on the way checked not to be an iCloud
    placeholder first); None if a file is gone or no longer a regular
    file.

    :raises BookNotDownloadedError: a folder or file is an iCloud
        placeholder.
    """
    checked: Set[str] = set()
    files = []
    for name in keyed_files:
        _content._check_dirs_local(root, posixpath.dirname(name), checked)
        try:
            st = _icloud.stat(root / name)
        except (OSError, ValueError) as e:
            if _icloud.is_materialize_error(e):
                raise _content._not_downloaded() from None
            return None
        if _icloud.is_dataless(st):
            raise _content._not_downloaded()
        if not _stat.S_ISREG(st.st_mode):
            return None
        files.append((name, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size))
    return _key(root_st, files)


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


class _Entry:
    """A cached value with its key and weight. ``verified`` (book indexes
    only): the index's chapter list was found equal to 1.10's full-load
    list for this key."""

    __slots__ = ("key", "value", "weight", "verified")

    def __init__(self, key: Any, value: Any, weight: int, verified: bool = False) -> None:
        self.key = key
        self.value = value
        self.weight = weight
        self.verified = verified


class _InFlight:
    """One build in progress (see :meth:`_IndexCache.begin`): other
    threads wait for :attr:`done`; then :attr:`ok` tells whether the
    builder got a value (:attr:`value`, stored in the cache or not) or
    failed."""

    __slots__ = ("done", "ok", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.ok = False
        self.value: Any = None


class _IndexCache:
    """A weighted LRU with single-flight builds (see the module docstring).

    Identities (dict keys) are tuples whose first item names the kind
    (``'book'``, ``'anchor'``, ``'encryption'``). An entry heavier than
    :attr:`max_weight` is never stored. :meth:`clear` bumps a generation,
    so a build that started before it doesn't store its result.

    A forked child starts with a new lock and no builds in progress (the
    threads running them were not copied), and stores nothing a build
    started in its parent returns (see :func:`_reset_after_fork`).
    """

    def __init__(self, max_weight: int = MAX_CACHE_BYTES, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_weight = max_weight
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self._weight = 0
        self._generation = 0
        self._inflight: Dict[tuple, _InFlight] = {}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def peek(self, ident: tuple) -> Optional[_Entry]:
        """The entry for ``ident``, without making it recently used."""
        with self._lock:
            return self._entries.get(ident)

    def touch(self, ident: tuple, entry: _Entry) -> None:
        """Make ``entry`` (if still stored) the most recently used."""
        with self._lock:
            if self._entries.get(ident) is entry:
                self._entries.move_to_end(ident)

    def mark_verified(self, ident: tuple, value: Any) -> None:
        with self._lock:
            entry = self._entries.get(ident)
            if entry is not None and entry.value is value:
                entry.verified = True

    def insert(self, ident: tuple, key: Any, value: Any, weight: int, generation: int,
               verified: bool = False) -> bool:
        """Store ``value`` (replacing any entry for ``ident``) unless it is
        heavier than :attr:`max_weight` or the cache was cleared since
        ``generation``; then evict least recently used entries down to
        the bounds. True if it is stored."""
        if weight > self.max_weight:
            return False
        with self._lock:
            if generation != self._generation:
                return False
            old = self._entries.pop(ident, None)
            if old is not None:
                self._weight -= old.weight
            self._entries[ident] = _Entry(key, value, weight, verified)
            self._weight += weight
            while self._entries and (self._weight > self.max_weight
                                     or len(self._entries) > self.max_entries):
                _, evicted = self._entries.popitem(last=False)
                self._weight -= evicted.weight
            return ident in self._entries

    def begin(self, ident: tuple) -> Tuple[bool, _InFlight, int]:
        """``(builder, flight, generation)``: the first caller for an
        ``ident`` builds it (and must call :meth:`end`, after setting the
        flight's outcome); later callers wait on ``flight.done``
        meanwhile."""
        with self._lock:
            flight = self._inflight.get(ident)
            if flight is not None:
                return False, flight, self._generation
            flight = self._inflight[ident] = _InFlight()
            return True, flight, self._generation

    def end(self, ident: tuple, flight: _InFlight) -> None:
        with self._lock:
            if self._inflight.get(ident) is flight:
                del self._inflight[ident]
        flight.done.set()

    def clear(self) -> None:
        """Drop every entry. Builds in progress don't store their results,
        and a later caller starts its own build instead of waiting for
        one that began before the clear."""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._weight = 0
            self._inflight = {}

    def _after_fork_in_child(self) -> None:
        """In a forked child (only the forking thread runs there): a new
        lock (the old one may have been held by a thread that wasn't
        copied), no builds in progress (their builders don't exist here,
        so nobody would wake their waiters), and a new generation (a
        build started in the parent stores nothing). Entries are kept,
        their values are immutable; unless the lock was held at the
        fork, when the entries may be half updated."""
        held = self._lock.locked()
        self._lock = threading.Lock()
        self._inflight = {}
        self._generation += 1
        if held:
            self._entries = OrderedDict()
            self._weight = 0

    def stats(self) -> Dict[str, int]:
        """Entry counts by kind, and the total estimated weight."""
        with self._lock:
            counts: Dict[str, int] = {"weight": self._weight}
            for ident in self._entries:
                counts[ident[0]] = counts.get(ident[0], 0) + 1
            return counts


_CACHE = _IndexCache()


def _clear_cache() -> None:
    """Empty the cache (registered with ``content.clear_content_cache``)."""
    _CACHE.clear()


_icloud.register_file_cache(_clear_cache)


def _reset_after_fork() -> None:
    """``os.register_at_fork`` hook: see
    :meth:`_IndexCache._after_fork_in_child`."""
    _CACHE._after_fork_in_child()


if hasattr(os, "register_at_fork") and not globals().get("_fork_hook_registered"):
    os.register_at_fork(after_in_child=_reset_after_fork)
    _fork_hook_registered = True


def _get_or_build(ident: tuple, lookup: Callable[[], Any],
                  build: Callable[[], Tuple[Any, Any, int, bool]],
                  still_valid: Optional[Callable[[Any], bool]] = None) -> Any:
    """``lookup()`` if it finds a value; else ``build()``'s value, built
    once however many threads ask at the same time: the first builds (and
    stores it if it can), the others wait and take its value, whether it
    was stored or not (if ``still_valid(value)``, when given, says it
    still is); a waiter builds itself only if the first failed (or its
    value is no longer valid). ``build()`` returns ``(value, key,
    weight, storable)``."""
    cache = _CACHE
    found = lookup()
    if found is not None:
        return found
    builder, flight, generation = cache.begin(ident)
    if not builder:
        flight.done.wait()
        if flight.ok and (still_valid is None or still_valid(flight.value)):
            return flight.value
        found = lookup()
        if found is not None:
            return found
        generation = cache.generation
        value, key, weight, storable = build()
        if storable:
            cache.insert(ident, key, value, weight, generation)
        return value
    try:
        found = lookup()  # built and stored while this thread got here
        if found is None:
            value, key, weight, storable = build()
            if storable:
                cache.insert(ident, key, value, weight, generation)
            found = value
        flight.value, flight.ok = found, True
        return found
    finally:
        cache.end(ident, flight)


# ---------------------------------------------------------------------------
# Book lookups
# ---------------------------------------------------------------------------


def _book_ident(root_st: os.stat_result) -> tuple:
    return ("book", root_st.st_dev, root_st.st_ino)


def _lookup(root: pathlib.Path, root_st: os.stat_result) -> Optional[_Entry]:
    """The cached index entry of the bundle, if its files are unchanged.

    :raises BookNotDownloadedError: a file it was read from (or a folder
        on the way) is now an iCloud placeholder.
    """
    ident = _book_ident(root_st)
    entry = _CACHE.peek(ident)
    if entry is None:
        return None
    if _current_key(root, root_st, entry.value.keyed_files) != entry.key:
        return None
    _CACHE.touch(ident, entry)
    return entry


def _index_for(root: pathlib.Path, root_st: os.stat_result) -> _BookIndex:
    """The bundle's index: cached, or built (once, whatever the number of
    threads) and stored. Raises what :func:`_build_index` raises."""

    def lookup() -> Optional[_BookIndex]:
        entry = _lookup(root, root_st)
        return None if entry is None else entry.value

    def build() -> Tuple[_BookIndex, tuple, int, bool]:
        index = _build_index(root, root_st)
        return index, index.key, index.weight, index.stable

    def still_valid(index: _BookIndex) -> bool:
        # Another thread's build, which may not be stored (too heavy, or
        # its files changed while it read them): its files must still be
        # the ones it read.
        return _current_key(root, root_st, index.keyed_files) == index.key

    return _get_or_build(_book_ident(root_st), lookup, build, still_valid)


def _held_index(root: pathlib.Path, root_st: os.stat_result,
                held: Optional[_BookIndex]) -> Optional[_BookIndex]:
    """``held`` (an index a ``BookContent`` kept from its last gate) if it
    was read from this bundle as it is now (same key); else None.

    :raises BookNotDownloadedError: as :func:`_current_key`.
    """
    if held is None or not held.stable or held.key[:3] != (root_st.st_dev, root_st.st_ino,
                                                           root_st.st_mtime_ns):
        return None
    return held if _current_key(root, root_st, held.keyed_files) == held.key else None


def _local_root(root: pathlib.Path) -> Optional[os.stat_result]:
    """The ``stat`` of a bundle folder that is on this Mac (not an iCloud
    placeholder, no iCloud stub), following a symlink; else None."""
    if _icloud.icloud_stub(root):
        return None
    st = _icloud.lstat(root)
    if _stat.S_ISLNK(st.st_mode):
        st = _icloud.stat(root)
    if _icloud.is_dataless(st) or not _stat.S_ISDIR(st.st_mode):
        return None
    return st


def _verified_index(path: Any) -> Optional[_BookIndex]:
    """For ``list_chapters``: the cached index of the bundle at ``path``,
    if its files are unchanged and its chapter list was found equal to
    1.10's full-load list. Never raises (any problem: None, and the
    caller reads the book 1.10's way)."""
    root = pathlib.Path(path)
    try:
        with _icloud.no_materialize():
            st = _local_root(root)
            entry = None if st is None else _lookup(root, st)
    except Exception:  # noqa: BLE001 (the full load reports the problem)
        return None
    if entry is None or not entry.verified:
        return None
    return entry.value


def _index_matching(path: Any, chapters: Tuple[Chapter, ...]) -> Optional[_BookIndex]:
    """For ``list_chapters``, after a full load gave ``chapters``: the
    bundle's index (cached, or built and stored) if its chapter list is
    the same, marked so that later calls can use it; else None. Never
    raises."""
    root = pathlib.Path(path)
    try:
        with _icloud.no_materialize():
            st = _local_root(root)
            if st is None:
                return None
            index = _index_for(root, st)
    except Exception:  # noqa: BLE001 (the caller has the full load's list)
        return None
    if index.chapters != tuple(chapters):
        return None
    _CACHE.mark_verified(_book_ident(st), index)
    return index


def _cached_index(key: Any) -> Optional[_BookIndex]:
    """The stored index with this key (as a gate returned it), without
    any I/O; None if it isn't stored (any more)."""
    if not isinstance(key, tuple) or len(key) != 4:
        return None
    entry = _CACHE.peek(("book", key[0], key[1]))
    return entry.value if entry is not None and entry.key == key else None


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class _Gated(NamedTuple):
    """What a gate found: ``reason`` None and the book's ``index`` (and its
    cache ``key``), or the ``reason`` it can't be read and, from
    :func:`_gate_path`, the ``error`` a ``BookContent`` method raises."""

    reason: Optional[UnavailableReason]
    key: Optional[tuple] = None
    index: Optional[_BookIndex] = None
    error: Optional[BaseException] = None


def _not_epub(root: pathlib.Path) -> _Gated:
    if root.suffix.lower() == ".pdf":
        message = "This book is a PDF; chapter listing/reading is only supported for EPUB books."
    else:
        message = (f"{quote_title(root.name)} is not an EPUB bundle directory; chapter "
                   f"listing/reading is only supported for EPUB books.")
    return _Gated(UnavailableReason.NOT_EPUB, error=NotEpubError(message))


def _not_downloaded(message: str = _icloud.PARTIAL_DOWNLOAD_MESSAGE) -> _Gated:
    return _Gated(UnavailableReason.NOT_DOWNLOADED, error=BookNotDownloadedError(message))


def _present(path: pathlib.Path) -> bool:
    """Whether a DRM marker file exists (following a symlink, as 1.10's
    ``exists()``); an unexpected error counts as present (fail closed).

    :raises BookNotDownloadedError: looking would download something.
    """
    try:
        st = _icloud.lstat(path)
        if _stat.S_ISLNK(st.st_mode):
            _icloud.stat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError) as e:
        if _icloud.is_materialize_error(e):
            raise _content._not_downloaded() from None
        return True


def _drm_evidence(root: pathlib.Path) -> Optional[str]:
    """The ``META-INF`` file that marks the bundle as DRM-protected, or
    None: 1.10's rules (``BookContent.is_drm_protected``), checked on every
    call. ``encryption.xml`` is parsed once per version of the file (its
    verdict is cached under its identity, size and modification time);
    an unreadable one counts as encrypted and isn't cached.

    :raises BookNotDownloadedError: a marker file is an iCloud placeholder.
    """
    meta = root / "META-INF"
    for name in ("sinf.xml", "rights.xml"):
        if _present(meta / name):
            return name
    enc = meta / "encryption.xml"
    try:
        st = _icloud.lstat(enc)
        if _stat.S_ISLNK(st.st_mode):
            st = _icloud.stat(enc)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError) as e:
        if _icloud.is_materialize_error(e):
            raise _content._not_downloaded() from None
        return "encryption.xml"
    if _icloud.is_dataless(st):
        raise _content._not_downloaded()
    if (not _stat.S_ISREG(st.st_mode) or (st.st_blocks == 0 and st.st_size > 0)
            or st.st_size > _content._MAX_ENCRYPTION_XML_BYTES):
        return "encryption.xml"
    ident = ("encryption", st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    entry = _CACHE.peek(ident)
    if entry is not None:
        verdict = entry.value
    else:
        generation = _CACHE.generation
        try:
            data, read_st, stable = _content._read_entry(
                root, "META-INF/encryption.xml", _content._MAX_ENCRYPTION_XML_BYTES)
        except BookNotDownloadedError:
            raise
        except Exception:  # noqa: BLE001 (fail closed, as 1.10)
            return "encryption.xml"
        verdict = _content._encryption_xml_bytes_hide_content(data)
        if stable and ("encryption", read_st.st_dev, read_st.st_ino, read_st.st_mtime_ns,
                       read_st.st_size) == ident:
            _CACHE.insert(ident, ident, verdict, 256, generation)
    return "encryption.xml" if verdict else None


def _gate_steps(root: pathlib.Path, held: Optional[_BookIndex] = None) -> _Gated:
    # (2) The bundle itself.
    if root.suffix.lower() != ".epub":
        return _not_epub(root)
    if _icloud.icloud_stub(root):
        return _not_downloaded()
    try:
        st = _icloud.lstat(root)
        if _stat.S_ISLNK(st.st_mode):
            st = _icloud.stat(root)
    except (FileNotFoundError, NotADirectoryError):
        return _not_downloaded(_MISSING_MESSAGE)
    if _icloud.is_dataless(st):
        return _not_downloaded()
    if not _stat.S_ISDIR(st.st_mode):
        return _not_epub(root)
    # (3) META-INF, then (4) the files a cached index was read from (and
    # the folders on the way). A placeholder found there is reported
    # after the DRM check, as a miss finds it while reading (after it).
    _content._check_dirs_local(root, "META-INF")
    pending: Optional[BookNotDownloadedError] = None
    index: Optional[_BookIndex] = None
    try:
        entry = _lookup(root, st)
        index = entry.value if entry is not None else _held_index(root, st, held)
    except BookNotDownloadedError as e:
        pending = e
    # (5) DRM, on every call.
    evidence = _drm_evidence(root)
    if evidence is not None:
        message = _FAIRPLAY_MESSAGE if evidence == "sinf.xml" else _ENCRYPTED_MESSAGE
        return _Gated(UnavailableReason.DRM, error=DRMProtectedError(message))
    if pending is not None:
        return _Gated(UnavailableReason.NOT_DOWNLOADED, error=pending)
    # (6) A miss reads the index.
    if index is None:
        index = _index_for(root, st)
    return _Gated(None, key=index.key, index=index)


def _gate_path(path: Any, held: Optional[_BookIndex] = None) -> _Gated:
    """The checks a ``BookContent`` method of 1.11 runs before reading the
    bundle at ``path``, and the book's index if they pass. The same result
    whether the index is cached or not.

    ``held``: an index the caller kept from an earlier gate of the same
    bundle; used, when the cache doesn't have the bundle's index (any
    more), if its files are unchanged, so an index that can't stay in the
    cache (too heavy, evicted) isn't read again on every call. Every
    check still runs.

    In order, with downloads of evicted files turned off throughout:
    (2) the bundle: not ``.epub`` or not a folder → ``NOT_EPUB``; missing,
    an iCloud placeholder or next to an iCloud stub → ``NOT_DOWNLOADED``;
    (3) ``META-INF`` not a placeholder; (4) a cached index's files (and
    the folders on the way) unchanged; (5) DRM (``sinf.xml``,
    ``rights.xml``, ``encryption.xml`` by 1.10's rules) → ``DRM``; then a
    placeholder found in (4) → ``NOT_DOWNLOADED``; (6) on a miss, the
    index read (``NOT_DOWNLOADED`` for a placeholder, ``UNREADABLE`` for
    anything else that fails). No ``du``, no walk of the bundle.
    """
    root = pathlib.Path(path)
    with _icloud.no_materialize():
        try:
            return _gate_steps(root, held)
        except BookNotDownloadedError as e:
            return _Gated(UnavailableReason.NOT_DOWNLOADED, error=e)
        except AppleBooksError as e:
            return _Gated(UnavailableReason.of(e) or UnavailableReason.UNREADABLE, error=e)
        except (OSError, ValueError) as e:
            if _icloud.is_materialize_error(e):
                return _not_downloaded()
            return _Gated(UnavailableReason.UNREADABLE, error=AppleBooksError(
                f"Could not read EPUB {quote_title(root.name)}: {detail(e)}"))


def _gate_book(book: Any) -> _Gated:
    """:func:`_gate_path` for a library book, after step (1), from the
    database alone (no file is touched when it refuses): an unowned
    Store series item without a file → ``NOT_OWNED``; no file, or a book
    Apple Books keeps only in iCloud (``ZSTATE`` 3) → ``NOT_DOWNLOADED``;
    a file that isn't ``.epub`` → ``NOT_EPUB``. Step (1) refusals carry
    no ``error``."""
    from py_apple_books.models.book import STATE_CLOUD_ONLY

    path = getattr(book, "path", None)
    if getattr(book, "is_store_series_item", False) and not path:
        return _Gated(UnavailableReason.NOT_OWNED)
    if not path or getattr(book, "state", None) == STATE_CLOUD_ONLY:
        return _Gated(UnavailableReason.NOT_DOWNLOADED)
    if pathlib.Path(path).suffix.lower() != ".epub":
        return _Gated(UnavailableReason.NOT_EPUB)
    return _gate_path(path)


def _book_gate(book: Any) -> Tuple[Optional[UnavailableReason], Optional[tuple]]:
    """``(reason, key)`` of :func:`_gate_book`: None and the index's cache
    key when the book can be read (:func:`_cached_index` finds the index
    by it, while it is stored), else the reason it can't."""
    gated = _gate_book(book)
    return gated.reason, gated.key


def _path_gate(path: Any) -> Tuple[Optional[UnavailableReason], Optional[tuple]]:
    """``(reason, key)`` of :func:`_gate_path`."""
    gated = _gate_path(path)
    return gated.reason, gated.key


# ---------------------------------------------------------------------------
# Anchor tables
# ---------------------------------------------------------------------------


class _AnchorTable(_MappingABC):
    """``{anchor: element path}`` of one content document (read-only).

    Each element is kept as one ``(parent, step)`` pair, and a path is
    built when it is looked up (in steps proportional to the element's
    depth), so the table's memory grows with the number of elements and
    anchors, however deeply they are nested. Iterating over every path
    costs the sum of the anchors' depths: look anchors up instead.
    """

    __slots__ = ("_anchors", "_parents", "_steps")

    def __init__(self, anchors: Dict[str, int], parents: "array[int]", steps: "array[int]") -> None:
        self._anchors = anchors
        self._parents = parents
        self._steps = steps

    def __getitem__(self, anchor: str) -> Tuple[int, ...]:
        node = self._anchors[anchor]
        parents, steps = self._parents, self._steps
        path: List[int] = []
        while node:
            path.append(steps[node])
            node = parents[node]
        path.reverse()
        return tuple(path)

    def __contains__(self, anchor: object) -> bool:
        return anchor in self._anchors

    def __iter__(self):
        return iter(self._anchors)

    def __len__(self) -> int:
        return len(self._anchors)

    def __repr__(self) -> str:
        return f"<anchor table: {len(self._anchors)} anchors>"

    @property
    def weight(self) -> int:
        """Estimated size in memory, in bytes (calibrated with tracemalloc
        on synthetic documents)."""
        return (512 + 18 * len(self._parents)
                + sum(110 + len(anchor) for anchor in self._anchors))


def _anchor_table(raw: bytes) -> Optional[_AnchorTable]:
    """``{anchor: element path}`` of a content document: every element
    ``id``, and every ``<a name>`` no element has as its id (the first
    in document order wins). An element path is its CFI steps from the
    root ``<html>`` element (the even numbers ``2 * (n + 1)`` for the
    n-th child element on the way; from the top of the document when it
    has no ``<html>``), from one iterative pre-order walk of bs4's
    ``html.parser`` tree. None for a document over
    :data:`MAX_ANCHOR_BYTES`. Time and memory are linear in the size of
    the document, whatever its nesting (see :class:`_AnchorTable`)."""
    if len(raw) > MAX_ANCHOR_BYTES:
        return None
    soup = BeautifulSoup(raw, "html.parser")
    top = soup.find("html")
    if top is None:
        top = soup
    # Node 0 is `top`; node n > 0 has its parent node and its CFI step.
    parents = array("q", [0])
    steps = array("q", [0])
    ids: Dict[str, int] = {}
    names: Dict[str, int] = {}
    stack: List[Tuple[Any, int]] = [(top, 0)]
    while stack:
        element, node = stack.pop()
        if node:
            anchor = element.get("id")
            if isinstance(anchor, str) and anchor and anchor not in ids:
                ids[anchor] = node
            if element.name == "a":
                name = element.get("name")
                if isinstance(name, str) and name and name not in names:
                    names[name] = node
        children = []
        count = 0
        for child in element.children:
            if isinstance(child, Tag):
                count += 1
                children.append((child, len(parents)))
                parents.append(node)
                steps.append(2 * count)
        stack.extend(reversed(children))
    names.update(ids)
    return _AnchorTable(names, parents, steps)


class _TooLarge:
    """Stored in place of an anchor table too heavy for the cache, so the
    file isn't parsed again on every call (see :func:`_anchor_table_for`)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<anchor table too large to keep>"


_TOO_LARGE = _TooLarge()


def _anchor_table_for(root: Any, href: str) -> Optional[Mapping[str, Tuple[int, ...]]]:
    """The cached anchor table (see :func:`_anchor_table`) of bundle entry
    ``href`` (bundle-relative, unquoted), read through
    ``content._read_entry`` and keyed by the file's identity, size and
    modification time; built once however many threads ask at the same
    time. None when the file is larger than :data:`MAX_ANCHOR_BYTES`, or
    its table would be heavier than the whole cache (that verdict is
    kept, so the file isn't parsed again).

    It checks only the file it reads (containment, iCloud placeholders
    on the way, size): run the book's gate (:func:`_gate_book` or
    :func:`_gate_path`, which checks DRM and the book's state) first.

    :raises BookNotDownloadedError: the file, or a folder on the way, is
        an iCloud placeholder.
    :raises UnsafeEpubEntryError: the entry is outside the bundle, not a
        regular file, or too large to read.
    :raises AppleBooksError: the file can't be read (the message names
        the entry, never a path).
    """
    root = pathlib.Path(root)

    def unreadable(e: OSError) -> AppleBooksError:
        return AppleBooksError(f"Could not read EPUB entry {quote_name(href)}: {detail(e)}")

    with _icloud.no_materialize():
        try:
            path = _content._safe_bundle_path(root, href)
            st = _icloud.stat(path)
        except OSError as e:
            err = _icloud.not_downloaded_error(e)
            if err is not None:
                raise err from None
            raise unreadable(e) from e
    if st.st_size > MAX_ANCHOR_BYTES:
        return None
    ident = ("anchor", st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

    def lookup() -> Any:
        entry = _CACHE.peek(ident)
        if entry is None:
            return None
        _CACHE.touch(ident, entry)
        return entry.value

    def build() -> Tuple[Any, tuple, int, bool]:
        try:
            data, read_st, stable = _content._read_entry(root, href, MAX_ANCHOR_BYTES)
        except OSError as e:
            raise unreadable(e) from e
        table = _anchor_table(data)
        if table is None:
            return _TOO_LARGE, ident, 256, False
        same = ("anchor", read_st.st_dev, read_st.st_ino, read_st.st_mtime_ns, read_st.st_size) == ident
        weight = table.weight
        if weight > _CACHE.max_weight:
            return _TOO_LARGE, ident, 256, stable and same
        return table, ident, weight, stable and same

    found = _get_or_build(ident, lookup, build)
    return None if found is _TOO_LARGE else found
