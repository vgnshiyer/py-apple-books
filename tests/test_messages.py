"""Tests for py_apple_books._messages: names, titles and OS errors in
error messages (shortened past 80 characters, path-free, and identical
to 1.10 for anything shorter)."""

import errno
import os
import pathlib

import pytest

from py_apple_books import _messages as m

HOME = os.path.expanduser("~")


@pytest.mark.parametrize("text, expected", [
    ("", ""),
    ("short", "short"),
    ("x" * 80, "x" * 80),
    ("a" * 60 + "b" * 21, "a" * 60 + "…" + "b" * 20),
    ("h" * 60 + "m" * 5000 + "t" * 20, "h" * 60 + "…" + "t" * 20),
])
def test_shorten(text, expected):
    assert m.shorten(text) == expected


def test_shorten_custom_bounds():
    assert m.shorten("abcdefghij", 5, 2, 2) == "ab…ij"
    assert m.shorten("abcdefghij", 5, 3, 0) == "abc…"
    assert m.shorten("abcde", 5, 2, 2) == "abcde"


@pytest.mark.parametrize("name", [
    "OEBPS/Text/ch1.xhtml",
    "META-INF/encryption.xml",
    "it's.xhtml",
    'say "hi".xhtml',
    "both ' and \".xhtml",
    "Text/x.xhtml\x00",
    "ünïcödé/名前.xhtml",
    "../../../outside/canary.txt",
    "n" * 80,
    "n" * 79 + "'",
])
def test_quote_name_is_repr_up_to_80_characters(name):
    assert m.quote_name(name) == repr(name)


@pytest.mark.parametrize("name", [
    "n" * 81,
    "OEBPS/" + "../" * 2000 + "outside.txt",
    "\x00" * 80,  # 80 characters, but escaped far longer
    "\\" * 80,
    "\U0001F600" * 5000,
    "​" * 300,
])
def test_quote_name_is_capped(name):
    quoted = m.quote_name(name)
    assert len(quoted) <= 85
    assert "…" in quoted
    assert quoted[0] in "'\""


def test_quote_name_keeps_start_and_end():
    name = "OEBPS/" + "x" * 5000 + "/chapter-final.xhtml"
    quoted = m.quote_name(name)
    assert quoted == repr(name[:60] + "…" + name[-20:])
    assert quoted.startswith("'OEBPS/xxx") and quoted.endswith("final.xhtml'")


def test_quote_name_of_non_strings():
    assert m.quote_name(5) == "5"
    assert m.quote_name(None) == "None"
    assert m.quote_name(b"bytes") == "b'bytes'"
    assert len(m.quote_name(b"x" * 500)) <= 85


@pytest.mark.parametrize("title, expected", [
    ("Some Book", "'Some Book'"),
    (None, "'None'"),
    ("Ender's Game", "'Ender's Game'"),
    ("t" * 80, "'" + "t" * 80 + "'"),
    ("a" * 60 + "b" * 100, "'" + "a" * 60 + "…" + "b" * 20 + "'"),
    (12, "'12'"),
])
def test_quote_title(title, expected):
    assert m.quote_title(title) == expected


def test_quote_title_matches_1_10_up_to_80_characters():
    for title in ("A", "Book.epub", "x" * 79, "Ünïcode — Title: Part 2", "x" * 80):
        assert m.quote_title(title) == f"'{title}'"


class TestDetail:
    def test_os_error_names_the_base_name_only(self):
        path = os.path.join(HOME, "Library", "Mobile Documents", "Books", "Book.epub", "ch1.xhtml")
        e = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        assert m.detail(e) == f"[Errno {errno.ENOENT}] {os.strerror(errno.ENOENT)}: 'ch1.xhtml'"

    def test_os_error_without_a_file(self):
        e = PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        assert m.detail(e) == f"[Errno {errno.EACCES}] {os.strerror(errno.EACCES)}"

    def test_os_error_with_bytes_or_fd(self):
        e = OSError(errno.EIO, "I/O error", b"/tmp/dir/f\xffx.xhtml")
        assert m.detail(e).startswith("[Errno 5] I/O error: '")
        assert "/tmp" not in m.detail(e)
        assert m.detail(OSError(errno.EBADF, "Bad file descriptor", 7)) == "[Errno 9] Bad file descriptor"
        assert m.detail(OSError(errno.ENOENT, "gone", pathlib.Path("/a/b/c.txt"))) == (
            "[Errno 2] gone: 'c.txt'")

    def test_os_error_with_a_long_name(self):
        e = OSError(errno.ENAMETOOLONG, "File name too long", "/x/" + "n" * 5000)
        text = m.detail(e)
        assert text.startswith(f"[Errno {errno.ENAMETOOLONG}] File name too long: '")
        assert len(text) < 150

    def test_os_error_without_errno_is_its_text(self):
        assert m.detail(OSError("plain message")) == "plain message"

    def test_other_exceptions(self):
        assert m.detail(ValueError("bad value")) == "bad value"
        assert m.detail(KeyError("k")) == "'k'"

    def test_home_is_tilde(self):
        e = ValueError(f"could not parse {HOME}/Library/x.opf")
        assert m.detail(e) == "could not parse ~/Library/x.opf"
        assert HOME not in m.detail(OSError(f"{HOME}/a"))

    def test_capped_at_200(self):
        text = "s" * 150 + "m" * 1000 + "e" * 50
        assert m.detail(RuntimeError(text)) == "s" * 150 + "…" + "e" * 50
        assert m.detail(RuntimeError("x" * 200)) == "x" * 200
