"""Tests for ``py_apple_books.testing.write_epub_bundle`` (stream 1.4):
every bundle shape the 1.11 content tests build, checked by reading the
written files and by loading them with ``BookContent``. Synthetic text
only.
"""

import hashlib
import importlib.util
import json
import os
import pathlib
import posixpath
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
from ebooklib import epub

from py_apple_books.content import BookContent
from py_apple_books.testing import write_epub, write_epub_bundle
from py_apple_books.testing.epub import DEFAULT_IDENTIFIER, NAV_MODES

REPO = pathlib.Path(__file__).resolve().parent.parent
OPF_NS = "{http://www.idpf.org/2007/opf}"

TWO_FILES = [
    ("c1", "<h1>One</h1><p>First synthetic text.</p>", {"href": "text/c1.xhtml"}),
    ("c2", "<h1>Two</h1><p>Second synthetic text.</p>", {"href": "text/c2.xhtml"}),
]
TWO_TOC = [("Chapter One", "text/c1.xhtml"), ("Chapter Two", "text/c2.xhtml")]


def tree(root: pathlib.Path) -> dict:
    """``{bundle-relative path: bytes}`` of every file under ``root``."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def tree_hash(root: pathlib.Path) -> str:
    h = hashlib.sha256()
    for rel, data in tree(root).items():
        h.update(rel.encode() + b"\0" + data + b"\0")
    return h.hexdigest()


def opf(bundle: pathlib.Path, opf_dir: str = "OEBPS") -> ET.Element:
    path = bundle / opf_dir / "content.opf" if opf_dir else bundle / "content.opf"
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    return ET.fromstring(path.read_bytes(), parser=parser)


def manifest(root: ET.Element) -> dict:
    return {i.get("id"): (i.get("href"), i.get("media-type"), i.get("properties"))
            for i in root.find(f"{OPF_NS}manifest")}


def spine(root: ET.Element) -> ET.Element:
    return root.find(f"{OPF_NS}spine")


def load(bundle: pathlib.Path) -> epub.EpubBook:
    return BookContent(bundle)._load_book()


def chapters(bundle: pathlib.Path) -> list:
    return [(c.id, c.title, c.href, c.fragment, c.depth) for c in BookContent(bundle).list_chapters()]


# -- navigation variants ------------------------------------------------------


@pytest.mark.parametrize("opf_dir", ["OEBPS", ""])
@pytest.mark.parametrize("nav", NAV_MODES)
def test_nav_variants_load_with_book_content(tmp_path, nav, opf_dir):
    bundle = write_epub_bundle(tmp_path / "b.epub", TWO_FILES, TWO_TOC, nav=nav, opf_dir=opf_dir)
    prefix = f"{opf_dir}/" if opf_dir else ""
    files = tree(bundle)
    assert ("META-INF/container.xml" in files and f"{prefix}content.opf" in files
            and f"{prefix}text/c1.xhtml" in files and f"{prefix}text/c2.xhtml" in files)
    assert (f"{prefix}nav.xhtml" in files) == (nav in ("both", "nav"))
    assert (f"{prefix}toc.ncx" in files) == (nav in ("both", "ncx", "ncx-undeclared"))
    root = opf(bundle, opf_dir)
    assert spine(root).get("toc") == ("ncx" if nav in ("both", "ncx") else None)
    assert [i.get("idref") for i in spine(root)] == ["c1", "c2"]  # the nav is not in the spine

    content = BookContent(bundle)
    found = content.list_chapters()
    hrefs = [f"{prefix}text/c1.xhtml", f"{prefix}text/c2.xhtml"]
    assert [c.href for c in found] == hrefs
    if nav == "none":  # 1.10's spine fallback
        assert [(c.id, c.title) for c in found] == [("c1", "Section 1"), ("c2", "Section 2")]
    else:
        assert [c.title for c in found] == ["Chapter One", "Chapter Two"]
    assert [content.get_chapter(c.id) for c in found] == [
        "One\n\nFirst synthetic text.", "Two\n\nSecond synthetic text."]
    assert content.get_chapter("c2") == "Two\n\nSecond synthetic text."


def test_container_points_at_the_package_document(tmp_path):
    for opf_dir, full in (("OEBPS", "OEBPS/content.opf"), ("", "content.opf"), ("a/b/", "a/b/content.opf")):
        bundle = write_epub_bundle(tmp_path / f"x{len(full)}.epub", TWO_FILES, TWO_TOC, opf_dir=opf_dir)
        container = ET.fromstring((bundle / "META-INF" / "container.xml").read_bytes())
        (rootfile,) = container.iter("{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        assert rootfile.get("full-path") == full
        assert (bundle / "mimetype").read_bytes() == b"application/epub+zip"
        assert [c.title for c in BookContent(bundle).list_chapters()] == ["Chapter One", "Chapter Two"]


def test_toc_nesting_fragments_and_headings(tmp_path):
    body = '<h1 id="a">A</h1><p>Alpha text.</p><h2 id="b">B</h2><p>Beta text.</p>'
    toc = [("Part", None, [("Alpha", "text/c1.xhtml#a"), ("Beta", "text/c1.xhtml#b", [("Two", "text/c2.xhtml")])])]
    files = [("c1", body, {"href": "text/c1.xhtml"}), TWO_FILES[1]]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, toc, nav="ncx")
    ncx = ET.fromstring((bundle / "OEBPS" / "toc.ncx").read_bytes())
    ns = "{http://www.daisy.org/z3986/2005/ncx/}"
    points = list(ncx.iter(f"{ns}navPoint"))
    assert [p.get("playOrder") for p in points] == ["1", "2", "3", "4"]
    assert [p.find(f"{ns}content") is None for p in points] == [True, False, False, False]
    assert [c[1:] for c in chapters(bundle)][1:] == [
        ("Alpha", "OEBPS/text/c1.xhtml", "a", 1), ("Beta", "OEBPS/text/c1.xhtml", "b", 1),
        ("Two", "OEBPS/text/c2.xhtml", "", 2)]
    # The same ToC in the nav document: a heading is a <span>, children nest.
    nav_bundle = write_epub_bundle(tmp_path / "n.epub", files, toc, nav="nav")
    text = (nav_bundle / "OEBPS" / "nav.xhtml").read_text()
    assert ('<li><span>Part</span><ol><li><a href="text/c1.xhtml#a">Alpha</a></li>'
            '<li><a href="text/c1.xhtml#b">Beta</a><ol><li><a href="text/c2.xhtml">Two</a></li></ol></li>'
            '</ol></li>') in text
    content = BookContent(nav_bundle)
    beta = next(c for c in content.list_chapters() if c.title == "Beta")
    assert content.get_chapter(beta.id) == "B\n\nBeta text."


# -- spine shapes -------------------------------------------------------------


def test_spine_with_a_comment_a_pi_and_an_idref_less_itemref(tmp_path):
    spine_xml = ('<!-- a comment --><itemref idref="c1"/><?pi data?><itemref/>'
                 '<itemref idref="c2" linear=" NO "/>')
    bundle = write_epub_bundle(tmp_path / "b.epub", TWO_FILES, TWO_TOC, spine_xml=spine_xml)
    text = (bundle / "OEBPS" / "content.opf").read_text()
    assert f'<spine toc="ncx">{spine_xml}</spine>' in text
    children = list(spine(opf(bundle)))
    assert [c.tag if isinstance(c.tag, str) else c.tag.__name__ for c in children] == [
        "Comment", f"{OPF_NS}itemref", "ProcessingInstruction", f"{OPF_NS}itemref", f"{OPF_NS}itemref"]
    book = load(bundle)
    assert [entry for entry in book.spine if entry[0]] == [("c1", "yes"), ("c2", " NO ")]
    assert [c[1] for c in chapters(bundle)] == ["Chapter One", "Chapter Two"]
    assert BookContent(bundle).get_chapter("c2") == "Two\n\nSecond synthetic text."


def test_a_thousand_repeated_itemrefs(tmp_path):
    bundle = write_epub_bundle(tmp_path / "b.epub", TWO_FILES[:1], nav="none",
                               spine_xml='<itemref idref="c1"/>' * 1000)
    assert len(spine(opf(bundle))) == 1000
    found = BookContent(bundle).list_chapters()
    assert len(found) == 1000 and {c.id for c in found} == {"c1"}
    assert found[-1].title == "Section 1000"


@pytest.mark.parametrize("linear, attribute, ebooklib_value", [
    (True, None, "yes"), (False, "no", "no"), (" NO ", " NO ", " NO "), ("yes", "yes", "yes")])
def test_linear(tmp_path, linear, attribute, ebooklib_value):
    files = [TWO_FILES[0], ("c2", "<p>x</p>", {"href": "text/c2.xhtml", "linear": linear})]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, TWO_TOC)
    assert [i.get("linear") for i in spine(opf(bundle))] == [None, attribute]
    assert load(bundle).spine == [("c1", "yes"), ("c2", ebooklib_value)]


def test_manifest_only_files(tmp_path):
    files = [*TWO_FILES, ("note", "<p>An endnote.</p>", {"in_spine": False})]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, TWO_TOC)
    root = opf(bundle)
    assert manifest(root)["note"] == ("note.xhtml", "application/xhtml+xml", None)
    assert [i.get("idref") for i in spine(root)] == ["c1", "c2"]
    assert BookContent(bundle).get_chapter("note") == "An endnote."


# 1.10 extracts an SVG item's text with bs4's HTML parser, which warns.
@pytest.mark.filterwarnings("ignore:It looks like you're using an HTML parser")
def test_text_html_and_svg_spine_items(tmp_path):
    html = b"<!DOCTYPE html><html><head><title>t</title></head><body><p>Legacy synthetic page.</p></body></html>"
    svg = ('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg">'
           '<text x="0" y="10">Synthetic figure text</text></svg>\n')
    files = [TWO_FILES[0],
             ("legacy", html, {"href": "text/legacy.html", "media_type": "text/html", "raw": True}),
             ("figure", svg, {"href": "img/figure.svg", "media_type": "image/svg+xml", "raw": True})]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, TWO_TOC[:1])
    assert (bundle / "OEBPS" / "text" / "legacy.html").read_bytes() == html
    assert (bundle / "OEBPS" / "img" / "figure.svg").read_text() == svg
    found = manifest(opf(bundle))
    assert found["legacy"][1] == "text/html" and found["figure"][1] == "image/svg+xml"
    assert [i.get("idref") for i in spine(opf(bundle))] == ["c1", "legacy", "figure"]
    content = BookContent(bundle)
    assert content.get_chapter("legacy") == "Legacy synthetic page."
    assert content.get_chapter("figure") == "Synthetic figure text"


def test_raw_bytes_in_another_encoding(tmp_path):
    doc = ('<?xml version="1.0" encoding="ISO-8859-1"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
           '<head><title>t</title></head><body><p>Caf\xe9 synth\xe9tique.</p></body></html>').encode("latin-1")
    bundle = write_epub_bundle(tmp_path / "b.epub", [("c1", doc, {"raw": True})], [("C", "c1.xhtml")])
    assert (bundle / "OEBPS" / "c1.xhtml").read_bytes() == doc
    assert BookContent(bundle).get_chapter("c1").startswith("Caf")


def test_extra_items(tmp_path):
    css = "p { margin: 0 }\n"
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
    bundle = write_epub_bundle(
        tmp_path / "b.epub", TWO_FILES, TWO_TOC,
        extra_items=[("css", "styles/main.css", "text/css", css),
                     ("cover", "images/cover.png", "image/png", png, "cover-image"),
                     ("gone", "missing.xhtml", "application/xhtml+xml", None),
                     ("outside", "../../outside.xhtml", "application/xhtml+xml", None)])
    files = tree(bundle)
    assert files["OEBPS/styles/main.css"] == css.encode() and files["OEBPS/images/cover.png"] == png
    assert "OEBPS/missing.xhtml" not in files
    found = manifest(opf(bundle))
    assert found["css"] == ("styles/main.css", "text/css", None)
    assert found["cover"] == ("images/cover.png", "image/png", "cover-image")
    assert found["gone"][0] == "missing.xhtml" and found["outside"][0] == "../../outside.xhtml"
    assert [i.get("idref") for i in spine(opf(bundle))] == ["c1", "c2"]
    assert not (tmp_path / "outside.xhtml").exists()


def test_percent_escaped_hrefs_name_the_decoded_file(tmp_path):
    files = [("c1", "<p>Spaced synthetic file.</p>", {"href": "text/chapter%201.xhtml"})]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, [("One", "text/chapter%201.xhtml")])
    assert (bundle / "OEBPS" / "text" / "chapter 1.xhtml").is_file()
    assert manifest(opf(bundle))["c1"][0] == "text/chapter%201.xhtml"
    assert BookContent(bundle).get_chapter("c1") == "Spaced synthetic file."


def test_files_outside_the_package_folder(tmp_path):
    files = [("c1", "<p>Beside the package folder.</p>", {"href": "../text/c1.xhtml"})]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, [("One", "../text/c1.xhtml")])
    assert (bundle / "text" / "c1.xhtml").is_file()
    found = BookContent(bundle).list_chapters()
    # (1.10 doesn't normalize a chapter href that leaves the package folder)
    assert [(c.title, posixpath.normpath(c.href)) for c in found] == [("One", "text/c1.xhtml")]
    assert BookContent(bundle).get_chapter("c1") == "Beside the package folder."


# -- navigation document in the spine, guide, landmarks ------------------------


def test_generated_nav_in_the_spine(tmp_path):
    files = [("toc-page", None, {"properties": "nav", "href": "text/toc.xhtml", "linear": False}), *TWO_FILES]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, TWO_TOC,
                               landmarks=[("toc", "Contents", "text/toc.xhtml"),
                                          ("bodymatter", "Start", "text/c1.xhtml#top")])
    root = opf(bundle)
    found = manifest(root)
    assert "nav" not in found and found["toc-page"] == ("text/toc.xhtml", "application/xhtml+xml", "nav")
    assert [(i.get("idref"), i.get("linear")) for i in spine(root)] == [
        ("toc-page", "no"), ("c1", None), ("c2", None)]
    assert not (bundle / "OEBPS" / "nav.xhtml").exists()
    text = (bundle / "OEBPS" / "text" / "toc.xhtml").read_text()
    # Hrefs are relative to the nav document's own folder.
    assert '<a href="c1.xhtml">Chapter One</a>' in text and '<a href="c2.xhtml">Chapter Two</a>' in text
    assert ('<nav epub:type="landmarks" id="landmarks" hidden=""><ol>'
            '<li><a epub:type="toc" href="toc.xhtml">Contents</a></li>'
            '<li><a epub:type="bodymatter" href="c1.xhtml#top">Start</a></li></ol></nav>') in text
    assert [c[1:3] for c in chapters(bundle)] == [("Chapter One", "OEBPS/text/c1.xhtml"),
                                                  ("Chapter Two", "OEBPS/text/c2.xhtml")]
    assert "Chapter One" in BookContent(bundle).get_chapter("toc-page")


def test_nav_hrefs_from_a_nav_outside_the_package_folder(tmp_path):
    files = [("toc-page", None, {"properties": "nav", "href": "../nav/toc.xhtml"}), *TWO_FILES]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, TWO_TOC + [("Far", "../far.xhtml#x")], nav="nav")
    text = (bundle / "nav" / "toc.xhtml").read_text()
    assert '<a href="../OEBPS/text/c1.xhtml">Chapter One</a>' in text
    assert '<a href="../far.xhtml#x">Far</a>' in text
    assert [posixpath.normpath(c[2]) for c in chapters(bundle)] == [
        "OEBPS/text/c1.xhtml", "OEBPS/text/c2.xhtml", "far.xhtml"]


def test_a_hand_written_nav(tmp_path):
    nav_doc = ('<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
               'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>t</title></head><body>'
               '<nav epub:type="toc"><ol><li><a href="text/c2.xhtml">Only Two</a></li></ol></nav></body></html>')
    files = [("mynav", nav_doc, {"properties": "nav", "raw": True, "in_spine": False}), *TWO_FILES]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, TWO_TOC, nav="nav")
    assert (bundle / "OEBPS" / "mynav.xhtml").read_text() == nav_doc
    assert "nav" not in manifest(opf(bundle))
    assert [c[1] for c in chapters(bundle)] == ["Only Two"]


def test_guide(tmp_path):
    bundle = write_epub_bundle(tmp_path / "b.epub", TWO_FILES, TWO_TOC,
                               guide=[("toc", "Contents", "nav.xhtml"), ("text", "Start", "text/c1.xhtml")])
    refs = opf(bundle).find(f"{OPF_NS}guide")
    assert [(r.get("type"), r.get("title"), r.get("href")) for r in refs] == [
        ("toc", "Contents", "nav.xhtml"), ("text", "Start", "text/c1.xhtml")]
    assert load(bundle).guide == [{"href": "nav.xhtml", "title": "Contents", "type": "toc"},
                                  {"href": "text/c1.xhtml", "title": "Start", "type": "text"}]
    assert opf(write_epub_bundle(tmp_path / "plain", TWO_FILES, TWO_TOC)).find(f"{OPF_NS}guide") is None


# -- package metadata ---------------------------------------------------------


def test_default_metadata(tmp_path):
    bundle = write_epub_bundle(tmp_path / "b.epub", TWO_FILES, TWO_TOC)
    book = load(bundle)
    assert book.get_metadata("DC", "title")[0][0] == "Synthetic Book"
    assert book.get_metadata("DC", "creator")[0][0] == "Test Author"
    assert book.get_metadata("DC", "language")[0][0] == "en"
    assert book.get_metadata("DC", "identifier")[0][0] == DEFAULT_IDENTIFIER
    assert opf(bundle).get("unique-identifier") == "id"


def test_metadata_left_out_and_raw_metadata(tmp_path):
    extra = ('<dc:publisher>Synthetic Press</dc:publisher>'
             '<dc:subject>Testing</dc:subject><meta name="calibre:series" content="Synthetic Series"/>')
    bundle = write_epub_bundle(tmp_path / "b.epub", TWO_FILES, TWO_TOC, language=None, title=None,
                               author=None, identifier=None, metadata_xml=extra)
    root = opf(bundle)
    assert root.get("unique-identifier") is None
    meta = root.find(f"{OPF_NS}metadata")
    dc = "{http://purl.org/dc/elements/1.1/}"
    assert [m.tag for m in meta] == [f"{OPF_NS}meta", f"{dc}publisher", f"{dc}subject", f"{OPF_NS}meta"]
    assert extra in (bundle / "OEBPS" / "content.opf").read_text()
    book = load(bundle)
    assert book.get_metadata("DC", "language") == [] and book.get_metadata("DC", "title") == []
    assert book.get_metadata("DC", "publisher")[0][0] == "Synthetic Press"
    assert [c[1] for c in chapters(bundle)] == ["Chapter One", "Chapter Two"]


def test_markup_in_names_is_escaped(tmp_path):
    files = [("c&1", "<p>x</p>", {"href": "a&b.xhtml"})]
    bundle = write_epub_bundle(tmp_path / "b.epub", files, [('Tom & "Jerry" <1>', "a&b.xhtml")],
                               title="A & B <c>", author="O'Hara")
    root = opf(bundle)  # well-formed
    assert manifest(root)["c&1"][0] == "a&b.xhtml"
    assert [c[1] for c in chapters(bundle)] == ['Tom & "Jerry" <1>']
    assert load(bundle).get_metadata("DC", "title")[0][0] == "A & B <c>"


# -- reproducibility and validation --------------------------------------------


def everything(dest: pathlib.Path) -> pathlib.Path:
    return write_epub_bundle(
        dest,
        [("toc-page", None, {"properties": "nav", "href": "text/toc.xhtml", "linear": False}),
         ("c1", '<h1 id="s1">One</h1><p>Synthetic.</p>', {"href": "text/c1.xhtml"}),
         ("c2", b"<html><body><p>Raw synthetic.</p></body></html>",
          {"href": "text/c2.html", "media_type": "text/html", "raw": True, "linear": " NO "}),
         ("notes", "<p>Notes.</p>", {"in_spine": False})],
        [("One", "text/c1.xhtml", [("S1", "text/c1.xhtml#s1")]), ("Two", "text/c2.html")],
        title="Everything", language="fr", extra_items=[("css", "s.css", "text/css", "p{}")],
        guide=[("toc", "Contents", "text/toc.xhtml")], landmarks=[("toc", "Contents", "text/toc.xhtml")],
        metadata_xml="<dc:publisher>P</dc:publisher>")


def test_output_is_byte_identical_across_runs(tmp_path):
    first, second = everything(tmp_path / "a"), everything(tmp_path / "b")
    assert tree(first) == tree(second)
    assert len(tree(first)) == 9


def test_output_is_byte_identical_across_processes(tmp_path):
    code = ("import sys, pathlib; sys.path.insert(0, sys.argv[2]); import tests.test_epub_builder as t; "
            "print(t.tree_hash(t.everything(pathlib.Path(sys.argv[1]))))")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}
    hashes = {subprocess.run([sys.executable, "-c", code, str(tmp_path / str(seed)), str(REPO)],
                             env={**env, "PYTHONHASHSEED": str(seed)}, capture_output=True, text=True,
                             check=True, cwd=REPO).stdout.strip() for seed in (1, 2)}
    assert hashes == {tree_hash(everything(tmp_path / "here"))}


def test_existing_files_are_kept_and_ours_replaced(tmp_path):
    dest = tmp_path / "b.epub"
    (dest / "OEBPS").mkdir(parents=True)
    (dest / "OEBPS" / "content.opf").write_text("stale")
    (dest / "extra.txt").write_text("kept")
    write_epub_bundle(dest, TWO_FILES, TWO_TOC)
    assert (dest / "extra.txt").read_text() == "kept"
    assert (dest / "OEBPS" / "content.opf").read_text().startswith("<?xml")


@pytest.mark.parametrize("kwargs, error", [
    ({"nav": "ncx2"}, ValueError),
    ({"opf_dir": "../up"}, ValueError),
    ({"opf_dir": "/abs"}, ValueError),
    ({"files": [("c1",)]}, ValueError),
    ({"files": [("c1", "<p/>", {"hreff": "x.xhtml"})]}, ValueError),
    ({"files": [("", "<p/>")]}, ValueError),
    ({"files": [("c1", b"<p/>")]}, TypeError),
    ({"files": [("c1", None)]}, TypeError),
    ({"files": [("c1", "<p/>", {"linear": 0})]}, ValueError),
    ({"files": [("c1", "<p/>", {"href": "../../escape.xhtml"})]}, ValueError),
    ({"files": [("c1", "<p/>", {"href": "/abs.xhtml"})]}, ValueError),
    ({"files": [("c1", "<p/>", {"href": "c.xhtml#frag"})]}, ValueError),
    ({"files": [("c1", "<p/>"), ("c1", "<p/>", {"href": "other.xhtml"})]}, ValueError),
    ({"files": [("c1", "<p/>"), ("c2", "<p/>", {"href": "c1.xhtml"})]}, ValueError),
    ({"files": [("c1", "<p/>"), ("c2", "<p/>", {"href": "C1.xhtml"})]}, ValueError),  # case only
    ({"files": [("c1", "<p/>", {"href": "a"}), ("c2", "<p/>", {"href": "A/b.xhtml"})]}, ValueError),
    ({"files": [("nav", "<p/>")]}, ValueError),  # the generated nav's id
    ({"files": [("c1", "<p/>", {"href": "toc.ncx"})]}, ValueError),  # the NCX's file
    ({"files": [("c1", "<p/>", {"href": "content.opf"})]}, ValueError),
    ({"files": [("n", None, {"properties": "nav"})], "nav": "ncx"}, ValueError),
    ({"files": [("n", None, {"properties": "nav"}), ("m", None, {"properties": "nav"})]}, ValueError),
    ({"landmarks": [("toc", "Contents", "nav.xhtml")], "nav": "ncx"}, ValueError),
    ({"landmarks": [("toc", "Contents")]}, ValueError),
    ({"guide": [("toc", "nav.xhtml")]}, ValueError),
    ({"toc": [("Only a title",)]}, ValueError),
    ({"toc": [("T", 5)]}, ValueError),
    ({"extra_items": [("css", "s.css", "text/css")]}, ValueError),
    ({"extra_items": [("css", "../../s.css", "text/css", "p{}")]}, ValueError),
    ({"extra_items": [("c1", "x.css", "text/css", "p{}")]}, ValueError),
    ({"extra_items": [("css", "s.css", "text/css", 5)]}, TypeError),
])
def test_invalid_arguments_write_nothing(tmp_path, kwargs, error):
    kwargs = {"files": [("c1", "<p>x</p>")], **kwargs}
    dest = tmp_path / "b.epub"
    with pytest.raises(error):
        write_epub_bundle(dest, **kwargs)
    assert not dest.exists()


# -- write_epub stays as it was -------------------------------------------------

# sha256 over (relative path, bytes) of every file, recorded from the
# released 1.10.0's write_epub: the demo bundles feed the MCP goldens.
WRITE_EPUB_SHA256 = {
    "default": "17ddb14c872d992683886276c67e3b0541d645607ed1fe3f2309ca6de4871df7",
    "drm_demo": "3abc4def3d8af0a3cd7dce8236a399b3e0f7026845836978a1d44aa6d2db8d66",
}


def test_write_epub_is_unchanged(tmp_path):
    assert tree_hash(write_epub(tmp_path / "a", "Synthetic Book")) == WRITE_EPUB_SHA256["default"]
    drm = write_epub(tmp_path / "b", "Locked Store Book",
                     identifier="urn:uuid:00000000-0000-4000-8000-000000000002")
    assert tree_hash(drm) == WRITE_EPUB_SHA256["drm_demo"]


# -- the testing package stays standard-library only ----------------------------

_STANDALONE = r"""
import importlib.util, json, pathlib, sys, tempfile
before = set(sys.modules)
tdir = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("_pab_testing", tdir / "__init__.py",
                                              submodule_search_locations=[str(tdir)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as tmp:
    lib = module.FixtureLibrary.create(pathlib.Path(tmp) / "home")
    module.seed_demo(lib, pathlib.Path(tmp) / "work")
    module.write_epub_bundle(pathlib.Path(tmp) / "b", [("c1", "<p>x</p>")], [("C", "c1.xhtml")])
    lib.add_series("S", [{"sequence": 1}])
    lib.write_prefs()
    lib.add_book_info_cache([{"asset_id": "A", "title": "T"}])
    module.page_location_blob(3)
print(json.dumps(sorted({name.split(".")[0] for name in set(sys.modules) - before})))
"""


def test_testing_package_uses_only_the_standard_library():
    """Loaded on its own (as tests/mcp_compat/run.py loads it, with no
    ``py_apple_books`` package around it) and used, it imports nothing
    outside the standard library."""
    # The installed package (editable or wheel), not a path under REPO: the
    # dist jobs run a copy of tests/ with no source tree beside it.
    tdir = pathlib.Path(importlib.util.find_spec("py_apple_books.testing").origin).parent
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", _STANDALONE, str(tdir)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    loaded = set(json.loads(proc.stdout.strip().splitlines()[-1]))
    outside = sorted(loaded - set(sys.stdlib_module_names) - {"_pab_testing"})
    assert not outside, f"non-stdlib imports: {outside}"
    assert "py_apple_books" not in loaded
