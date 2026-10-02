"""Unzipped EPUB bundles in the shapes real books come in (provisional API).

:func:`write_epub_bundle` writes an EPUB 3 bundle the way Apple Books
stores one (a folder, not a ``.zip``), byte for byte the same on every
run, with control over the parts that vary between real books: where
the package document lives, which navigation documents exist and how
they are declared, sub-folders, fragments and nesting in the table of
contents, non-linear and repeated spine entries, comments and
processing instructions in the spine, other media types, guide and
landmark references, and raw package metadata.

:func:`~py_apple_books.testing.write_epub` (the demo library's builder)
is separate and unchanged: its output is pinned because the MCP
compatibility goldens depend on it.

Standard library only, like the rest of :mod:`py_apple_books.testing`.
"""

from __future__ import annotations

import pathlib
import posixpath
import re
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import unquote

XHTML = "application/xhtml+xml"
NCX = "application/x-dtbncx+xml"
#: Values of ``nav``: which navigation documents a bundle has.
NAV_MODES = ("both", "nav", "ncx", "ncx-undeclared", "none")
DEFAULT_IDENTIFIER = "urn:uuid:00000000-0000-4000-8000-0000000000aa"

_OPF_NAME = "content.opf"
_NAV_ID, _NAV_HREF = "nav", "nav.xhtml"
_NCX_ID, _NCX_HREF = "ncx", "toc.ncx"
_FILE_OPTIONS = frozenset({"href", "media_type", "linear", "in_spine", "raw", "properties"})
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_MODIFIED = "2026-09-01T00:00:00Z"


def _attr(value: Any) -> str:
    return escape(str(value), quote=True)


def _encode(data: Union[str, bytes]) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"file content must be str or bytes, not {type(data).__name__}")


def _bundle_path(opf_dir: str, href: str, what: str) -> str:
    """The bundle-relative path a manifest ``href`` names (percent-decoded,
    as readers decode it). Raises ValueError for one outside the bundle."""
    if not isinstance(href, str) or not href:
        raise ValueError(f"{what}: href must be a non-empty string")
    path = unquote(href)
    if "\x00" in path or path.startswith("/") or "#" in href:
        raise ValueError(f"{what}: href must be a relative path with no fragment: {href!r}")
    norm = posixpath.normpath(posixpath.join(opf_dir, path))
    if norm in (".", "..") or norm.startswith("../"):
        raise ValueError(f"{what}: href {href!r} would be written outside the bundle")
    return norm


def _relative(href: Optional[str], opf_dir: str, doc_dir: str) -> Optional[str]:
    """``href`` (relative to the OPF folder) as seen from a document in
    ``doc_dir`` (bundle-relative). Unchanged when the document sits in the
    OPF folder, or for an absolute, scheme or fragment-only reference."""
    if href is None or doc_dir == opf_dir:
        return href
    path, sep, fragment = href.partition("#")
    if not path or path.startswith("/") or _SCHEME.match(path):
        return href
    target = posixpath.normpath(posixpath.join(opf_dir, path)).split("/")
    start = [p for p in doc_dir.split("/") if p]
    common = 0
    while common < min(len(target), len(start)) and target[common] == start[common] != "..":
        common += 1
    parts = [".."] * (len(start) - common) + target[common:]
    return "/".join(parts) + sep + fragment


def _toc_entries(toc, where: str = "toc") -> List[Tuple[str, Optional[str], list]]:
    out = []
    for i, entry in enumerate(toc):
        if not isinstance(entry, (tuple, list)) or len(entry) not in (2, 3):
            raise ValueError(f"{where}[{i}] must be (title, href) or (title, href, children)")
        title, href = entry[0], entry[1]
        children = _toc_entries(entry[2], f"{where}[{i}] children") if len(entry) == 3 else []
        if href is not None and not isinstance(href, str):
            raise ValueError(f"{where}[{i}]: href must be a string or None")
        out.append((str(title), href, children))
    return out


def _xhtml(title: str, body: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>{escape(title)}</title></head>"
            f"<body>{body}</body></html>\n")


def _nav_document(toc, landmarks, rel) -> str:
    def items(entries) -> str:
        out = []
        for title, href, children in entries:
            label = (f'<a href="{_attr(rel(href))}">{escape(title)}</a>' if href is not None
                     else f"<span>{escape(title)}</span>")
            out.append(f"<li>{label}{items(children) if children else ''}</li>")
        return "<ol>" + "".join(out) + "</ol>"

    marks = ""
    if landmarks:
        links = "".join(f'<li><a epub:type="{_attr(kind)}" href="{_attr(rel(href))}">{escape(title)}</a></li>'
                        for kind, title, href in landmarks)
        marks = f'<nav epub:type="landmarks" id="landmarks" hidden=""><ol>{links}</ol></nav>'
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            "<head><title>Contents</title></head><body>"
            f'<nav epub:type="toc" id="toc">{items(toc)}</nav>{marks}'
            "</body></html>\n")


def _ncx_document(toc, title: str, identifier: Optional[str]) -> str:
    counter = [0]

    def points(entries) -> str:
        out = []
        for label, href, children in entries:
            counter[0] += 1
            n = counter[0]
            content = f'<content src="{_attr(href)}"/>' if href is not None else ""
            out.append(f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{escape(label)}</text>'
                       f"</navLabel>{content}{points(children) if children else ''}</navPoint>")
        return "".join(out)

    uid = f'<meta name="dtb:uid" content="{_attr(identifier)}"/>' if identifier is not None else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f"<head>{uid}</head><docTitle><text>{escape(title)}</text></docTitle>"
            f"<navMap>{points(toc)}</navMap></ncx>\n")


def write_epub_bundle(dest, files: Sequence[Sequence[Any]], toc: Sequence[Sequence[Any]] = (), *,
                      title: Optional[str] = "Synthetic Book", author: Optional[str] = "Test Author",
                      language: Optional[str] = "en", identifier: Optional[str] = DEFAULT_IDENTIFIER,
                      nav: str = "both", opf_dir: str = "OEBPS",
                      extra_items: Sequence[Sequence[Any]] = (), spine_xml: Optional[str] = None,
                      guide: Sequence[Sequence[str]] = (), landmarks: Sequence[Sequence[str]] = (),
                      metadata_xml: Optional[str] = None) -> pathlib.Path:
    """Write an unzipped EPUB 3 bundle to ``dest`` and return ``dest``.

    The output depends only on the arguments: two calls with the same
    arguments write the same bytes. Nothing is read; files already in
    ``dest`` that the bundle doesn't name are left alone.

    ``files``: the content documents, in spine order. Each is
    ``(manifest_id, body)`` or ``(manifest_id, body, opts)``. ``body`` is
    the inner XHTML of ``<body>`` (a str), wrapped in a minimal XHTML
    document. ``opts`` keys:

    - ``href``: path relative to the package document, default
      ``<manifest_id>.xhtml``; sub-folders and ``..`` are allowed as long
      as the file stays inside the bundle. Percent-escapes name the file
      on disk decoded, as readers decode them (``a%20b.xhtml`` is written
      as ``a b.xhtml``).
    - ``media_type``: default ``application/xhtml+xml``.
    - ``linear``: True (default, no attribute), False (``linear="no"``),
      or a str written as the attribute value verbatim (e.g. ``' NO '``).
    - ``in_spine``: default True; False lists the file in the manifest
      only.
    - ``raw``: default False; True writes ``body`` (str or bytes) as the
      whole file, for ``text/html``, SVG, stylesheets or odd encodings.
    - ``properties``: the manifest ``properties`` attribute, verbatim.
      ``'nav'`` makes this file the navigation document, so the nav is
      in the spine at this file's place: with ``body=None`` the
      generated navigation document (``toc`` and ``landmarks``) is
      written to its ``href`` (``nav`` must then be ``'both'`` or
      ``'nav'``), otherwise ``body`` is used as written. Either way no
      separate ``nav`` item is added.

    ``toc``: nested ``(title, href)`` or ``(title, href, children)``
    entries; ``href`` is relative to the package document's folder like
    a manifest href, may carry a ``#fragment``, and may be None for a
    heading without a link (``<span>`` in the nav, a ``navPoint`` with no
    ``content`` in the NCX). Hrefs are rewritten relative to a nav
    document placed in another folder.

    ``nav``: ``'both'`` (nav document plus an NCX named by the spine's
    ``toc`` attribute), ``'nav'`` (nav document only), ``'ncx'`` (NCX
    only, named by ``toc``), ``'ncx-undeclared'`` (NCX in the manifest
    with no ``toc`` attribute) or ``'none'`` (no table of contents). The
    generated nav document (id ``nav``, ``nav.xhtml``) is in the manifest
    but not the spine; the NCX is ``ncx``, ``toc.ncx``.

    ``opf_dir``: the package document's folder inside the bundle
    (``''``: the bundle root); the package document is
    ``<opf_dir>/content.opf``.

    ``extra_items``: manifest-only items ``(id, href, media_type, data)``
    or ``(id, href, media_type, data, properties)`` such as stylesheets,
    images or fonts. ``data`` is str or bytes; None lists the item
    without writing a file (its href is then written as given, unchecked:
    a missing or out-of-bundle entry).

    ``spine_xml``: raw XML written as the whole content of ``<spine>``
    instead of the generated ``<itemref>`` elements (comments, processing
    instructions, repeated, unknown or ``idref``-less entries).

    ``guide``: OPF 2 guide references ``(type, title, href)``.
    ``landmarks``: ``(epub_type, title, href)`` entries of a landmarks
    nav in the generated navigation document (they need one).

    ``title``, ``author``, ``language`` and ``identifier`` fill
    ``dc:title``, ``dc:creator``, ``dc:language`` and ``dc:identifier``;
    None leaves the element out (and, for ``identifier``, the package's
    ``unique-identifier``). ``metadata_xml`` is raw XML added at the end of
    ``<metadata>``, where the ``dc`` and ``opf`` prefixes are declared.

    Raises ValueError for an unknown ``nav`` or option, a malformed
    entry, a duplicate manifest id, two items written to one file (names
    compared ignoring case, as on the default macOS disk) or inside
    another, or a file that would land outside the bundle; TypeError for
    a body of the wrong type. Nothing is written then.
    """
    if nav not in NAV_MODES:
        raise ValueError(f"nav must be one of {NAV_MODES}, not {nav!r}")
    if not isinstance(opf_dir, str):
        raise ValueError("opf_dir must be a string ('' for the bundle root)")
    opf_dir = posixpath.normpath(opf_dir) if opf_dir.strip("/") else ""
    if opf_dir == ".":
        opf_dir = ""
    if opf_dir.startswith(("/", "../")) or opf_dir == ".." or "\x00" in opf_dir:
        raise ValueError(f"opf_dir must be a folder inside the bundle, not {opf_dir!r}")
    dest = pathlib.Path(dest)
    toc_entries = _toc_entries(toc)
    has_nav_doc = nav in ("both", "nav")
    has_ncx = nav in ("both", "ncx", "ncx-undeclared")

    # -- the documents and manifest items, validated before anything is written
    items: List[Dict[str, Any]] = []
    for i, entry in enumerate(files):
        if not isinstance(entry, (tuple, list)) or len(entry) not in (2, 3):
            raise ValueError(f"files[{i}] must be (manifest_id, body) or (manifest_id, body, opts)")
        item_id, body = entry[0], entry[1]
        opts = dict(entry[2]) if len(entry) == 3 else {}
        unknown = sorted(set(opts) - _FILE_OPTIONS)
        if unknown:
            raise ValueError(f"files[{i}]: unknown options {unknown}; known: {sorted(_FILE_OPTIONS)}")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"files[{i}]: manifest id must be a non-empty string")
        properties = opts.get("properties")
        is_nav = properties is not None and "nav" in str(properties).split()
        raw = bool(opts.get("raw", False))
        if body is None:
            if not is_nav:
                raise TypeError(f"files[{i}]: body is None; only a 'nav' item may leave it out")
            if not has_nav_doc:
                raise ValueError(f"files[{i}]: a generated nav document needs nav='both' or 'nav'")
        elif not raw and not isinstance(body, str):
            raise TypeError(f"files[{i}]: body must be a str (or set raw=True for bytes)")
        linear = opts.get("linear", True)
        if not isinstance(linear, (bool, str)):
            raise ValueError(f"files[{i}]: linear must be a bool or a str")
        href = opts.get("href", f"{item_id}.xhtml")
        items.append({"id": item_id, "href": href, "media_type": opts.get("media_type", XHTML),
                      "properties": properties, "linear": linear,
                      "in_spine": bool(opts.get("in_spine", True)), "raw": raw, "body": body,
                      "is_nav": is_nav, "path": _bundle_path(opf_dir, href, f"files[{i}]")})
    nav_items = [x for x in items if x["is_nav"]]
    if len(nav_items) > 1:
        raise ValueError("only one file may have the 'nav' property")
    own_nav = nav_items[0] if nav_items else None
    generated_nav: Optional[Dict[str, Any]] = None
    if has_nav_doc:
        if own_nav is None:
            generated_nav = {"id": _NAV_ID, "href": _NAV_HREF, "path": _bundle_path(opf_dir, _NAV_HREF, "nav")}
        elif own_nav["body"] is None:
            generated_nav = own_nav
    if landmarks and generated_nav is None:
        raise ValueError("landmarks need a generated nav document (nav='both' or 'nav')")
    landmark_entries = []
    for i, entry in enumerate(landmarks):
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise ValueError(f"landmarks[{i}] must be (epub_type, title, href)")
        landmark_entries.append(tuple(str(v) for v in entry))
    guide_entries = []
    for i, entry in enumerate(guide):
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise ValueError(f"guide[{i}] must be (type, title, href)")
        guide_entries.append(tuple(str(v) for v in entry))

    extras = []
    for i, entry in enumerate(extra_items):
        if not isinstance(entry, (tuple, list)) or len(entry) not in (4, 5):
            raise ValueError(f"extra_items[{i}] must be (id, href, media_type, data[, properties])")
        item_id, href, media_type, data = entry[:4]
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"extra_items[{i}]: id must be a non-empty string")
        if data is not None:
            _encode(data)
        extras.append({"id": item_id, "href": href, "media_type": media_type, "data": data,
                       "properties": entry[4] if len(entry) == 5 else None,
                       "path": _bundle_path(opf_dir, href, f"extra_items[{i}]") if data is not None else None})

    manifest_ids = ([_NCX_ID] if has_ncx else []) + ([_NAV_ID] if generated_nav is not None and own_nav is None else [])
    manifest_ids += [x["id"] for x in items] + [x["id"] for x in extras]
    seen = set()
    for item_id in manifest_ids:
        if item_id in seen:
            raise ValueError(f"manifest id {item_id!r} is used twice")
        seen.add(item_id)

    opf_path = posixpath.join(opf_dir, _OPF_NAME) if opf_dir else _OPF_NAME
    outputs: Dict[str, bytes] = {}
    folded: Dict[str, str] = {}

    def put(path: str, data: Union[str, bytes]) -> None:
        # Compared case-insensitively: the macOS default disk can't keep
        # names apart that differ only in case.
        if path.casefold() in folded:
            raise ValueError(f"two items are written to {path!r} (names compared ignoring case)")
        folded[path.casefold()] = path
        outputs[path] = _encode(data)

    put("mimetype", "application/epub+zip")
    put("META-INF/container.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        f'    <rootfile full-path="{_attr(opf_path)}" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n")

    # -- package document
    meta = []
    if identifier is not None:
        meta.append(f'    <dc:identifier id="id">{escape(identifier)}</dc:identifier>')
    if title is not None:
        meta.append(f"    <dc:title>{escape(title)}</dc:title>")
    if author is not None:
        meta.append(f"    <dc:creator>{escape(author)}</dc:creator>")
    if language is not None:
        meta.append(f"    <dc:language>{escape(language)}</dc:language>")
    meta.append(f'    <meta property="dcterms:modified">{_MODIFIED}</meta>')
    if metadata_xml is not None:
        meta.append(f"    {metadata_xml}")

    def manifest_item(item_id, href, media_type, properties=None) -> str:
        props = f' properties="{_attr(properties)}"' if properties is not None else ""
        return f'    <item id="{_attr(item_id)}" href="{_attr(href)}" media-type="{_attr(media_type)}"{props}/>'

    manifest = []
    if has_ncx:
        manifest.append(manifest_item(_NCX_ID, _NCX_HREF, NCX))
    if generated_nav is not None and own_nav is None:
        manifest.append(manifest_item(_NAV_ID, _NAV_HREF, XHTML, "nav"))
    manifest += [manifest_item(x["id"], x["href"], x["media_type"], x["properties"]) for x in items]
    manifest += [manifest_item(x["id"], x["href"], x["media_type"], x["properties"]) for x in extras]

    spine_toc = f' toc="{_NCX_ID}"' if nav in ("both", "ncx") else ""
    if spine_xml is None:
        refs = []
        for x in items:
            if not x["in_spine"]:
                continue
            linear = x["linear"]
            attr = "" if linear is True else ' linear="no"' if linear is False else f' linear="{_attr(linear)}"'
            refs.append(f'    <itemref idref="{_attr(x["id"])}"{attr}/>')
        spine = f"  <spine{spine_toc}>\n" + "".join(r + "\n" for r in refs) + "  </spine>\n"
    else:
        spine = f"  <spine{spine_toc}>{spine_xml}</spine>\n"
    guide_xml = ""
    if guide_entries:
        guide_xml = ("  <guide>\n" + "".join(
            f'    <reference type="{_attr(t)}" title="{_attr(n)}" href="{_attr(h)}"/>\n'
            for t, n, h in guide_entries) + "  </guide>\n")
    unique = ' unique-identifier="id"' if identifier is not None else ""
    put(opf_path,
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"{unique}>\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n'
        + "".join(m + "\n" for m in meta) +
        "  </metadata>\n"
        "  <manifest>\n" + "".join(m + "\n" for m in manifest) + "  </manifest>\n"
        + spine + guide_xml +
        "</package>\n")

    # -- navigation documents
    if generated_nav is not None:
        nav_dir = posixpath.dirname(generated_nav["path"])
        put(generated_nav["path"],
            _nav_document(toc_entries, landmark_entries, lambda h: _relative(h, opf_dir, nav_dir)))
    if has_ncx:
        put(_bundle_path(opf_dir, _NCX_HREF, "ncx"), _ncx_document(toc_entries, title or "", identifier))

    # -- content
    for x in items:
        if x is generated_nav:
            continue
        put(x["path"], x["body"] if x["raw"] else _xhtml("Synthetic", x["body"]))
    for x in extras:
        if x["data"] is not None:
            put(x["path"], x["data"])

    for key, path in folded.items():
        parts = key.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in folded:
                raise ValueError(f"{path!r} would be written inside the file {folded['/'.join(parts[:i])]!r}")
    for path, data in outputs.items():
        target = dest.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest
