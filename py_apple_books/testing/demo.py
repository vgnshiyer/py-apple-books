"""A small deterministic demo library covering the read path's edge cases.

:func:`seed_demo` fills a :class:`~py_apple_books.testing.FixtureLibrary`
with books, collections and annotations chosen to exercise every read
tool: reading-status corner cases, Store-series rows, a readable EPUB,
a DRM-protected one, deleted and tombstone annotations, orphans and
search text with quotes and LIKE wildcards. The MCP golden runner
(``tests/mcp_compat/run.py``) and the smoke tests use it.

EPUBs are written as unzipped bundles, the way Books stores them, by
plain file writes so the output is byte-for-byte reproducible.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from html import escape
from typing import Dict, List, Tuple

from .fixture import STORE_SERIES, FixtureLibrary

_UTC = _dt.timezone.utc

DEMO_CHAPTERS: List[Tuple[str, str, List[str]]] = [
    ("chap1", "Chapter 1", [
        "Opening words. a synthetic highlight sits here in chapter 1. Closing words.",
        "A removed synthetic highlight was here once.",
    ]),
    ("chap2", "Chapter 2", [
        "Opening words. Don't panic, it's 100% fine.",
        "Please don’t touch snake_case names. Closing words.",
    ]),
]


def _day(day: int) -> _dt.datetime:
    return _dt.datetime(2026, 9, day, 12, 0, tzinfo=_UTC)


def _cfi(chapter_id: str, step: int, *, point: bool = False) -> str:
    """A CFI into ``chapter_id`` (spine position ``step``) with Books' bracket hint."""
    if point:
        return f"epubcfi(/6/{step}[{chapter_id}]!/4/4/1:0)"
    return f"epubcfi(/6/{step}[{chapter_id}]!/4/4,/1:0,/1:21)"


def _write(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_epub(dest, title: str, chapters=DEMO_CHAPTERS, *, author: str = "Test Author",
               identifier: str = "urn:uuid:00000000-0000-4000-8000-000000000001") -> pathlib.Path:
    """Write an unzipped EPUB 3 bundle (with an EPUB 2 NCX) to ``dest``.

    ``chapters`` is a list of ``(manifest_id, title, [paragraph, ...])``.
    Returns ``dest``.
    """
    dest = pathlib.Path(dest)
    oebps = dest / "OEBPS"
    (dest / "META-INF").mkdir(parents=True, exist_ok=True)
    oebps.mkdir(parents=True, exist_ok=True)
    _write(dest / "mimetype", "application/epub+zip")
    _write(
        dest / "META-INF" / "container.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        '  </rootfiles>\n'
        '</container>\n')
    manifest = "\n".join(
        f'    <item id="{cid}" href="{cid}.xhtml" media-type="application/xhtml+xml"/>'
        for cid, _, _ in chapters)
    spine = "\n".join(f'    <itemref idref="{cid}"/>' for cid, _, _ in chapters)
    _write(
        oebps / "content.opf",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="id">{escape(identifier)}</dc:identifier>\n'
        f'    <dc:title>{escape(title)}</dc:title>\n'
        f'    <dc:creator>{escape(author)}</dc:creator>\n'
        '    <dc:language>en</dc:language>\n'
        '    <meta property="dcterms:modified">2026-09-01T00:00:00Z</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        f'{manifest}\n'
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        '    <itemref idref="nav" linear="no"/>\n'
        f'{spine}\n'
        '  </spine>\n'
        '</package>\n')
    nav_items = "\n".join(
        f'      <li><a href="{cid}.xhtml">{escape(ctitle)}</a></li>' for cid, ctitle, _ in chapters)
    _write(
        oebps / "nav.xhtml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        f'<head><title>{escape(title)}</title></head>\n'
        '<body>\n'
        '  <nav epub:type="toc" id="toc">\n'
        '    <ol>\n'
        f'{nav_items}\n'
        '    </ol>\n'
        '  </nav>\n'
        '</body>\n'
        '</html>\n')
    nav_points = "\n".join(
        f'    <navPoint id="np{i}" playOrder="{i}">\n'
        f'      <navLabel><text>{escape(ctitle)}</text></navLabel>\n'
        f'      <content src="{cid}.xhtml"/>\n'
        f'    </navPoint>'
        for i, (cid, ctitle, _) in enumerate(chapters, start=1))
    _write(
        oebps / "toc.ncx",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head><meta name="dtb:uid" content="{escape(identifier)}"/></head>\n'
        f'  <docTitle><text>{escape(title)}</text></docTitle>\n'
        '  <navMap>\n'
        f'{nav_points}\n'
        '  </navMap>\n'
        '</ncx>\n')
    for cid, ctitle, paragraphs in chapters:
        body = "\n".join(f"  <p>{escape(p, quote=False)}</p>" for p in paragraphs)
        _write(
            oebps / f"{cid}.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            f'<head><title>{escape(ctitle)}</title></head>\n'
            '<body>\n'
            f'  <h1>{escape(ctitle)}</h1>\n'
            f'{body}\n'
            '</body>\n'
            '</html>\n')
    return dest


def seed_demo(lib: FixtureLibrary, workdir) -> Dict[str, dict]:
    """Fill ``lib`` with the demo library; EPUB bundles go under ``workdir``.

    Returns the ids, grouped as ``{'books': {...}, 'collections': {...},
    'annotations': {...}, 'paths': {...}}``. The content is fixed, so
    ids and output are identical on every run against an empty store.

    Books (by key): ``synthetic`` in progress with a readable EPUB;
    ``finished`` finished at 100%; ``finished_zero`` finished at 0%;
    ``unstarted``; ``series_stack`` a Store-series container that isn't
    owned (``can_redownload=0``); ``owned_series`` a Series-source volume
    with ``can_redownload=1``; ``drm`` a FairPlay-protected EPUB.

    Annotations: a live highlight, a note, an underline, a deleted
    highlight, a reading-position row (type 3), a tombstone, an orphan,
    one on a book without a file, and highlights whose text holds a
    straight apostrophe with ``%`` and a curly apostrophe with ``_``.
    """
    workdir = pathlib.Path(workdir)
    epub = write_epub(workdir / "books" / "Synthetic Book.epub", "Synthetic Book")
    drm = write_epub(workdir / "books" / "Locked Store Book.epub", "Locked Store Book",
                     identifier="urn:uuid:00000000-0000-4000-8000-000000000002")
    _write(
        drm / "META-INF" / "sinf.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<fairplay:sinf xmlns:fairplay="http://itunes.apple.com/ns/epub"/>\n')

    system = lib.seed_system_collections()
    books = {
        "synthetic": lib.add_book(
            "Synthetic Book", genre="Fiction", progress=0.42, last_opened=_day(20),
            created=_day(1), path=epub),
        "finished": lib.add_book(
            "Don't Panic", "Second Author", genre="Science Fiction", finished=True,
            progress=1.0, last_opened=_day(10), created=_day(2)),
        "finished_zero": lib.add_book(
            "Finished At Zero", genre="History", finished=True, progress=0.0,
            last_opened=_day(8), created=_day(3)),
        "unstarted": lib.add_book("Unopened Book", created=_day(4)),
        "series_stack": lib.add_book(
            "Series Stack", genre="Fantasy", content_type=5, data_source=STORE_SERIES,
            state=5, last_opened=_day(26), created=_day(5)),
        "owned_series": lib.add_book(
            "Owned Series Volume", genre="Fantasy", data_source=STORE_SERIES,
            can_redownload=1, last_opened=_day(6), created=_day(6)),
        "drm": lib.add_book(
            "Locked Store Book", "Store Author", progress=0.25, last_opened=_day(12),
            created=_day(7), path=drm),
    }

    shelf = lib.add_collection("Shelf")
    lib.add_to_collection(shelf, books["synthetic"])
    lib.add_to_collection(shelf, books["finished"])
    collections = {
        "shelf": shelf,
        "deleted": lib.add_collection("Deleted Shelf", deleted=True),
        "system": system,
    }

    synthetic = books["synthetic"]
    annotations = {
        "highlight": lib.add_annotation(
            synthetic, "a synthetic highlight", created=_day(21), location=_cfi("chap1", 4)),
        "note": lib.add_annotation(
            synthetic, "Opening words.", kind="note", note="my synthetic note", color="green",
            created=_day(22), location=_cfi("chap2", 6)),
        "underline": lib.add_annotation(
            synthetic, "Closing words.", kind="underline", created=_day(23),
            location=_cfi("chap1", 4)),
        "deleted": lib.add_annotation(
            synthetic, "removed synthetic highlight", deleted=True, created=_day(24),
            location=_cfi("chap1", 4)),
        "position": lib.add_annotation(
            synthetic, None, kind="reading_position", created=_day(25),
            location=_cfi("chap1", 4, point=True)),
        "tombstone": lib.add_annotation(None, None, kind="tombstone"),
        "orphan": lib.add_annotation(
            "ORPHANASSET0000000000000000000000", "orphan synthetic highlight", created=_day(19)),
        "no_file": lib.add_annotation(
            books["finished"], "towel day", color="purple", created=_day(15)),
        "apostrophe": lib.add_annotation(
            synthetic, "Don't panic, it's 100% fine.", color="blue", created=_day(18),
            location=_cfi("chap2", 6)),
        "curly": lib.add_annotation(
            synthetic, "don’t touch snake_case names", color="pink", created=_day(17),
            location=_cfi("chap2", 6)),
    }
    return {
        "books": books,
        "collections": collections,
        "annotations": annotations,
        "paths": {"epub": epub, "drm_epub": drm},
    }
