"""Access to the full text of non-DRM books in the Apple Books library.

This module reads EPUB content from the user's library without triggering
unwanted iCloud hydration. Many imported books live in
``~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`` and may
be stored as iCloud placeholders — the inode appears normal but no disk
blocks are allocated until something reads the file.

The placeholder check and DRM gates deliberately use only metadata
operations (``stat``, ``du``, filesystem existence) so a "can I read this
book?" check never causes an unexpected download.

For the actual EPUB parsing and HTML-to-text extraction, this module uses
:mod:`ebooklib` and :mod:`bs4` — well-tested third-party libraries that
handle real-world EPUB quirks. A small stdlib fallback kicks in when an
EPUB's OPF omits the ``<spine toc=…>`` attribute (e.g. *The 4-Hour
Workweek*); in that case ebooklib returns an empty ToC, so we detect the
NCX by media-type and parse it ourselves.
"""

import pathlib
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Set, Union

from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import epub

from py_apple_books.exceptions import AppleBooksError

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

        For EPUB bundles, DRM is indicated by ``META-INF/encryption.xml``
        (Apple Books Store purchases also add ``sinf.xml`` with FairPlay
        license data and ``signatures.xml``). Imported EPUBs never have
        these files. Only EPUBs are checked today.
        """
        if not self.is_epub:
            return False
        return (self.path / "META-INF" / "encryption.xml").exists()

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
        if not self.is_epub:
            raise AppleBooksError(
                "list_chapters() is only supported for EPUB bundles; "
                f"path {self.path!s} is not a .epub directory."
            )

        book = self._load_book()

        chapters = self._chapters_from_ebooklib_toc(book)
        if chapters:
            return chapters

        chapters = self._chapters_from_media_type_ncx()
        if chapters:
            return chapters

        return self._chapters_from_spine(book)

    # -- chapter reading ----------------------------------------------------

    def get_chapter_content(self, chapter_id: str) -> str:
        """Return the plain-text content of a chapter.

        Looks up ``chapter_id`` against the result of :meth:`list_chapters`
        (matches either :attr:`Chapter.id` or the 1-based :attr:`Chapter.order`
        rendered as a string). For chapters carrying a fragment, the
        returned text is scoped to the region from that anchor up to
        the next fragment anchor in the same file, preventing sibling
        sections in a multi-section XHTML file from leaking into one
        another.

        HTML → plain text extraction is done with :mod:`bs4` using the
        stdlib ``html.parser`` backend.

        :param chapter_id: Identifier from :meth:`list_chapters`.
        :return: Plain text, with paragraph breaks preserved.
        :raises AppleBooksError: if the book is not an EPUB, no chapter
            matches, or the chapter file cannot be read.
        """
        if not self.is_epub:
            raise AppleBooksError(
                "get_chapter_content() is only supported for EPUB bundles; "
                f"path {self.path!s} is not a .epub directory."
            )

        chapters = self.list_chapters()
        if not chapters:
            raise AppleBooksError("Book has no chapters to read.")

        wanted_id = str(chapter_id)
        match: Optional[Chapter] = None
        for ch in chapters:
            if ch.id == wanted_id or str(ch.order) == wanted_id:
                match = ch
                break
        if match is None:
            raise AppleBooksError(
                f"No chapter matches id {chapter_id!r}. "
                f"Use list_chapters() to see available ids."
            )

        html_bytes = self._read_chapter_bytes(match.href)

        # Every *other* fragment in the same file is a stop anchor, so
        # sibling sections don't leak into one another when parent/child
        # navPoints share an XHTML file (Project Gutenberg style).
        stop_anchors: Set[str] = {
            ch.fragment
            for ch in chapters
            if ch.href == match.href and ch.fragment and ch.fragment != match.fragment
        }

        return _extract_chapter_text(
            html_bytes,
            start_anchor=match.fragment or None,
            stop_anchors=stop_anchors,
        )

    # -- internal helpers ---------------------------------------------------

    def _load_book(self) -> epub.EpubBook:
        """Lazily read and cache the EPUB via ebooklib."""
        if self._book is None:
            try:
                self._book = epub.read_epub(str(self.path))
            except Exception as e:
                raise AppleBooksError(
                    f"Could not read EPUB at {self.path}: {e}"
                ) from e
        return self._book

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

        # Build a lookup from href (with and without fragment) to
        # manifest item id for stable chapter ids.
        href_to_item_id = {}
        for item in book.get_items():
            name = getattr(item, "file_name", None) or getattr(item, "get_name", lambda: "")()
            if name:
                href_to_item_id[name] = item.get_id()

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

            key = (bare, fragment)
            if key in seen:
                return
            seen.add(key)

            order += 1
            item_id = href_to_item_id.get(bare, "")
            chapter_id = item_id if item_id else str(order)

            chapters.append(
                Chapter(
                    id=chapter_id,
                    title=title,
                    href=bare,
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

        ncx_dir_in_epub = pathlib.PurePosixPath(ncx_item.file_name).parent
        return _parse_ncx_bytes(ncx_bytes, ncx_dir_in_epub)

    def _chapters_from_spine(self, book: epub.EpubBook) -> List[Chapter]:
        """Last-resort chapter list from the EPUB spine alone.

        Produces synthetic titles (``Section N``) since there's no
        navigation document to pull real titles from.
        """
        chapters: List[Chapter] = []
        for order, entry in enumerate(book.spine, start=1):
            spine_id = entry[0] if isinstance(entry, tuple) else entry
            item = book.get_item_with_id(spine_id)
            href = item.file_name if item else ""
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
        """Return raw bytes for a chapter file.

        Tries ebooklib's ``get_item_with_href`` first (covers most
        cases); falls back to iterating items and matching on
        ``file_name``; ultimately falls back to reading the file
        directly from disk relative to the bundle root.
        """
        book = self._load_book()
        item = book.get_item_with_href(href)
        if item is None:
            # ebooklib's lookup is exact-match; try a scan.
            for candidate in book.get_items():
                if candidate.file_name == href:
                    item = candidate
                    break
        if item is not None:
            try:
                return item.get_content()
            except Exception:
                pass  # fall through to disk read

        abs_path = self.path / href
        if not abs_path.exists():
            raise AppleBooksError(
                f"Chapter file {href!r} is declared in the EPUB "
                f"manifest but missing on disk."
            )
        try:
            return abs_path.read_bytes()
        except OSError as e:
            raise AppleBooksError(
                f"Could not read chapter file {abs_path}: {e}"
            ) from e


# ---------------------------------------------------------------------------
# NCX override (ebooklib blind spot: OPF without <spine toc=…>)
# ---------------------------------------------------------------------------


_NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"


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
            # EPUB-root-relative path.
            href_rel = (ncx_dir_in_epub / bare).as_posix()
            # PurePosixPath keeps leading "./"; strip it for cleanliness.
            href_rel = href_rel.lstrip("./")

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
# XHTML → plain text extraction (bs4)
# ---------------------------------------------------------------------------


# Tags whose contents should be dropped entirely.
_SKIP_TAGS = {"script", "style", "head"}

# Tags that produce a paragraph break in the extracted text.
_BLOCK_LEVEL_TAGS = {
    "address", "article", "aside", "blockquote", "br", "details", "div",
    "dl", "dd", "dt", "figure", "footer", "header", "hgroup", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr",
    "td", "th", "ul",
}


def _extract_chapter_text(
    html_bytes: bytes,
    start_anchor: Optional[str],
    stop_anchors: Set[str],
) -> str:
    """Extract plain text from an XHTML chapter, scoped to an anchor window.

    Delegates the heavy lifting to :mod:`bs4`:

    * Drops ``<script>`` / ``<style>`` / ``<head>`` outright via
      :meth:`Tag.decompose`.
    * Pre-inserts ``"\\n"`` text nodes around every block-level tag so
      the final :meth:`Tag.get_text` naturally produces paragraph-
      separated output.
    * For fragment-scoped reads, walks only the siblings of the anchor
      element and stops at the first sibling whose subtree contains a
      stop-anchor id.

    Falls back to whole-document extraction when the start anchor is
    set but can't be found in the source — some EPUBs ship stale NCX
    ids, so we'd rather return the full file than nothing.
    """
    soup = BeautifulSoup(html_bytes, "html.parser")

    # Drop ignorable content up front.
    for tag_name in _SKIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Insert paragraph-break markers around block-level tags so bs4's
    # get_text() produces readable output without manual recursion.
    for tag in list(soup.find_all(_BLOCK_LEVEL_TAGS)):
        tag.insert_before("\n")
        tag.insert_after("\n")

    if start_anchor:
        anchor_el = soup.find(id=start_anchor)
        if anchor_el is not None:
            return _text_in_window(soup, anchor_el, stop_anchors)

    root = soup.body if soup.body is not None else soup
    return _normalize_whitespace(root.get_text())


def _text_in_window(
    soup: BeautifulSoup, anchor_el: Tag, stop_anchors: Set[str]
) -> str:
    """Collect text strings in document order from ``anchor_el`` forward,
    stopping at the first element whose id is in ``stop_anchors``.

    Walks :attr:`soup.descendants` (pre-order DFS) rather than the
    sibling chain. This is essential because EPUB section anchors are
    often empty ``<a id="…"/>`` tags *nested inside* a heading or
    paragraph — the real chapter body lives in sibling ``<p>`` tags of
    the outer container, not of the anchor itself. A sibling-only walk
    would never escape the heading and would return nothing.

    Assumes the caller has already pre-inserted ``"\\n"`` text nodes
    around block-level tags so the collected string has natural
    paragraph breaks.
    """
    collecting = False
    parts: List[str] = []
    for node in soup.descendants:
        if isinstance(node, Tag):
            if node is anchor_el:
                collecting = True
                continue
            if collecting and node.get("id") in stop_anchors:
                break
        elif collecting and isinstance(node, NavigableString):
            parts.append(str(node))
    return _normalize_whitespace("".join(parts))


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace while preserving paragraph breaks."""
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    out: List[str] = []
    prev_blank = True
    for line in lines:
        if line:
            out.append(line)
            prev_blank = False
        elif not prev_blank:
            out.append("")
            prev_blank = True
    return "\n".join(out).strip()


