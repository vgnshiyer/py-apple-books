"""Access to the full text of non-DRM books in the Apple Books library.

This module reads EPUB content from the user's library without triggering
unwanted iCloud hydration. Many imported books live in
``~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`` and may
be stored as iCloud placeholders — the inode appears normal but no disk
blocks are allocated until something reads the file.

The placeholder check and DRM gates deliberately use only metadata
operations (``stat``, ``du``, filesystem existence) so a "can I read this
book?" check never causes an unexpected download. The one exception is
``META-INF/encryption.xml``, which the DRM gate has to parse — and only
when that file is already on local disk.

For the actual EPUB parsing and HTML-to-text extraction, this module uses
:mod:`ebooklib` and :mod:`bs4` — well-tested third-party libraries that
handle real-world EPUB quirks. A small stdlib fallback kicks in when an
EPUB's OPF omits the ``<spine toc=…>`` attribute (e.g. *The 4-Hour
Workweek*); in that case ebooklib returns an empty ToC, so we detect the
NCX by media-type and parse it ourselves.

Every file read from an EPUB bundle is confined to that bundle: entries
that resolve outside it (absolute or ``../`` hrefs, symlinks) or that
aren't regular files (FIFOs, device nodes) are refused, since a crafted
book could otherwise expose unrelated local files or block forever.

Since 1.11 no read can download an evicted iCloud file either: every
folder is checked not to be an iCloud placeholder before a name inside
it is looked up, every entry before it is read, and every read runs
with downloads of evicted files turned off for the reading thread (see
:mod:`py_apple_books._icloud`), so a missed placeholder fails with
:class:`BookNotDownloadedError` instead of being downloaded.

Also since 1.11, a process-wide index of each book (its chapter list and
spine, read from ``container.xml``, the package document and the
navigation files only; see :mod:`py_apple_books._epub_index`) serves
repeat :meth:`BookContent.list_chapters` calls and the spine API
(:meth:`BookContent.list_spine_items`,
:meth:`BookContent.get_spine_item_text`,
:meth:`BookContent.iter_spine_text`), which then read only the files
whose text is asked for. :func:`clear_content_cache` empties it.
"""

import copy
import errno
import os
import pathlib
import posixpath
import stat
import subprocess
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

from ebooklib import epub

from py_apple_books import _icloud
from py_apple_books._content_reading import _ReadingMixin
from py_apple_books._content_resolve import _ResolveMixin
from py_apple_books._messages import detail, quote_name, quote_title
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    ChapterNotFoundError,
    DRMProtectedError,
    InvalidArgumentError,
    NotEpubError,
    UnsafeEpubEntryError,
)
from py_apple_books.positions import ReadBoundary, ResolvedBoundary, TextPosition, UnavailableReason
from py_apple_books.text import normalize_unicode as _normalize_unicode
from py_apple_books.utils import extract_chapter_text, normalize_whitespace

PathLike = Union[str, pathlib.Path]


# ---------------------------------------------------------------------------
# Placeholder detection (stdlib, does not hydrate)
# ---------------------------------------------------------------------------


# An EPUB bundle whose recursive disk usage is at or below this threshold is
# considered an iCloud placeholder. The small non-zero cutoff tolerates the
# partial-skeleton state that can appear after a listdir materializes a few
# KB of directory entries without fetching any actual book content.
_PLACEHOLDER_SIZE_THRESHOLD_KB = 4


def is_downloaded(path: PathLike) -> bool:
    """Return True if a book file or bundle is materialized on local disk.

    iCloud files in Apple Books' Documents folder can exist as placeholders —
    the inode appears normal (``stat()`` returns the logical size) but no
    disk blocks are allocated until hydration is triggered. Reading bytes
    or listing a bundle's contents will trigger a download.

    This check uses only non-hydrating metadata operations:

    * For a single file (e.g. ``.pdf``): ``os.stat`` alone. A placeholder
      has ``st_blocks == 0`` with ``st_size > 0``; a downloaded file has
      ``st_blocks > 0``.
    * For a directory bundle (e.g. unzipped ``.epub``): the top-level
      directory's ``st_blocks`` is always 0 on APFS regardless of content
      state, so we shell out to ``du -sk`` which walks the tree via
      ``lstat``. This has been verified empirically to **not** trigger
      iCloud hydration on macOS.

    Since 1.11 a path that is itself an iCloud placeholder (``SF_DATALESS``,
    checked with ``lstat`` before ``du`` runs) or has an iCloud stub
    (``.<name>.icloud``) next to it is reported as not downloaded first.

    Fails open: any filesystem error or unexpected state returns True,
    letting the actual read operation surface a clearer error.

    :param path: Path to a file or directory bundle.
    :return: True if locally available; False if it is an iCloud placeholder
        or does not exist.
    """
    path = pathlib.Path(path)
    if not _root_is_local(path):
        return False
    if not path.exists():
        return False

    try:
        st = path.stat()
    except OSError:
        return False

    if path.is_file():
        # Clean, pure-Python signal for single-file placeholders.
        if st.st_blocks == 0 and st.st_size > 0:
            return False
        return True

    if path.is_dir():
        try:
            result = subprocess.run(
                ["du", "-sk", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True  # fail open

        if result.returncode != 0:
            return True

        try:
            size_kb = int(result.stdout.split()[0])
        except (ValueError, IndexError):
            return True

        return size_kb > _PLACEHOLDER_SIZE_THRESHOLD_KB

    # Symlink / other: treat as present; any downstream read will fail cleanly.
    return True


def _root_is_local(path: pathlib.Path) -> bool:
    """False if ``path`` (or, for a symlink, its target) is an iCloud
    placeholder or has an iCloud stub next to it. Any other error is
    left to the caller's own checks."""
    with _icloud.no_materialize():
        try:
            if _icloud.icloud_stub(path):
                return False
            st = _icloud.lstat(path)
            if _icloud.is_dataless(st):
                return False
            if stat.S_ISLNK(st.st_mode) and _icloud.is_dataless(_icloud.stat(path)):
                return False
        except (OSError, ValueError) as e:
            if _icloud.is_materialize_error(e):
                return False
    return True


def _not_downloaded() -> BookNotDownloadedError:
    """The error for a read that reached an iCloud placeholder."""
    return BookNotDownloadedError(_icloud.PARTIAL_DOWNLOAD_MESSAGE)


def _too_large_message(name: str, limit: int) -> str:
    if limit % (1024 * 1024) == 0:
        size = f"{limit // (1024 * 1024)} MiB"
    else:
        size = f"{limit} bytes"
    return f"EPUB entry {quote_name(name)} is larger than {size}."


def _check_dirs_local(root: pathlib.Path, rel_dir: str, checked: Optional[Set[str]] = None) -> None:
    """Raise :class:`BookNotDownloadedError` if a folder on the way to
    ``rel_dir`` (bundle-relative, lexical: ``"OEBPS/Text"`` checks
    ``OEBPS``, then ``OEBPS/Text``) is an iCloud placeholder, each
    checked before anything inside it is looked up.

    Names that leave the bundle lexically aren't checked (containment
    refuses them), and the walk stops at the first folder that can't be
    stat'ed, so the read itself reports it. Folders in ``checked`` are
    skipped; the ones found local are added to it.
    """
    if not rel_dir or rel_dir in (".", "..") or rel_dir.startswith(("/", "../")):
        return
    current = ""
    for part in rel_dir.split("/"):
        current = f"{current}/{part}" if current else part
        if checked is not None and current in checked:
            continue
        try:
            st = _icloud.lstat(root / current)
            if stat.S_ISLNK(st.st_mode):
                st = _icloud.stat(root / current)
        except (OSError, ValueError) as e:
            if _icloud.is_materialize_error(e):
                raise _not_downloaded() from None
            return
        if _icloud.is_dataless(st):
            raise _not_downloaded()
        if checked is not None:
            checked.add(current)


# ---------------------------------------------------------------------------
# Bundle containment (every read from an EPUB bundle goes through here)
# ---------------------------------------------------------------------------


# Upper bound for a single bundle entry. Real books carry fonts and videos
# in the tens of MiB (largest seen: a 22 MiB font), so this only stops a
# hostile entry from exhausting memory — ebooklib reads every manifest
# item eagerly.
_MAX_ENTRY_BYTES = 256 * 1024 * 1024


def _resolve_strictly(path: pathlib.Path, rel: str) -> pathlib.Path:
    """``path.resolve(strict=True)``, refusing symlink loops and bad names.

    Non-strict resolution isn't safe for containment: before Python
    3.13 it gives up at a symlink loop and returns the rest of the path
    unresolved, so ``loop/../link`` comes back looking like an in-bundle
    path while ``link`` still points outside. A missing entry still
    raises :class:`FileNotFoundError`, like any other unreadable one.

    :param rel: The entry name, for the error message.
    :raises UnsafeEpubEntryError: on a symlink loop or a NUL byte.
    """
    try:
        return path.resolve(strict=True)
    except OSError as e:
        if e.errno != errno.ELOOP:
            raise
    except (RuntimeError, ValueError):
        # RuntimeError: a symlink loop, as Python < 3.13 reports it.
        # ValueError: a NUL byte in the name (a ToC href containing
        # "%00").
        pass
    raise UnsafeEpubEntryError(
        f"EPUB entry {quote_name(rel)} can't be resolved inside the book bundle.",
        entry=rel,
    )


def _safe_bundle_path(root: pathlib.Path, rel: str) -> pathlib.Path:
    """Resolve a bundle-relative entry name, refusing anything unsafe.

    Symlinks are resolved before the containment check, so a link that
    points outside the bundle is caught just like an absolute or
    ``../`` name. The entry must be a regular file: FIFOs and device
    nodes (``/dev/stdin``, ``/dev/zero``) would block or never end, and
    are rejected from ``stat`` alone, without opening them. The bundle
    and each folder on the way are checked not to be iCloud placeholders
    before a name inside them is looked up, and so is the entry.

    :param root: The EPUB bundle directory.
    :param rel: Entry name relative to ``root`` (``/``-separated).
    :raises UnsafeEpubEntryError: if the entry escapes the bundle, isn't
        a regular file, or exceeds :data:`_MAX_ENTRY_BYTES`.
    :raises BookNotDownloadedError: if the bundle, a folder or the entry
        is an iCloud placeholder.
    :raises OSError: if the entry can't be stat'ed (e.g.
        :class:`FileNotFoundError` when it doesn't exist).
    """
    root = _resolve_strictly(root, rel)
    try:
        root_st = _icloud.stat(root)
    except OSError as e:
        if _icloud.is_materialize_error(e):
            raise _not_downloaded() from None
        raise
    if _icloud.is_dataless(root_st):
        raise _not_downloaded()
    _check_dirs_local(root, posixpath.dirname(posixpath.normpath(rel)))
    candidate = root / posixpath.normpath(rel)
    try:
        path = _resolve_strictly(candidate, rel)
    except (FileNotFoundError, NotADirectoryError):
        # Nothing exists to read. Still report a name that points
        # outside the bundle as an escape, not a missing file, so a
        # crafted book can't probe which outside paths exist.
        try:
            escapes = not candidate.resolve().is_relative_to(root)
        except (OSError, RuntimeError, ValueError):
            escapes = False
        if escapes:
            raise UnsafeEpubEntryError(
                f"EPUB entry {quote_name(rel)} points outside the book bundle.",
                entry=rel,
            ) from None
        raise
    if not path.is_relative_to(root):
        raise UnsafeEpubEntryError(
            f"EPUB entry {quote_name(rel)} points outside the book bundle.",
            entry=rel,
        )
    st = _icloud.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeEpubEntryError(
            f"EPUB entry {quote_name(rel)} is not a regular file.",
            entry=rel,
        )
    if st.st_size > _MAX_ENTRY_BYTES:
        raise UnsafeEpubEntryError(_too_large_message(rel, _MAX_ENTRY_BYTES), entry=rel)
    # After the 1.10 checks, so an oversized sparse file is still
    # refused as oversized.
    if _icloud.is_dataless(st):
        raise _not_downloaded()
    return path


def _read_entry_bytes(root: PathLike, href: str, max_bytes: int) -> bytes:
    """The bytes of one bundle entry, for new content APIs (1.11): the
    one guarded primitive they read book files through.

    :func:`_safe_bundle_path` (containment, a regular file, the
    :data:`_MAX_ENTRY_BYTES` cap, no iCloud placeholder on the way), then
    the file is opened (no symlink follow, non-blocking) and read with
    downloads of evicted files turned off for this thread. Its size is
    checked again on the open file, and at most ``max_bytes + 1`` bytes
    are read, so an entry that grows past ``max_bytes`` is refused.

    :param href: Entry name relative to ``root`` (``/``-separated,
        unquoted).
    :raises UnsafeEpubEntryError: see :func:`_safe_bundle_path`; also an
        entry larger than ``max_bytes``.
    :raises BookNotDownloadedError: the entry (or a folder on the way)
        is an iCloud placeholder.
    :raises OSError: it can't be read (e.g. :class:`FileNotFoundError`).
        Its text may name a path: wrap it with
        :func:`py_apple_books._messages.detail` before showing it.
    """
    return _read_entry(root, href, max_bytes)[0]


def _read_entry(root: PathLike, href: str, max_bytes: int) -> Tuple[bytes, os.stat_result, bool]:
    """:func:`_read_entry_bytes`, also returning the ``fstat`` of the open
    file taken before the read, and whether a second ``fstat`` after the
    read found it unchanged (same size and modification time): the
    identity a cache of anything derived from the bytes is keyed by.
    Same checks and errors as :func:`_read_entry_bytes`."""
    root = pathlib.Path(root)
    with _icloud.no_materialize():
        try:
            path = _safe_bundle_path(root, href)
            fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise UnsafeEpubEntryError(
                        f"EPUB entry {quote_name(href)} is not a regular file.",
                        entry=href,
                    )
                if st.st_size > max_bytes:
                    raise UnsafeEpubEntryError(_too_large_message(href, max_bytes), entry=href)
                if _icloud.is_dataless(st):
                    raise _not_downloaded()
                chunks: List[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(1 << 20, max_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafeEpubEntryError(_too_large_message(href, max_bytes), entry=href)
                    chunks.append(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError as e:
            err = _icloud.not_downloaded_error(e)
            if err is not None:
                raise err from None
            raise
    stable = (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns) == (total, st.st_mtime_ns)
    return b"".join(chunks), st, stable


def _escapes_bundle(href: str) -> bool:
    """True if a bundle-relative href climbs above the bundle root or
    is absolute. Lexical only — :func:`_safe_bundle_path` is what
    enforces containment on read."""
    norm = posixpath.normpath(href)
    return norm == ".." or norm.startswith("../") or norm.startswith("/")


class _ContainedEpubReader(epub.EpubReader):
    """:class:`ebooklib.epub.EpubReader` confined to the book bundle.

    ebooklib's directory backend opens ``os.path.join(root, name)`` for
    the container, the OPF and every manifest item at load time, with no
    containment check. Routing those reads through
    :func:`_safe_bundle_path` makes a crafted book fail to load instead.
    Zipped EPUBs keep ebooklib's own reader: archive members are looked
    up by name and can't leave the archive.
    """

    def __init__(self, epub_file_name, options=None):
        super().__init__(epub_file_name, options)
        self._root: Optional[pathlib.Path] = None
        # Bundle-relative directory -> (resolved path, st_dev, st_ino),
        # already checked to lie inside the bundle.
        self._resolved_dirs: Dict[str, Tuple[pathlib.Path, int, int]] = {}
        # Bundle-relative directories checked not to be iCloud placeholders.
        self._local_dirs: Set[str] = set()
        # META-INF/container.xml as read during load (BookContent derives
        # the OPF directory from it, without reading it again).
        self.container_bytes: Optional[bytes] = None

    def _entry_path(self, name: str) -> pathlib.Path:
        """:func:`_safe_bundle_path`, amortised over one book load.

        ebooklib reads every manifest item (a thousand, for image-heavy
        books), and resolving each full path from scratch dominated load
        time. Each directory is resolved and containment-checked once,
        and pinned by device and inode so a directory swapped mid-load
        isn't trusted; an entry then costs a ``stat`` of its directory
        and an ``lstat`` of itself. Anything else — symlinked entries,
        odd names, directories outside the bundle or changed since —
        takes the full check, so outcomes match it exactly.

        Before any of that, the bundle and each folder on the way are
        checked (once per load) not to be iCloud placeholders, and the
        entry's ``lstat`` is too: a placeholder raises
        :class:`BookNotDownloadedError` before anything inside it is
        looked up or read.
        """
        if self._root is None:
            root = _resolve_strictly(pathlib.Path(self.file_name), name)
            try:
                root_st = _icloud.stat(root)
            except OSError as e:
                if _icloud.is_materialize_error(e):
                    raise _not_downloaded() from None
                raise
            if _icloud.is_dataless(root_st):
                raise _not_downloaded()
            self._root = root
        rel = posixpath.normpath(name)
        parent, base = posixpath.split(rel)
        if base in ("", ".", ".."):
            return _safe_bundle_path(self._root, name)
        _check_dirs_local(self._root, parent, self._local_dirs)
        try:
            cached = self._resolved_dirs.get(parent)
            if cached is None:
                directory = (self._root / parent).resolve(strict=True)
                if not directory.is_relative_to(self._root):
                    return _safe_bundle_path(self._root, name)
                dir_st = _icloud.stat(directory)
                cached = (directory, dir_st.st_dev, dir_st.st_ino)
                self._resolved_dirs[parent] = cached
            else:
                dir_st = _icloud.stat(cached[0])
                if (dir_st.st_dev, dir_st.st_ino) != cached[1:]:
                    del self._resolved_dirs[parent]
                    return _safe_bundle_path(self._root, name)
            if _icloud.is_dataless(dir_st):
                raise _not_downloaded()
            path = cached[0] / base
            st = _icloud.lstat(path)
        except BookNotDownloadedError:
            raise
        except (OSError, RuntimeError, ValueError) as e:
            if _icloud.is_materialize_error(e):
                raise _not_downloaded() from None
            return _safe_bundle_path(self._root, name)
        if stat.S_ISLNK(st.st_mode):
            return _safe_bundle_path(self._root, name)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeEpubEntryError(
                f"EPUB entry {quote_name(name)} is not a regular file.",
                entry=name,
            )
        if st.st_size > _MAX_ENTRY_BYTES:
            raise UnsafeEpubEntryError(_too_large_message(name, _MAX_ENTRY_BYTES), entry=name)
        if _icloud.is_dataless(st):
            raise _not_downloaded()
        return path

    def read_file(self, name):
        if isinstance(self.zf, zipfile.ZipFile):
            return super().read_file(name)
        with _icloud.no_materialize():
            try:
                path = self._entry_path(name)
                data = path.read_bytes()
            except OSError as e:
                err = _icloud.not_downloaded_error(e)
                if err is not None:
                    raise err from None
                # Name the entry, not the absolute path — messages reach
                # MCP clients verbatim.
                raise AppleBooksError(
                    f"Could not read EPUB entry {quote_name(name)}: {e.strerror}"
                ) from e
        if self.container_bytes is None and posixpath.normpath(name) == "META-INF/container.xml":
            self.container_bytes = data
        return data


# ---------------------------------------------------------------------------
# DRM detection
# ---------------------------------------------------------------------------


# OCF font obfuscation algorithms. DRM-free publisher EPUBs (InDesign
# exports, for one) list their embedded fonts in encryption.xml under
# these; the text itself stays plain.
_FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",  # IDPF
    "http://ns.adobe.com/pdf/enc#RC",  # Adobe
}

# encryption.xml is parsed on every get_book_content call, before the
# book loads. Real ones are a few KB; a larger one counts as encrypted
# rather than being parsed.
_MAX_ENCRYPTION_XML_BYTES = 1024 * 1024


def _local_name(tag: str) -> str:
    """An element tag without its ``{namespace}`` prefix."""
    return tag.rpartition("}")[2]


def _encryption_xml_hides_content(bundle: pathlib.Path) -> bool:
    """True if a bundle's ``META-INF/encryption.xml`` encrypts anything
    beyond font obfuscation.

    Fails closed: an unreadable, oversized or malformed file, an
    ``EncryptedData`` without an algorithm, or a file that's still an
    iCloud placeholder (reading it would trigger a download) all count
    as encrypted. Elements are matched by local name, so a file that
    leaves out the xmlenc namespace is still inspected. Read with
    downloads of evicted files turned off.
    """
    try:
        with _icloud.no_materialize():
            enc = _safe_bundle_path(bundle, "META-INF/encryption.xml")
            st = _icloud.stat(enc)
            if _icloud.is_dataless(st) or (st.st_blocks == 0 and st.st_size > 0):
                return True
            if st.st_size > _MAX_ENCRYPTION_XML_BYTES:
                return True
            data = enc.read_bytes()
    except Exception:
        # This gate must never raise.
        return True
    return _encryption_xml_bytes_hide_content(data)


def _encryption_xml_bytes_hide_content(data: bytes) -> bool:
    """The verdict of :func:`_encryption_xml_hides_content` on the bytes
    of an ``encryption.xml``: True if it encrypts anything beyond font
    obfuscation, or can't be parsed. Never raises."""
    try:
        root = ET.fromstring(data)
    except Exception:
        # Not just ParseError: expat raises ValueError or LookupError
        # for some declared encodings (Shift_JIS, unknown names).
        return True
    for data in root.iter():
        if _local_name(data.tag) != "EncryptedData":
            continue
        method = next(
            (el for el in data if _local_name(el.tag) == "EncryptionMethod"),
            None,
        )
        algorithm = method.get("Algorithm") if method is not None else None
        if algorithm not in _FONT_OBFUSCATION_ALGORITHMS:
            return True
    return False


# ---------------------------------------------------------------------------
# Media types whose text is not extracted (1.11)
# ---------------------------------------------------------------------------


# Spine and manifest items of these types have no text to extract:
# get_spine_item_text() refuses them instead of parsing their bytes as
# HTML. Any other type (XHTML, HTML, SVG, XML, a missing or unknown type)
# is read as HTML, as 1.10 read every spine entry.
_BINARY_MEDIA_MAJOR = frozenset({"image", "audio", "video", "font"})
_BINARY_MEDIA_TYPES = frozenset({
    "text/css",
    "application/x-dtbncx+xml",
    "application/smil+xml",
    "application/javascript", "application/x-javascript", "application/ecmascript",
    "text/javascript", "text/ecmascript",
    "application/pdf",
    "application/font-woff", "application/font-woff2", "application/x-font-woff",
    "application/font-sfnt", "application/x-font-ttf", "application/x-font-truetype",
    "application/x-font-otf", "application/x-font-opentype", "application/vnd.ms-opentype",
    "application/vnd.ms-fontobject", "application/x-font-type1",
})


def _is_text_media_type(media_type: Optional[str]) -> bool:
    """False for a media type with no text to extract (images other than
    SVG, audio, video, fonts, stylesheets, NCX, SMIL, scripts, PDF);
    True for every other type, including a missing one. Compared without
    case or parameters."""
    if not isinstance(media_type, str):
        return True
    mt = media_type.split(";", 1)[0].strip().lower()
    if not mt or mt == "image/svg+xml":
        return True
    return mt.split("/", 1)[0] not in _BINARY_MEDIA_MAJOR and mt not in _BINARY_MEDIA_TYPES


# ---------------------------------------------------------------------------
# Dataclasses (public API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chapter:
    """A single navigable entry in an EPUB's table of contents.

    :param id: Stable identifier suitable for round-trip to
        :meth:`BookContent.get_chapter`. Prefers the manifest
        item id; falls back to the 1-based :attr:`order` as a string
        when the item id is ambiguous or missing.
    :param title: Human-readable chapter title from the navigation doc
        (EPUB3) or NCX ``navLabel`` (EPUB2).
    :param href: Path relative to the EPUB bundle root, with any URL
        fragment (``#anchor``) stripped.
    :param fragment: The fragment from the navigation reference, or an
        empty string if none. Kept separately so callers can anchor
        into a specific section.
    :param order: 1-based position in the flattened ToC.
    :param depth: Nesting level in the ToC tree. 0 = top-level chapter,
        1 = first-level subsection, etc.
    :param spine_index: (1.11) The 0-based position of the chapter's
        file in the book's spine (its first, if the spine lists the file
        more than once), the number ``Location.spine_index`` and
        :attr:`SpineItem.index` use; None when the file isn't in the
        spine, or for a :class:`Chapter` built without it. Not part of
        equality, hashing or ``repr``, so chapters compare as in 1.10.
    """

    id: str
    title: str
    href: str
    fragment: str
    order: int
    depth: int
    spine_index: Optional[int] = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class SpineItem:
    """One entry of a book's spine, the reading order its package
    document lists (1.11). See :meth:`BookContent.list_spine_items`.

    :param index: 0-based position among the element children of the
        package document's ``<spine>`` (comments and processing
        instructions don't count; every element does), so it is the
        number ``Location.spine_index`` gives for a location in this
        file and the spine index of a
        :class:`~py_apple_books.positions.TextPosition`.
    :param item_id: The manifest id the entry names (``idref``), or None
        for an entry that names none (not an ``itemref``, or one without
        an ``idref``).
    :param href: The file's path relative to the book bundle (like
        :attr:`Chapter.href`), or None when the entry names no manifest
        item.
    :param media_type: The file's manifest media type, as written, or
        None.
    :param linear: False for an entry marked ``linear="no"`` (notes,
        pop-ups and other auxiliary content; compared without case or
        surrounding spaces); True otherwise.
    :param is_toc_page: True for a table-of-contents page: the EPUB 3
        navigation document, or a file the package's guide
        (``type="toc"``) or the navigation document's landmarks
        (``epub:type="toc"``) points to.
    :param readable: True when the entry names a manifest document whose
        text :meth:`BookContent.get_spine_item_text` can extract (not an
        image, stylesheet, font, audio, video, script or other binary
        type, and inside the bundle).
    :param toc_orders: The :attr:`Chapter.order` of every table of
        contents entry in this file, ascending (empty when none is).
    """

    index: int
    item_id: Optional[str]
    href: Optional[str]
    media_type: Optional[str]
    linear: bool
    is_toc_page: bool
    readable: bool
    toc_orders: Tuple[int, ...] = ()


@dataclass(frozen=True)
class SpineText:
    """Text of one spine item, or a slice of it, from
    :meth:`BookContent.iter_spine_text` (1.11).

    :param index: The item's spine index (:attr:`SpineItem.index`).
    :param item_id: Its manifest id (None for an entry that names none).
    :param href: Its path relative to the book bundle, or None.
    :param linear: See :attr:`SpineItem.linear`.
    :param is_toc_page: See :attr:`SpineItem.is_toc_page`.
    :param offset: Where :attr:`text` starts, in characters into the
        item's text (:meth:`BookContent.get_spine_item_text`).
    :param text: The slice of the item's text; empty when
        :attr:`readable` is False.
    :param length: The length of the item's whole text (0 when
        :attr:`readable` is False).
    :param readable: False for an item whose text couldn't be read
        (:attr:`SpineItem.readable` is False, or reading it failed);
        such an item is reported once, with no text.
    """

    index: int
    item_id: Optional[str]
    href: Optional[str]
    linear: bool
    is_toc_page: bool
    offset: int
    text: str
    length: int
    readable: bool = True

    @property
    def position(self) -> TextPosition:
        """Where :attr:`text` starts."""
        return TextPosition(self.index, self.offset)

    @property
    def end(self) -> TextPosition:
        """Just after :attr:`text` (exclusive): pass it as ``start`` to
        continue from here."""
        return TextPosition(self.index, self.offset + len(self.text))

    @property
    def complete(self) -> bool:
        """True when :attr:`text` is the item's whole text."""
        return self.readable and self.offset == 0 and len(self.text) == self.length


#: Most hits one page of a book-text search returns.
MAX_SEARCH_HITS = 1000
#: Most characters of context a book-text search snippet takes on each
#: side of a hit.
MAX_SNIPPET_CONTEXT = 2000


@dataclass(frozen=True)
class TextHit:
    """One match of a book-text search (1.11).

    :param start: Where the match starts.
    :param end: Just after the match (exclusive).
    :param item_id: The manifest id of the spine item it is in, or None.
    :param snippet: The match with some text around it, on one line.
    """

    start: TextPosition
    end: TextPosition
    item_id: Optional[str]
    snippet: str


@dataclass(frozen=True)
class TextSearchResult:
    """One page of book-text search results (1.11).

    :param hits: The matches, in reading order.
    :param next_start: Where the next page starts (pass it as ``start``),
        or None when there are no more matches in scope.
    :param total: How many matches there are in scope, when counted;
        else None.
    :param withheld_in_item: Matches in the boundary's own spine item
        past the boundary, when counted; else None.
    :param withheld_later: Matches in later spine items, when counted;
        else None.
    """

    hits: Tuple[TextHit, ...]
    next_start: Optional[TextPosition] = None
    total: Optional[int] = None
    withheld_in_item: Optional[int] = None
    withheld_later: Optional[int] = None

    @property
    def truncated(self) -> bool:
        """True when more matches follow (:attr:`next_start` is set)."""
        return self.next_start is not None


# ---------------------------------------------------------------------------
# BookContent
# ---------------------------------------------------------------------------


class BookContent(_ResolveMixin, _ReadingMixin):
    """File-level access to a single book in the Apple Books library.

    Wraps a filesystem path with format detection (EPUB bundle directory
    vs single PDF file), iCloud materialization state, DRM detection,
    and — for EPUBs — chapter listing, per-chapter text extraction, and
    (1.11) the book's spine: :meth:`list_spine_items`,
    :meth:`get_spine_item_text` and :meth:`iter_spine_text`.

    Construct via :meth:`PyAppleBooks.get_book_content`, which performs
    the pre-filesystem gate checks. Direct construction from a path is
    also supported for testing.

    An instance may be shared between threads: the book is read once.
    It can be pickled and copied (the parsed book goes with it).

    :param path: Absolute path to the book file or bundle directory.
    :param book_id: The library id of the book (1.11;
        :meth:`PyAppleBooks.get_book_content` sets it), or None.
    """

    def __init__(self, path: PathLike, *, book_id: Optional[int] = None) -> None:
        self.path = pathlib.Path(path)
        self._book: Optional[epub.EpubBook] = None
        self._opf_dir_cache: Optional[pathlib.PurePosixPath] = None
        self._book_id = book_id
        self._init_runtime_state()

    def _init_runtime_state(self) -> None:
        """Per-instance state that is never pickled or copied (locks,
        memos); set up again by :meth:`__setstate__`."""
        self._lock = threading.Lock()
        # list_chapters(), once computed (a tuple of shared Chapters).
        self._chapters_memo: Optional[Tuple[Chapter, ...]] = None
        # get_spine_item_text() default text by manifest id: at most one
        # book's text, dropped with the instance.
        self._spine_text_memo: Dict[str, str] = {}

    # Kept by pickle and copy; everything else is runtime state.
    _PICKLED = ("path", "_book", "_book_id", "_opf_dir_cache")

    def __getstate__(self) -> dict:
        return {name: getattr(self, name, None) for name in self._PICKLED}

    def __setstate__(self, state: dict) -> None:
        # Also accepts the state of a 1.10 instance (its __dict__:
        # path, _book, _opf_dir_cache).
        self.path = pathlib.Path(state["path"])
        self._book = state.get("_book")
        self._opf_dir_cache = state.get("_opf_dir_cache")
        self._book_id = state.get("_book_id")
        self._init_runtime_state()

    def __repr__(self) -> str:
        return f"BookContent(path={str(self.path)!r})"

    @property
    def book_id(self) -> Optional[int]:
        """The library id of the book, when the instance came from
        :meth:`PyAppleBooks.get_book_content`; else None."""
        return self._book_id

    # -- format / state properties ------------------------------------------

    @property
    def is_epub(self) -> bool:
        """True if the path is an EPUB bundle directory (Apple stores
        EPUBs unzipped on disk)."""
        return self.path.is_dir() and self.path.suffix.lower() == ".epub"

    @property
    def is_pdf(self) -> bool:
        """True if the path is a single PDF file."""
        return self.path.is_file() and self.path.suffix.lower() == ".pdf"

    @property
    def is_downloaded(self) -> bool:
        """True if the file/bundle is locally materialized (not an iCloud
        placeholder). Does not trigger hydration."""
        return is_downloaded(self.path)

    @property
    def is_drm_protected(self) -> bool:
        """True if the book is DRM-protected and its content cannot be read.

        For EPUB bundles, DRM is indicated by any of:

        * ``META-INF/sinf.xml`` — FairPlay license data (Apple Books
          Store purchases);
        * ``META-INF/rights.xml`` — Adobe ADEPT;
        * ``META-INF/encryption.xml`` encrypting anything other than
          fonts. Font obfuscation alone (common in DRM-free publisher
          EPUBs) leaves the text readable and doesn't count; a
          malformed file does.

        Only EPUBs are checked today.
        """
        if not self.is_epub:
            return False
        return self._drm_evidence() is not None

    def _drm_evidence(self) -> Optional[str]:
        """Name of the ``META-INF`` file that marks this bundle as
        DRM-protected (``"sinf.xml"``, ``"rights.xml"`` or
        ``"encryption.xml"``), or None. See :attr:`is_drm_protected`.

        Never looks inside an iCloud placeholder folder: if the bundle or
        its ``META-INF`` folder is one, the answer is unknown and the
        gate fails closed (``"encryption.xml"``, as for an
        ``encryption.xml`` that is itself a placeholder).
        :meth:`PyAppleBooks.get_book_content` refuses such a book as not
        downloaded before it gets here."""
        meta_inf = self.path / "META-INF"
        with _icloud.no_materialize():
            try:
                for folder in (self.path, meta_inf):
                    st = _icloud.lstat(folder)
                    if stat.S_ISLNK(st.st_mode):
                        st = _icloud.stat(folder)
                    if _icloud.is_dataless(st):
                        return "encryption.xml"
            except (OSError, ValueError) as e:
                if _icloud.is_materialize_error(e):
                    return "encryption.xml"
            try:
                for name in ("sinf.xml", "rights.xml"):
                    if (meta_inf / name).exists():
                        return name
                encryption = meta_inf / "encryption.xml"
                if encryption.exists() and _encryption_xml_hides_content(self.path):
                    return "encryption.xml"
            except OSError as e:
                # Path.exists() lets some errors through before 3.12.
                if _icloud.is_materialize_error(e):
                    return "encryption.xml"
                raise
        return None

    # -- chapter listing ----------------------------------------------------

    def list_chapters(self) -> List[Chapter]:
        """Return the book's chapter list in reading order.

        Uses :mod:`ebooklib` as the primary parser. For EPUBs where
        ebooklib returns an empty ToC because the OPF omits the
        ``<spine toc=…>`` attribute (a real-world quirk found in e.g.
        *The 4-Hour Workweek*), falls back to locating the NCX via its
        manifest ``media-type`` and parsing it directly. As a last
        resort, emits one entry per spine item with a synthetic
        ``Section N`` title.

        Since 1.11 the list is computed once per instance, and other
        instances on the same bundle are served from a process-wide index
        while the bundle's ``container.xml``, package document and
        navigation files are unchanged (same file, size and modification
        time), so a repeat call reads nothing but file metadata. Each
        call returns a new list; the :class:`Chapter` objects in it are
        shared (they are immutable) and carry
        :attr:`Chapter.spine_index`. The values are those of 1.10: the
        index is used only after it gave the same list as reading the
        whole book did. :func:`clear_content_cache` drops the index.

        :raises AppleBooksError: if the book is not an EPUB or ebooklib
            cannot read the package.
        """
        self._require_epub()
        chapters = self._chapters_memo
        if chapters is None:
            chapters = self._chapter_list()
            self._chapters_memo = chapters
        return list(chapters)

    def _chapter_list(self) -> Tuple[Chapter, ...]:
        """:meth:`list_chapters`, uncached on this instance: the
        process-wide index when it is known to give 1.10's list for the
        bundle as it is now; else 1.10's way, by reading the whole book
        (with 1.10's errors), after which the index is built (or
        checked) and kept if it gives the same list."""
        if self._book is None:
            index = _epub_index._verified_index(self.path)
            if index is not None:
                return index.chapters
        book = self._load_book()
        chapters = tuple(self._chapters_from_loaded_book(book))
        index = _epub_index._index_matching(self.path, chapters)
        return index.chapters if index is not None else chapters

    def _chapters_from_loaded_book(self, book: epub.EpubBook) -> List[Chapter]:
        """1.10's chapter list from the loaded book: the ToC, else the
        NCX found by media type, else one entry per spine item."""
        chapters = self._chapters_from_ebooklib_toc(book)
        if chapters:
            return chapters

        chapters = self._chapters_from_media_type_ncx()
        if chapters:
            return chapters

        return self._chapters_from_spine(book)

    # -- the spine (1.11) -------------------------------------------------------

    def list_spine_items(self) -> List[SpineItem]:
        """The book's spine (its reading order) as :class:`SpineItem`
        entries, one per element of the package document's ``<spine>``,
        in order (1.11).

        Reads only the book's ``container.xml``, package document and
        navigation files (kept in the process-wide index, like
        :meth:`list_chapters`); nothing else is opened.

        :raises NotEpubError: the book is not an EPUB bundle.
        :raises BookNotDownloadedError: part of the book is stored only
            in iCloud (nothing is downloaded).
        :raises DRMProtectedError: the book is DRM-protected.
        :raises AppleBooksError: the book's package can't be read.
        """
        return list(self._gated_index().spine)

    def get_spine_item_text(self, item: Union[str, int], *, normalize_unicode: bool = False) -> str:
        """The plain text of one whole spine file (1.11).

        The text of the entire file, never scoped to a table-of-contents
        entry: the same text a chapter id or the spine id from an
        annotation's location gives for a file that holds one chapter,
        extracted the same way as :meth:`get_chapter` does. Offsets in
        it are the offsets of
        :class:`~py_apple_books.positions.TextPosition`.

        :param item: A manifest id (``str``, as in
            :attr:`SpineItem.item_id` or ``Location.chapter_id``), or a
            0-based spine index (``int``, as in :attr:`SpineItem.index` or
            ``Location.spine_index``).
        :param normalize_unicode: Remove soft hyphens, zero-width spaces
            and BOMs and compose accents
            (:func:`py_apple_books.text.normalize_unicode`), then tidy the
            whitespace again. Offsets then no longer match the default
            text.
        :raises ChapterNotFoundError: no such manifest document, or no
            spine entry at that index (``bool`` and negative indexes
            included), or it names an image, stylesheet, font, audio,
            video, script or other binary file.
        :raises InvalidArgumentError: ``item`` is neither a str nor an
            int.
        :raises NotEpubError: the book is not an EPUB bundle.
        :raises BookNotDownloadedError: the file, or part of the book's
            package, is stored only in iCloud (nothing is downloaded).
        :raises DRMProtectedError: the book is DRM-protected.
        :raises UnsafeEpubEntryError: the file is outside the book
            bundle, not a regular file or too large.
        :raises AppleBooksError: the file can't be read.
        """
        if isinstance(item, bool) or (isinstance(item, int) and item < 0):
            raise ChapterNotFoundError(f"No spine entry at index {item!r} in this book.")
        if not isinstance(item, (str, int)):
            raise InvalidArgumentError(
                f"item must be a manifest id (str) or a spine index (int), "
                f"not {type(item).__name__}.")
        index = self._gated_index()
        if isinstance(item, int):
            if item >= len(index.spine):
                raise ChapterNotFoundError(f"No spine entry at index {item} in this book.")
            entry = index.spine[item]
            if entry.item_id is None or not entry.readable:
                raise ChapterNotFoundError(f"No text document at spine index {item} in this book.")
            item_id = entry.item_id
            missing = f"No text document at spine index {item} in this book."
        else:
            item_id = item
            missing = f"No text document with id {quote_name(item)} in this book."
        text = self._spine_text_memo.get(item_id)
        if text is None:
            text = self._read_spine_text(index, item_id, missing)
            self._spine_text_memo[item_id] = text
        if normalize_unicode:
            text = normalize_whitespace(_normalize_unicode(text))
        return text

    def iter_spine_text(
        self,
        *,
        start: Optional[TextPosition] = None,
        until: Optional[Union[TextPosition, ResolvedBoundary]] = None,
        include_nonlinear: bool = False,
        include_toc_pages: bool = False,
    ) -> Iterator[SpineText]:
        """The book's text, spine item by spine item, in reading order
        (1.11).

        Yields one :class:`SpineText` per spine item in scope, from
        ``start`` up to ``until`` (exclusive): the first item from
        ``start``'s offset, the ``until`` item cut at ``until``'s offset,
        nothing after it. Items marked non-linear and table-of-contents
        pages are left out unless asked for. An item whose slice is
        empty is skipped; an item in scope whose text can't be read is
        yielded once, with ``readable=False`` and no text (an item
        partly stored in iCloud, or a DRM-protected book, raises
        instead).

        To page through the text, take what you need and continue from
        the :attr:`SpineText.position` you stopped at (or a slice's
        :attr:`SpineText.end`). Each item's text is read when the
        iteration reaches it (:meth:`get_spine_item_text`); nothing is
        locked between items, so other threads can use the instance
        meanwhile.

        :param start: Where to start (default: the start of the book).
        :param until: Where to stop: a
            :class:`~py_apple_books.positions.TextPosition`, or a
            :class:`~py_apple_books.positions.ResolvedBoundary` (its
            ``position``; a ``ReadBoundary`` must be placed first, with
            ``resolve_boundary``). Default: the end of the book.
        :param include_nonlinear: Also yield items marked
            ``linear="no"``.
        :param include_toc_pages: Also yield table-of-contents pages.
        :raises InvalidArgumentError: a bad ``start`` or ``until``, or a
            boundary resolved for another book. Raised by the call, not
            the first ``next()``.
        :raises NotEpubError, BookNotDownloadedError,
            DRMProtectedError, AppleBooksError: as for
            :meth:`list_spine_items`, from the call.
        """
        if start is not None and not isinstance(start, TextPosition):
            raise InvalidArgumentError("start must be a TextPosition or None.")
        stop = self._until_position(until)
        spine = self._gated_index().spine
        return self._spine_texts(spine, start or TextPosition(0, 0), stop,
                                 bool(include_nonlinear), bool(include_toc_pages))

    def _until_position(self, until: Any) -> Optional[TextPosition]:
        """``until`` of :meth:`iter_spine_text` as a position (None: the
        end of the book)."""
        if until is None or isinstance(until, TextPosition):
            return until
        if isinstance(until, ResolvedBoundary):
            mine = self._book_id
            theirs = (until.book_id, getattr(until.boundary, "book_id", None))
            if mine is not None and any(b is not None and b != mine for b in theirs):
                raise InvalidArgumentError("This boundary was resolved for another book.")
            if not isinstance(until.position, TextPosition):
                raise InvalidArgumentError("The boundary has no TextPosition.")
            return until.position
        if isinstance(until, ReadBoundary):
            raise InvalidArgumentError(
                "until takes a TextPosition or a ResolvedBoundary: place a ReadBoundary "
                "in the book's text with resolve_boundary() first.")
        raise InvalidArgumentError("until must be a TextPosition, a ResolvedBoundary or None.")

    def _spine_texts(self, spine: Tuple[SpineItem, ...], start: TextPosition,
                     stop: Optional[TextPosition], include_nonlinear: bool,
                     include_toc_pages: bool) -> Iterator[SpineText]:
        for entry in spine:
            if entry.index < start.spine_index:
                continue
            if stop is not None and entry.index > stop.spine_index:
                break
            if not entry.linear and not include_nonlinear:
                continue
            if entry.is_toc_page and not include_toc_pages:
                continue
            lo = start.offset if entry.index == start.spine_index else 0
            hi = stop.offset if stop is not None and entry.index == stop.spine_index else None
            if hi is not None and hi <= lo:
                continue
            text: Optional[str] = None
            if entry.readable:
                try:
                    text = self.get_spine_item_text(entry.index)
                except (BookNotDownloadedError, DRMProtectedError, NotEpubError):
                    raise
                except AppleBooksError:
                    text = None
            if text is None:
                if lo == 0:
                    yield SpineText(entry.index, entry.item_id, entry.href, entry.linear,
                                    entry.is_toc_page, 0, "", 0, readable=False)
                continue
            piece = text[lo:hi]
            if piece:
                yield SpineText(entry.index, entry.item_id, entry.href, entry.linear,
                                entry.is_toc_page, lo, piece, len(text))

    def _gated_index(self) -> "_epub_index._BookIndex":
        """The book's index, after the checks every new content API runs
        (see :func:`py_apple_books._epub_index._gate_path`); raises what
        they find."""
        self._require_epub()
        gated = _epub_index._gate_path(self.path)
        if gated.reason is not None:
            if gated.reason == UnavailableReason.NOT_EPUB:
                self._require_epub()
            raise gated.error
        return gated.index

    def _read_spine_text(self, index: "_epub_index._BookIndex", item_id: str, missing: str) -> str:
        """The text of manifest document ``item_id``, as 1.10's
        :meth:`_spine_item_text` extracts it: from the book this instance
        has loaded, else from the index plus one read of the file."""
        book = self._book if self._book is not None else index.book
        item = book.get_item_with_id(item_id)
        if item is None or not _is_text_media_type(getattr(item, "media_type", None)):
            raise ChapterNotFoundError(missing)
        try:
            with _icloud.no_materialize():
                deferred = getattr(item, "content", None)
                if isinstance(deferred, _epub_index._Deferred):
                    filled = copy.copy(item)
                    filled.content = _read_entry_bytes(self.path, deferred.entry, _MAX_ENTRY_BYTES)
                    item = filled
                html_bytes = item.get_content()
        except AppleBooksError:
            raise
        except Exception as e:
            err = _icloud.not_downloaded_error(e)
            if err is not None:
                raise err from None
            raise AppleBooksError(
                f"Could not read spine entry {quote_name(item_id)}: {detail(e)}"
            ) from e
        return extract_chapter_text(html_bytes, start_anchor=None, stop_anchors=set())

    # -- chapter reading ----------------------------------------------------

    def get_chapter(self, chapter_id: str) -> str:
        """Return the plain-text content of a chapter or sub-section.

        ``chapter_id`` is a manifest item id — i.e. any spine entry in
        the EPUB. Two lookup paths:

        1. **ToC chapter** — the id matches one of :meth:`list_chapters`
           entries. Returns text scoped to that navPoint's fragment
           when the XHTML file hosts multiple navPoints (Project
           Gutenberg layout), so sibling sections don't leak into one
           another.
        2. **Any other spine entry** (sub-sections not in the ToC) —
           falls through to ebooklib's manifest lookup directly and
           returns the whole file's text. This handles EPUBs whose
           spine is finer-grained than the ToC (e.g. a book where each
           chapter has a ``chXX_sub01`` fine-section file that the ToC
           doesn't list).

        HTML → plain text extraction is done with :mod:`bs4` using the
        stdlib ``html.parser`` backend.

        :param chapter_id: Manifest item id from :meth:`list_chapters`
            or from a :class:`~py_apple_books.models.location.Location`.
        :return: Plain text of the chapter, with paragraph breaks
            preserved.
        :raises ChapterNotFoundError: if no ToC chapter or spine entry
            matches ``chapter_id`` (an :class:`AppleBooksError`).
        :raises AppleBooksError: if the book is not an EPUB or the
            chapter can't be read.
        """
        self._require_epub()

        wanted_id = str(chapter_id)

        # Path 1: match a ToC chapter (enables fragment scoping).
        chapters = self.list_chapters()
        match: Optional[Chapter] = None
        for ch in chapters:
            if ch.id == wanted_id or str(ch.order) == wanted_id:
                match = ch
                break

        if match is not None:
            html_bytes = self._read_chapter_bytes(match.href)
            # Other navPoint fragments in the same file become stop
            # anchors so sibling sections don't bleed into one another.
            stop_anchors: Set[str] = {
                ch.fragment
                for ch in chapters
                if ch.href == match.href
                and ch.fragment
                and ch.fragment != match.fragment
            }
            return extract_chapter_text(
                html_bytes,
                start_anchor=match.fragment or None,
                stop_anchors=stop_anchors,
            )

        # Path 2: fall back to raw spine — works for sub-sections that
        # aren't in the ToC. ebooklib's manifest knows every spine item.
        return self._spine_item_text(wanted_id)

    # -- internal helpers ---------------------------------------------------

    def _spine_item_text(self, item_id: str) -> str:
        """Plain text of the whole file behind manifest item ``item_id``,
        with no ToC fragment scoping — :meth:`get_chapter`'s path 2, also
        used to locate annotations by their CFI's manifest id.

        :raises ChapterNotFoundError: if no manifest item has that id.
        :raises AppleBooksError: if the book is not an EPUB or the item's
            content can't be read.
        """
        self._require_epub()
        book = self._load_book()
        item = book.get_item_with_id(item_id)
        if item is None:
            # No method names: the text reaches MCP clients, whose tools
            # are named differently.
            raise ChapterNotFoundError(
                f"No chapter or spine entry with id {quote_name(item_id)} in this "
                f"book. Pass an id from the book's table of contents, or "
                f"a chapter's 1-based order (e.g. \"5\")."
            )
        try:
            with _icloud.no_materialize():
                html_bytes = item.get_content()
        except Exception as e:
            err = _icloud.not_downloaded_error(e)
            if err is not None:
                raise err from None
            raise AppleBooksError(
                f"Could not read spine entry {quote_name(item_id)}: {detail(e)}"
            ) from e
        return extract_chapter_text(
            html_bytes,
            start_anchor=None,
            stop_anchors=set(),
        )

    def _require_epub(self) -> None:
        """Raise :class:`NotEpubError` (an :class:`AppleBooksError`)
        unless the path is an EPUB bundle. The message names the format
        or file name, never the absolute path — it reaches MCP clients
        verbatim."""
        if self.is_epub:
            return
        if self.is_pdf:
            raise NotEpubError(
                "This book is a PDF; chapter listing/reading is only "
                "supported for EPUB books."
            )
        raise NotEpubError(
            f"{quote_title(self.path.name)} is not an EPUB bundle directory; chapter "
            f"listing/reading is only supported for EPUB books."
        )

    def _load_book(self) -> epub.EpubBook:
        """Lazily read and cache the EPUB via ebooklib, confined to the
        bundle (see :class:`_ContainedEpubReader`), with downloads of
        evicted files turned off. Threads sharing the instance read it
        once; the lock is held only while reading."""
        book = self._book
        if book is not None:
            return book
        with self._lock:
            if self._book is None:
                book, container = self._read_book()
                if self._opf_dir_cache is None:
                    self._opf_dir_cache = (
                        _opf_dir_from_container_bytes(container)
                        if container is not None
                        else _opf_dir_from_container(self.path)
                    )
                self._book = book
            return self._book

    def _read_book(self) -> Tuple[epub.EpubBook, Optional[bytes]]:
        """The parsed book, and its ``META-INF/container.xml`` bytes."""
        # Same steps as epub.read_epub(), with the contained reader.
        reader = _ContainedEpubReader(str(self.path))
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
                f"Could not read EPUB {quote_title(self.path.name)}: {detail(e)}"
            ) from e
        return book, reader.container_bytes

    def _opf_dir(self) -> pathlib.PurePosixPath:
        """Cached OPF directory relative to the EPUB bundle root.

        ebooklib's :attr:`EpubItem.file_name` is OPF-relative, while we
        want :attr:`Chapter.href` to be bundle-relative so callers can
        resolve it with ``content.path / chapter.href``. This helper
        gives us the prefix to prepend.

        Set from the ``container.xml`` bytes read with the book; read
        from the bundle only for an instance unpickled from 1.10 state
        without it.
        """
        if self._opf_dir_cache is None:
            self._opf_dir_cache = _opf_dir_from_container(self.path)
        return self._opf_dir_cache

    def _to_bundle_relative(self, opf_relative: str) -> str:
        """Convert an OPF-relative href (ebooklib convention) to a
        bundle-relative path rooted at :attr:`path`."""
        if not opf_relative:
            return opf_relative
        return _to_bundle_relative(self._opf_dir(), opf_relative)

    def _to_opf_relative(self, bundle_relative: str) -> str:
        """Inverse of :meth:`_to_bundle_relative` — strip the OPF-dir
        prefix from a bundle-relative href so ebooklib's internal
        lookups find it."""
        if not bundle_relative:
            return bundle_relative
        opf_dir = self._opf_dir()
        opf_dir_str = str(opf_dir)
        if opf_dir_str in ("", "."):
            return bundle_relative
        prefix = f"{opf_dir_str}/"
        if bundle_relative.startswith(prefix):
            return bundle_relative[len(prefix):]
        return bundle_relative

    def _chapters_from_ebooklib_toc(self, book: epub.EpubBook) -> List[Chapter]:
        """See :func:`_toc_chapters`."""
        return _toc_chapters(book, self._to_bundle_relative)

    def _chapters_from_media_type_ncx(self) -> List[Chapter]:
        """See :func:`_media_type_ncx_chapters`."""
        return _media_type_ncx_chapters(self._load_book(), self._to_bundle_relative)

    def _chapters_from_spine(self, book: epub.EpubBook) -> List[Chapter]:
        """See :func:`_spine_chapters`."""
        return _spine_chapters(book, self._to_bundle_relative)

    def _read_chapter_bytes(self, href: str) -> bytes:
        """Return raw bytes for a chapter file, given a bundle-relative href.

        ebooklib's ``get_item_with_href`` and ``EpubItem.file_name`` use
        the OPF-relative convention, so we strip the OPF-dir prefix
        before asking ebooklib. The disk fallback keeps the
        bundle-relative form so ``self.path / href`` resolves correctly,
        and refuses hrefs that leave the bundle.
        """
        book = self._load_book()
        opf_relative = self._to_opf_relative(href)
        item = book.get_item_with_href(opf_relative)
        if item is None:
            # ebooklib's lookup is exact-match; try a scan.
            for candidate in book.get_items():
                if candidate.file_name == opf_relative:
                    item = candidate
                    break
        if item is not None:
            try:
                return item.get_content()
            except Exception:
                pass  # fall through to disk read

        try:
            with _icloud.no_materialize():
                return _safe_bundle_path(self.path, href).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            raise AppleBooksError(
                f"Chapter file {quote_name(href)} is declared in the EPUB "
                f"manifest but missing on disk."
            ) from None
        except OSError as e:
            err = _icloud.not_downloaded_error(e)
            if err is not None:
                raise err from None
            raise AppleBooksError(
                f"Could not read chapter file {quote_name(href)}: {e.strerror}"
            ) from e


# ---------------------------------------------------------------------------
# Chapter lists (1.10's, shared by BookContent and the book index)
# ---------------------------------------------------------------------------


def _to_bundle_relative(opf_dir: pathlib.PurePosixPath, opf_relative: str) -> str:
    """An OPF-relative href (ebooklib convention) as a path relative to the
    bundle, given the OPF's directory in the bundle."""
    if not opf_relative:
        return opf_relative
    opf_dir_str = str(opf_dir)
    if opf_dir_str in ("", "."):
        return opf_relative
    return f"{opf_dir_str}/{opf_relative}"


def _book_chapters(book: epub.EpubBook, rel: Callable[[str], str]) -> List[Chapter]:
    """1.10's chapter list of a loaded book: its ToC
    (:func:`_toc_chapters`), else the NCX found by media type
    (:func:`_media_type_ncx_chapters`), else one entry per spine item
    (:func:`_spine_chapters`). ``rel`` turns an OPF-relative href into a
    bundle-relative one; it is called only when needed."""
    chapters = _toc_chapters(book, rel)
    if chapters:
        return chapters
    chapters = _media_type_ncx_chapters(book, rel)
    if chapters:
        return chapters
    return _spine_chapters(book, rel)


def _toc_chapters(book: epub.EpubBook, rel: Callable[[str], str]) -> List[Chapter]:
    """Flatten ebooklib's nested ToC into a list of :class:`Chapter`.

    Handles EPUB3 nav documents and EPUB2 NCX transparently (that's
    what we get ebooklib for). Returns an empty list if ebooklib
    couldn't locate a ToC — the caller then tries the stdlib
    fallback.
    """
    toc = book.toc
    if not toc:
        return []

    # Build a lookup from bundle-relative href to manifest item id
    # for stable chapter ids. ebooklib stores file_name as
    # OPF-relative; we normalize to bundle-relative up front so
    # matching works against whichever convention a ToC entry uses.
    href_to_item_id = {}
    for item in book.get_items():
        name = getattr(item, "file_name", None) or getattr(item, "get_name", lambda: "")()
        if name:
            href_to_item_id[rel(name)] = item.get_id()

    chapters: List[Chapter] = []
    order = 0

    # ebooklib sometimes emits duplicate links in the ToC (same href +
    # fragment). Track what we've seen to keep the output tidy.
    seen: Set[Tuple[str, str]] = set()

    def walk(nodes: Iterable[Any], depth: int) -> None:
        nonlocal order
        for node in nodes:
            if isinstance(node, tuple):
                section, children = node
                title = getattr(section, "title", "") or ""
                href = getattr(section, "href", "") or ""
                if title or href:
                    emit(title, href, depth)
                walk(children, depth + 1)
            else:  # epub.Link
                title = getattr(node, "title", "") or ""
                href = getattr(node, "href", "") or ""
                if title or href:
                    emit(title, href, depth)

    def emit(title: str, href: str, depth: int) -> None:
        nonlocal order
        title = title.strip()
        href = href.strip()
        if not title:
            return

        bare, _, fragment = href.partition("#")
        bare = urllib.parse.unquote(bare)
        fragment = urllib.parse.unquote(fragment)

        # ToC hrefs are OPF-relative in ebooklib; normalize to
        # bundle-relative so Chapter.href resolves against the bundle.
        bundle_href = rel(bare)
        # Never advertise an entry pointing outside the bundle —
        # reading it would be refused anyway. An absolute href is
        # checked before the OPF-dir prefix turns it into an
        # in-bundle-looking "OEBPS//abs".
        if bare.startswith("/") or _escapes_bundle(bundle_href):
            return

        key = (bundle_href, fragment)
        if key in seen:
            return
        seen.add(key)

        order += 1
        item_id = href_to_item_id.get(bundle_href, "")
        chapter_id = item_id if item_id else str(order)

        chapters.append(
            Chapter(
                id=chapter_id,
                title=title,
                href=bundle_href,
                fragment=fragment,
                order=order,
                depth=depth,
            )
        )

    walk(toc, 0)

    # When multiple ToC entries in the same file share the same item
    # id (siblings differing only by fragment), fall back to order
    # suffixes so Chapter.id stays unique.
    id_counts = Counter(ch.id for ch in chapters)
    if any(n > 1 for n in id_counts.values()):
        chapters = [
            Chapter(
                id=(str(ch.order) if id_counts[ch.id] > 1 else ch.id),
                title=ch.title,
                href=ch.href,
                fragment=ch.fragment,
                order=ch.order,
                depth=ch.depth,
            )
            for ch in chapters
        ]
    return chapters


def _media_type_ncx_chapters(book: epub.EpubBook, rel: Callable[[str], str]) -> List[Chapter]:
    """Override for EPUBs whose OPF doesn't declare ``<spine toc=…>``
    (real example: *The 4-Hour Workweek*). ebooklib returns an empty
    ToC for these, so we pick the NCX out of the manifest by
    ``media-type`` — ebooklib already parsed the manifest, we just
    ask for the items — and parse the NCX ourselves.

    Returns an empty list if no NCX is found, signaling the caller
    to try the spine-fallback path.
    """
    ncx_item = None
    for item in book.get_items():
        if getattr(item, "media_type", "") == "application/x-dtbncx+xml":
            ncx_item = item
            break
    if ncx_item is None:
        return []

    try:
        ncx_bytes = ncx_item.get_content()
    except Exception:
        return []

    # NCX content srcs resolve relative to the NCX file's location.
    # file_name is OPF-relative, so prepend the OPF dir to get the
    # bundle-relative directory of the NCX file.
    ncx_bundle_path = pathlib.PurePosixPath(rel(ncx_item.file_name))
    return _parse_ncx_bytes(ncx_bytes, ncx_bundle_path.parent)


def _spine_chapters(book: epub.EpubBook, rel: Callable[[str], str]) -> List[Chapter]:
    """Last-resort chapter list from the EPUB spine alone.

    Produces synthetic titles (``Section N``) since there's no
    navigation document to pull real titles from. Follows ebooklib's
    view of the spine, so a comment in it counts as an entry.
    """
    chapters: List[Chapter] = []
    for order, entry in enumerate(book.spine, start=1):
        spine_id = entry[0] if isinstance(entry, tuple) else entry
        item = book.get_item_with_id(spine_id)
        href = rel(item.file_name) if item else ""
        chapters.append(
            Chapter(
                id=spine_id or str(order),
                title=f"Section {order}",
                href=href,
                fragment="",
                order=order,
                depth=0,
            )
        )
    return chapters


# ---------------------------------------------------------------------------
# NCX override (ebooklib blind spot: OPF without <spine toc=…>)
# ---------------------------------------------------------------------------


_NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
_NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"


def _opf_dir_from_container(epub_root: pathlib.Path) -> pathlib.PurePosixPath:
    """Return the OPF file's directory relative to the EPUB bundle root.

    Parses ``META-INF/container.xml`` and reads the ``<rootfile
    full-path="…">`` attribute to locate the OPF, then returns its
    parent directory as a :class:`pathlib.PurePosixPath`.

    Used to normalize ebooklib's manifest item hrefs (which are
    OPF-relative) to bundle-relative paths so :attr:`Chapter.href` and
    the NCX fallback both speak the same convention — callers can
    always do ``bundle_root / chapter.href``. Returns the empty
    PurePosixPath (``PurePosixPath('.')``) when the OPF sits at the
    bundle root, and falls back to empty on any parse error, on an
    unsafe ``container.xml``, or when the OPF path leaves the bundle.
    Since 1.11 a ``container.xml`` that is an iCloud placeholder raises
    :class:`BookNotDownloadedError` instead (it is never downloaded).
    The parsing is :func:`_opf_dir_from_container_bytes`.
    """
    try:
        with _icloud.no_materialize():
            container = _safe_bundle_path(epub_root, "META-INF/container.xml")
            data = container.read_bytes()
    except BookNotDownloadedError:
        raise
    except OSError as e:
        err = _icloud.not_downloaded_error(e)
        if err is not None:
            raise err from None
        return pathlib.PurePosixPath()
    except (AppleBooksError, ValueError):
        return pathlib.PurePosixPath()
    return _opf_dir_from_container_bytes(data)


def _opf_dir_from_container_bytes(data: bytes) -> pathlib.PurePosixPath:
    """The OPF directory named by ``container.xml`` bytes (see
    :func:`_opf_dir_from_container`); empty on any parse error or when
    the OPF path leaves the bundle."""
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError, LookupError):
        # ValueError/LookupError: expat rejects some declared encodings.
        return pathlib.PurePosixPath()
    rootfile = root.find(f".//{{{_NS_CONTAINER}}}rootfile")
    full_path = rootfile.get("full-path") if rootfile is not None else None
    if not full_path or _escapes_bundle(full_path):
        return pathlib.PurePosixPath()
    return pathlib.PurePosixPath(full_path).parent


def _parse_ncx_bytes(
    ncx_bytes: bytes, ncx_dir_in_epub: pathlib.PurePosixPath
) -> List[Chapter]:
    """Parse an NCX ``navMap`` into flattened :class:`Chapter` entries.

    Called only when ebooklib's ToC parse returned empty. ebooklib has
    already pulled the NCX bytes out of the manifest for us; we just
    need to walk the navMap because ebooklib skips this step when the
    spine doesn't declare a ``toc`` idref.

    :param ncx_bytes: Raw NCX XML bytes from
        ``ebooklib`` item ``get_content()``.
    :param ncx_dir_in_epub: Directory of the NCX file relative to the
        EPUB root (as a PurePosixPath). NCX ``content src`` values are
        URIs relative to this directory; we resolve them to EPUB-root-
        relative paths so they match :meth:`BookContent.path` / href.
    """
    try:
        ncx_root = ET.fromstring(ncx_bytes)
    except (ET.ParseError, ValueError, LookupError):
        # ValueError/LookupError: expat rejects some declared encodings.
        return []

    nav_map = ncx_root.find(f"{{{_NS_NCX}}}navMap")
    if nav_map is None:
        return []

    # Some real-world NCX files repeat navPoint ids (observed: 14x "bm1"
    # in *The 4-Hour Workweek*). Count occurrences so we can fall back
    # to ``order`` for collided ids.
    all_np_ids = [
        np.get("id")
        for np in nav_map.findall(f".//{{{_NS_NCX}}}navPoint")
        if np.get("id")
    ]
    np_id_counts = Counter(all_np_ids)

    chapters: List[Chapter] = []
    order = 0

    def walk(nodes, depth: int) -> None:
        nonlocal order
        for np in nodes:
            label_el = np.find(f"{{{_NS_NCX}}}navLabel/{{{_NS_NCX}}}text")
            content_el = np.find(f"{{{_NS_NCX}}}content")
            if label_el is None or content_el is None:
                continue
            title = (label_el.text or "").strip()
            src = (content_el.get("src") or "").strip()
            if not title or not src:
                continue

            bare, _, fragment = src.partition("#")
            bare = urllib.parse.unquote(bare)
            fragment = urllib.parse.unquote(fragment)

            # NCX src is relative to the NCX file; normalize to an
            # EPUB-root-relative path. normpath resolves "../" segments
            # without mangling dot-prefixed names (".x.xhtml").
            href_rel = posixpath.normpath((ncx_dir_in_epub / bare).as_posix())
            if href_rel == ".":
                href_rel = ""
            # Drop entries that climb out of the bundle or are absolute.
            if _escapes_bundle(href_rel):
                continue

            order += 1
            np_id = np.get("id")
            if np_id and np_id_counts[np_id] == 1:
                chapter_id = np_id
            else:
                chapter_id = str(order)

            chapters.append(
                Chapter(
                    id=chapter_id,
                    title=title,
                    href=href_rel,
                    fragment=fragment,
                    order=order,
                    depth=depth,
                )
            )

            walk(np.findall(f"{{{_NS_NCX}}}navPoint"), depth + 1)

    walk(nav_map.findall(f"{{{_NS_NCX}}}navPoint"), 0)
    return chapters


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


def clear_content_cache() -> None:
    """Drop everything cached from book files, process-wide: chapter
    indexes and any other data a module derived from a book's files
    (1.11). Call it after changing a book's files in place; the next
    read reads them again. Thread-safe. :class:`BookContent` instances
    keep the book they already read.
    """
    _icloud.clear_file_caches()


# The book index (container, package and navigation files of a book, kept
# process-wide). Imported last: it builds on the reader and chapter code
# above, and BookContent's methods use it.
from py_apple_books import _epub_index  # noqa: E402
