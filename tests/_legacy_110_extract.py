"""Frozen copy of py-apple-books 1.10.0's HTML → text extraction.

The oracle for ``tests/test_extract_equivalence.py``: 1.11 rewrote
:func:`py_apple_books.utils.extract_chapter_text` as one linear walk
over the parse tree (1.10 inserted newline nodes around every block tag,
which is quadratic in the number of siblings), and its output must stay
byte-identical to this code's on every input.

Do not edit. Everything from ``_SKIP_TAGS`` to ``normalize_whitespace``
is ``py_apple_books/utils.py`` at tag v1.10.0 (commit 0fb2285), copied
verbatim. The only addition is :func:`raw_text`, a test helper below
the copy that runs the same steps without the final
``normalize_whitespace``, so the tests can also compare the text before
normalization (a stricter check, which the 1.11 section and chapter
spans rely on).
"""

from __future__ import annotations

import re
from typing import Optional, Set

from bs4 import BeautifulSoup, CData, NavigableString, Tag


# ---------------------------------------------------------------------------
# Verbatim from py_apple_books/utils.py at v1.10.0
# ---------------------------------------------------------------------------


# Contents of these elements is dropped entirely before extraction.
_SKIP_TAGS = {"script", "style", "head"}

# String node types that count as text — exactly the types bs4's own
# ``get_text()`` keeps. Its other NavigableString subclasses (comments,
# doctypes, processing instructions, ``<rt>``/``<rp>`` ruby annotations,
# ``<template>`` contents) are left out on both extraction paths.
_TEXT_STRING_TYPES = (NavigableString, CData)

# Tags that should introduce a newline before/after their contents so
# paragraph breaks survive ``get_text()``.
_BLOCK_LEVEL_TAGS = {
    "address", "article", "aside", "blockquote", "br", "details", "div",
    "dl", "dd", "dt", "figure", "footer", "header", "hgroup", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr",
    "td", "th", "ul",
}


def extract_chapter_text(
    html_bytes: bytes,
    start_anchor: Optional[str] = None,
    stop_anchors: Optional[Set[str]] = None,
) -> str:
    """Return the plain-text content of a chapter's XHTML bytes.

    If ``start_anchor`` is given, the extracted text starts at the
    element with ``id=start_anchor`` and continues in document order
    until one of the ``stop_anchors`` is encountered — this lets
    multi-section XHTML files (Project Gutenberg layout) be split by
    their NCX fragment anchors without bleed-through.

    When ``start_anchor`` is absent or can't be found, the whole
    ``<body>`` (or whole document if there's no body) is returned.
    """
    stop_anchors = stop_anchors or set()
    soup = BeautifulSoup(html_bytes, "html.parser")

    for tag_name in _SKIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Insert explicit newlines around block-level tags so bs4's own
    # ``get_text()`` produces paragraph-separated output.
    for tag in list(soup.find_all(_BLOCK_LEVEL_TAGS)):
        tag.insert_before("\n")
        tag.insert_after("\n")

    if start_anchor:
        anchor_el = soup.find(id=start_anchor)
        if anchor_el is not None:
            return _text_in_window(soup, anchor_el, stop_anchors)

    root = soup.body if soup.body is not None else soup
    return normalize_whitespace(root.get_text())


def _text_in_window(
    soup: BeautifulSoup, anchor_el: Tag, stop_anchors: Set[str]
) -> str:
    """Collect text in document order from ``anchor_el`` forward,
    stopping at the first element whose ``id`` is in ``stop_anchors``.
    Used by :func:`extract_chapter_text` for fragment scoping.
    """
    collecting = False
    parts: list = []
    for node in soup.descendants:
        if isinstance(node, Tag):
            if node is anchor_el:
                collecting = True
                continue
            if collecting and node.get("id") in stop_anchors:
                break
        elif collecting and type(node) in _TEXT_STRING_TYPES:
            parts.append(str(node))
    return normalize_whitespace("".join(parts))


def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace while preserving paragraph breaks.

    * Runs of horizontal whitespace collapse to a single space.
    * Each line is stripped.
    * Consecutive blank lines collapse to a single blank line
      (paragraph boundary).
    * Leading and trailing whitespace on the overall string is stripped.
    """
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    out: list = []
    prev_blank = True
    for line in lines:
        if line:
            out.append(line)
            prev_blank = False
        elif not prev_blank:
            out.append("")
            prev_blank = True
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Test helper (not 1.10 code)
# ---------------------------------------------------------------------------


def raw_text(
    html_bytes: bytes,
    start_anchor: Optional[str] = None,
    stop_anchors: Optional[Set[str]] = None,
) -> str:
    """1.10's :func:`extract_chapter_text` steps, verbatim, but returning
    the joined text before ``normalize_whitespace``."""
    stop_anchors = stop_anchors or set()
    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag_name in _SKIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in list(soup.find_all(_BLOCK_LEVEL_TAGS)):
        tag.insert_before("\n")
        tag.insert_after("\n")
    if start_anchor:
        anchor_el = soup.find(id=start_anchor)
        if anchor_el is not None:
            collecting = False
            parts: list = []
            for node in soup.descendants:
                if isinstance(node, Tag):
                    if node is anchor_el:
                        collecting = True
                        continue
                    if collecting and node.get("id") in stop_anchors:
                        break
                elif collecting and type(node) in _TEXT_STRING_TYPES:
                    parts.append(str(node))
            return "".join(parts)
    root = soup.body if soup.body is not None else soup
    return root.get_text()
