"""Synthetic EPUB bundles for chapter spans (stream 3.2).

Every shape is built with ``write_epub_bundle`` from made-up text. Each
builder takes a destination folder and returns the bundle path;
``SHAPES`` maps a name to its builder, for parametrized tests (together
with ``tests/_epub_shapes.SHAPES``).
"""

from __future__ import annotations

import pathlib
from typing import Callable, Dict

from py_apple_books.testing import write_epub_bundle


def _p(*paragraphs: str) -> str:
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def nested(dest: pathlib.Path) -> pathlib.Path:
    """Parts holding chapters, a chapter over two files, sections inside
    them (orders 1-7: Part One, Chapter 1, S1, S2, Chapter 2, Part Two,
    Chapter 3)."""
    return write_epub_bundle(
        dest / "Nested.epub",
        [("part1", "<h1>Part One</h1>" + _p("part one intro.")),
         ("ch1", "<h2>Chapter 1</h2>" + _p("ch1 text.") + '<h3 id="s1">S1</h3>' + _p("s1 text.")),
         ("ch1b", _p("ch1 continued.") + '<h3 id="s2">S2</h3>' + _p("s2 text.")),
         ("ch2", "<h2>Chapter 2</h2>" + _p("ch2 text.")),
         ("part2", "<h1>Part Two</h1>"),
         ("ch3", "<h2>Chapter 3</h2>" + _p("ch3 text."))],
        toc=[("Part One", "part1.xhtml", [
                ("Chapter 1", "ch1.xhtml", [("S1", "ch1.xhtml#s1"), ("S2", "ch1b.xhtml#s2")]),
                ("Chapter 2", "ch2.xhtml")]),
             ("Part Two", "part2.xhtml", [("Chapter 3", "ch3.xhtml")])])


def same_place(dest: pathlib.Path) -> pathlib.Path:
    """A part and its first chapter at the start of one file (an NCX, which
    keeps both entries), and a part whose chapter's anchor is the file's
    first element."""
    return write_epub_bundle(
        dest / "SamePlace.epub",
        [("p1", "<h1>Part I</h1>" + _p("first chapter text.")),
         ("p2", '<h2 id="c2">Chapter 2</h2>' + _p("second chapter text."))],
        toc=[("Part I", "p1.xhtml", [("Chapter 1", "p1.xhtml")]),
             ("Part II", "p2.xhtml", [("Chapter 2", "p2.xhtml#c2")])],
        nav="ncx-undeclared")


def leading_anchors(dest: pathlib.Path) -> pathlib.Path:
    """Entries anchored before any text of their file: a part anchored at
    the top of its first chapter's file (the chapter listed after it,
    without a fragment), and two empty anchors listed out of order."""
    return write_epub_bundle(
        dest / "Leading.epub",
        [("f1", '<div><h1 id="part">Part</h1></div>' + _p("chapter text.")),
         ("f2", '<a id="x"></a><a id="y"></a>' + _p("second text."))],
        toc=[("Part", "f1.xhtml#part", [("Chapter", "f1.xhtml")]),
             ("Y", "f2.xhtml#y"), ("X", "f2.xhtml#x")])


def nonlinear(dest: pathlib.Path) -> pathlib.Path:
    """A note marked linear="no" between a chapter's two files (listed in
    the ToC), a repeated file, a missing manifest id and an idref-less
    entry in the spine, a comment and a processing instruction."""
    return write_epub_bundle(
        dest / "Nonlinear.epub",
        [("c1", "<h1>One</h1>" + _p("c1 text.")),
         ("notes", _p("note text."), {"linear": False}),
         ("c1b", _p("c1b text.")),
         ("c2", "<h1>Two</h1>" + _p("c2 text.")),
         ("extra", _p("not in the spine."), {"in_spine": False})],
        toc=[("One", "c1.xhtml"), ("Notes", "notes.xhtml"), ("Two", "c2.xhtml")],
        spine_xml=('<itemref idref="c1"/><itemref idref="notes" linear="no"/>'
                   '<!-- <itemref idref="c2"/> --><itemref idref="ghost"/><itemref/>'
                   '<?pi idref="c2"?><itemref idref="c1b"/><itemref idref="c1"/>'
                   '<itemref idref="c2"/>'))


def images(dest: pathlib.Path) -> pathlib.Path:
    """An image-only page and an image file as ToC entries."""
    return write_epub_bundle(
        dest / "Images.epub",
        [("cover", '<div><img src="plate.png" alt=""/></div>'),
         ("plate", b"\x89PNGdata", {"raw": True, "href": "plate.png", "media_type": "image/png"}),
         ("c1", "<h1>One</h1>" + _p("one text."))],
        toc=[("Cover", "cover.xhtml"), ("Plate", "plate.png"), ("One", "c1.xhtml")])


def out_of_order(dest: pathlib.Path) -> pathlib.Path:
    """A ToC whose order differs from the reading order, across files and
    inside one."""
    return write_epub_bundle(
        dest / "OutOfOrder.epub",
        [("a", _p("a text.")),
         ("b", '<h2 id="x">X</h2>' + _p("x text.") + '<h2 id="y">Y</h2>' + _p("y text.")),
         ("c", _p("c text."))],
        toc=[("C", "c.xhtml"), ("Y", "b.xhtml#y"), ("A", "a.xhtml"), ("X", "b.xhtml#x")])


def named_anchors(dest: pathlib.Path) -> pathlib.Path:
    """Fragments that only ``<a name>`` carries, and one whose id comes
    after an ``<a name>`` of the same value."""
    return write_epub_bundle(
        dest / "Named.epub",
        [("g", '<h2><a name="n1"></a>One</h2>' + _p("one text.")
          + '<h2><a name="n2"></a>Two</h2>' + _p("two text.")
          + '<p><a name="n3"></a>decoy</p><h2 id="n3">Three</h2>' + _p("three text."))],
        toc=[("One", "g.xhtml#n1"), ("Two", "g.xhtml#n2"), ("Three", "g.xhtml#n3")])


def missing_fragments(dest: pathlib.Path) -> pathlib.Path:
    """ToC fragments that name no element, in the start file and in a later
    one."""
    return write_epub_bundle(
        dest / "Missing.epub",
        [("m1", "<h1>One</h1>" + _p("one text.") + '<h2 id="b">B</h2>' + _p("b text.")),
         ("m2", _p("two text.")),
         ("m3", "<h1>Three</h1>" + _p("three text."))],
        toc=[("One", "m1.xhtml"), ("Ghost", "m1.xhtml#nowhere"), ("B", "m1.xhtml#b"),
             ("Two", "m2.xhtml#missing"), ("Three", "m3.xhtml")])


def digit_ids(dest: pathlib.Path) -> pathlib.Path:
    """An NCX (found by media type) whose navPoint ids are digits that
    aren't the entries' orders, so they become chapter ids that collide
    with order strings."""
    bundle = write_epub_bundle(
        dest / "DigitIds.epub",
        [("d1", _p("first.")), ("d2", _p("second.")), ("d3", _p("third."))],
        toc=[("First", "d1.xhtml"), ("Second", "d2.xhtml"), ("Third", "d3.xhtml")],
        nav="ncx-undeclared")
    ncx = bundle / "OEBPS" / "toc.ncx"
    data = ncx.read_text(encoding="utf-8")
    for old, new in (('id="np1"', 'id="3"'), ('id="np2"', 'id="1"'), ('id="np3"', 'id="2"')):
        data = data.replace(old, new)
    ncx.write_text(data, encoding="utf-8")
    return bundle


def unicode_text(dest: pathlib.Path) -> pathlib.Path:
    """Soft hyphens, zero-width spaces and decomposed accents."""
    return write_epub_bundle(
        dest / "Unicode.epub",
        [("u1", _p("co­op​ era", "café")), ("u2", _p("next."))],
        toc=[("U", "u1.xhtml")])


SHAPES: Dict[str, Callable[[pathlib.Path], pathlib.Path]] = {
    "nested": nested,
    "same_place": same_place,
    "leading_anchors": leading_anchors,
    "nonlinear": nonlinear,
    "images": images,
    "out_of_order": out_of_order,
    "named_anchors": named_anchors,
    "missing_fragments": missing_fragments,
    "digit_ids": digit_ids,
    "unicode_text": unicode_text,
}
