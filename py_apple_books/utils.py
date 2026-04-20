"""Package-internal utilities shared across py_apple_books.

This module is a grab bag of small helpers that don't belong to any
single domain in the package — date conversion, config reads, HTML →
plain-text extraction, whitespace normalization. Keep it as a flat
collection of pure functions; anything with real domain weight
(EPUB-specific parsing, book-access logic) lives in its own module.
"""

from __future__ import annotations

import configparser
import pathlib
import re
from datetime import datetime
from typing import Optional, Set

from bs4 import BeautifulSoup, NavigableString, Tag


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


# ---------------------------------------------------------------------------
# HTML → plain text extraction
# ---------------------------------------------------------------------------
#
# EPUBs ship XHTML. ebooklib gives us raw bytes via ``item.get_content()``
# but no text extraction; bs4's ``get_text()`` concatenates everything
# without block-level awareness (``<p>A</p><p>B</p>`` → ``"AB"``). The
# helpers below preprocess the DOM so bs4's own ``get_text()`` produces
# readable paragraph-separated output, and add anchor-window support
# so Project Gutenberg-style multi-section files can be split cleanly
# by their NCX fragments.


# Contents of these elements is dropped entirely before extraction.
_SKIP_TAGS = {"script", "style", "head"}

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
        elif collecting and isinstance(node, NavigableString):
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
