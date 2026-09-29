"""Tests for py_apple_books.testing: the schema fixtures, FixtureLibrary
and the schema dump's privacy guarantees."""

import datetime as dt
import json
import pathlib
import plistlib
import re
import sqlite3

import pytest

from py_apple_books.testing import (
    ANNOTATION_KINDS,
    COLORS,
    DEFAULT_SCHEMA,
    STORE_SERIES,
    SYSTEM_COLLECTIONS,
    FixtureLibrary,
    available_schemas,
    core_data_time,
    dump_schema,
)
from py_apple_books.testing.fixture import SCHEMAS_DIR, _version_key

REPO = pathlib.Path(__file__).resolve().parent.parent
BOOKKEEPING = {"Z_PRIMARYKEY", "Z_METADATA"}


def ddl(con: sqlite3.Connection) -> list:
    return sorted(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))


def plist(path) -> dict:
    con = sqlite3.connect(path)
    try:
        return plistlib.loads(con.execute("SELECT Z_PLIST FROM Z_METADATA").fetchone()[0])
    finally:
        con.close()


# -- committed schema fixtures ---------------------------------------------


def test_default_schema_is_the_newest():
    assert available_schemas() and DEFAULT_SCHEMA == available_schemas()[-1]


def test_schema_order_is_numeric():
    names = ["macos-26.7-25G229_books-8.5-6570", "macos-9.1-A_books-1.0-1", "macos-26.10-B_books-8.5-6570"]
    assert sorted(names, key=_version_key) == [names[1], names[0], names[2]]


@pytest.mark.parametrize("schema", available_schemas())
def test_schema_sql_is_rowless(schema):
    for store in ("BKLibrary", "AEAnnotation"):
        sql = (SCHEMAS_DIR / schema / f"{store}.sql").read_text()
        # Only DDL plus the two Core Data bookkeeping tables.
        assert set(re.findall(r"^INSERT INTO (\w+)", sql, re.M)) == BOOKKEEPING
        assert "/Users/" not in sql
        con = sqlite3.connect(":memory:")
        con.executescript(sql)
        assert {r[0] for r in con.execute("SELECT Z_MAX FROM Z_PRIMARYKEY")} == {0}
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        assert {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                for t in tables if t not in BOOKKEEPING} == dict.fromkeys(set(tables) - BOOKKEEPING, 0)


@pytest.mark.parametrize("schema", available_schemas())
def test_meta_json_keys(schema):
    meta = json.loads((SCHEMAS_DIR / schema / "meta.json").read_text())
    assert {"macos", "macos_build", "books", "books_build", "stores"} <= meta.keys()
    assert schema == f"macos-{meta['macos']}-{meta['macos_build']}_books-{meta['books']}-{meta['books_build']}"
    for store in ("BKLibrary", "AEAnnotation"):
        info = meta["stores"][store]
        assert {"file", "entities", "framework_version", "tables"} <= info.keys()
        assert info["file"].endswith(".sqlite") and info["entities"] and info["tables"]
        assert not any("count" in key.lower() for key in info)  # no row counts


def test_versions_table_lists_every_schema():
    doc = next((p for p in (REPO / "changes" / "harness.md", REPO / "README.md")
                if p.is_file() and "Supported versions" in p.read_text()), None)
    if doc is None:
        pytest.skip("no 'Supported versions' section in this checkout")
    lines = doc.read_text().splitlines()
    for schema in available_schemas():
        meta = json.loads((SCHEMAS_DIR / schema / "meta.json").read_text())
        wanted = [meta["macos"], meta["macos_build"], meta["books"], meta["books_build"]]
        assert any(all(w in line for w in wanted) for line in lines), f"{doc.name} doesn't list {schema}"


# -- FixtureLibrary --------------------------------------------------------


def test_create_uses_canonical_names_and_fresh_uuids(make_library):
    a, b = make_library(), make_library()
    assert a.library_path.name == a.meta["stores"]["BKLibrary"]["file"] == "BKLibrary-1-091020131601.sqlite"
    assert a.annotation_path.name == "AEAnnotation_v10312011_1727_local.sqlite"
    assert a.data_dir == a.root / "Library/Containers/com.apple.iBooksX/Data/Documents"
    uuid_of = lambda lib: lib.execute("library", "SELECT Z_UUID FROM Z_METADATA")
    assert uuid_of(a) != uuid_of(b)
    hashes = plist(a.library_path)["NSStoreModelVersionHashes"]
    assert hashes["BKLibraryAsset"].hex() == a.meta["stores"]["BKLibrary"]["entities"]["BKLibraryAsset"]


def test_fixed_store_uuids(tmp_path):
    lib = FixtureLibrary.create(tmp_path, store_uuids={"BKLibrary": "LIB-UUID", "AEAnnotation": "ANNO-UUID"})
    assert lib.execute("library", "SELECT Z_UUID FROM Z_METADATA") == [("LIB-UUID",)]
    assert lib.execute("annotations", "SELECT Z_UUID FROM Z_METADATA") == [("ANNO-UUID",)]


def test_identifiers_are_deterministic(make_library):
    def seed(lib):
        book = lib.add_book("X")
        collection = lib.add_collection("C")
        lib.add_annotation(book, "y")
        return [
            lib.execute("library", "SELECT Z_PK, ZASSETID, ZASSETGUID FROM ZBKLIBRARYASSET"),
            lib.execute("library", "SELECT Z_PK, ZCOLLECTIONID FROM ZBKCOLLECTION"),
            lib.execute("annotations", "SELECT Z_PK, ZANNOTATIONASSETID, ZANNOTATIONUUID FROM ZAEANNOTATION"),
            [book, collection],
        ]

    assert seed(make_library()) == seed(make_library())


def test_reset_restarts_primary_keys_in_place(make_library):
    lib = make_library()
    inode = lib.library_path.stat().st_ino
    for _ in range(3):
        lib.add_annotation(lib.add_book("X"), "y")
    lib.seed_system_collections()
    lib.reset()
    assert lib.library_path.stat().st_ino == inode
    assert lib.execute("library", "SELECT count(*) FROM ZBKLIBRARYASSET") == [(0,)]
    assert lib.execute("library", "SELECT count(*) FROM ZBKCOLLECTION") == [(0,)]
    assert lib.execute("annotations", "SELECT count(*) FROM ZAEANNOTATION") == [(0,)]
    assert {r[0] for r in lib.execute("library", "SELECT Z_MAX FROM Z_PRIMARYKEY")} == {0}
    assert lib.add_book("again")["id"] == 1
    assert lib.add_annotation(None, "again") == 1
    assert len(lib.execute("library", "SELECT * FROM Z_METADATA")) == 1


def test_primary_keys_follow_z_max(make_library):
    lib = make_library()
    assert [lib.add_book(f"B{i}")["id"] for i in range(3)] == [1, 2, 3]
    assert lib.execute("library", "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKLibraryAsset'") == [(3,)]
    ent = lib.execute("library", "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKLibraryAsset'")[0][0]
    assert lib.execute("library", "SELECT DISTINCT Z_ENT, Z_OPT FROM ZBKLIBRARYASSET") == [(ent, 1)]


EXPECTED_KINDS = {
    # kind: (ZANNOTATIONTYPE, ZANNOTATIONSTYLE, ZANNOTATIONISUNDERLINE)
    "highlight": (2, COLORS["yellow"], 0),
    "note": (2, COLORS["yellow"], 0),
    "underline": (2, 0, 1),
    "bookmark": (1, 0, 0),
    "reading_position": (3, 0, 0),
    "tombstone": (0, 0, 0),
}


@pytest.mark.parametrize("kind", sorted(ANNOTATION_KINDS))
def test_annotation_kind_codes(make_library, kind):
    lib = make_library()
    book = lib.add_book("X")
    pk = lib.add_annotation(book, "text", kind=kind, created=dt.datetime(2026, 9, 1))
    row = lib.execute("annotations", "SELECT ZANNOTATIONTYPE, ZANNOTATIONSTYLE, ZANNOTATIONISUNDERLINE, "
                      "ZANNOTATIONASSETID, ZANNOTATIONCREATIONDATE, ZANNOTATIONNOTE, ZANNOTATIONDELETED "
                      "FROM ZAEANNOTATION WHERE Z_PK = ?", (pk,))[0]
    assert row[:3] == EXPECTED_KINDS[kind]
    if kind == "tombstone":
        assert row[3:5] == ("", None) and row[6] == 1
    else:
        assert row[3] == book["asset_id"] and row[4] == core_data_time(dt.datetime(2026, 9, 1)) and row[6] == 0
    assert (row[5] is not None) == (kind == "note")


def test_invalid_kind_or_color_writes_nothing(make_library):
    lib = make_library()
    with pytest.raises(ValueError):
        lib.add_annotation(None, "x", kind="scribble")
    with pytest.raises(ValueError):
        lib.add_annotation(None, "x", color="orange")
    assert lib.execute("annotations", "SELECT count(*) FROM ZAEANNOTATION") == [(0,)]


def test_book_defaults(make_library):
    lib = make_library()
    owned = lib.add_book("Owned", progress=0.5)
    series = lib.add_book("Series", data_source=STORE_SERIES)
    owned_series = lib.add_book("Owned Series", data_source=STORE_SERIES, can_redownload=1)
    rows = dict(lib.execute("library", "SELECT Z_PK, ZCANREDOWNLOAD FROM ZBKLIBRARYASSET"))
    assert rows == {owned["id"]: 1, series["id"]: 0, owned_series["id"]: 1}
    assert lib.execute("library", "SELECT ZREADINGPROGRESS, ZISFINISHED, ZISHIDDEN FROM ZBKLIBRARYASSET "
                       "WHERE Z_PK = ?", (owned["id"],)) == [(0.5, None, 0)]
    raw = lib.add_book("Raw", raw={"ZRATING": 5, "ZISEXPLICIT": 1})
    assert lib.execute("library", "SELECT ZRATING, ZISEXPLICIT FROM ZBKLIBRARYASSET WHERE Z_PK = ?",
                       (raw["id"],)) == [(5, 1)]


def test_collections(make_library):
    lib = make_library()
    system = lib.seed_system_collections()
    assert set(system) == set(SYSTEM_COLLECTIONS) and len(system) == 8
    shelf = lib.add_collection("Shelf")
    gone = lib.add_collection("Gone", deleted=True)
    book = lib.add_book("X")
    member = lib.add_to_collection(shelf, book)
    assert lib.execute("library", "SELECT ZCOLLECTION, ZASSET, ZASSETID FROM ZBKCOLLECTIONMEMBER "
                       "WHERE Z_PK = ?", (member,)) == [(shelf["id"], book["id"], book["asset_id"])]
    assert lib.execute("library", "SELECT ZDELETEDFLAG FROM ZBKCOLLECTION WHERE Z_PK = ?", (gone["id"],)) == [(1,)]
    assert system["Want_To_Read_Collection_ID"]["collection_id"] == "Want_To_Read_Collection_ID"


def test_populate(make_library):
    lib = make_library()
    made = lib.populate(books=6, annotations_per_book=4)
    assert len(made["books"]) == 6 and made["annotations"] == 24
    assert lib.execute("annotations", "SELECT count(DISTINCT ZANNOTATIONASSETID) FROM ZAEANNOTATION") == [(6,)]
    assert lib.execute("library", "SELECT count(*) FROM ZBKLIBRARYASSET WHERE ZISFINISHED = 1") == [(2,)]


def test_core_data_time():
    assert core_data_time(None) is None
    assert core_data_time(12.5) == 12.5
    assert core_data_time(dt.datetime(2001, 1, 1)) == 0.0  # naive means UTC
    aware = dt.datetime(2001, 1, 1, 2, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert core_data_time(aware) == 0.0


def test_wal_store_opens_read_only_while_written(make_library):
    lib = make_library(journal_mode="WAL")
    lib.add_book("Committed")
    writer = sqlite3.connect(lib.library_path, isolation_level=None)
    reader = sqlite3.connect(f"file:{lib.library_path}?mode=ro", uri=True)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE ZBKLIBRARYASSET SET ZTITLE = 'Uncommitted'")
        assert reader.execute("SELECT ZTITLE FROM ZBKLIBRARYASSET").fetchall() == [("Committed",)]
        writer.execute("COMMIT")
        assert reader.execute("SELECT ZTITLE FROM ZBKLIBRARYASSET").fetchall() == [("Uncommitted",)]
    finally:
        reader.close()
        writer.close()


def test_default_journal_mode_is_rollback(make_library):
    lib = make_library()
    assert lib.execute("library", "PRAGMA journal_mode") == [("delete",)]
    assert not pathlib.Path(f"{lib.library_path}-wal").exists()


# -- dump_schema -----------------------------------------------------------


def test_dump_is_schema_only_and_private(make_library, tmp_path, capsys):
    """Privacy regression: a populated store dumps to DDL only."""
    lib = make_library()
    book = lib.add_book("SECRET-TITLE", "SECRET-AUTHOR", path="/Users/someone/SECRET.epub", genre="SECRET-GENRE")
    lib.add_annotation(book, "SECRET-HIGHLIGHT", kind="note", note="SECRET-NOTE",
                       location="epubcfi(/6/4[SECRET]!/4/2,/1:0,/1:5)")
    lib.add_to_collection(lib.add_collection("SECRET-COLLECTION"), book)
    out = tmp_path / "out"
    assert dump_schema.main(["--data-dir", str(lib.data_dir), "--out", str(out)]) == 0
    (fixture,) = out.iterdir()
    assert sorted(p.name for p in fixture.iterdir()) == ["AEAnnotation.sql", "BKLibrary.sql", "meta.json"]
    captured = capsys.readouterr()
    blob = b"".join(p.read_bytes() for p in fixture.iterdir()) + (captured.out + captured.err).encode()
    assert b"SECRET" not in blob and b"/Users/" not in blob

    for store, path in (("BKLibrary", lib.library_path), ("AEAnnotation", lib.annotation_path)):
        sql = (fixture / f"{store}.sql").read_text()
        dump_schema.self_check(sql)
        rebuilt = sqlite3.connect(":memory:")
        rebuilt.executescript(sql)
        # The DDL round-trips exactly.
        assert ddl(rebuilt) == ddl(sqlite3.connect(path))
        # Bookkeeping rows are kept, with Z_MAX reset and a fresh store UUID.
        assert {r[0] for r in rebuilt.execute("SELECT Z_MAX FROM Z_PRIMARYKEY")} == {0}
        assert rebuilt.execute("SELECT Z_UUID FROM Z_METADATA").fetchone() != \
            sqlite3.connect(path).execute("SELECT Z_UUID FROM Z_METADATA").fetchone()
        assert rebuilt.execute("SELECT count(*) FROM Z_MODELCACHE").fetchone() == (0,)


def test_dump_plist_keeps_only_schema_keys(make_library, tmp_path):
    lib = make_library()
    con = sqlite3.connect(lib.library_path)
    meta = plistlib.loads(con.execute("SELECT Z_PLIST FROM Z_METADATA").fetchone()[0])
    meta["NSStoreUUID"] = "SECRET-UUID"
    meta["BKDatabase-Metadata"] = {**meta.get("BKDatabase-Metadata", {}), "BKLibraryOwnerDSID": "SECRET-DSID"}
    con.execute("UPDATE Z_METADATA SET Z_PLIST = ?", (plistlib.dumps(meta, fmt=plistlib.FMT_BINARY),))
    con.commit()
    con.close()
    sql, info = dump_schema.dump_store(lib.library_path)
    rebuilt = sqlite3.connect(":memory:")
    rebuilt.executescript(sql)
    kept = plistlib.loads(rebuilt.execute("SELECT Z_PLIST FROM Z_METADATA").fetchone()[0])
    assert kept.keys() <= dump_schema.PLIST_KEYS
    assert kept["BKDatabase-Metadata"].keys() <= dump_schema.BK_META_KEYS
    assert "SECRET" not in sql
    assert info["entities"] == lib.meta["stores"]["BKLibrary"]["entities"]


@pytest.mark.parametrize("schema", available_schemas())
def test_committed_fixture_round_trips_through_dump(make_library, schema):
    lib = make_library(schema)
    for store, path in (("BKLibrary", lib.library_path), ("AEAnnotation", lib.annotation_path)):
        sql, info = dump_schema.dump_store(path)
        assert dump_schema.table_columns(sql) == dump_schema.table_columns(
            (SCHEMAS_DIR / schema / f"{store}.sql").read_text())
        assert info == lib.meta["stores"][store]


def test_self_check_rejects_rows_and_paths():
    base = "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);\n"
    with pytest.raises(dump_schema.DumpError):
        dump_schema.self_check(base + "CREATE TABLE ZX (a); INSERT INTO ZX VALUES (1);")
    with pytest.raises(dump_schema.DumpError):
        dump_schema.self_check(base + "INSERT INTO Z_PRIMARYKEY VALUES (1, 'X', 0, 7);")
    with pytest.raises(dump_schema.DumpError):
        dump_schema.self_check(base + "-- /Users/someone\n")
    dump_schema.self_check(base + "INSERT INTO Z_PRIMARYKEY VALUES (1, 'X', 0, 0);")


def test_compare_reports_column_and_hash_diffs(make_library, tmp_path, capsys):
    lib = make_library()
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET ADD COLUMN ZNEWATTRIBUTE INTEGER")
    lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZFUTUREPROOFING12")
    dumped = {store: dump_schema.dump_store(path) for store, path in
              (("BKLibrary", lib.library_path), ("AEAnnotation", lib.annotation_path))}
    dumped["BKLibrary"][1]["entities"]["BKLibraryAsset"] = "00" * 32
    report = "\n".join(dump_schema.compare(dumped, DEFAULT_SCHEMA))
    assert "ZBKLIBRARYASSET: column added: ZNEWATTRIBUTE INTEGER" in report
    assert "ZAEANNOTATION: column removed: ZFUTUREPROOFING12" in report
    assert "entity BKLibraryAsset" in report
    assert report.endswith("compare: 2 column diffs, 1 entity-hash diffs")

    clean = make_library()
    assert dump_schema.main(["--data-dir", str(clean.data_dir), "--out", str(tmp_path / "o"), "--compare"]) == 0
    assert "compare: 0 column diffs, 0 entity-hash diffs" in capsys.readouterr().out
