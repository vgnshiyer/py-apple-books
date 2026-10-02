"""Helpers shared by the stream 3.1 tests (positions): synthetic books
whose files the resolver reads, and a fake iCloud that marks chosen
paths as placeholders (``os.lstat``/``os.stat`` report SF_DATALESS for
them, as in tests/test_content_gate.py)."""

from __future__ import annotations

import os
import pathlib
from types import SimpleNamespace
from typing import List

import pytest

from py_apple_books import _icloud
from py_apple_books.testing import write_epub_bundle


def p(*paragraphs: str) -> str:
    return "".join(f"<p>{x}</p>" for x in paragraphs)


def cfi(index: int, item_id: str = None, path: str = "/4/2/1:0") -> str:
    """A CFI into spine item ``index`` (0-based), with an optional bracket
    hint and a content path (``''`` for none)."""
    hint = f"[{item_id}]" if item_id else ""
    return f"epubcfi(/6/{2 * (index + 1)}{hint}{'!' + path if path else ''})"


def gutenberg_body() -> str:
    """One file, three ToC entries: body children h1 (2), h2#ch1 (4),
    p (6), h2 (8) holding a#ch2 (2), p (10), h2#ch3 (12), p (14)."""
    return ("<h1>Title page</h1>"
            '<h2 id="ch1">I</h2><p>one one</p>'
            '<h2><a id="ch2"/>II</h2><p>two two</p>'
            '<h2 id="ch3">III</h2><p>three</p>')


def split_book(dest: pathlib.Path, toc=None) -> pathlib.Path:
    """A converter's split files: s0 holds entries A (#a, step 2) and B
    (#b, step 6); s1 holds none; s2 holds C (#c, step 4)."""
    files = [("s0", '<h1 id="a">Part A</h1>' + p("a text.") + '<h1 id="b">Part B</h1>' + p("b text.")),
             ("s1", p("b continues.")),
             ("s2", p("before c.") + '<h1 id="c">Part C</h1>' + p("c text."))]
    toc = toc if toc is not None else [("A", "s0.xhtml#a"), ("B", "s0.xhtml#b"), ("C", "s2.xhtml#c")]
    return write_epub_bundle(dest / "Split.epub", files, toc=toc)


def _dataless_copy(st):
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields["st_flags"] = getattr(st, "st_flags", 0) | _icloud.SF_DATALESS
    return SimpleNamespace(**fields)


class FakeICloud:
    def __init__(self):
        self.marked = set()
        self.stats: List[str] = []

    def mark(self, *paths):
        for path in paths:
            self.marked.add(os.fspath(path))
            self.marked.add(os.path.realpath(path))
        self.stats.clear()

    def touched(self, path) -> List[str]:
        stats = list(self.stats)  # before realpath() below stats anything
        real = os.path.realpath(path)
        return [x for x in stats
                if os.path.realpath(x) == real or os.path.realpath(x).startswith(real + os.sep)]


@pytest.fixture
def icloud(monkeypatch):
    fake = FakeICloud()
    real_lstat, real_stat = os.lstat, os.stat

    def wrap(real):
        def call(path, *args, dir_fd=None, **kwargs):
            if dir_fd is None and isinstance(path, (str, bytes, os.PathLike)):
                fake.stats.append(os.fsdecode(path))
            st = real(path, *args, dir_fd=dir_fd, **kwargs)
            if dir_fd is None and isinstance(path, (str, os.PathLike)) and os.fspath(path) in fake.marked:
                return _dataless_copy(st)
            return st
        return call

    monkeypatch.setattr(os, "lstat", wrap(real_lstat))
    monkeypatch.setattr(os, "stat", wrap(real_stat))
    return fake


@pytest.fixture
def parses(monkeypatch):
    """Counts of index builds and anchor-table parses."""
    from py_apple_books import _epub_index

    counts = {"index": 0, "anchors": 0}
    build, table = _epub_index._build_index, _epub_index._anchor_table

    def counted_build(*args, **kwargs):
        counts["index"] += 1
        return build(*args, **kwargs)

    def counted_table(*args, **kwargs):
        counts["anchors"] += 1
        return table(*args, **kwargs)

    monkeypatch.setattr(_epub_index, "_build_index", counted_build)
    monkeypatch.setattr(_epub_index, "_anchor_table", counted_table)
    return counts
