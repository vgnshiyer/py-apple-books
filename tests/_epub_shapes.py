"""Synthetic EPUB bundles in the shapes the book index must handle (stream 2.1).

Every shape is built with ``write_epub_bundle`` from made-up text. Each
builder takes a destination folder and returns the bundle path; ``SHAPES``
maps a name to its builder, for parametrized tests.
"""

from __future__ import annotations

import pathlib
from typing import Callable, Dict

from py_apple_books.testing import write_epub_bundle


def _p(*paragraphs: str) -> str:
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def plain(dest: pathlib.Path) -> pathlib.Path:
    """Three chapters, nav document and NCX, ToC one entry per file."""
    return write_epub_bundle(
        dest / "Plain.epub",
        [("ch1", "<h1>One</h1>" + _p("Alpha  text one.", "more")),
         ("ch2", "<h1>Two</h1>" + _p("Beta text two.")),
         ("ch3", "<h1>Three</h1>" + _p("Gamma &amp; text &lt;three&gt;."))],
        toc=[("One", "ch1.xhtml"), ("Two", "ch2.xhtml"), ("Three", "ch3.xhtml")])


def subfile(dest: pathlib.Path) -> pathlib.Path:
    """A spine file the ToC doesn't list (a chapter's second part)."""
    return write_epub_bundle(
        dest / "Sub.epub",
        [("ch1", _p("first file text.")), ("ch1_sub01", _p("subfile text.")),
         ("ch2", _p("second chapter text."))],
        toc=[("One", "ch1.xhtml"), ("Two", "ch2.xhtml")])


def ncx_fallback(dest: pathlib.Path) -> pathlib.Path:
    """An NCX the spine doesn't name (no ``toc`` attribute) and no nav:
    ebooklib finds no ToC, 1.10 finds the NCX by media type."""
    return write_epub_bundle(
        dest / "NcxFallback.epub",
        [("c1", _p("ncx one.")), ("c2", _p("ncx two."))],
        toc=[("First", "c1.xhtml"), ("Second", "c2.xhtml", [("Second, part", "c2.xhtml#p")])],
        nav="ncx-undeclared")


def ncx_only(dest: pathlib.Path) -> pathlib.Path:
    """An NCX named by the spine, no nav document."""
    return write_epub_bundle(
        dest / "NcxOnly.epub",
        [("a", _p("first ncx.")), ("b", _p("second ncx."))],
        toc=[("A", "a.xhtml"), ("B", "b.xhtml")], nav="ncx")


def nav_in_spine(dest: pathlib.Path) -> pathlib.Path:
    """The navigation document is the first spine item."""
    return write_epub_bundle(
        dest / "NavInSpine.epub",
        [("toc", None, {"properties": "nav", "href": "toc.xhtml"}),
         ("c1", _p("nav spine one.")), ("c2", _p("nav spine two."))],
        toc=[("One", "c1.xhtml"), ("Two", "c2.xhtml")], nav="nav")


def gutenberg(dest: pathlib.Path) -> pathlib.Path:
    """One file holding several ToC entries (fragments), and a large
    image the index must not read."""
    body = ("<h1>Title page</h1>"
            '<h2 id="ch1">I</h2><p>one one</p>'
            '<h2><a id="ch2"/>II</h2><p>two two</p>'
            '<h2 id="ch3">III</h2><p>three</p>')
    return write_epub_bundle(
        dest / "Gutenberg.epub",
        [("body", body)],
        toc=[("I", "body.xhtml#ch1"), ("II", "body.xhtml#ch2"), ("III", "body.xhtml#ch3")],
        extra_items=[("img", "big.png", "image/png", b"\x89PNG" + b"\0" * 300_000)])


def calibre_split(dest: pathlib.Path) -> pathlib.Path:
    """A converter's split files: one chapter spans two files, and a ToC
    entry points into the middle of the first."""
    return write_epub_bundle(
        dest / "Calibre.epub",
        [("index_split_000", '<h1 id="a">Part A</h1>' + _p("a text.")
          + '<h1 id="b">Part B</h1>' + _p("b text begins.")),
         ("index_split_001", _p("b text continues.") + '<h1 id="c">Part C</h1>' + _p("c text."))],
        toc=[("A", "index_split_000.xhtml#a"), ("B", "index_split_000.xhtml#b"),
             ("C", "index_split_001.xhtml#c")])


def phantom_sections(dest: pathlib.Path) -> pathlib.Path:
    """No ToC at all, and a comment and a processing instruction in the
    spine: 1.10 lists one 'Section N' per entry of ebooklib's spine view,
    comment and instruction included."""
    return write_epub_bundle(
        dest / "Phantom.epub",
        [("s1", _p("section one.")), ("s2", _p("section two.")), ("s3", _p("section three."))],
        nav="none",
        spine_xml=('<itemref idref="s1"/><!-- <itemref idref="old"/> -->'
                   '<itemref idref="s2"/><?pi x?><itemref idref="s3"/>'))


def root_opf(dest: pathlib.Path) -> pathlib.Path:
    """The package document at the bundle root, content in a sub-folder."""
    return write_epub_bundle(
        dest / "RootOpf.epub",
        [("c1", _p("root opf one."), {"href": "text/c1.xhtml"}),
         ("c2", _p("root opf two."), {"href": "text/c2.xhtml"})],
        toc=[("One", "text/c1.xhtml"), ("Two", "text/c2.xhtml")], opf_dir="")


def mixed_types(dest: pathlib.Path) -> pathlib.Path:
    """Spine items of other types (HTML, SVG, an image) and manifest-only
    stylesheet, image and font."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
           '<text x="1" y="5">Vector words</text></svg>')
    html = "<html><head><title>t</title></head><body><p>Plain html page.</p></body></html>"
    return write_epub_bundle(
        dest / "Mixed.epub",
        [("c1", _p("xhtml text.")),
         ("page", html, {"raw": True, "href": "page.html", "media_type": "text/html"}),
         ("art", svg, {"raw": True, "href": "art.svg", "media_type": "image/svg+xml"}),
         ("pic", b"\x89PNGdata", {"raw": True, "href": "pic.png", "media_type": "image/png"})],
        toc=[("One", "c1.xhtml"), ("Page", "page.html")],
        extra_items=[("css", "style.css", "text/css", "p { margin: 0 }"),
                     ("font", "f.otf", "font/otf", b"OTTO"),
                     ("cover", "cover.jpg", "image/jpeg", b"\xff\xd8\xff")])


def toc_pages(dest: pathlib.Path) -> pathlib.Path:
    """ToC pages named three ways: the nav document (in the spine), a
    guide reference and a landmarks link; plus a non-linear note."""
    return write_epub_bundle(
        dest / "TocPages.epub",
        [("navdoc", None, {"properties": "nav", "href": "navdoc.xhtml"}),
         ("contents", _p("Contents: One, Two.")),
         ("printed", _p("Printed contents.")),
         ("c1", _p("toc pages one.")),
         ("notes", _p("A note."), {"linear": " NO "}),
         ("c2", _p("toc pages two."))],
        toc=[("One", "c1.xhtml"), ("Two", "c2.xhtml")], nav="nav",
        guide=[("toc", "Contents", "contents.xhtml#top")],
        landmarks=[("toc", "Printed", "printed.xhtml"), ("bodymatter", "Start", "c1.xhtml")])


SHAPES: Dict[str, Callable[[pathlib.Path], pathlib.Path]] = {
    "plain": plain,
    "subfile": subfile,
    "ncx_fallback": ncx_fallback,
    "ncx_only": ncx_only,
    "nav_in_spine": nav_in_spine,
    "gutenberg": gutenberg,
    "calibre_split": calibre_split,
    "phantom_sections": phantom_sections,
    "root_opf": root_opf,
    "mixed_types": mixed_types,
    "toc_pages": toc_pages,
}
