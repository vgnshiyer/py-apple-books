"""The suite's audit hook (tests/_fs_audit.py): what it sees and what block() refuses."""

import contextvars
import ctypes
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse

import pytest

from tests import _fs_audit
from tests._fs_audit import Policy, block, record


def test_installed_before_the_package_was_imported():
    assert _fs_audit.installed()
    assert _fs_audit.package_imported_before_install is False


def test_record_sees_file_reads_listings_and_connections(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    db = tmp_path / "s.sqlite"
    with record() as rec:
        f.read_text()
        os.listdir(tmp_path)
        with os.scandir(tmp_path):
            pass
        shutil.copyfile(f, tmp_path / "b.txt")
        sqlite3.connect(db).close()
    real = lambda p: os.path.realpath(p)  # noqa: E731 (tmp_path may sit behind /var -> /private/var)
    assert real(f) in rec.paths("open")
    assert real(tmp_path) in rec.paths("os.listdir")
    assert real(tmp_path) in rec.paths("os.scandir")
    assert real(f) in rec.paths("shutil.copyfile")
    assert rec.paths("sqlite3.connect") == [real(db)]


def test_record_sees_an_sqlite_uri_as_its_path(tmp_path):
    folder = tmp_path / "with space"
    folder.mkdir()
    db = folder / "s.sqlite"
    sqlite3.connect(db).close()
    uri = f"file:{urllib.parse.quote(str(db))}?mode=ro"
    with record() as rec:
        sqlite3.connect(uri, uri=True).close()
        sqlite3.connect(":memory:").close()
    assert rec.paths("sqlite3.connect") == [os.path.realpath(db), ":memory:"]


def test_record_sees_processes_and_dlopen():
    with record() as rec:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        ctypes.CDLL(None)
    assert [e.path for e in rec.of("subprocess.Popen")] == [sys.executable]
    assert rec.of("ctypes.dlopen")


def test_only_inside_the_block(tmp_path):
    (tmp_path / "a").write_text("x")
    (tmp_path / "a").read_text()
    with record() as rec:
        pass
    (tmp_path / "a").read_text()
    assert rec.events == []


def test_other_threads_only_with_all_threads(tmp_path):
    f = tmp_path / "a"
    f.write_text("x")

    def read_in_thread():
        t = threading.Thread(target=f.read_text)
        t.start()
        t.join()

    with record() as rec:
        read_in_thread()
    assert os.path.realpath(f) not in rec.paths("open")
    with record(all_threads=True) as rec:
        read_in_thread()
    assert os.path.realpath(f) in rec.paths("open")
    # A context copied from the block carries it into the thread.
    with record() as rec:
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(f.read_text,))
        t.start()
        t.join()
    assert os.path.realpath(f) in rec.paths("open")


def test_nested_recordings_both_see_events(tmp_path):
    f = tmp_path / "a"
    f.write_text("x")
    with record() as outer:
        with record() as inner:
            f.read_text()
    assert inner.paths("open") == outer.paths("open") == [os.path.realpath(f)]


def _home(tmp_path):
    home = tmp_path / "home"
    docs = home.joinpath(*_fs_audit.DOCUMENTS)
    for sub in ("BKLibrary", "AEAnnotation"):
        (docs / sub).mkdir(parents=True)
        (docs / sub / "store.sqlite").write_text("")
    other = home / "Library" / "Containers" / "com.apple.other" / "Data"
    other.mkdir(parents=True)
    (other / "secret").write_text("x")
    icloud = home / "Library" / "Mobile Documents" / "iCloud~com~apple~iBooks" / "Documents"
    icloud.mkdir(parents=True)
    (icloud / "book.pdf").write_text("x")
    book = tmp_path / "books" / "a.epub"
    book.mkdir(parents=True)
    (book / "mimetype").write_text("application/epub+zip")
    return home, docs, other / "secret", icloud / "book.pdf", book


def test_block_refuses_books_icloud_containers_and_processes(tmp_path):
    home, docs, secret, pdf, book = _home(tmp_path)
    policy = Policy.for_library(home, books=[book])
    with block(policy) as rec:
        (docs / "BKLibrary" / "store.sqlite").read_text()  # a store: allowed
        sqlite3.connect(":memory:").close()
        (tmp_path / "elsewhere").write_text("ok")  # outside HOME/Library: allowed
        with pytest.raises(PermissionError):
            (book / "mimetype").read_text()
        with pytest.raises(PermissionError):
            os.listdir(book)
        with pytest.raises(PermissionError):
            pdf.read_bytes()
        with pytest.raises(PermissionError):
            os.listdir(pdf.parent)
        with pytest.raises(PermissionError):
            secret.read_text()
        with pytest.raises(PermissionError):
            sqlite3.connect(f"file:{urllib.parse.quote(str(pdf))}?mode=ro", uri=True)
        with pytest.raises(PermissionError):
            subprocess.run([sys.executable, "-c", "pass"])
    reasons = sorted({reason for _, reason in rec.refused})
    assert reasons == ["a container file outside the allowed stores", "a denied path",
                       "iCloud Drive (Mobile Documents)", "process creation"]
    assert len(rec.refused) == 7
    # Refused events are recorded too.
    assert all(any(e is seen for seen in rec.events) for e, _ in rec.refused)


def test_block_ends_with_the_context(tmp_path):
    home, _, secret, _, _ = _home(tmp_path)
    with block(Policy.for_library(home)):
        with pytest.raises(PermissionError):
            secret.read_text()
    assert secret.read_text() == "x"


def test_policy_switches(tmp_path):
    home, _, secret, pdf, book = _home(tmp_path)
    loose = Policy.for_library(home, subprocess=True, containers=True, mobile_documents=True)
    with block(loose) as rec:
        secret.read_text()
        pdf.read_bytes()
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert rec.refused == []
    with block(Policy(home=str(home), dlopen=False)) as rec:
        with pytest.raises(PermissionError):
            ctypes.CDLL(None)
    # deny wins over allow; extra adds a rule of its own.
    with block(Policy(home=str(home), allow=[book], deny=[book])):
        with pytest.raises(PermissionError):
            (book / "mimetype").read_text()
    no_txt = Policy(home=str(home), extra=lambda ev: "txt" if (ev.path or "").endswith(".txt") else None)
    (tmp_path / "x.txt").write_text("")
    with block(no_txt):
        with pytest.raises(PermissionError):
            (tmp_path / "x.txt").read_text()


def test_library_reads_under_block(library, tmp_path):
    """A store read passes the library policy; reading a book bundle is
    refused and recorded (whatever error the library turns it into)."""
    from py_apple_books import PyAppleBooks

    bundle = tmp_path / "Blocked.epub"
    bundle.mkdir()
    (bundle / "mimetype").write_text("application/epub+zip")
    book_id = library.add_book("Blocked", path=bundle)["id"]
    api = PyAppleBooks()
    with block(Policy.for_library(library.root, books=[bundle])) as rec:
        assert [b.id for b in api.list_books()] == [book_id]
        with pytest.raises(Exception):
            api.get_book_content(book_id)
    api.close()
    assert rec.refused, "the bundle read was not refused"
    root = os.path.realpath(bundle)
    for event, reason in rec.refused:  # du (process creation) or the bundle itself
        assert reason == "process creation" or event.path.startswith(root), (event.event, reason)
