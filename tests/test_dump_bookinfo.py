"""Tests for ``dump_schema``'s AEBookInfo capture (stream 0.3): the
schema-only ``AEBookInfo.sql`` fixture, its header-based open rule and
its privacy self-check. Synthetic caches only (tests/_bookinfo_cases.py).
"""

import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import sys
from urllib.parse import quote

import pytest

from py_apple_books.testing import available_schemas, dump_schema
from py_apple_books.testing.fixture import DEFAULT_SCHEMA, SCHEMAS_DIR
from tests import _bookinfo_cases as cases
from tests import _fs_audit
from tests._bookinfo_cases import CACHE_NAME, CASES, SECRET, TABLE

STORE_FILES = ["AEAnnotation.sql", "BKLibrary.sql", "meta.json"]


def dump(lib, out, *extra):
    return dump_schema.main(["--data-dir", str(lib.data_dir), "--out", str(out), *extra])


def fixture_dir(out: pathlib.Path) -> pathlib.Path:
    (only,) = out.iterdir()
    return only


def schema_objects(sql: str) -> list:
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(sql)
        return sorted(mem.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    finally:
        mem.close()


def cache_objects(path: pathlib.Path) -> list:
    # immutable: this check itself must not create a sidecar
    con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
    try:
        return sorted(con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL",
            (TABLE,)))
    finally:
        con.close()


@pytest.fixture
def lib(make_library):
    return make_library()


@pytest.fixture
def folder(lib):
    path = dump_schema.book_info_dir(lib.data_dir)
    path.mkdir(parents=True)
    return path


class FakeStat:
    """An ``os.stat_result`` with some fields replaced."""

    def __init__(self, st, **fields):
        self._st, self._fields = st, fields

    def __getattr__(self, name):
        return self._fields[name] if name in self._fields else getattr(self._st, name)


def patch_lstat(monkeypatch, target, **fields):
    real = dump_schema._lstat

    def fake(path):
        st = real(path)
        if st is not None and os.path.abspath(path) == os.path.abspath(target):
            return FakeStat(st, **fields)
        return st

    monkeypatch.setattr(dump_schema, "_lstat", fake)


# -- the committed fixture --------------------------------------------------


def test_default_schema_has_book_info():
    assert (SCHEMAS_DIR / DEFAULT_SCHEMA / "AEBookInfo.sql").is_file()


@pytest.mark.parametrize("schema", available_schemas())
def test_committed_book_info_is_schema_only(schema):
    sql_file = SCHEMAS_DIR / schema / "AEBookInfo.sql"
    meta = json.loads((SCHEMAS_DIR / schema / "meta.json").read_text())
    assert sql_file.is_file() == ("book_info" in meta)
    if not sql_file.is_file():
        pytest.skip("no AEBookInfo.sql in this schema")
    sql = sql_file.read_text()
    dump_schema.self_check_book_info(sql)
    # DDL only: CREATE statements, no literal that could carry a value.
    statements = [line for line in sql.splitlines() if line and not line.startswith("--")]
    assert all(re.match(r"CREATE (TABLE|INDEX) ", s) for s in statements)
    assert "'" not in sql and '"' not in sql and "/Users/" not in sql
    # meta.json describes it: the bare cache name and its column count.
    info = meta["book_info"]
    assert info.keys() == {"file", "columns"}
    assert dump_schema.BOOK_INFO_NAME.fullmatch(info["file"])
    assert info["columns"] == len(dump_schema.table_columns(sql)[TABLE])
    # The columns the removed-books reader looks up.
    assert {"ZDATABASEKEY", "ZBOOKTITLE", "ZBOOKAUTHOR"} <= dump_schema.table_columns(sql)[TABLE].keys()


# -- the dump ---------------------------------------------------------------


def test_dump_writes_book_info_and_no_rows(lib, folder, tmp_path, capsys):
    """Privacy regression: a populated cache dumps to its DDL only."""
    path = folder / CACHE_NAME
    # Closed cleanly: no sidecars. (A running Books or a crash leaves both;
    # see the wal_live and wal_dormant_sidecars cases.)
    cases.create(path, "WAL", rows=5).close()
    before = cases.listing(folder)
    out = tmp_path / "out"
    assert dump(lib, out) == 0
    fixture = fixture_dir(out)
    assert sorted(p.name for p in fixture.iterdir()) == ["AEAnnotation.sql", "AEBookInfo.sql",
                                                         "BKLibrary.sql", "meta.json"]
    captured = capsys.readouterr()
    blob = b"".join(p.read_bytes() for p in fixture.iterdir()) + (captured.out + captured.err).encode()
    assert SECRET.encode() not in blob and b"/Users/" not in blob

    sql = (fixture / "AEBookInfo.sql").read_text()
    dump_schema.self_check_book_info(sql)
    assert [o[3] for o in schema_objects(sql)] == [o[3] for o in cache_objects(path)]  # DDL round-trips
    meta = json.loads((fixture / "meta.json").read_text())
    assert meta["book_info"] == {"file": CACHE_NAME, "columns": len(dump_schema.table_columns(sql)[TABLE])}
    # The store entries are what they are without a cache.
    assert meta["stores"] == lib.meta["stores"]
    # Nothing was created or changed beside the cache.
    assert cases.listing(folder) == before


def test_dump_is_deterministic(folder):
    path = folder / CACHE_NAME
    cases.create(path).close()
    assert dump_schema.dump_book_info(path) == dump_schema.dump_book_info(path)


def test_only_zaebookinfo_and_its_indexes(folder):
    path = folder / CACHE_NAME
    con = cases.create(path)
    con.executescript(
        "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);"
        "CREATE INDEX OTHER_INDEX ON Z_PRIMARYKEY (Z_NAME);"
        f"CREATE VIEW V AS SELECT * FROM {TABLE};"
        f"CREATE TRIGGER T AFTER INSERT ON {TABLE} BEGIN SELECT 1; END;"
        f"CREATE TABLE ZUNIQUE (Z_PK INTEGER PRIMARY KEY, ZKEY VARCHAR UNIQUE);")
    con.close()
    sql, info = dump_schema.dump_book_info(path)
    kinds = [(kind, tbl) for kind, _, tbl, _ in schema_objects(sql)]
    assert {k for k, _ in kinds} == {"table", "index"} and {t for _, t in kinds} == {TABLE}
    # Table first, then its indexes by name.
    created = [s for s in sql.splitlines() if s.startswith("CREATE")]
    assert created[0].startswith(f"CREATE TABLE {TABLE}")
    names = [re.match(r"CREATE INDEX (\S+)", s).group(1) for s in created[1:]]
    assert names == sorted(names) and len(names) == len(created) - 1 >= 1


def test_newest_cache_by_natural_order(lib, folder, tmp_path):
    for version in ("v20250715-26.2", "v20250715-26.10", "v20250715-26.7"):
        cases.create(folder / f"AEBookInfo-{version}.sqlite").close()
    # Not caches: other names, sidecars, a name the rule rejects.
    for name in ("AEBookInfo-v20250715-27.0.sqlite-wal", "Other-v99.sqlite", "AEBookInfo-.sqlite",
                 "AEBookInfo-v 99.sqlite", "AEBookInfo-v99.sqlite.bak"):
        (folder / name).write_bytes(b"x" * 200)
    assert dump_schema.find_book_info(lib.data_dir) == folder / "AEBookInfo-v20250715-26.10.sqlite"
    assert dump(lib, tmp_path / "out") == 0
    meta = json.loads((fixture_dir(tmp_path / "out") / "meta.json").read_text())
    assert meta["book_info"]["file"] == "AEBookInfo-v20250715-26.10.sqlite"


@pytest.mark.parametrize("make_folder", [False, True])
def test_no_cache_keeps_the_stores_only_dump(lib, tmp_path, capsys, make_folder):
    if make_folder:
        dump_schema.book_info_dir(lib.data_dir).mkdir(parents=True)
    assert dump_schema.find_book_info(lib.data_dir) is None
    assert dump(lib, tmp_path / "out") == 0
    fixture = fixture_dir(tmp_path / "out")
    assert sorted(p.name for p in fixture.iterdir()) == STORE_FILES
    assert "book_info" not in json.loads((fixture / "meta.json").read_text())
    assert "no AEBookInfo cache found" in capsys.readouterr().err


def test_cache_folder_follows_the_container_layout(lib, tmp_path):
    assert dump_schema.book_info_dir(lib.data_dir) == (
        lib.root / "Library/Containers/com.apple.iBooksX/Data/Library/Caches/AEEpubInfoSource")
    # Only a Documents folder has Books' container around it.
    assert dump_schema.book_info_dir(tmp_path / "stores") is None
    assert dump_schema.find_book_info(tmp_path / "stores") is None


def test_unreadable_cache_is_a_note_and_the_stores_are_dumped(lib, folder, tmp_path, capsys):
    (folder / CACHE_NAME).write_bytes(b"not a database" * 20)
    assert dump(lib, tmp_path / "out") == 0
    fixture = fixture_dir(tmp_path / "out")
    assert sorted(p.name for p in fixture.iterdir()) == STORE_FILES
    assert "book_info" not in json.loads((fixture / "meta.json").read_text())
    err = capsys.readouterr().err
    assert f"note: AEBookInfo.sql not written: {CACHE_NAME} is not a SQLite file" in err
    assert str(folder) not in err


def test_lstat_failure_is_a_note_without_the_path(lib, tmp_path, capsys):
    # The container's Library is a file: lstat of the cache folder fails
    # with ENOTDIR, which is not "no cache" and must not end the dump.
    (lib.data_dir.parent / "Library").write_text("not a folder")
    assert dump(lib, tmp_path / "out") == 0
    fixture = fixture_dir(tmp_path / "out")
    assert sorted(p.name for p in fixture.iterdir()) == STORE_FILES
    err = capsys.readouterr().err
    assert "note: AEBookInfo.sql not written: AEEpubInfoSource can't be looked up: [Errno" in err
    assert str(lib.data_dir.parent) not in err


def test_cache_without_the_table_is_refused(folder):
    path = folder / CACHE_NAME
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ZOTHER (Z_PK INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(dump_schema.DumpError, match="has no ZAEBOOKINFO table"):
        dump_schema.dump_book_info(path)


@pytest.mark.parametrize("leak", [
    f"INSERT INTO {TABLE} (ZBOOKTITLE) VALUES ('{SECRET}title');",
    f"CREATE TEMP TABLE leak AS SELECT '{SECRET}title' AS t;",
    f"-- {SECRET}title\n",
])
def test_rows_never_reach_the_output(lib, folder, tmp_path, monkeypatch, capsys, leak):
    """The self-check stands between a dump and the file: SQL that would
    carry a value is not written."""
    cases.create(folder / CACHE_NAME).close()
    real = dump_schema.dump_book_info

    def leaky(path):
        sql, info = real(path)
        return sql + leak + "\n", info

    monkeypatch.setattr(dump_schema, "dump_book_info", leaky)
    assert dump(lib, tmp_path / "out") == 0
    fixture = fixture_dir(tmp_path / "out")
    assert sorted(p.name for p in fixture.iterdir()) == STORE_FILES
    err = capsys.readouterr().err
    assert "note: AEBookInfo.sql not written: self-check: AEBookInfo.sql holds more than plain" in err
    assert SECRET not in err


def test_stale_book_info_sql_is_reported(lib, tmp_path, capsys):
    out = tmp_path / "out"
    assert dump(lib, out) == 0
    stale = fixture_dir(out) / "AEBookInfo.sql"
    stale.write_text("-- from an earlier dump\n")
    capsys.readouterr()
    assert dump(lib, out) == 0
    assert stale.read_text() == "-- from an earlier dump\n"
    assert "AEBookInfo.sql is left from an earlier dump" in capsys.readouterr().err


# -- self-check -------------------------------------------------------------


def test_self_check_book_info(tmp_path):
    ddl = cases.ddl()
    dump_schema.self_check_book_info(ddl)
    # The header comments are optional; the statements alone pass too.
    dump_schema.self_check_book_info("\n".join(l for l in ddl.splitlines() if not l.startswith("--")))
    attached = tmp_path / "attached.db"
    rejected = {
        "a row": ddl + f"INSERT INTO {TABLE} (Z_PK) VALUES (1);",
        "a row by CREATE AS": f"CREATE TABLE {TABLE} AS SELECT 'x' AS ZBOOKTITLE;",
        "a row by CREATE AS, no literal": f"CREATE TABLE {TABLE} AS SELECT char(83, 69) AS ZBOOKTITLE;",
        "another table": ddl + "CREATE TABLE ZOTHER (a);",
        "an empty bookkeeping table": ddl + "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER);",
        "an index on another table": ddl + "CREATE TABLE ZOTHER (a); CREATE INDEX I ON ZOTHER (a);",
        "a view": ddl + f"CREATE VIEW V AS SELECT * FROM {TABLE};",
        "a trigger": ddl + f"CREATE TRIGGER T AFTER INSERT ON {TABLE} BEGIN SELECT 1; END;",
        "statistics": ddl + "ANALYZE;",
        "a pragma": ddl + "PRAGMA user_version = 7;",
        "no table": "-- nothing\n",
        "a path": ddl + "-- /Users/someone\n",
        "SQL that does not run": ddl + "CREATE TABLE (;",
        "an unterminated statement": ddl + "CREATE INDEX I ON ZAEBOOKINFO (ZGENRE)",
        # Values the old check let through: it only looked at main's
        # tables and their rows.
        "a TEMP table holding a row": ddl + f"CREATE TEMP TABLE leak AS SELECT '{SECRET}title' AS t;",
        "a TEMP table by schema name": ddl + "CREATE TABLE temp.leak (a);",
        "an attached table holding a row":
            ddl + f"ATTACH ':memory:' AS e; CREATE TABLE e.leak AS SELECT '{SECRET}title' AS t;",
        "an attached file": ddl + f"ATTACH DATABASE '{attached}' AS e; CREATE TABLE e.t (x);",
        "VACUUM INTO a file": ddl + f"VACUUM INTO '{attached}';",
        "a DEFAULT literal": ddl.replace("ZBOOKTITLE VARCHAR", f"ZBOOKTITLE VARCHAR DEFAULT '{SECRET}title'"),
        "a double-quoted literal": ddl.replace("ZBOOKTITLE VARCHAR", f'ZBOOKTITLE VARCHAR DEFAULT "{SECRET}"'),
        "a blob literal": ddl.replace("ZORTHOGRAPHY BLOB", "ZORTHOGRAPHY BLOB DEFAULT X'00'"),
        "a partial index literal": ddl + f"CREATE INDEX I ON {TABLE} (ZGENRE) WHERE ZBOOKTITLE <> '{SECRET}';",
        "a comment": ddl + f"-- {SECRET}title by {SECRET}author\n",
        "an inline comment": ddl.replace("ZORTHOGRAPHY BLOB", f"ZORTHOGRAPHY BLOB /* {SECRET} */"),
        "an end-of-line comment": ddl.replace(" );", f" ); -- {SECRET}", 1),
        "a backtick-quoted name": ddl + f"CREATE INDEX I ON {TABLE} (`ZGENRE`);",
        "a NUL": ddl.replace(" );", f" ) \x00{SECRET};", 1),
    }
    for label, sql in rejected.items():
        with pytest.raises(dump_schema.DumpError):
            dump_schema.self_check_book_info(sql)
            pytest.fail(f"accepted {label}")
    assert not attached.exists()


def test_schema_scripts_run_without_attach_or_temp(tmp_path):
    """The authorizer itself, under the text checks: SQL that passes no
    text check still can't attach a file or leave a TEMP object."""
    ddl = cases.ddl()
    attached = tmp_path / "attached.db"
    payloads = [
        f"ATTACH DATABASE '{attached}' AS e; CREATE TABLE e.t (x); INSERT INTO e.t VALUES ('written');",
        "CREATE TEMP TABLE leak (a);",
        "CREATE TABLE temp.leak (a);",
        "PRAGMA writable_schema = ON;",
        f"VACUUM INTO '{attached}';",
    ]
    for payload in payloads:
        for run in (lambda sql: dump_schema._schema_db(sql, "test").close(),
                    dump_schema.table_columns, dump_schema.self_check):
            with pytest.raises(dump_schema.DumpError, match="self-check: .* does not run: .*auth"):
                run(ddl + payload)
    assert not attached.exists()
    # What the stores' SQL needs still runs: their DDL and the bookkeeping rows.
    for store in ("BKLibrary", "AEAnnotation"):
        sql = (SCHEMAS_DIR / DEFAULT_SCHEMA / f"{store}.sql").read_text()
        dump_schema.self_check(sql)
        assert dump_schema.table_columns(sql)
    # An insert into another table is refused while the script runs.
    with pytest.raises(dump_schema.DumpError, match="not authorized"):
        dump_schema.self_check("CREATE TABLE ZX (a); INSERT INTO ZX VALUES (1);")


class _NoAuthorizer(sqlite3.Connection):
    def set_authorizer(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("payload", [
    "CREATE TEMP TABLE leak (a);",
    "ATTACH ':memory:' AS e; CREATE TABLE e.leak (a);",
])
def test_schema_scripts_are_checked_after_running_too(monkeypatch, payload):
    # Behind the authorizer: were it bypassed, what the script left
    # outside main's schema still fails the check.
    real = sqlite3.connect
    monkeypatch.setattr(dump_schema.sqlite3, "connect",
                        lambda *a, **k: real(*a, factory=_NoAuthorizer, **k))
    with pytest.raises(dump_schema.DumpError, match="creates something outside its own schema"):
        dump_schema._schema_db(cases.ddl() + payload, "test")


def _inject(path: pathlib.Path, name: str, sql: str) -> None:
    """Set ``name``'s sqlite_master text, as a crafted file could."""
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA writable_schema = ON")
        con.execute("UPDATE sqlite_master SET sql = ? WHERE name = ?", (sql, name))
        con.execute("PRAGMA writable_schema = OFF")
    finally:
        con.close()


def test_crafted_cache_schema_is_never_run(lib, folder, tmp_path, capsys):
    """A cache whose sqlite_master text carries more than DDL: the dump
    refuses it before anything runs it, and no file appears."""
    path = folder / CACHE_NAME
    cases.create(path).close()
    attached = tmp_path / "attached.db"
    original = cache_objects(path)
    table_sql = next(sql for kind, _, _, sql in original if kind == "table")
    _inject(path, TABLE, f"{table_sql}; ATTACH DATABASE '{attached}' AS e; CREATE TABLE e.t (x); "
                         f"INSERT INTO e.t VALUES ('written')")
    # The cache still opens: SQLite compiles only the first statement.
    con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    assert con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] > 0
    con.close()
    with pytest.raises(dump_schema.DumpError, match="is not a plain CREATE TABLE or CREATE INDEX statement"):
        dump_schema.dump_book_info(path)
    assert dump(lib, tmp_path / "out", "--compare") == 0
    assert sorted(p.name for p in fixture_dir(tmp_path / "out").iterdir()) == STORE_FILES
    captured = capsys.readouterr()
    assert "note: AEBookInfo.sql not written" in captured.err
    assert str(attached) not in captured.out + captured.err
    assert not attached.exists()


@pytest.mark.parametrize("extra", [
    f"CREATE INDEX Z_PARTIAL ON {TABLE} (ZGENRE) WHERE ZBOOKTITLE <> '{SECRET}literal'",
    f'CREATE INDEX Z_QUOTED ON {TABLE} ("ZGENRE")',
])
def test_cache_ddl_with_a_literal_is_refused(folder, extra):
    # Valid SQL, but text that could carry a value: not copied.
    path = folder / CACHE_NAME
    con = cases.create(path)
    con.execute(extra)
    con.close()
    with pytest.raises(dump_schema.DumpError, match="not a plain CREATE TABLE") as info:
        dump_schema.dump_book_info(path)
    assert SECRET not in str(info.value)


def _inject_raw(path: pathlib.Path, name: str, text: bytes) -> None:
    """Like :func:`_inject`, with bytes stored as text (invalid UTF-8)."""
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA writable_schema = ON")
        con.execute("UPDATE sqlite_master SET sql = CAST(? AS TEXT) WHERE name = ?", (text, name))
        con.execute("PRAGMA writable_schema = OFF")
    finally:
        con.close()


@pytest.mark.parametrize("variant, message", [
    ("nul", "is not a plain CREATE TABLE or CREATE INDEX statement"),
    ("invalid_utf8", "schema entry is not valid UTF-8"),
    ("parse_error", "can't be read: "),
])
def test_crafted_schema_text_is_a_note_without_it(lib, folder, tmp_path, capsys, variant, message):
    """Schema text that would stop Python's statement check (a NUL), or
    that SQLite or Python would quote in an error: a fixed message
    without the text, and the stores are still dumped."""
    path = folder / CACHE_NAME
    cases.create(path).close()
    table_sql = next(sql for kind, _, _, sql in cache_objects(path) if kind == "table")
    if variant == "nul":
        _inject(path, TABLE, table_sql + " \x00SECRETVALUE")
    elif variant == "invalid_utf8":  # an identifier byte, so SQLite still parses it
        _inject_raw(path, TABLE, table_sql.encode().replace(
            b"ZORTHOGRAPHY BLOB", b"ZORTHOGRAPHY BLOB, Z\xffSECRETVALUE INTEGER"))
    else:  # SQLite's message would be: near "SECRETVALUE": syntax error
        _inject(path, TABLE, table_sql + " SECRETVALUE")
    with pytest.raises(dump_schema.DumpError, match=re.escape(message)) as info:
        dump_schema.dump_book_info(path)
    assert "SECRETVALUE" not in str(info.value) and str(folder) not in str(info.value)
    assert dump(lib, tmp_path / "out") == 0
    assert sorted(p.name for p in fixture_dir(tmp_path / "out").iterdir()) == STORE_FILES
    captured = capsys.readouterr()
    assert f"note: AEBookInfo.sql not written: {CACHE_NAME}" in captured.err
    assert "SECRETVALUE" not in captured.out + captured.err


def test_sqlite_reason_keeps_only_fixed_sentences():
    for text in ("database is locked", "disk I/O error", "unable to open database file"):
        assert dump_schema._sqlite_reason(sqlite3.OperationalError(text)) == text
    quoted = sqlite3.OperationalError('malformed database schema (ZAEBOOKINFO) - near "SECRETVALUE": syntax error')
    assert "SECRETVALUE" not in dump_schema._sqlite_reason(quoted)
    assert "SECRETVALUE" not in dump_schema._sqlite_reason(sqlite3.OperationalError("no such table: SECRETVALUE"))


def test_schema_script_with_a_nul_is_a_dump_error():
    # Python 3.10 would run the script up to the NUL and skip the rest
    # unseen; later versions raise ValueError. Either way: refused.
    for run in (dump_schema.self_check, dump_schema.table_columns):
        with pytest.raises(dump_schema.DumpError, match="holds a NUL character"):
            run(cases.ddl() + f"\x00INSERT INTO {TABLE} (ZBOOKTITLE) VALUES ('{SECRET}title');")


def test_crafted_store_schema_is_never_run(lib, tmp_path, capsys):
    attached = tmp_path / "attached.db"
    con = sqlite3.connect(lib.library_path)
    table_sql = con.execute("SELECT sql FROM sqlite_master WHERE name = 'ZBKCOLLECTION'").fetchone()[0]
    con.close()
    _inject(lib.library_path, "ZBKCOLLECTION",
            f"{table_sql}; ATTACH DATABASE '{attached}' AS e; CREATE TABLE e.t (x)")
    with pytest.raises(dump_schema.DumpError, match="not a single CREATE statement"):
        dump_schema.dump_store(lib.library_path)
    assert dump(lib, tmp_path / "out") == 1
    assert "not a single CREATE statement" in capsys.readouterr().err
    assert not attached.exists() and not (tmp_path / "out").exists()


def test_store_entries_may_be_triggers_and_views(lib):
    """Core Data stores may carry triggers (with literals and function
    calls) and views: still dumped, self-checked and compared."""
    con = sqlite3.connect(lib.library_path)
    con.executescript(
        "CREATE TRIGGER Z_DA_ZBKCOLLECTION AFTER UPDATE OF ZTITLE ON ZBKCOLLECTION FOR EACH ROW BEGIN "
        "UPDATE ZBKCOLLECTION SET ZTITLE = upper('x;y') WHERE Z_PK = NEW.Z_PK; END;"
        "CREATE VIEW ZV AS SELECT Z_PK FROM ZBKCOLLECTION;")
    con.close()
    sql, _ = dump_schema.dump_store(lib.library_path)
    dump_schema.self_check(sql)
    assert "ZBKCOLLECTION" in dump_schema.table_columns(sql)


@pytest.mark.parametrize("entry, store_ok, plain_ok", [
    ("CREATE TABLE ZX ( Z_PK INTEGER PRIMARY KEY, ZA VARCHAR(255) )", True, True),
    ("CREATE UNIQUE INDEX I ON ZX (ZA COLLATE BINARY ASC)", True, True),
    ("CREATE INDEX I ON ZX (ZA) WHERE ZA > 0", True, True),
    ("  CREATE INDEX I ON ZX (ZA)  ", True, True),
    ("CREATE TRIGGER T AFTER INSERT ON ZX BEGIN SELECT 'a;b'; SELECT 2; END", True, False),
    ("CREATE VIEW V AS SELECT 1", True, False),
    ("CREATE VIRTUAL TABLE V USING fts5(a)", False, False),
    ("CREATE TABLE ZX (a); ATTACH 'x' AS e", False, False),
    ("CREATE TABLE ZX (a);", False, False),
    ("CREATE TABLE ZX (a) -- note", False, False),
    ("CREATE TABLE ZX (a /* note */)", False, False),
    ("CREATE TABLE ZX (a DEFAULT 'x')", True, False),
    ("CREATE TABLE ZX (a DEFAULT 'x;y')", True, False),
    ("CREATE INDEX I ON ZX (`ZA`)", True, False),
    ("CREATE TABLE ZX (a)\x00; ATTACH 'x' AS e", False, False),
    ("CREATE TABLE ZX (a)\x00", False, False),
    ("create table ZX (a)", False, False),
    ("INSERT INTO ZX VALUES (1)", False, False),
])
def test_schema_entry_rules(entry, store_ok, plain_ok):
    assert dump_schema._is_store_entry(entry) is store_ok
    assert dump_schema._is_plain_ddl(entry) is plain_ok


@pytest.mark.parametrize("schema", available_schemas())
def test_committed_ddl_passes_the_entry_rules(schema):
    # Apple's own DDL, as committed: what a dump of a real library copies.
    for name, rule in (("BKLibrary.sql", dump_schema._is_store_entry),
                       ("AEAnnotation.sql", dump_schema._is_store_entry),
                       ("AEBookInfo.sql", dump_schema._is_plain_ddl)):
        sql_file = SCHEMAS_DIR / schema / name
        if not sql_file.is_file():
            continue
        entries = [line[:-1] for line in sql_file.read_text().splitlines() if line.startswith("CREATE ")]
        assert entries and all(rule(e) for e in entries), name


# -- the open rule (shared cases) -------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_open_rule(case, tmp_path):
    folder = tmp_path / "AEEpubInfoSource"
    folder.mkdir()
    with case.make(folder) as path:
        before, before_bytes = cases.listing(folder), cases.contents(folder)
        if case.mode is cases.REFUSED:
            with pytest.raises(dump_schema.DumpError) as info:
                dump_schema.book_info_open_mode(path)
            assert str(folder) not in str(info.value)
        else:
            assert dump_schema.book_info_open_mode(path) == case.mode
        try:
            sql, info = dump_schema.dump_book_info(path)
        except dump_schema.DumpError as e:
            assert case.readable is not True, e
            assert str(folder) not in str(e)
        else:
            assert case.readable is not False
            dump_schema.self_check_book_info(sql)
            assert info == {"file": CACHE_NAME, "columns": len(dump_schema.table_columns(cases.ddl())[TABLE])}
        # No file created, removed or changed: no sidecar made by a
        # read-only open, no hot journal rolled back, no checkpoint. Only
        # SQLite's shared-memory WAL index may be written in place (read
        # marks; a rebuild when no connection had the cache open).
        after, after_bytes = cases.listing(folder), cases.contents(folder)
        if case.mode == cases.WAL:
            shm = f"{CACHE_NAME}-shm"
            assert after.keys() == before.keys() and after[shm][0] == before[shm][0]
            for files in (before, after, before_bytes, after_bytes):
                del files[shm]
        assert after == before
        assert after_bytes == before_bytes


def test_stray_wal_case_is_a_real_hazard(tmp_path):
    """What the journal_stray_wal case guards against: a plain read-only
    open of a rollback-journal cache reads a -wal beside it and creates
    -shm. So the rule must refuse it, not open it."""
    stray = next(c for c in CASES if c.name == "journal_stray_wal")
    with stray.make(tmp_path) as path:
        con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        try:
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 3  # 2 + the WAL's row
        finally:
            con.close()
        assert f"{CACHE_NAME}-shm" in cases.sidecars(path)


_DUMP_ONE = """
import sys
from py_apple_books.testing import dump_schema
try:
    dump_schema.dump_book_info(sys.argv[1])
except dump_schema.DumpError as e:
    print("refused:", e)
"""


@pytest.mark.parametrize("journal_mode, side", [("WAL", "-journal"), ("DELETE", "-wal")])
def test_fifo_sidecar_never_blocks(tmp_path, journal_mode, side):
    """A FIFO sidecar no process writes to: SQLite's open of a FIFO
    ``-journal`` blocks forever, so the rule refuses the file before
    SQLite runs. In a subprocess, so a regression fails instead of
    hanging the suite."""
    path = tmp_path / CACHE_NAME
    con = cases.create(path, journal_mode)
    try:
        if journal_mode == "WAL":
            con.execute("PRAGMA wal_autocheckpoint=0")
            cases.fill(con, 1)  # keeps -wal and -shm: the rule would pick mode=ro
        else:
            con.close()
        os.mkfifo(f"{path}{side}")
        run = subprocess.run([sys.executable, "-c", _DUMP_ONE, str(path)],
                             capture_output=True, text=True, timeout=60)
    finally:
        con.close()
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("refused:") and str(tmp_path) not in run.stdout


def test_sidecar_created_by_a_journal_read_is_reported(tmp_path, monkeypatch):
    """Should a -wal appear after the rule picked ``mode=ro`` for a
    rollback-journal cache, SQLite creates -shm beside it: the read
    succeeds, and the new file is reported."""
    stray = next(c for c in CASES if c.name == "journal_stray_wal")
    with stray.make(tmp_path) as path:
        monkeypatch.setattr(dump_schema, "book_info_open_mode", lambda p: dump_schema.OPEN_JOURNAL)
        with pytest.raises(dump_schema.DumpError, match="removed, replaced or created during the read") as info:
            dump_schema.dump_book_info(path)
        assert f"{CACHE_NAME}-shm" in cases.sidecars(path)
    assert str(tmp_path) not in str(info.value)


def test_open_rule_constants_match_the_cases():
    assert (dump_schema.OPEN_JOURNAL, dump_schema.OPEN_WAL, dump_schema.OPEN_WAL_IMMUTABLE) == (
        cases.JOURNAL, cases.WAL, cases.WAL_IMMUTABLE)
    assert {c.mode for c in CASES} == {cases.JOURNAL, cases.WAL, cases.WAL_IMMUTABLE, cases.REFUSED}


def test_immutable_read_is_rechecked(folder, monkeypatch):
    path = folder / CACHE_NAME
    cases.create(path, "WAL").close()
    assert dump_schema.book_info_open_mode(path) == dump_schema.OPEN_WAL_IMMUTABLE
    calls = iter(range(100))
    real = dump_schema._signature
    monkeypatch.setattr(dump_schema, "_signature", lambda p: (real(p), next(calls)))
    with pytest.raises(dump_schema.DumpError, match="changed while being read"):
        dump_schema.dump_book_info(path)


@pytest.mark.parametrize("kind", ["not_sqlite", "short", "symlink", "directory", "fifo", "missing"])
def test_open_rule_refuses_non_local_files(folder, kind):
    path = folder / CACHE_NAME
    if kind == "not_sqlite":
        path.write_bytes(b"SQLite format 2\x00" + b"\x00" * 100)
    elif kind == "short":
        path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 20)
    elif kind == "symlink":
        target = folder.parent / "elsewhere.sqlite"
        cases.create(target).close()
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)  # never opened: a read would block
    with pytest.raises(dump_schema.DumpError):
        dump_schema.book_info_open_mode(path)


@pytest.mark.parametrize("fields", [
    {"st_flags": dump_schema._SF_DATALESS},
    {"st_flags": 0, "st_blocks": 0},  # a size with no blocks: evicted
])
def test_evicted_cache_is_never_opened(folder, monkeypatch, fields):
    path = folder / CACHE_NAME
    cases.create(path).close()
    patch_lstat(monkeypatch, path, **fields)
    opened = []
    real_open = os.open
    monkeypatch.setattr(dump_schema.os, "open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    with pytest.raises(dump_schema.DumpError, match="not a local SQLite file"):
        dump_schema.dump_book_info(path)
    assert opened == []


def test_compressed_file_without_blocks_is_local(folder, monkeypatch):
    path = folder / CACHE_NAME
    cases.create(path).close()
    patch_lstat(monkeypatch, path, st_flags=dump_schema._UF_COMPRESSED, st_blocks=0)
    assert dump_schema.book_info_open_mode(path) == dump_schema.OPEN_JOURNAL


def test_evicted_sidecar_means_immutable(folder, monkeypatch):
    wal_live = next(c for c in CASES if c.name == "wal_live")
    with wal_live.make(folder) as path:
        assert dump_schema.book_info_open_mode(path) == dump_schema.OPEN_WAL
        patch_lstat(monkeypatch, f"{path}-shm", st_flags=dump_schema._SF_DATALESS)
        assert dump_schema.book_info_open_mode(path) == dump_schema.OPEN_WAL_IMMUTABLE


# -- no byte of an evicted file is read (the suite's audit hook, R13) -------

READS = ("open", "sqlite3.connect", "shutil.copyfile")
LISTINGS = ("os.listdir", "os.scandir")
EVICTED = [
    pytest.param({"st_flags": dump_schema._SF_DATALESS}, id="SF_DATALESS"),
    pytest.param({"st_flags": 0, "st_blocks": 0}, id="no-blocks"),
]


def touching(rec, path, *events) -> list:
    """Audit events on ``path`` or a file whose name extends it (its
    sidecars), or below it."""
    root = os.path.realpath(path)
    return [e for e in rec.of(*events) if e.path and e.path.startswith(root)]


@pytest.mark.parametrize("fields", EVICTED)
def test_evicted_cache_reads_no_byte(lib, folder, tmp_path, monkeypatch, fields):
    path = folder / CACHE_NAME
    cases.create(path, "WAL").close()
    patch_lstat(monkeypatch, path, **fields)
    with _fs_audit.record() as rec:
        with pytest.raises(dump_schema.DumpError, match="not a local SQLite file"):
            dump_schema.dump_book_info(path)
    assert touching(rec, path, *READS) == []
    # End to end: the stores are dumped, the cache is not read.
    with _fs_audit.record() as rec:
        assert dump(lib, tmp_path / "out") == 0
    assert rec.under(lib.library_path, "sqlite3.connect")  # the hook saw the stores being read
    assert touching(rec, path, *READS) == []


def test_evicted_folder_is_never_listed_or_read(lib, folder, tmp_path, monkeypatch):
    cases.create(folder / CACHE_NAME).close()
    patch_lstat(monkeypatch, folder, st_flags=dump_schema._SF_DATALESS)
    with _fs_audit.record() as rec:
        with pytest.raises(dump_schema.DumpError, match="not a local folder"):
            dump_schema.find_book_info(lib.data_dir)
        assert dump(lib, tmp_path / "out") == 0
    assert touching(rec, folder, *READS, *LISTINGS) == []


@pytest.mark.parametrize("fields", EVICTED)
@pytest.mark.parametrize("side", ["-wal", "-shm"])
def test_evicted_wal_sidecar_is_never_opened(folder, monkeypatch, fields, side):
    """Only the main file is opened, immutable: SQLite never opens the
    sidecars (made unreadable here, so an open would fail the read)."""
    path = folder / CACHE_NAME
    con = cases.create(path, "WAL")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the schema is in the main file
    con.execute("PRAGMA wal_autocheckpoint=0")
    cases.fill(con, 1)  # and a committed row in the -wal
    try:
        assert cases.sidecars(path) == [f"{CACHE_NAME}-shm", f"{CACHE_NAME}-wal"]
        patch_lstat(monkeypatch, f"{path}{side}", **fields)
        for s in ("-wal", "-shm"):
            os.chmod(f"{path}{s}", 0)
        with _fs_audit.record() as rec:
            sql, _ = dump_schema.dump_book_info(path)
        dump_schema.self_check_book_info(sql)
    finally:
        for s in ("-wal", "-shm"):
            os.chmod(f"{path}{s}", 0o644)
        con.close()
    assert touching(rec, f"{path}-", *READS) == []
    connects = touching(rec, path, "sqlite3.connect")
    assert connects and all("immutable=1" in os.fsdecode(e.args[0]) for e in connects)


def test_evicted_journal_is_never_opened(folder, monkeypatch):
    path = folder / CACHE_NAME
    cases.create(path, "PERSIST").close()
    assert cases.sidecars(path) == [f"{CACHE_NAME}-journal"]
    patch_lstat(monkeypatch, f"{path}-journal", st_flags=dump_schema._SF_DATALESS)
    with _fs_audit.record() as rec:
        with pytest.raises(dump_schema.DumpError, match=f"{CACHE_NAME}-journal is not a local file"):
            dump_schema.dump_book_info(path)
    assert touching(rec, path, "sqlite3.connect") == []
    assert touching(rec, f"{path}-", *READS) == []


@pytest.mark.parametrize("fields", [
    {"st_ino": -1},
    {"st_mode": stat.S_IFIFO | 0o644},
    {"st_flags": dump_schema._SF_DATALESS},
    {"st_flags": 0, "st_blocks": 0},
])
def test_file_changed_while_being_opened_is_refused(folder, monkeypatch, fields):
    # Replaced, or evicted, between the lstat and the open: the header
    # is not read and SQLite never opens the file.
    path = folder / CACHE_NAME
    cases.create(path).close()
    real = os.fstat
    monkeypatch.setattr(dump_schema.os, "fstat", lambda fd: FakeStat(real(fd), **fields))
    monkeypatch.setattr(dump_schema.os, "read", lambda *a: pytest.fail("read the header"))
    with pytest.raises(dump_schema.DumpError, match="changed while being opened"):
        dump_schema.dump_book_info(path)


@pytest.mark.parametrize("versions, mode", [
    ((1, 1), dump_schema.OPEN_JOURNAL),
    ((2, 2), dump_schema.OPEN_WAL_IMMUTABLE),
    ((1, 2), dump_schema.OPEN_WAL_IMMUTABLE),  # either byte means WAL
    ((2, 1), dump_schema.OPEN_WAL_IMMUTABLE),
])
def test_wal_is_read_from_either_header_byte(folder, versions, mode):
    path = folder / CACHE_NAME
    cases.create(path).close()
    with open(path, "r+b") as f:
        f.seek(18)
        f.write(bytes(versions))
    assert dump_schema.book_info_open_mode(path) == mode


def test_wal_sidecars_replaced_during_the_read_are_reported(folder, monkeypatch):
    """The mode is chosen while Books has the cache open; Books closes it
    before SQLite reads, and the read-only open creates new sidecars."""
    wal_live = next(c for c in CASES if c.name == "wal_live")
    with wal_live.make(folder) as path:
        assert dump_schema.book_info_open_mode(path) == dump_schema.OPEN_WAL
    assert cases.sidecars(path) == []  # Books closed it
    monkeypatch.setattr(dump_schema, "book_info_open_mode", lambda p: dump_schema.OPEN_WAL)
    with pytest.raises(dump_schema.DumpError, match="removed, replaced or created during the read") as info:
        dump_schema.dump_book_info(path)
    assert str(folder) not in str(info.value)


# -- the folder -------------------------------------------------------------


def test_symlinked_folder_is_refused(lib, tmp_path):
    real = tmp_path / "real-cache"
    real.mkdir()
    cases.create(real / CACHE_NAME).close()
    folder = dump_schema.book_info_dir(lib.data_dir)
    folder.parent.mkdir(parents=True)
    folder.symlink_to(real, target_is_directory=True)
    with pytest.raises(dump_schema.DumpError, match="not a local folder"):
        dump_schema.find_book_info(lib.data_dir)


def test_evicted_folder_is_never_listed(lib, folder, monkeypatch):
    cases.create(folder / CACHE_NAME).close()
    patch_lstat(monkeypatch, folder, st_flags=dump_schema._SF_DATALESS)
    monkeypatch.setattr(dump_schema.os, "listdir", lambda *a: pytest.fail("listed an evicted folder"))
    with pytest.raises(dump_schema.DumpError, match="not a local folder"):
        dump_schema.find_book_info(lib.data_dir)


@pytest.mark.parametrize("cloud", ["Mobile Documents", "com~apple~CloudDocs", "CloudStorage",
                                   "mobile documents", "COM~APPLE~CLOUDDOCS", "cloudstorage"])
def test_folder_resolving_into_cloud_storage_is_refused(lib, tmp_path, monkeypatch, cloud):
    # The container's Library folder is a symlink into a fake cloud tree:
    # the cache folder itself is a real directory there.
    remote = tmp_path / "fake-home" / cloud / "box" / "Library"
    (remote / dump_schema.BOOK_INFO_DIR.relative_to("Library")).mkdir(parents=True)
    cases.create(remote / dump_schema.BOOK_INFO_DIR.relative_to("Library") / CACHE_NAME).close()
    (lib.data_dir.parent / "Library").symlink_to(remote, target_is_directory=True)
    monkeypatch.setattr(dump_schema.os, "listdir", lambda *a: pytest.fail("listed a cloud folder"))
    with pytest.raises(dump_schema.DumpError, match="iCloud Drive or cloud storage"):
        dump_schema.find_book_info(lib.data_dir)


@pytest.mark.parametrize("cloud", ["Mobile Documents", "MOBILE DOCUMENTS", "CloudStorage"])
def test_data_dir_in_cloud_storage_is_refused_before_any_lookup(tmp_path, monkeypatch, cloud):
    docs = tmp_path / cloud / "box" / "Data" / "Documents"
    (docs.parent / dump_schema.BOOK_INFO_DIR).mkdir(parents=True)
    monkeypatch.setattr(dump_schema, "_lstat", lambda p: pytest.fail("looked up a path in cloud storage"))
    with pytest.raises(dump_schema.DumpError, match="iCloud Drive or cloud storage"):
        dump_schema.find_book_info(docs)


def test_unlistable_folder_is_refused_without_its_path(lib, folder, monkeypatch):
    def denied(*a):
        raise PermissionError(1, "Operation not permitted", str(folder))

    monkeypatch.setattr(dump_schema.os, "listdir", denied)
    with pytest.raises(dump_schema.DumpError) as info:
        dump_schema.find_book_info(lib.data_dir)
    assert "[Errno 1] Operation not permitted" in str(info.value) and str(folder) not in str(info.value)


# -- compare ----------------------------------------------------------------


def test_compare_reports_book_info_columns(lib, folder, tmp_path, capsys):
    path = folder / CACHE_NAME
    cases.create(path).close()
    dumped = {store: dump_schema.dump_store(p) for store, p in
              (("BKLibrary", lib.library_path), ("AEAnnotation", lib.annotation_path))}
    book_info = dump_schema.dump_book_info(path)
    report = dump_schema.compare(dumped, DEFAULT_SCHEMA, book_info)
    assert "  AEBookInfo: 0 column diffs" in report
    assert report[-1] == "compare: 0 column diffs, 0 entity-hash diffs"

    con = sqlite3.connect(path)
    con.execute(f"ALTER TABLE {TABLE} ADD COLUMN ZNEWATTRIBUTE INTEGER")
    con.execute(f"ALTER TABLE {TABLE} DROP COLUMN ZGENRE")
    con.commit()
    con.close()
    report = dump_schema.compare(dumped, DEFAULT_SCHEMA, dump_schema.dump_book_info(path))
    assert f"  AEBookInfo: {TABLE}: column added: ZNEWATTRIBUTE INTEGER" in report
    assert f"  AEBookInfo: {TABLE}: column removed: ZGENRE" in report
    assert report[-1] == "compare: 2 column diffs, 0 entity-hash diffs"

    # No cache dumped: said, and not counted.
    report = dump_schema.compare(dumped, DEFAULT_SCHEMA)
    assert "  AEBookInfo: no cache dumped; not compared" in report
    assert report[-1] == "compare: 0 column diffs, 0 entity-hash diffs"

    assert dump(lib, tmp_path / "out", "--compare") == 0
    out = capsys.readouterr().out
    assert "AEBookInfo: ZAEBOOKINFO: column added: ZNEWATTRIBUTE INTEGER" in out
    assert out.rstrip().endswith("compare: 2 column diffs, 0 entity-hash diffs")


def test_compare_against_a_fixture_without_book_info(lib, folder, monkeypatch, tmp_path):
    cases.create(folder / CACHE_NAME).close()
    # A copy of the default fixture without AEBookInfo.sql.
    schemas = tmp_path / "schemas"
    copy = schemas / DEFAULT_SCHEMA
    copy.mkdir(parents=True)
    for name in STORE_FILES:
        (copy / name).write_bytes((SCHEMAS_DIR / DEFAULT_SCHEMA / name).read_bytes())
    monkeypatch.setattr(dump_schema, "SCHEMAS_DIR", schemas)
    dumped = {store: dump_schema.dump_store(p) for store, p in
              (("BKLibrary", lib.library_path), ("AEAnnotation", lib.annotation_path))}
    report = dump_schema.compare(dumped, DEFAULT_SCHEMA, dump_schema.dump_book_info(folder / CACHE_NAME))
    assert "  AEBookInfo: not in the committed fixture; not compared" in report
    assert report[-1] == "compare: 0 column diffs, 0 entity-hash diffs"


def test_natural_key():
    names = ["AEBookInfo-v20250715-26.10.sqlite", "AEBookInfo-v20250715-26.2.sqlite",
             "AEBookInfo-v20250715-26.7.sqlite", "AEBookInfo-v20240101-27.0.sqlite",
             "AEBookInfo-a.sqlite", "AEBookInfo-v9.sqlite"]
    assert sorted(names, key=dump_schema._natural_key) == [
        "AEBookInfo-a.sqlite", "AEBookInfo-v9.sqlite", "AEBookInfo-v20240101-27.0.sqlite",
        "AEBookInfo-v20250715-26.2.sqlite", "AEBookInfo-v20250715-26.7.sqlite",
        "AEBookInfo-v20250715-26.10.sqlite"]


def test_open_flags_do_not_follow_symlinks(folder, monkeypatch):
    path = folder / CACHE_NAME
    cases.create(path).close()
    seen = []
    real_open = os.open
    monkeypatch.setattr(dump_schema.os, "open", lambda p, flags, *a: seen.append(flags) or real_open(p, flags, *a))
    dump_schema.book_info_open_mode(path)
    assert seen and all(f & os.O_NOFOLLOW and not f & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) for f in seen)


def test_lstat_wrapper(tmp_path):
    # The patch point returns None for a missing path; any other failure
    # is a DumpError without the path, never a bare OSError.
    assert dump_schema._lstat("/nonexistent/AEBookInfo-x.sqlite") is None
    assert stat.S_ISDIR(dump_schema._lstat("/").st_mode)
    (tmp_path / "file").write_text("")
    with pytest.raises(dump_schema.DumpError, match=r"^AEBookInfo-x.sqlite can't be looked up: \[Errno") as info:
        dump_schema._lstat(tmp_path / "file" / "AEBookInfo-x.sqlite")  # ENOTDIR
    assert str(tmp_path) not in str(info.value)


def test_lookup_failures_are_dump_errors(tmp_path, monkeypatch):
    """Each helper the library's reader may reuse keeps to its documented
    error, DumpError, when a lookup fails with something other than
    'no such file'."""
    lib_docs = tmp_path / "Data" / "Documents"
    folder = lib_docs.parent / dump_schema.BOOK_INFO_DIR
    folder.mkdir(parents=True)
    path = folder / CACHE_NAME
    cases.create(path).close()
    real = os.lstat

    def denied_for(suffix):
        def fake(p, *a, **k):
            if os.fspath(p).endswith(suffix):
                raise PermissionError(13, "Permission denied", os.fspath(p))
            return real(p, *a, **k)
        return fake

    for suffix, call in (("AEEpubInfoSource", lambda: dump_schema.find_book_info(lib_docs)),
                         (".sqlite", lambda: dump_schema.book_info_open_mode(path)),
                         ("-wal", lambda: dump_schema.book_info_open_mode(path)),
                         ("-journal", lambda: dump_schema.book_info_open_mode(path)),
                         ("-shm", lambda: dump_schema.dump_book_info(path))):
        monkeypatch.setattr(dump_schema.os, "lstat", denied_for(suffix))
        with pytest.raises(dump_schema.DumpError, match=r"can't be looked up: \[Errno 13\]") as info:
            call()
        assert str(tmp_path) not in str(info.value)
