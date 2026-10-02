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
from urllib.parse import quote

import pytest

from py_apple_books.testing import available_schemas, dump_schema
from py_apple_books.testing.fixture import DEFAULT_SCHEMA, SCHEMAS_DIR
from tests import _bookinfo_cases as cases
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
    cases.create(path, "WAL", rows=5).close()  # closed cleanly: no sidecars, as Books leaves it
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


def test_cache_without_the_table_is_refused(folder):
    path = folder / CACHE_NAME
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ZOTHER (Z_PK INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(dump_schema.DumpError, match="has no ZAEBOOKINFO table"):
        dump_schema.dump_book_info(path)


def test_rows_never_reach_the_output(lib, folder, tmp_path, monkeypatch, capsys):
    """The self-check stands between a dump and the file: SQL that would
    carry a row is not written."""
    cases.create(folder / CACHE_NAME).close()
    real = dump_schema.dump_book_info

    def leaky(path):
        sql, info = real(path)
        return sql + f"INSERT INTO {TABLE} (ZBOOKTITLE) VALUES ('{SECRET}title');\n", info

    monkeypatch.setattr(dump_schema, "dump_book_info", leaky)
    assert dump(lib, tmp_path / "out") == 0
    fixture = fixture_dir(tmp_path / "out")
    assert sorted(p.name for p in fixture.iterdir()) == STORE_FILES
    assert "self-check: AEBookInfo.sql holds rows" in capsys.readouterr().err


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


def test_self_check_book_info():
    ddl = cases.ddl()
    dump_schema.self_check_book_info(ddl)
    rejected = {
        "a row": ddl + f"INSERT INTO {TABLE} (Z_PK) VALUES (1);",
        "a row by CREATE AS": f"CREATE TABLE {TABLE} AS SELECT 'x' AS ZBOOKTITLE;",
        "another table": ddl + "CREATE TABLE ZOTHER (a);",
        "an empty bookkeeping table": ddl + "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER);",
        "an index on another table": ddl + "CREATE TABLE ZOTHER (a); CREATE INDEX I ON ZOTHER (a);",
        "a view": ddl + f"CREATE VIEW V AS SELECT * FROM {TABLE};",
        "a trigger": ddl + f"CREATE TRIGGER T AFTER INSERT ON {TABLE} BEGIN SELECT 1; END;",
        "statistics": ddl + "ANALYZE;",
        "no table": "-- nothing\n",
        "a path": ddl + "-- /Users/someone\n",
        "SQL that does not run": ddl + "CREATE TABLE (;",
    }
    for label, sql in rejected.items():
        with pytest.raises(dump_schema.DumpError):
            dump_schema.self_check_book_info(sql)
            pytest.fail(f"accepted {label}")


# -- the open rule (shared cases) -------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_open_rule(case, tmp_path):
    folder = tmp_path / "AEEpubInfoSource"
    folder.mkdir()
    with case.make(folder) as path:
        assert dump_schema.book_info_open_mode(path) == case.mode
        before = cases.listing(folder)
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
        # read-only open, no hot journal rolled back, no checkpoint.
        assert cases.listing(folder) == before


def test_open_rule_constants_match_the_cases():
    assert (dump_schema.OPEN_JOURNAL, dump_schema.OPEN_WAL, dump_schema.OPEN_WAL_IMMUTABLE) == (
        cases.JOURNAL, cases.WAL, cases.WAL_IMMUTABLE)
    assert {c.mode for c in CASES} == {cases.JOURNAL, cases.WAL, cases.WAL_IMMUTABLE}


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


@pytest.mark.parametrize("cloud", ["Mobile Documents", "com~apple~CloudDocs", "CloudStorage"])
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


def test_lstat_wrapper():
    # The patch point returns None for a missing path, never raises.
    assert dump_schema._lstat("/nonexistent/AEBookInfo-x.sqlite") is None
    assert stat.S_ISDIR(dump_schema._lstat("/").st_mode)
