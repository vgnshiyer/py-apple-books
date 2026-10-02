"""Text cells that aren't valid UTF-8 (BAD-UTF8).

sqlite3 refuses to read such a cell, and up to 1.10 that failed every
query that met one: one bad title broke ``list_books``. Reads now show
U+FFFD in place of the invalid bytes, and errors never quote the cell.

Synthetic libraries only; the invalid bytes are written with
``CAST(? AS TEXT)``.
"""

import logging
import sqlite3
import threading

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB
from py_apple_books.db import client
from py_apple_books.db.client import INVALID_TEXT_WARNING, AppleBooksDBClient
from py_apple_books.exceptions import DBQueryError, QueryTimeoutError

BAD_TITLE = b"AB\xffC"
# A UTF-16 surrogate encoded as UTF-8 (CESU-8), which UTF-8 forbids.
CESU = b"\xed\xa0\x80"
# Over 200 bytes, with newlines, words the error mapping looks for and
# sqlite3's own message syntax in it.
NOISY = b"SECRET line one\n with text 'SECRET' interrupted locked anno_db.x\n" * 4 + b"\xff\xfe"

TITLES = "SELECT ZTITLE FROM ZBKLIBRARYASSET ORDER BY Z_PK"
FINE, REPLACED = "Fine Book", "AB�C"


def _set_text(lib, store, table, column, pk, raw: bytes) -> None:
    lib.execute(store, f"UPDATE {table} SET {column} = CAST(? AS TEXT) WHERE Z_PK = ?", (raw, pk))
    [(kind,)] = lib.execute(store, f"SELECT typeof({column}) FROM {table} WHERE Z_PK = ?", (pk,))
    assert kind == "text"


def _warnings(caplog):
    return [r for r in caplog.records if r.getMessage() == INVALID_TEXT_WARNING]


def _assert_scrubbed(e, column="ZTITLE"):
    """``e`` names the column and nothing of the cell, and carries no
    exception that does."""
    assert type(e) is DBQueryError
    assert str(e) == f"Error executing query: Could not decode to UTF-8 column '{column}'"
    assert e.__cause__ is None and e.__context__ is None
    assert "SECRET" not in repr(e.args)


@pytest.fixture
def bad(make_library):
    """A library with one book title and one highlight's representative
    text that aren't valid UTF-8, next to valid rows."""
    lib = make_library()
    fine = lib.add_book(FINE)
    book = lib.add_book("placeholder")
    _set_text(lib, "library", "ZBKLIBRARYASSET", "ZTITLE", book["id"], BAD_TITLE)
    lib.add_annotation(fine, "a fine highlight")
    note = lib.add_annotation(book, "another highlight")
    _set_text(lib, "annotations", "ZAEANNOTATION", "ZANNOTATIONREPRESENTATIVETEXT", note, CESU)
    lib.book_ids = [fine["id"], book["id"]]
    return lib


@pytest.fixture
def db(bad):
    db = LibraryDB(data_dir=bad.data_dir)
    yield db
    db.close()


@pytest.fixture
def noisy(make_library):
    """A library whose one title is :data:`NOISY`."""
    lib = make_library()
    book = lib.add_book("placeholder")
    _set_text(lib, "library", "ZBKLIBRARYASSET", "ZTITLE", book["id"], NOISY)
    return lib


# -- reads ------------------------------------------------------------------------


def test_lists_read_invalid_text_with_replacement_characters(bad, caplog):
    api = PyAppleBooks(bad.data_dir)
    try:
        with caplog.at_level(logging.WARNING, logger="py_apple_books"):
            assert [b.title for b in api.list_books(order_by="id")] == [FINE, REPLACED]
            assert api.get_library_stats().total_books == 2
            texts = [a.representative_text for a in api.list_annotations(order_by="id")]
            assert [b.title for b in api.get_book_by_title("AB")] == [REPLACED]
    finally:
        api.close()
    assert texts[1] == CESU.decode("utf-8", "replace") and set(texts[1]) == {"�"}
    assert len(_warnings(caplog)) == 1
    record = _warnings(caplog)[0]
    assert record.name == "py_apple_books.db" and record.levelno == logging.WARNING


def test_valid_text_stays_on_the_fast_path(lib_db, caplog):
    lib_db.fixture.add_book(FINE)
    with caplog.at_level(logging.WARNING, logger="py_apple_books"):
        assert lib_db.execute(TITLES) == [(FINE,)]
    assert not lib_db._lenient_text and not _warnings(caplog)
    assert lib_db._idle[0].conn.text_factory is str


def test_lenient_reading_is_sticky_and_warns_once(db, caplog, monkeypatch):
    retries = []
    make_lenient = LibraryDB._make_lenient

    def counted(self):
        retries.append(self)
        make_lenient(self)

    monkeypatch.setattr(LibraryDB, "_make_lenient", counted)
    with caplog.at_level(logging.WARNING, logger="py_apple_books"):
        assert db.execute(TITLES) == [(FINE,), (REPLACED,)]
        assert db._lenient_text and retries == [db]
        for _ in range(3):
            assert db.execute(TITLES) == [(FINE,), (REPLACED,)]
        db.close()  # kept by close()
        assert db.execute(TITLES) == [(FINE,), (REPLACED,)]
        db._pid = -1  # a forked child keeps it too (see test_db_client.test_fork_bookkeeping)
        assert db.execute(TITLES) == [(FINE,), (REPLACED,)]
    assert retries == [db] and len(_warnings(caplog)) == 1
    for pooled in db._inherited:
        pooled.conn.close()


def test_each_library_warns_once(bad, caplog):
    with caplog.at_level(logging.WARNING, logger="py_apple_books"):
        for _ in range(2):
            with LibraryDB(data_dir=bad.data_dir) as db:
                assert db.execute(TITLES)[1] == (REPLACED,)
                assert db.execute(TITLES)[1] == (REPLACED,)
    assert len(_warnings(caplog)) == 2


def test_connections_handed_out_stay_strict(db):
    assert db.execute(TITLES)[1] == (REPLACED,)  # the library is lenient now
    [pooled] = db._idle
    assert pooled.conn.text_factory is str
    with db.connection() as conn:
        assert conn is pooled.conn and conn.text_factory is str
        with pytest.raises(sqlite3.OperationalError, match="^Could not decode to UTF-8"):
            conn.execute(TITLES).fetchall()
        conn.text_factory = bytes  # not left on the pooled connection
    assert pooled.conn.text_factory is str
    conn = db.open_connection()
    try:
        assert conn.text_factory is str
        with pytest.raises(sqlite3.OperationalError, match="^Could not decode to UTF-8"):
            conn.execute(TITLES).fetchall()
    finally:
        conn.close()


def test_the_retry_binds_iterator_params_again(db):
    params = (pk for pk in db_ids(db))
    rows = db.execute("SELECT ZTITLE FROM ZBKLIBRARYASSET WHERE Z_PK IN (?, ?) ORDER BY Z_PK", params)
    assert rows == [(FINE,), (REPLACED,)] and db._lenient_text


def db_ids(db):
    with db.connection() as conn:
        return [pk for (pk,) in conn.execute("SELECT Z_PK FROM ZBKLIBRARYASSET ORDER BY Z_PK")]


def test_the_retry_uses_the_same_connection(bad):
    """No second checkout: one connection is enough, and the retry stays
    within the statement's deadline."""
    with LibraryDB(data_dir=bad.data_dir, max_connections=1, max_idle=1, query_timeout=5) as db:
        assert db.execute(TITLES) == [(FINE,), (REPLACED,)]
        assert len(db._idle) == 1 and db._idle[0].conn.text_factory is str


def test_an_error_of_the_retry_is_not_chained_to_the_decode_error(db):
    factories = []

    def fn(conn):
        factories.append(conn.text_factory)
        if len(factories) == 1:
            return conn.execute(TITLES).fetchall()
        return conn.execute("SELECT * FROM no_such_table").fetchall()

    with pytest.raises(DBQueryError, match="^Error executing query: no such table") as exc:
        db._run(fn)
    assert factories == [str, client._decode_lenient]
    cause = exc.value.__cause__
    assert isinstance(cause, sqlite3.OperationalError) and cause.__context__ is None


def test_threads_share_one_switch(bad, caplog):
    db = LibraryDB(data_dir=bad.data_dir, max_connections=4)
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(timeout=30)
            for _ in range(20):
                results.append(tuple(db.execute(TITLES)))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    with caplog.at_level(logging.WARNING, logger="py_apple_books"):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    try:
        assert not errors and len(results) == 160
        assert set(results) == {((FINE,), (REPLACED,))}
        assert len(_warnings(caplog)) == 1
        assert all(p.conn.text_factory is str for p in db._idle)
    finally:
        db.close()


# -- errors never quote the cell ----------------------------------------------------


def test_sqlite3_still_words_the_decode_error_as_expected():
    """The retry and the scrubbing recognise sqlite3's error by its
    wording (CPython's ``Modules/_sqlite/cursor.c``). Run on every
    supported Python in CI, this fails if a release changes it."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (ZTITLE TEXT)")
        conn.execute("INSERT INTO t VALUES (CAST(? AS TEXT))", (NOISY,))
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute("SELECT ZTITLE FROM t").fetchall()
        assert str(exc.value).startswith("Could not decode to UTF-8 column 'ZTITLE' with text '")
        assert client._is_decode_error(exc.value)
        assert client._scrub_sqlite_message(exc.value) == "Could not decode to UTF-8 column 'ZTITLE'"
        conn.text_factory = client._decode_lenient
        assert conn.execute("SELECT ZTITLE FROM t").fetchone() == (NOISY.decode("utf-8", "replace"),)
    finally:
        conn.close()


def test_scrub_sqlite_message():
    scrub = client._scrub_sqlite_message
    other = sqlite3.OperationalError("no such table: x")
    assert scrub(other) == "no such table: x" and not client._is_decode_error(other)
    assert not client._is_decode_error(sqlite3.DatabaseError("Could not decode to UTF-8 column 'A'"))
    assert not client._is_decode_error(sqlite3.OperationalError())
    # Only a plain column name is kept.
    weird = sqlite3.OperationalError("Could not decode to UTF-8 column 'we'ird' with text 'SECRET'")
    assert scrub(weird) == "Could not decode to UTF-8"
    unknown = sqlite3.OperationalError("Could not decode to UTF-8: SECRET")
    assert scrub(unknown) == "Could not decode to UTF-8"


def test_pooled_decode_error_quotes_nothing(noisy):
    """A decode error the retry can't cure (the function insists on
    strict text) names the column only, with a deadline set and words
    in the cell that would otherwise map it to another error."""
    def strict(conn):
        conn.text_factory = str
        return conn.execute(TITLES).fetchall()

    with LibraryDB(data_dir=noisy.data_dir, query_timeout=30) as db:
        for _ in range(2):  # before and after the library turned lenient
            with pytest.raises(DBQueryError) as exc:
                db._run(strict)
            assert not isinstance(exc.value, QueryTimeoutError)
            _assert_scrubbed(exc.value)
        assert db.execute(TITLES) == [(NOISY.decode("utf-8", "replace"),)]
        assert db._idle[0].conn.text_factory is str


def test_assigned_cursor_decode_error_quotes_nothing(noisy):
    """1.9's assigned cursor (no retry there) gets the same message."""
    conn = sqlite3.connect(noisy.library_path)
    db = LibraryDB(data_dir=noisy.data_dir)
    try:
        client_ = AppleBooksDBClient(db)
        client_.cursor = conn.cursor()
        with pytest.raises(DBQueryError) as exc:
            client_.execute(TITLES)
        _assert_scrubbed(exc.value)
        client_.cursor = None
        assert client_.execute(TITLES) == [(NOISY.decode("utf-8", "replace"),)]
    finally:
        conn.close()
        db.close()
