"""The 1.11 extraction core reproduces 1.10's ``extract_chapter_text``.

1.11 rebuilt :func:`py_apple_books.utils.extract_chapter_text` from
``_parse`` / ``_walk`` (one linear walk that emits the paragraph
newlines) instead of 1.10's newline nodes inserted into the tree. Every
chapter text the library returns goes through it, so its output must
not change by a single byte. The oracle is a frozen copy of the 1.10
code (``tests/_legacy_110_extract.py``).

* Seeded fuzz: several seeds of 3,000 generated documents per
  generator, compared with the oracle on the final text and on the raw
  text before whitespace normalization, for the whole document and for
  anchor windows. The generators cover comments, CDATA, processing
  instructions, declarations, ruby, script/style/head/template, unclosed
  and stray tags, text outside ``<body>``, documents without a body,
  several bodies, nested heads, entities, odd encodings and invalid
  bytes. CI runs this file on the lowest supported beautifulsoup4 (the
  ``lowest-deps`` job) and on the latest release (every other job).
* Hand-written cases for the shapes EPUBs actually have.
* Performance guards: the walk is linear where 1.10 was quadratic in the
  number of sibling tags.
"""

from __future__ import annotations

import random
import time
from typing import List, Optional, Set, Tuple

import pytest
from bs4 import BeautifulSoup

from py_apple_books import utils
from py_apple_books.utils import _anchor_index, _parse, _walk, extract_chapter_text
from tests import _legacy_110_extract as legacy


CASES_PER_SEED = 3000
SEEDS = (0, 1, 2)

# Documents that start with an XML declaration, or look like a file
# name, make bs4 warn; both implementations parse the same bytes the
# same way, so the warnings say nothing here.
pytestmark = [
    pytest.mark.filterwarnings("ignore::bs4.XMLParsedAsHTMLWarning"),
    pytest.mark.filterwarnings("ignore::bs4.MarkupResemblesLocatorWarning"),
]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _outcome(fn, *args):
    """``("ok", value)`` or ``("raised", type name, message)``: the two
    implementations must agree on errors too."""
    try:
        return ("ok", fn(*args))
    except Exception as e:  # noqa: BLE001 - compared, not swallowed
        return ("raised", type(e).__name__, str(e))


def _new_raw(html: bytes, start: Optional[str], stops) -> str:
    """The 1.11 core's text before normalization, composed the way
    ``extract_chapter_text`` composes it."""
    stops = stops or set()
    soup = _parse(html)
    if start:
        el = soup.find(id=start)
        if el is not None:
            return _walk(soup, el, lambda t: t.get("id") in stops)[0]
    root = soup.body if soup.body is not None else soup
    return _walk(root)[0]


def assert_same(html: bytes, start: Optional[str] = None, stops=None) -> None:
    old = _outcome(legacy.extract_chapter_text, html, start, stops)
    new = _outcome(extract_chapter_text, html, start, stops)
    assert new == old, (html[:300], start, stops)
    old_raw = _outcome(legacy.raw_text, html, start, stops)
    new_raw = _outcome(_new_raw, html, start, stops)
    assert new_raw == old_raw, (html[:300], start, stops)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Block-level and inline tags, ruby, the skipped tags, template and body
# itself (nested bodies and bodies in odd places included).
_TAGS = [
    "p", "div", "span", "em", "b", "i", "a", "section", "article", "h1", "h2",
    "li", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre", "table", "tr",
    "td", "th", "figure", "nav", "aside", "header", "footer", "main",
    "ruby", "rb", "rt", "rp", "rtc", "script", "style", "head", "body",
    "html", "template", "title", "textarea", "svg", "math", "sup", "sub",
    "small", "address", "details", "hgroup", "noscript", "iframe", "object",
    "svg:p", "epub:switch", "P", "DIV", "Br",
]
_VOID = ["br", "hr", "img", "meta", "link", "wbr", "input", "BR", "col"]
_WORDS = [
    "alpha", "beta", "漢字", "かな", "한국어", "Donau­dampf", "x​y",
    "﻿bom", "Việt", "é", "  ", "\n", "\r\n", "\r", "\t",
    " ", " ", " ", "\x0b", "\x0c", "\x1c", "\x85",
    "&amp;", "&nbsp;", "&#x200b;", "&#160;", "&lt;p&gt;", "&#", "&bogus;",
    "&#xD800;", "&#0;", "<", ">", "a < b", "x>y", "]]>", "--", "été",
    "\U0001F600", "\U0001F1EB\U0001F1F7", "mid word", "",
]
_OTHER = [
    "<!-- c -->", "<!---->", "<!-->", "<!--->", "<!-- a -- b -->",
    "<![CDATA[cd]]>", "<![CDATA[x<p>y</p>]]>", "<![CDATA[", "<?pi x?>",
    "<?xml version='1.0'?>", "<!DOCTYPE html>", "<!ELEMENT br EMPTY>",
    "<!x>", "</>", "< p>", "<p", "<!", "<?", "</p >", "</ p>", "<a/b>",
]


def _attrs(r: random.Random, ids: List[str]) -> str:
    out = []
    k = r.random()
    if k < 0.40:
        if ids and r.random() < 0.15:
            value = r.choice(ids)  # duplicate id
        else:
            value = f"a{len(ids)}"
            ids.append(value)
        quote = r.choice(['"', "'", ""])
        out.append(f" {r.choice(['id', 'id', 'ID'])}={quote}{value}{quote}")
    elif k < 0.43:
        out.append(' id=""')
        ids.append("")
    elif k < 0.46:
        out.append(" id")  # valueless
    if r.random() < 0.15:
        value = f"n{len(ids)}"
        ids.append(value)
        out.append(f' name="{value}"')
    if r.random() < 0.1:
        out.append(' class="x y" lang="fr"')
    if r.random() < 0.03:
        out.append(' id="dup1" id="dup2"')
        ids.extend(["dup1", "dup2"])
    return "".join(out)


def _structured(r: random.Random, depth: int, ids: List[str]) -> str:
    """Mostly well-formed nesting, with some unclosed tags."""
    out = []
    for _ in range(r.randint(1, 4)):
        k = r.random()
        if k < 0.33 or depth > 3:
            out.append(r.choice(_WORDS))
        elif k < 0.42:
            out.append(r.choice(_OTHER))
        elif k < 0.47:
            out.append(f"<{r.choice(_VOID)}{_attrs(r, ids)}{r.choice(['', '/', ' /'])}>")
        else:
            t = r.choice(_TAGS)
            attrs = _attrs(r, ids)
            inner = _structured(r, depth + 1, ids)
            roll = r.random()
            if roll < 0.06:
                out.append(f"<{t}{attrs}>{inner}")  # never closed
            elif roll < 0.09:
                out.append(f"<{t}{attrs}/>{inner}")  # self-closing non-void
            elif roll < 0.12:
                out.append(f"<{t}{attrs}>{inner}</{r.choice(_TAGS)}>")  # mismatched
            else:
                out.append(f"<{t}{attrs}>{inner}</{t}>")
    return "".join(out)


def _tag_soup(r: random.Random, ids: List[str]) -> str:
    """A flat stream of start tags, end tags (often unmatched), text and
    markup oddities: whatever html.parser makes of it."""
    out = []
    for _ in range(r.randint(1, 40)):
        k = r.random()
        if k < 0.35:
            out.append(f"<{r.choice(_TAGS)}{_attrs(r, ids)}>")
        elif k < 0.55:
            out.append(f"</{r.choice(_TAGS)}>")
        elif k < 0.80:
            out.append(r.choice(_WORDS))
        elif k < 0.90:
            out.append(r.choice(_OTHER))
        else:
            out.append(f"<{r.choice(_VOID)}{_attrs(r, ids)}>")
    return "".join(out)


def _shape(r: random.Random, body: str, extra: str) -> str:
    roll = r.random()
    if roll < 0.45:
        return f"<html><head><title>t</title><style>p{{}}</style></head><body>{body}</body></html>"
    if roll < 0.55:
        return body  # no html, no body
    if roll < 0.62:
        return f"<html><body>{body}</body>tail {extra}</html>trailer"
    if roll < 0.68:
        return f"lead {extra}<body>{body}</body>"
    if roll < 0.74:
        return f"<body>{extra}</body><body>{body}</body>"  # two bodies
    if roll < 0.79:
        return f"<html><head><body>{extra}</body></head><body>{body}</body></html>"
    if roll < 0.84:
        return f"<head><head>{extra}</head><script>{extra}</script></head>{body}"
    if roll < 0.88:
        return f"<html><body>{body}<head>{extra}</head></body></html>"
    if roll < 0.92:
        return f"<div><body>{body}</body></div>{extra}"
    if roll < 0.96:
        return f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>{body}</body></html>'
    return f"<html><body><template>{extra}</template>{body}<ruby>漢<rp>(</rp><rt>kan</rt><rp>)</rp></ruby></body></html>"


def _encode(r: random.Random, text: str) -> bytes:
    roll = r.random()
    if roll < 0.80:
        return text.encode("utf-8", "surrogatepass")
    if roll < 0.85:
        return b"\xef\xbb\xbf" + text.encode("utf-8", "surrogatepass")
    if roll < 0.89:
        return text.encode("utf-16")
    if roll < 0.93:
        return ('<meta charset="iso-8859-1">' + text).encode("latin-1", "replace")
    raw = bytearray(text.encode("utf-8", "surrogatepass"))
    for _ in range(r.randint(1, 4)):
        raw.insert(r.randint(0, len(raw)), r.choice([0xFF, 0xFE, 0x80, 0xC3, 0x00, 0xED]))
    return bytes(raw)


def _anchors(r: random.Random, ids: List[str]) -> Tuple[Optional[str], Optional[Set[str]]]:
    pool = ids + ["missing"]
    roll = r.random()
    if roll < 0.15:
        start = None
    elif roll < 0.20:
        start = ""
    else:
        start = r.choice(pool)
    roll = r.random()
    if roll < 0.15:
        stops = None
    else:
        stops = set(r.sample(ids, min(len(ids), r.randint(0, 4))))
        if r.random() < 0.05:
            stops.add("")
        if start and r.random() < 0.05:
            stops.add(start)
        if r.random() < 0.05:
            stops.add("missing")
    return start, stops


def _cases(seed: int, generator: str):
    r = random.Random(f"{generator}-{seed}")
    for _ in range(CASES_PER_SEED):
        ids: List[str] = []
        if generator == "structured":
            body = _structured(r, 0, ids)
            extra = _structured(r, 2, ids)
        else:
            body = _tag_soup(r, ids)
            extra = _tag_soup(r, ids)
        html = _encode(r, _shape(r, body, extra))
        start, stops = _anchors(r, ids)
        yield html, start, stops


# ---------------------------------------------------------------------------
# Fuzz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("generator", ["structured", "tag_soup"])
def test_fuzz_matches_110(seed, generator):
    for html, start, stops in _cases(seed, generator):
        assert_same(html, start, stops)


def test_generators_cover_the_shapes():
    """The fuzz corpus reaches what it claims to: anchors found and
    missed, stops hit, bodyless documents, CDATA, comments, ruby text."""
    seen = set()
    for html, start, stops in _cases(0, "structured"):
        soup = BeautifulSoup(html, "html.parser")
        if soup.body is None:
            seen.add("bodyless")
        if start and soup.find(id=start) is not None:
            seen.add("anchor found")
        if start and soup.find(id=start) is None:
            seen.add("anchor missing")
        if b"CDATA" in html:
            seen.add("cdata")
        if b"<!--" in html:
            seen.add("comment")
        if b"<rt" in html:
            seen.add("ruby")
        if start and stops and soup.find(id=start) is not None:
            el = soup.find(id=start)
            if any(t.get("id") in stops for t in el.find_all_next(True)):
                seen.add("stop hit")
    assert seen == {
        "bodyless", "anchor found", "anchor missing", "cdata", "comment",
        "ruby", "stop hit",
    }


# ---------------------------------------------------------------------------
# Hand-written shapes
# ---------------------------------------------------------------------------

_GUTENBERG = (
    b"<?xml version='1.0' encoding='utf-8'?>\n"
    b"<html xmlns='http://www.w3.org/1999/xhtml'><head><title>T</title>"
    b"<style>p { margin: 0 }</style></head><body>"
    b"<div class='chapter'><h2 id='c1'><a id='pg1'></a>CHAPTER I</h2>"
    b"<p>It was a <i>dark</i> and stormy night.</p><p>Rain fell.<br/>Then\n more.</p></div>"
    b"<div class='chapter'><h2 id='c2'>CHAPTER II</h2><p>Morning came.</p>"
    b"<table><tr><td>a</td><td>b</td></tr></table></div>"
    b"<div><h2 id='c3'>CHAPTER III</h2><ul><li>one</li><li>two</li></ul></div>"
    b"<script>var x = '<p>no</p>';</script></body></html>"
)

_SHAPES = [
    b"",
    b"   ",
    b"plain text, no markup",
    b"<p>A</p><p>B</p>",
    b"<html><body></body></html>",
    b"<html><head><title>Only a title</title></head></html>",
    b"<body><p>a</p></body><p>after body</p>",
    b"<p>before</p><body><p>in body</p></body>",
    b"<html><body><ruby>\xe6\xbc\xa2<rp>(</rp><rt>kan</rt><rp>)</rp></ruby>ji</body></html>",
    b"<html><body><template><p>t</p></template><p>x</p></body></html>",
    b"<body><p>a<!-- note --><![CDATA[c<d]]><?pi?>b</p></body>",
    b"<body><div><div><div><p>deep</p></div></div></div></body>",
    b"<body><p>unclosed <p>nested <div>block</body>",
    b"<body></p></div>stray end tags<p>x</p></body>",
    b"<body><span id='a'></span>one<span id='b'></span>two<span id='a'></span>three</body>",
    _GUTENBERG,
    # Deep nesting (unclosed tags) and many siblings: both walks are
    # iterative, and 1.10's sibling insertions were quadratic.
    b"<body>" + b"<div>" * 3000 + b"deep" + b"<p>x</p>" * 3 + b"</body>",
    b"<body>" + b"<p>w <i>x</i></p>" * 1000 + b"</body>",
]


@pytest.mark.parametrize("html", _SHAPES, ids=[f"shape{i}" for i in range(len(_SHAPES))])
def test_shapes_whole_file(html):
    assert_same(html)


@pytest.mark.parametrize(
    "start,stops",
    [
        ("c1", {"c2", "c3"}),
        ("c2", {"c1", "c3"}),
        ("c3", {"c1", "c2"}),
        ("pg1", {"c2"}),
        ("c1", set()),
        ("c1", None),
        ("c2", {"c2"}),
        ("missing", {"c2"}),
        ("", {"c2"}),
        (None, {"c2"}),
        ("c1", {""}),
        ("c1", ["c3"]),
        ("c1", frozenset({"c3"})),
    ],
)
def test_gutenberg_windows(start, stops):
    assert_same(_GUTENBERG, start, stops)


def test_duplicate_ids_window_starts_at_the_first():
    html = b"<body><span id='a'></span>one<span id='b'></span>two<span id='a'></span>three</body>"
    assert_same(html, "a", {"b"})
    assert_same(html, "b", {"a"})
    assert extract_chapter_text(html, "b", {"a"}) == "two"


def test_inputs_are_not_mutated_and_repeat_calls_agree():
    html = bytearray(_GUTENBERG)
    first = extract_chapter_text(bytes(html), "c2", {"c3"})
    assert bytes(html) == _GUTENBERG
    assert extract_chapter_text(bytes(html), "c2", {"c3"}) == first
    stops = {"c3"}
    extract_chapter_text(_GUTENBERG, "c2", stops)
    assert stops == {"c3"}


def test_output_is_a_plain_str():
    for html in (_GUTENBERG, b"<p>x</p>", b"x", b""):
        assert type(extract_chapter_text(html)) is str
        assert type(_walk(_parse(html))[0]) is str
    assert type(extract_chapter_text(_GUTENBERG, "c1", {"c2"})) is str


def test_skip_tag_order_does_not_matter(monkeypatch):
    """``_SKIP_TAGS`` is a set, iterated in hash-seed order; removing
    script, style and head in any order leaves the same tree."""
    html = (
        b"<html><head><head><script>s</script><style>t</style></head>"
        b"<body>in head</body></head><body><script>x</script><p>keep</p>"
        b"<style>y</style><head>late</head></body></html>"
    )
    seen = set()
    for order in (["script", "style", "head"], ["head", "style", "script"],
                  ["style", "head", "script"]):
        monkeypatch.setattr(utils, "_SKIP_TAGS", order)
        seen.add(str(_parse(html)))
        assert extract_chapter_text(html) == legacy.extract_chapter_text(html) == "keep"
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Performance guards (1.10 was quadratic in the number of sibling tags)
# ---------------------------------------------------------------------------


def _best_of(n, fn):
    best = float("inf")
    for _ in range(n):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def test_perf_10000_sibling_paragraphs():
    html = ("<html><body>" + "<p>x</p>" * 10_000 + "</body></html>").encode()
    out = []
    elapsed = _best_of(3, lambda: out.append(extract_chapter_text(html)))
    assert out[0] == "\n\n".join(["x"] * 10_000)
    assert elapsed < 1.0, f"{elapsed:.2f}s for 10,000 sibling paragraphs"


def test_perf_600_anchors():
    words = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do".split()
    para = "<p>" + " ".join(words * 2) + "</p>"
    html = (
        "<html><body>"
        + "".join(f'<span id="x{k}"></span>{para}' for k in range(600))
        + "</body></html>"
    ).encode()
    frags = {f"x{k}" for k in range(600)}

    def run():
        soup = _parse(html)
        ids, _names = _anchor_index(soup)
        assert len(ids) == 600
        # A whole-file walk that has to test every tag against 600 stops
        # (none of which it reaches before the end) ...
        text, stopped = _walk(soup.body, None, lambda t: t.get("id") in {"nope"})
        assert not stopped and text
        # ... and windows from the first and the middle anchor.
        extract_chapter_text(html, "x0", frags - {"x0"})
        extract_chapter_text(html, "x300", frags - {"x300"})

    elapsed = _best_of(3, run)
    assert elapsed < 0.2, f"{elapsed:.3f}s for a 600-anchor document"
