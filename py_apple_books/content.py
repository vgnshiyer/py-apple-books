"""Access to the full text of non-DRM books in the Apple Books library.

This module handles reading EPUB content from the user's Apple Books library
without triggering unwanted iCloud hydration. Many imported books live in
``~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`` and may be
stored as iCloud placeholders — the inode appears normal but no disk blocks
are allocated until something reads the file.

The helpers here deliberately use only metadata operations (``stat``, ``du``)
to inspect files, so a simple "is this book readable?" check never causes an
unexpected download.
"""

import pathlib
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional, Set, Tuple, Union

from py_apple_books.exceptions import AppleBooksError

PathLike = Union[str, pathlib.Path]


# XML namespaces used by EPUB metadata files.
_NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
_NS_OPF = "http://www.idpf.org/2007/opf"
_NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"


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


class BookContent:
    """File-level access to a single book in the Apple Books library.

    Thin wrapper around a filesystem path that knows how to introspect its
    format (EPUB bundle directory vs single PDF file) and iCloud materialization
    state. Content-reading methods (``list_chapters``, ``get_chapter_content``,
    ``search``) will be added as subsequent steps.

    Construct via :meth:`PyAppleBooks.get_book_content`, which performs the
    pre-filesystem gate checks (path exists in the library DB, book is not a
    DRM-protected Store purchase, file is locally downloaded).

    :param path: Absolute path to the book file or bundle directory. For
        imported books this is typically under
        ``~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents``.
    """

    def __init__(self, path: PathLike) -> None:
        self.path = pathlib.Path(path)

    def __repr__(self) -> str:
        return f"BookContent(path={str(self.path)!r})"

    @property
    def is_epub(self) -> bool:
        """True if the path is an EPUB bundle directory (Apple stores EPUBs
        unzipped on disk)."""
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

    def list_chapters(self) -> List["Chapter"]:
        """Return the book's chapter list in reading order.

        Parses ``META-INF/container.xml`` to locate the OPF package, reads
        the manifest + spine from the OPF, and then extracts titled entries
        from the EPUB2 NCX navigation document (``toc.ncx`` or the file
        referenced by ``<spine toc=...>``).

        Nested navPoints are flattened; their nesting level is preserved in
        :attr:`Chapter.depth`. If no NCX is available (rare — EPUB3-only
        books that ship solely a nav document), the fallback uses spine
        order with synthetic titles; full EPUB3 nav-doc support can be
        added when a real sample shows up.

        :raises AppleBooksError: if the book is not an EPUB or its metadata
            files are malformed.
        """
        if not self.is_epub:
            raise AppleBooksError(
                "list_chapters() is only supported for EPUB bundles; "
                f"path {self.path!s} is not a .epub directory."
            )

        opf_path = _find_opf_path(self.path)
        manifest, spine, ncx_id = _parse_opf(opf_path)
        opf_dir = opf_path.parent

        if ncx_id and ncx_id in manifest:
            ncx_path = (opf_dir / manifest[ncx_id]).resolve()
            if ncx_path.exists():
                return _ncx_to_chapters(
                    ncx_path, manifest, spine, epub_root=self.path
                )

        # Fallback: no usable NCX — synthesize chapters from the spine.
        return _spine_to_chapters(manifest, spine, epub_root=self.path, opf_dir=opf_dir)

    def search_book_content(
        self,
        query: str,
        limit: int = 20,
        case_sensitive: bool = False,
        context_chars: int = 80,
    ) -> List["SearchMatch"]:
        """Find occurrences of ``query`` across every chapter of the book.

        Walks the chapter list in reading order, extracts each chapter's
        plain text via :meth:`get_chapter_content`, and returns every
        substring match with a small window of surrounding context.
        Multiple hits in one chapter produce multiple :class:`SearchMatch`
        entries. Results preserve reading order (by chapter, then by
        position within the chapter), so callers can follow the book's
        structure rather than a relevance ranking.

        :param query: Substring to look for. Empty strings are rejected.
        :param limit: Maximum number of matches to return. Iteration
            stops early once the cap is reached.
        :param case_sensitive: When False (default) the match is
            case-insensitive; the returned ``matched_text`` still reflects
            the original casing from the source.
        :param context_chars: Approximate number of characters of context
            to include on each side of the match. Snapped to whitespace
            boundaries so snippets don't start or end mid-word.
        :raises AppleBooksError: if the book is not an EPUB or the query
            is empty.
        """
        if not self.is_epub:
            raise AppleBooksError(
                "search_book_content() is only supported for EPUB bundles; "
                f"path {self.path!s} is not a .epub directory."
            )
        if not query.strip():
            raise AppleBooksError("query must be a non-empty string.")

        # Build a whitespace-tolerant regex: each run of whitespace in the
        # user's query becomes ``\s+``, so a search for "the new rich"
        # matches across paragraph breaks and line wraps that appear in the
        # extracted text (``"the\n\nnew rich"``). Other characters are
        # escaped literally.
        words = query.split()
        if not words:
            raise AppleBooksError("query must be a non-empty string.")
        pattern = re.compile(
            r"\s+".join(re.escape(w) for w in words),
            flags=0 if case_sensitive else re.IGNORECASE,
        )

        matches: List[SearchMatch] = []
        for ch in self.list_chapters():
            if len(matches) >= limit:
                break
            try:
                text = self.get_chapter_content(ch.id)
            except AppleBooksError:
                # Skip chapters whose underlying file is missing or
                # otherwise unreadable rather than aborting the search.
                continue
            if not text:
                continue

            for m in pattern.finditer(text):
                pos = m.start()
                match_len = m.end() - m.start()
                matches.append(
                    SearchMatch(
                        chapter_id=ch.id,
                        chapter_title=ch.title,
                        matched_text=text[pos : pos + match_len],
                        context=_build_context(text, pos, match_len, context_chars),
                        position=pos,
                    )
                )
                if len(matches) >= limit:
                    break

        return matches

    def get_chapter_content(self, chapter_id: str) -> str:
        """Return the plain-text content of a chapter.

        Looks up ``chapter_id`` against the result of :meth:`list_chapters`.
        The id may match either the :attr:`Chapter.id` (preferred) or the
        1-based :attr:`Chapter.order` rendered as a string (e.g. ``"5"``),
        accommodating both navPoint ids and ordinal references.

        For chapters carrying a fragment (``href#anchor``), the returned
        text is scoped to the region from that anchor up to the next
        fragment anchor that appears in the same file — preventing
        sibling sections in a multi-section XHTML file from leaking into
        one another. Chapters without a fragment return the full file.

        HTML is stripped via a stateful ``html.parser`` walker: block-level
        elements insert paragraph breaks, inline elements join with a
        space, and ``<script>``/``<style>`` contents are excluded.
        Consecutive blank lines are collapsed.

        :param chapter_id: Identifier from :meth:`list_chapters`.
        :return: Plain text of the chapter, with paragraph breaks.
        :raises AppleBooksError: if the book is not an EPUB, no chapter
            matches ``chapter_id``, or the underlying XHTML file cannot
            be read.
        """
        if not self.is_epub:
            raise AppleBooksError(
                "get_chapter_content() is only supported for EPUB bundles; "
                f"path {self.path!s} is not a .epub directory."
            )

        chapters = self.list_chapters()
        if not chapters:
            raise AppleBooksError("Book has no chapters to read.")

        # Accept either Chapter.id or Chapter.order (as string).
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

        xhtml_path = self.path / match.href
        if not xhtml_path.exists():
            raise AppleBooksError(
                f"Chapter file {match.href!r} is declared in the EPUB "
                f"manifest but missing on disk."
            )

        try:
            html_bytes = xhtml_path.read_bytes()
        except OSError as e:
            raise AppleBooksError(
                f"Could not read chapter file {xhtml_path}: {e}"
            ) from e

        # Pick stop-anchors: every *other* fragment in the same file.
        # When a navPoint spans a subtree (e.g. "FIRST PART" with a nested
        # "To Romain Rolland"), the nested fragment becomes a stop anchor,
        # which mimics reader expectations — the nested section reads
        # separately via its own chapter entry.
        stop_anchors: Set[str] = {
            ch.fragment
            for ch in chapters
            if ch.href == match.href and ch.fragment and ch.fragment != match.fragment
        }

        html_text = _decode_html_bytes(html_bytes)
        return _extract_chapter_text(
            html_text,
            start_anchor=match.fragment or None,
            stop_anchors=stop_anchors,
        )

    @property
    def is_drm_protected(self) -> bool:
        """True if the book is DRM-protected and its content cannot be read.

        For EPUB bundles, DRM is indicated by ``META-INF/encryption.xml``
        (Apple Books Store purchases also add ``sinf.xml`` with FairPlay
        license data and ``signatures.xml``). Imported EPUBs never have
        these files.

        Only EPUBs are checked today; PDFs are assumed readable (Apple
        Books doesn't apply DRM to user-imported PDFs, and Store-purchased
        PDFs are rare-to-nonexistent).

        Note: this touches the filesystem (``META-INF/encryption.xml``
        exists check). For iCloud placeholders the stat will work without
        hydrating content, but the caller should gate on
        :attr:`is_downloaded` before trusting this result.
        """
        if not self.is_epub:
            return False
        return (self.path / "META-INF" / "encryption.xml").exists()


@dataclass(frozen=True)
class SearchMatch:
    """A single occurrence of a search query within a book's chapter.

    :param chapter_id: ID of the chapter containing the match (matches
        :attr:`Chapter.id` from the same book's :meth:`list_chapters`).
    :param chapter_title: Human-readable title of the chapter.
    :param matched_text: The actual substring from the source, preserving
        original casing even for case-insensitive searches.
    :param context: Short snippet of surrounding text (roughly
        ``context_chars`` on each side of the match, snapped to
        whitespace), with newlines flattened to spaces and leading/
        trailing ellipses where the snippet was cut.
    :param position: 0-based character offset of the match within the
        chapter's extracted plain text.
    """

    chapter_id: str
    chapter_title: str
    matched_text: str
    context: str
    position: int


@dataclass(frozen=True)
class Chapter:
    """A single navigable entry in an EPUB's table of contents.

    :param id: Stable identifier. Prefers the manifest item id (spine
        ``idref``) when the NCX points at a file in the spine, falls back
        to the NCX ``navPoint`` id otherwise. Safe to round-trip to
        :meth:`BookContent.get_chapter_content` (not yet implemented).
    :param title: Human-readable chapter title from the NCX ``navLabel``.
    :param href: Path relative to the EPUB bundle root, with any URL
        fragment (``#anchor``) stripped. Suitable for
        ``bundle_root / chapter.href``.
    :param fragment: The fragment from the NCX ``<content src="…">``, or
        an empty string if none. Kept separately so callers can anchor
        into a specific section.
    :param order: 1-based position in the NCX navigation tree (or spine,
        for fallback). Monotonically increasing across the flattened list.
    :param depth: Nesting level in the NCX tree. 0 = top-level chapter,
        1 = first-level subsection, etc.
    """

    id: str
    title: str
    href: str
    fragment: str
    order: int
    depth: int


# ---------------------------------------------------------------------------
# EPUB parsing helpers (stdlib XML only)
# ---------------------------------------------------------------------------


def _find_opf_path(epub_root: pathlib.Path) -> pathlib.Path:
    """Locate the OPF package file via ``META-INF/container.xml``.

    Apple Books stores EPUBs in widely varying layouts — Siddhartha ships
    the OPF at ``OEBPS/content.opf``; *The 4-Hour Workweek* puts it at the
    bundle root with a custom name — so the ``<rootfile>`` declaration in
    ``container.xml`` is the only reliable way to find it.
    """
    container = epub_root / "META-INF" / "container.xml"
    if not container.exists():
        raise AppleBooksError(
            f"EPUB at {epub_root} is missing META-INF/container.xml."
        )
    try:
        root = ET.parse(container).getroot()
    except ET.ParseError as e:
        raise AppleBooksError(f"Could not parse {container}: {e}") from e

    rootfile = root.find(f".//{{{_NS_CONTAINER}}}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise AppleBooksError(
            f"{container} does not declare a <rootfile full-path=…>."
        )
    return (epub_root / rootfile.get("full-path")).resolve()


def _parse_opf(opf_path: pathlib.Path) -> Tuple[dict, List[str], Optional[str]]:
    """Parse the OPF package file.

    :return: ``(manifest, spine, ncx_id)``

        * ``manifest`` — dict mapping item ``id`` → ``href`` (as declared
          in the OPF, relative to the OPF file's directory).
        * ``spine`` — list of ``idref`` strings in reading order.
        * ``ncx_id`` — id of the NCX manifest item. Prefers
          ``<spine toc="...">`` when declared; otherwise falls back to
          the manifest item whose ``media-type`` identifies NCX. Some
          real-world EPUBs (e.g. *The 4-Hour Workweek*) omit the
          ``toc`` attribute but still ship an NCX.
    """
    try:
        root = ET.parse(opf_path).getroot()
    except ET.ParseError as e:
        raise AppleBooksError(f"Could not parse OPF {opf_path}: {e}") from e

    manifest = {}
    ncx_from_media_type: Optional[str] = None
    for item in root.findall(f".//{{{_NS_OPF}}}manifest/{{{_NS_OPF}}}item"):
        item_id = item.get("id")
        href = item.get("href")
        if not item_id or not href:
            continue
        manifest[item_id] = href
        if item.get("media-type") == "application/x-dtbncx+xml" and ncx_from_media_type is None:
            ncx_from_media_type = item_id

    spine_el = root.find(f".//{{{_NS_OPF}}}spine")
    spine: List[str] = []
    ncx_id: Optional[str] = None
    if spine_el is not None:
        ncx_id = spine_el.get("toc")
        for itemref in spine_el.findall(f"{{{_NS_OPF}}}itemref"):
            idref = itemref.get("idref")
            if idref:
                spine.append(idref)

    if not ncx_id or ncx_id not in manifest:
        ncx_id = ncx_from_media_type

    return manifest, spine, ncx_id


def _ncx_to_chapters(
    ncx_path: pathlib.Path,
    manifest: dict,
    spine: List[str],
    epub_root: pathlib.Path,
) -> List[Chapter]:
    """Walk an NCX navMap (EPUB2 ToC) and produce flattened Chapter entries."""
    try:
        ncx_root = ET.parse(ncx_path).getroot()
    except ET.ParseError as e:
        raise AppleBooksError(f"Could not parse NCX {ncx_path}: {e}") from e

    nav_map = ncx_root.find(f"{{{_NS_NCX}}}navMap")
    if nav_map is None:
        return _spine_to_chapters(
            manifest, spine, epub_root=epub_root, opf_dir=ncx_path.parent
        )

    ncx_dir = ncx_path.parent

    # Some real-world NCX files contain repeated ``navPoint id=…`` attributes,
    # even though XML IDs must be unique (e.g. *The 4-Hour Workweek*'s NCX has
    # 14 navPoints all named "bm1"). Scan once to count occurrences; only use
    # navPoint ids as Chapter.id when unambiguous.
    from collections import Counter
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
            # NCX src is a URI per EPUB spec — percent-decode before
            # using as a filesystem path. Real-world examples: files
            # with ``!`` in the name appear as ``%21`` in the NCX.
            bare = urllib.parse.unquote(bare)
            fragment = urllib.parse.unquote(fragment)
            target_abs = (ncx_dir / bare).resolve()
            try:
                href_rel = target_abs.relative_to(epub_root.resolve())
            except ValueError:
                # Target escapes the bundle — skip rather than expose outside paths.
                continue

            order += 1

            # Chapter id strategy:
            # 1. navPoint id, if it's unique across the NCX (best for
            #    round-tripping — Project Gutenberg and most modern EPUBs).
            # 2. Otherwise, the 1-based ``order`` rendered as a string.
            #    Stable within one session, easy for Claude/callers to
            #    pass back.
            np_id = np.get("id")
            if np_id and np_id_counts[np_id] == 1:
                chapter_id = np_id
            else:
                chapter_id = str(order)

            chapters.append(
                Chapter(
                    id=chapter_id,
                    title=title,
                    href=str(href_rel),
                    fragment=fragment,
                    order=order,
                    depth=depth,
                )
            )

            walk(np.findall(f"{{{_NS_NCX}}}navPoint"), depth + 1)

    walk(nav_map.findall(f"{{{_NS_NCX}}}navPoint"), 0)
    return chapters


def _spine_to_chapters(
    manifest: dict,
    spine: List[str],
    epub_root: pathlib.Path,
    opf_dir: pathlib.Path,
) -> List[Chapter]:
    """Fallback chapter list when no NCX / nav doc is available.

    Emits one chapter per spine entry with a synthetic title.
    """
    chapters: List[Chapter] = []
    for order, idref in enumerate(spine, start=1):
        href = manifest.get(idref)
        if not href:
            continue
        target_abs = (opf_dir / href).resolve()
        try:
            href_rel = target_abs.relative_to(epub_root.resolve())
        except ValueError:
            continue
        chapters.append(
            Chapter(
                id=idref,
                title=f"Section {order}",
                href=str(href_rel),
                fragment="",
                order=order,
                depth=0,
            )
        )
    return chapters


# ---------------------------------------------------------------------------
# XHTML → plain text extraction
# ---------------------------------------------------------------------------


# Tags that introduce a line break in the extracted text (roughly, HTML's
# block-level elements). Everything else is treated as inline.
_BLOCK_LEVEL_TAGS = {
    "address", "article", "aside", "blockquote", "br", "details", "div",
    "dl", "dd", "dt", "figure", "footer", "header", "hgroup", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr",
    "td", "th", "ul",
}

# Content of these elements is excluded from the extracted text entirely.
# Note: only *container* elements are listed. Void elements like <meta>
# and <link> don't have text content and have no closing tag, so tracking
# depth on them would leak (HTMLParser fires only handle_starttag, not
# handle_endtag, for void elements).
_SKIP_TAGS = {"script", "style", "head"}


def _decode_html_bytes(data: bytes) -> str:
    """Decode an XHTML chapter's bytes to str, honoring an XML declaration
    encoding when present, otherwise falling back to UTF-8.

    EPUB XHTML files usually ship as UTF-8 but can declare ``iso-8859-1``
    (e.g. older Penguin/Random House builds). HTMLParser works on str, so
    we decode once up front rather than letting the parser guess.
    """
    # Look for <?xml ... encoding="..."?> in the first 200 bytes.
    head = data[:200]
    m = re.search(rb'encoding=["\']([A-Za-z0-9_.\-]+)["\']', head)
    encoding = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


class _ChapterTextExtractor(HTMLParser):
    """html.parser-based walker that extracts plain text from a chapter's
    XHTML, optionally scoped to a start/stop anchor window.

    Scoping rules:

    * If ``start_anchor`` is None, collection begins immediately.
    * If ``start_anchor`` is provided, collection begins when an element
      with ``id="start_anchor"`` is entered. If that anchor is never
      found, collection falls back to starting from the document root.
    * Collection ends when an element with an ``id`` in ``stop_anchors``
      is entered (the matching element's content is not emitted).
    """

    def __init__(
        self,
        start_anchor: Optional[str] = None,
        stop_anchors: Optional[Set[str]] = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth = 0
        self._start_anchor = start_anchor
        self._stop_anchors = stop_anchors or set()
        # If no start anchor is requested, start collecting right away.
        self._collecting = start_anchor is None
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        attr_d = dict(attrs)
        anchor_id = attr_d.get("id")

        if anchor_id:
            if not self._collecting and anchor_id == self._start_anchor:
                self._collecting = True
            elif self._collecting and anchor_id in self._stop_anchors:
                self._done = True
                return

        if not self._collecting:
            return

        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_LEVEL_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        # Self-closing tags like <br/> and <hr/>.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._done:
            return
        if not self._collecting:
            return
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag in _BLOCK_LEVEL_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._done or not self._collecting or self._skip_depth > 0:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        if not self._collecting:
            # Start anchor never showed up — fall back to a second pass
            # over the whole document. Callers handle this gracefully by
            # re-running the extractor without a start anchor.
            return ""
        raw = "".join(self._parts)
        return _normalize_whitespace(raw)


def _normalize_whitespace(text: str) -> str:
    """Collapse redundant whitespace while preserving paragraph structure.

    * Collapses runs of spaces/tabs to a single space.
    * Trims each line.
    * Collapses runs of blank lines to a single blank line (paragraph break).
    * Strips leading/trailing whitespace on the overall string.
    """
    # Normalize non-newline whitespace first.
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    out: List[str] = []
    prev_blank = True  # suppress leading blanks
    for line in lines:
        if line:
            out.append(line)
            prev_blank = False
        elif not prev_blank:
            out.append("")
            prev_blank = True
    return "\n".join(out).strip()


def _build_context(
    text: str, match_pos: int, match_len: int, chars: int
) -> str:
    """Extract a whitespace-aware snippet of text around a match.

    Starts ``chars`` characters before the match and ends ``chars``
    after, then widens the boundaries outward to the nearest whitespace
    so snippets don't start or end mid-word. Newlines are flattened to
    spaces and runs of whitespace are collapsed, giving readable
    single-line context. Leading/trailing ellipses indicate when the
    snippet was cut.
    """
    raw_start = max(0, match_pos - chars)
    raw_end = min(len(text), match_pos + match_len + chars)

    # Snap start forward to whitespace so we don't begin mid-word.
    start = raw_start
    if start > 0:
        sp = text.find(" ", raw_start, match_pos)
        if sp != -1:
            start = sp + 1

    # Snap end back to whitespace so we don't end mid-word.
    end = raw_end
    if end < len(text):
        sp = text.rfind(" ", match_pos + match_len, raw_end)
        if sp != -1:
            end = sp

    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _extract_chapter_text(
    html_text: str,
    start_anchor: Optional[str],
    stop_anchors: Set[str],
) -> str:
    """Run :class:`_ChapterTextExtractor` with the given anchor window.

    Falls back to whole-document extraction if ``start_anchor`` is set
    but never encountered in the document (some EPUBs carry NCX anchors
    that reference stale ids).
    """
    extractor = _ChapterTextExtractor(
        start_anchor=start_anchor, stop_anchors=stop_anchors
    )
    extractor.feed(html_text)
    extractor.close()
    text = extractor.get_text()
    if text or start_anchor is None:
        return text
    # Start anchor never matched — retry without scoping so callers always
    # get *something* readable rather than an empty string.
    fallback = _ChapterTextExtractor(start_anchor=None, stop_anchors=set())
    fallback.feed(html_text)
    fallback.close()
    return fallback.get_text()
