"""Package-internal utilities shared across py_apple_books.

This module is a grab bag of small helpers that don't belong to any
single domain in the package — date conversion, config reads, HTML →
plain-text extraction, whitespace normalization. Keep it as a flat
collection of pure functions; anything with real domain weight
(EPUB-specific parsing, book-access logic) lives in its own module.
"""

from __future__ import annotations

import configparser
import math
import pathlib
import re
from datetime import datetime
from typing import Callable, Dict, Optional, Set, Tuple

from bs4 import BeautifulSoup, CData, NavigableString, Tag


# ---------------------------------------------------------------------------
# Apple timestamp helpers
# ---------------------------------------------------------------------------

# Seconds between Unix epoch (1970-01-01) and Apple/Core Data epoch (2001-01-01)
APPLE_EPOCH_OFFSET = 978307200


def get_mappings(model_name: str) -> dict:
    mappings_path = pathlib.Path(__file__).parent / "mappings.ini"
    config = configparser.ConfigParser()
    config.read(mappings_path)
    return dict(config.items(model_name))


def apple_timestamp_to_datetime(raw):
    """Convert an Apple/Core Data timestamp (seconds since 2001-01-01)
    to a :class:`datetime`."""
    if raw is None:
        return None
    return datetime.fromtimestamp(float(raw) + APPLE_EPOCH_OFFSET)


def _apple_datetime_or_none(raw) -> Optional[datetime]:
    """:func:`apple_timestamp_to_datetime` for model fields, which never
    raises: a corrupt date reads as None instead of failing every list
    the row is in.

    A datetime is returned as is, so the conversion is idempotent (a
    model built from another one's fields keeps its dates). None for
    None, for anything ``float()`` can't convert, for NaN and the
    infinities, and for values outside the range a datetime can hold
    on this platform (``OverflowError``, ``OSError``, ``ValueError``;
    Core Data's distantPast, 0000-12-30, is one). Every other value
    gives exactly what :func:`apple_timestamp_to_datetime` gives (naive
    local time).
    """
    if raw is None or isinstance(raw, datetime):
        return raw
    try:
        seconds = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds):
        return None
    try:
        return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTML → plain text extraction
# ---------------------------------------------------------------------------
#
# EPUBs ship XHTML. ebooklib gives us raw bytes via ``item.get_content()``
# but no text extraction; bs4's ``get_text()`` concatenates everything
# without block-level awareness (``<p>A</p><p>B</p>`` → ``"AB"``). The
# extraction core has three parts, each one pass over the document:
#
# * :func:`_parse` parses the bytes (stdlib ``html.parser`` backend) and
#   drops script, style and head elements with their contents;
# * :func:`_walk` collects the text of a subtree in document order and
#   emits a newline on entering and on leaving each block-level tag, so
#   paragraph breaks survive. It can start at an element and stop at the
#   first tag a predicate picks, which splits Project Gutenberg-style
#   multi-section files cleanly by their NCX fragment anchors;
# * :func:`_anchor_index` maps every fragment id (and ``<a name>``) of a
#   document to its element.
#
# :func:`extract_chapter_text` is composed from them. Its output is
# byte-identical to 1.10's, which inserted newline nodes into the tree
# around every block-level tag before calling bs4's ``get_text()``: that
# mutated the tree and took quadratic time in the number of sibling tags.
# ``tests/test_extract_equivalence.py`` pins the equivalence against a
# frozen copy of the 1.10 code.


# Contents of these elements is dropped entirely before extraction.
_SKIP_TAGS = {"script", "style", "head"}

# String node types that count as text — exactly the types bs4's own
# ``get_text()`` keeps. Its other NavigableString subclasses (comments,
# doctypes, processing instructions, ``<rt>``/``<rp>`` ruby annotations,
# ``<template>`` contents) are left out on both extraction paths.
_TEXT_STRING_TYPES = (NavigableString, CData)

# Tags that introduce a newline before and after their contents so
# paragraph breaks survive extraction.
_BLOCK_LEVEL_TAGS = {
    "address", "article", "aside", "blockquote", "br", "details", "div",
    "dl", "dd", "dt", "figure", "footer", "header", "hgroup", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr",
    "td", "th", "ul",
}

# Sentinel for an exhausted child iterator in :func:`_walk`.
_END = object()


def _parse(html_bytes: bytes) -> BeautifulSoup:
    """Parse XHTML bytes with bs4's ``html.parser`` backend and remove
    every script, style and head element, contents included.

    The tree is otherwise as parsed: nothing is inserted (1.10 inserted
    newline strings at this point; :func:`_walk` emits them instead).
    """
    soup = BeautifulSoup(html_bytes, "html.parser")
    for tag_name in _SKIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    return soup


def _walk(
    top: Tag,
    start_el: Optional[Tag] = None,
    is_stop: Optional[Callable[[Tag], bool]] = None,
) -> Tuple[str, bool]:
    """Collect the text under ``top`` in one document-order walk.

    Only strings of the types bs4's ``get_text()`` keeps count (see
    ``_TEXT_STRING_TYPES``). A block-level tag adds ``"\\n"`` when the
    walk enters it and again when it leaves it, after its contents;
    ``top`` itself adds none. The text is returned raw: callers apply
    :func:`normalize_whitespace`.

    :param top: The tag (or soup) whose descendants are walked.
    :param start_el: If given, collect from this element on: its own
        contents and everything after it in document order. The newline
        for entering it is not included; the one for leaving it is, as
        are those for leaving its block-level ancestors below ``top``.
        Nothing is collected if it is not under ``top``.
    :param is_stop: Called with each tag reached while collecting
        (never with ``start_el``). The walk ends at the first tag it
        returns true for, with the text before that tag (including the
        newline for entering it, if it is block-level).
    :return: ``(text, stopped)``; ``stopped`` is True when ``is_stop``
        ended the walk.
    """
    parts: list = []
    append = parts.append
    collecting = start_el is None
    block = _BLOCK_LEVEL_TAGS
    text_types = _TEXT_STRING_TYPES
    # One child iterator per open tag; ``open_tags[i]`` owns
    # ``stack[i + 1]`` (``stack[0]`` iterates ``top``'s children).
    stack = [iter(top.contents)]
    open_tags: list = []
    while stack:
        node = next(stack[-1], _END)
        if node is _END:
            stack.pop()
            if open_tags:
                tag = open_tags.pop()
                if collecting and tag.name in block:
                    append("\n")
            continue
        if isinstance(node, Tag):
            if collecting:
                if node.name in block:
                    append("\n")
                if is_stop is not None and is_stop(node):
                    return "".join(parts), True
            elif node is start_el:
                collecting = True
            stack.append(iter(node.contents))
            open_tags.append(node)
        elif collecting and type(node) in text_types:
            append(node)
    return "".join(parts), False


def _anchor_index(soup: Tag) -> Tuple[Dict[str, Tag], Dict[str, Tag]]:
    """Every fragment target of a parsed document, in one pass.

    :return: ``(ids, names)``: each ``id`` value mapped to the first
        element (in document order) that carries it, which is the
        element ``soup.find(id=value)`` returns; and each ``name`` of an
        ``<a>`` element mapped to the first such ``<a>``. Resolve a
        fragment in ``ids`` first, then in ``names``.
    """
    ids: Dict[str, Tag] = {}
    names: Dict[str, Tag] = {}
    for el in soup.descendants:
        if not isinstance(el, Tag):
            continue
        attrs = el.attrs
        value = attrs.get("id")
        if isinstance(value, str) and value not in ids:
            ids[value] = el
        if el.name == "a":
            value = attrs.get("name")
            if isinstance(value, str) and value not in names:
                names[value] = el
    return ids, names


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
    soup = _parse(html_bytes)

    if start_anchor:
        anchor_el = soup.find(id=start_anchor)
        if anchor_el is not None:
            return _text_in_window(soup, anchor_el, stop_anchors)

    root = soup.body if soup.body is not None else soup
    return normalize_whitespace(_walk(root)[0])


def _text_in_window(
    soup: BeautifulSoup, anchor_el: Tag, stop_anchors: Set[str]
) -> str:
    """Collect text in document order from ``anchor_el`` forward,
    stopping at the first element whose ``id`` is in ``stop_anchors``.
    Used by :func:`extract_chapter_text` for fragment scoping.

    ``soup`` is a tree from :func:`_parse` (since 1.11 no newline nodes
    are inserted into it; :func:`_walk` emits them).
    """
    text, _ = _walk(soup, anchor_el, lambda tag: tag.get("id") in stop_anchors)
    return normalize_whitespace(text)


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


def snap_window(
    text: str,
    match_pos: int,
    match_len: int,
    chars_before: int,
    chars_after: int,
) -> str:
    """Extract a window of text around a match, snapped to whitespace
    boundaries so the snippet never starts or ends mid-word.

    Collapses interior whitespace so the output is a single readable
    line. Prefixes and suffixes with an ellipsis (``…``) when the
    window was cut out of a larger source.

    :param text: The text to extract from.
    :param match_pos: 0-based character offset where the match begins.
    :param match_len: Length of the matched substring.
    :param chars_before: Approximate number of characters to include
        before the match.
    :param chars_after: Approximate number of characters to include
        after the match.
    """
    raw_start = max(0, match_pos - chars_before)
    raw_end = min(len(text), match_pos + match_len + chars_after)

    start = raw_start
    if start > 0:
        sp = text.find(" ", raw_start, match_pos)
        if sp != -1:
            start = sp + 1

    end = raw_end
    if end < len(text):
        sp = text.rfind(" ", match_pos + match_len, raw_end)
        if sp != -1:
            end = sp

    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"
