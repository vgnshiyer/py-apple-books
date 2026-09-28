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
"""

import pathlib
import posixpath
import stat
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Set, Tuple, Union

from ebooklib import epub

from py_apple_books.exceptions import AppleBooksError, UnsafeEpubEntryError
from py_apple_books.utils import extract_chapter_text

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

    Fails open: any filesystem error or unexpected state returns True,
    letting the actual read operation surface a clearer error.

    :param path: Path to a file or directory bundle.
    :return: True if locally available; False if it is an iCloud placeholder
        or does not exist.
    """
    path = pathlib.Path(path)
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


# ---------------------------------------------------------------------------
# Bundle containment (every read from an EPUB bundle goes through here)
# ---------------------------------------------------------------------------


# Upper bound for a single bundle entry. Real books carry fonts and videos
# in the tens of MiB (largest seen: a 22 MiB font), so this only stops a
# hostile entry from exhausting memory — ebooklib reads every manifest
# item eagerly.
_MAX_ENTRY_BYTES = 256 * 1024 * 1024


def _safe_bundle_path(root: pathlib.Path, rel: str) -> pathlib.Path:
    """Resolve a bundle-relative entry name, refusing anything unsafe.

    Symlinks are resolved before the containment check, so a link that
    points outside the bundle is caught just like an absolute or
    ``../`` name. The entry must be a regular file: FIFOs and device
    nodes (``/dev/stdin``, ``/dev/zero``) would block or never end, and
    are rejected from ``stat`` alone, without opening them.

    :param root: The EPUB bundle directory.
    :param rel: Entry name relative to ``root`` (``/``-separated).
    :raises UnsafeEpubEntryError: if the entry escapes the bundle, isn't
        a regular file, or exceeds :data:`_MAX_ENTRY_BYTES`.
    :raises OSError: if the entry can't be stat'ed (e.g.
        :class:`FileNotFoundError` when it doesn't exist).
    """
    try:
        root = root.resolve()
        path = (root / posixpath.normpath(rel)).resolve()
    except (OSError, RuntimeError):  # RuntimeError: symlink loop
        raise UnsafeEpubEntryError(
            f"EPUB entry {rel!r} can't be resolved inside the book bundle."
        ) from None
    if not path.is_relative_to(root):
        raise UnsafeEpubEntryError(
            f"EPUB entry {rel!r} points outside the book bundle."
        )
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeEpubEntryError(
            f"EPUB entry {rel!r} is not a regular file."
        )
    if st.st_size > _MAX_ENTRY_BYTES:
        raise UnsafeEpubEntryError(
            f"EPUB entry {rel!r} is larger than "
            f"{_MAX_ENTRY_BYTES // (1024 * 1024)} MiB."
        )
    return path


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

    def read_file(self, name):
        if isinstance(self.zf, zipfile.ZipFile):
            return super().read_file(name)
        try:
            path = _safe_bundle_path(pathlib.Path(self.file_name), name)
            return path.read_bytes()
        except OSError as e:
            # Name the entry, not the absolute path — messages reach
            # MCP clients verbatim.
            raise AppleBooksError(
                f"Could not read EPUB entry {name!r}: {e.strerror}"
            ) from e


# ---------------------------------------------------------------------------
# DRM detection
# ---------------------------------------------------------------------------


_NS_XMLENC = "http://www.w3.org/2001/04/xmlenc#"

# OCF font obfuscation algorithms. DRM-free publisher EPUBs (InDesign
# exports, for one) list their embedded fonts in encryption.xml under
# these; the text itself stays plain.
_FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",  # IDPF
    "http://ns.adobe.com/pdf/enc#RC",  # Adobe
}


def _encryption_xml_hides_content(bundle: pathlib.Path) -> bool:
    """True if a bundle's ``META-INF/encryption.xml`` encrypts anything
    beyond font obfuscation.

    Fails closed: an unreadable or malformed file, an ``EncryptedData``
    without an algorithm, or a file that's still an iCloud placeholder
    (reading it would trigger a download) all count as encrypted.
    """
    try:
        enc = _safe_bundle_path(bundle, "META-INF/encryption.xml")
        st = enc.stat()
        if st.st_blocks == 0 and st.st_size > 0:
            return True
        root = ET.fromstring(enc.read_bytes())
    except (AppleBooksError, OSError, ET.ParseError):
        return True
    for data in root.iter(f"{{{_NS_XMLENC}}}EncryptedData"):
        method = data.find(f"{{{_NS_XMLENC}}}EncryptionMethod")
        algorithm = method.get("Algorithm") if method is not None else None
        if algorithm not in _FONT_OBFUSCATION_ALGORITHMS:
            return True
    return False


# ---------------------------------------------------------------------------
# Dataclasses (public API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chapter:
    """A single navigable entry in an EPUB's table of contents.

    :param id: Stable identifier suitable for round-trip to
        :meth:`BookContent.get_chapter_content`. Prefers the manifest
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
    """

    id: str
    title: str
    href: str
    fragment: str
    order: int
    depth: int


# ---------------------------------------------------------------------------
# BookContent
# ---------------------------------------------------------------------------


class BookContent:
    """File-level access to a single book in the Apple Books library.

    Wraps a filesystem path with format detection (EPUB bundle directory
    vs single PDF file), iCloud materialization state, DRM detection,
    and — for EPUBs — chapter listing, per-chapter text extraction, and
    substring search.

    Construct via :meth:`PyAppleBooks.get_book_content`, which performs
    the pre-filesystem gate checks. Direct construction from a path is
    also supported for testing.

    :param path: Absolute path to the book file or bundle directory.
    """

    def __init__(self, path: PathLike) -> None:
        self.path = pathlib.Path(path)
        self._book: Optional[epub.EpubBook] = None
        self._opf_dir_cache: Optional[pathlib.PurePosixPath] = None

    def __repr__(self) -> str:
        return f"BookContent(path={str(self.path)!r})"

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
        ``"encryption.xml"``), or None. See :attr:`is_drm_protected`."""
        meta_inf = self.path / "META-INF"
        for name in ("sinf.xml", "rights.xml"):
            if (meta_inf / name).exists():
                return name
        encryption = meta_inf / "encryption.xml"
        if encryption.exists() and _encryption_xml_hides_content(self.path):
            return "encryption.xml"
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

        :raises AppleBooksError: if the book is not an EPUB or ebooklib
            cannot read the package.
        """
        self._require_epub()

        book = self._load_book()

        chapters = self._chapters_from_ebooklib_toc(book)
        if chapters:
            return chapters

        chapters = self._chapters_from_media_type_ncx()
        if chapters:
            return chapters

        return self._chapters_from_spine(book)

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
           spine is finer-grained than the ToC (e.g. Isaacson's *Elon
           Musk*, where each chapter has a ``chXX_sub01`` fine-section
           file that the ToC doesn't list).

        HTML → plain text extraction is done with :mod:`bs4` using the
        stdlib ``html.parser`` backend.

        :param chapter_id: Manifest item id from :meth:`list_chapters`
            or from a :class:`~py_apple_books.models.location.Location`.
        :return: Plain text of the chapter, with paragraph breaks
            preserved.
        :raises AppleBooksError: if the book is not an EPUB or no spine
            entry matches ``chapter_id``.
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
        book = self._load_book()
        item = book.get_item_with_id(wanted_id)
        if item is None:
            raise AppleBooksError(
                f"No chapter or spine entry with id {chapter_id!r}. "
                f"Use list_chapters() to see available ids."
            )
        try:
            html_bytes = item.get_content()
        except Exception as e:
            raise AppleBooksError(
                f"Could not read spine entry {chapter_id!r}: {e}"
            ) from e
        return extract_chapter_text(
            html_bytes,
            start_anchor=None,
            stop_anchors=set(),
        )

    # -- internal helpers ---------------------------------------------------

    def _require_epub(self) -> None:
        """Raise unless the path is an EPUB bundle. The message names
        the format or file name, never the absolute path — it reaches
        MCP clients verbatim."""
        if self.is_epub:
            return
        if self.is_pdf:
            raise AppleBooksError(
                "This book is a PDF; chapter listing/reading is only "
                "supported for EPUB books."
            )
        raise AppleBooksError(
            f"'{self.path.name}' is not an EPUB bundle directory; chapter "
            f"listing/reading is only supported for EPUB books."
        )

    def _load_book(self) -> epub.EpubBook:
        """Lazily read and cache the EPUB via ebooklib, confined to the
        bundle (see :class:`_ContainedEpubReader`)."""
        if self._book is None:
            # Same steps as epub.read_epub(), with the contained reader.
            reader = _ContainedEpubReader(str(self.path))
            try:
                book = reader.load()
                reader.process()
            except AppleBooksError:
                raise
            except Exception as e:
                raise AppleBooksError(
                    f"Could not read EPUB '{self.path.name}': {e}"
                ) from e
            self._book = book
        return self._book

    def _opf_dir(self) -> pathlib.PurePosixPath:
        """Cached OPF directory relative to the EPUB bundle root.

        ebooklib's :attr:`EpubItem.file_name` is OPF-relative, while we
        want :attr:`Chapter.href` to be bundle-relative so callers can
        resolve it with ``content.path / chapter.href``. This helper
        gives us the prefix to prepend.
        """
        if self._opf_dir_cache is None:
            self._opf_dir_cache = _opf_dir_from_container(self.path)
        return self._opf_dir_cache

    def _to_bundle_relative(self, opf_relative: str) -> str:
        """Convert an OPF-relative href (ebooklib convention) to a
        bundle-relative path rooted at :attr:`path`."""
        if not opf_relative:
            return opf_relative
        opf_dir = self._opf_dir()
        opf_dir_str = str(opf_dir)
        if opf_dir_str in ("", "."):
            return opf_relative
        return f"{opf_dir_str}/{opf_relative}"

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
                href_to_item_id[self._to_bundle_relative(name)] = item.get_id()

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
            # bundle-relative so Chapter.href resolves against self.path.
            bundle_href = self._to_bundle_relative(bare)
            # Never advertise an entry pointing outside the bundle —
            # reading it would be refused anyway.
            if _escapes_bundle(bundle_href):
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

    def _chapters_from_media_type_ncx(self) -> List[Chapter]:
        """Override for EPUBs whose OPF doesn't declare ``<spine toc=…>``
        (real example: *The 4-Hour Workweek*). ebooklib returns an empty
        ToC for these, so we pick the NCX out of the manifest by
        ``media-type`` — ebooklib already parsed the manifest, we just
        ask for the items — and parse the NCX ourselves.

        Returns an empty list if no NCX is found, signaling the caller
        to try the spine-fallback path.
        """
        book = self._load_book()
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
        ncx_bundle_path = pathlib.PurePosixPath(
            self._to_bundle_relative(ncx_item.file_name)
        )
        return _parse_ncx_bytes(ncx_bytes, ncx_bundle_path.parent)

    def _chapters_from_spine(self, book: epub.EpubBook) -> List[Chapter]:
        """Last-resort chapter list from the EPUB spine alone.

        Produces synthetic titles (``Section N``) since there's no
        navigation document to pull real titles from.
        """
        chapters: List[Chapter] = []
        for order, entry in enumerate(book.spine, start=1):
            spine_id = entry[0] if isinstance(entry, tuple) else entry
            item = book.get_item_with_id(spine_id)
            href = self._to_bundle_relative(item.file_name) if item else ""
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
            return _safe_bundle_path(self.path, href).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            raise AppleBooksError(
                f"Chapter file {href!r} is declared in the EPUB "
                f"manifest but missing on disk."
            ) from None
        except OSError as e:
            raise AppleBooksError(
                f"Could not read chapter file {href!r}: {e.strerror}"
            ) from e


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
    """
    try:
        container = _safe_bundle_path(epub_root, "META-INF/container.xml")
        root = ET.fromstring(container.read_bytes())
    except (AppleBooksError, OSError, ET.ParseError):
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
    except ET.ParseError:
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




