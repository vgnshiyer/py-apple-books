"""Tests for py_apple_books.testing: the schema fixtures, FixtureLibrary
and the schema dump's privacy guarantees."""

import datetime as dt
import json
import pathlib
import plistlib
import re
import sqlite3
import struct

import pytest

from py_apple_books.testing import (
    ANNOTATION_KINDS,
    COLORS,
    DEFAULT_SCHEMA,
    STORE_SERIES,
    SYSTEM_COLLECTIONS,
    UBIQUITY,
    YEAR_ZERO,
    FixtureLibrary,
    available_schemas,
    core_data_time,
    dump_schema,
    page_location_blob,
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


# -- 1.11 fixture helpers (stream 1.4) ----------------------------------------


@pytest.fixture
def lib(make_library):
    return make_library()


@pytest.fixture
def open_api():
    """``open_api(lib)``: a ``PyAppleBooks`` over ``lib``, closed after the test."""
    from py_apple_books import PyAppleBooks

    opened = []

    def _open(lib):
        opened.append(PyAppleBooks(data_dir=lib.data_dir))
        return opened[-1]

    yield _open
    for api in opened:
        api.close()


def local(value: dt.datetime) -> dt.datetime:
    """How the models read a Core Data date: a naive local datetime."""
    return dt.datetime.fromtimestamp(core_data_time(value) + 978307200)


def test_book_finish_and_engaged_dates(lib, open_api):
    finished_at, engaged_at = dt.datetime(2026, 3, 4, 5, 6), dt.datetime(2026, 3, 5, 7, 8)
    done = lib.add_book("Done", finished=True, finished_date=finished_at, last_engaged=engaged_at)
    stray = lib.add_book("Stray", finished_date=800000000.0)
    plain = lib.add_book("Plain")
    rows = {r[0]: r[1:] for r in lib.execute(
        "library", "SELECT Z_PK, ZDATEFINISHED, ZLASTENGAGEDDATE, ZISFINISHED FROM ZBKLIBRARYASSET")}
    assert rows == {done["id"]: (core_data_time(finished_at), core_data_time(engaged_at), 1),
                    stray["id"]: (800000000.0, None, None),  # a date doesn't mark it finished
                    plain["id"]: (None, None, None)}
    api = open_api(lib)
    book = api.get_book_by_id(done["id"])
    assert (book.finished_date, book.last_engaged_date) == (local(finished_at), local(engaged_at))
    assert api.get_book_by_id(plain["id"]).finished_date is None


def test_default_rows_name_no_new_column(lib):
    """Rows built with the defaults are 1.10's rows: they still insert into
    a store that lacks every column the 1.11 helpers can set."""
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZDATEFINISHED")
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZLASTENGAGEDDATE")
    for column in ("ZPLUSERDATA", "ZFUTUREPROOFING10", "ZFUTUREPROOFING8"):
        lib.execute("annotations", f"ALTER TABLE ZAEANNOTATION DROP COLUMN {column}")
    book = lib.add_book("Book", finished=True)
    lib.add_annotation(book, "a highlight")
    lib.add_annotation(book, None, kind="reading_position")
    assert lib.populate(books=2, annotations_per_book=2)["annotations"] == 4
    with pytest.raises(sqlite3.OperationalError):
        lib.add_book("Dated", finished_date=1.0)


def test_page_location_blob():
    from py_apple_books.models import PageLocation

    blob = page_location_blob(41, ordinal=3)
    assert blob.startswith(b"bplist00") and blob == page_location_blob(41, 3)
    assert plistlib.loads(blob) == {"class": "BKPageLocation", "pageOffset": 41,
                                    "super": {"class": "BKLocation", "ordinal": 3}}
    assert PageLocation.from_plist(blob) == PageLocation(ordinal=3, page_offset=41)
    assert PageLocation.from_plist(page_location_blob(-1)) is None  # written as given


def test_annotation_position_columns(lib, open_api):
    book = lib.add_book("PDF", content_type=3)
    blob = page_location_blob(41)
    position = lib.add_annotation(book, None, kind="reading_position", user_data=blob,
                                  position_fraction=0.25, furthest_fraction=1)
    garbage = lib.add_annotation(book, None, kind="bookmark", position_fraction=" 0.5x",
                                 furthest_fraction=float("nan"))
    highlight = lib.add_annotation(book, "text", user_data=bytearray(b"raw"), position_fraction="0.5")
    plain = lib.add_annotation(book, "plain")
    rows = {r[0]: r[1:] for r in lib.execute(
        "annotations", "SELECT Z_PK, ZPLUSERDATA, ZFUTUREPROOFING10, typeof(ZFUTUREPROOFING10), "
        "ZFUTUREPROOFING8 FROM ZAEANNOTATION")}
    assert rows == {position: (blob, "0.25", "text", "1.0"), garbage: (None, " 0.5x", "text", "nan"),
                    highlight: (b"raw", "0.5", "text", None), plain: (None, None, "null", None)}
    api = open_api(lib)
    anno = api.get_annotation_by_id(position)
    assert (anno.position_fraction, anno.furthest_fraction) == (0.25, 1.0)
    assert (anno.page_location.page_offset, anno.page_location.page) == (41, 42)
    assert api.get_annotation_by_id(garbage).position_fraction is None


@pytest.mark.parametrize("kwargs", [
    {"user_data": "not bytes"}, {"position_fraction": True}, {"furthest_fraction": [0.5]}])
def test_annotation_position_columns_reject_wrong_types(lib, kwargs):
    with pytest.raises(TypeError):
        lib.add_annotation(None, "x", **kwargs)
    assert lib.execute("annotations", "SELECT count(*) FROM ZAEANNOTATION") == [(0,)]


SERIES_COLUMNS = ("SELECT Z_PK, ZTITLE, ZCONTENTTYPE, ZDATASOURCEIDENTIFIER, ZCANREDOWNLOAD, ZSTATE, "
                  "ZSTOREID, ZSERIESID, ZSERIESCONTAINER, ZSEQUENCENUMBER, ZSEQUENCEDISPLAYNAME, "
                  "ZSERIESISORDERED FROM ZBKLIBRARYASSET ORDER BY Z_PK")


def test_add_series(lib, open_api):
    made = lib.add_series("Series S", [
        {"sequence": 1, "label": "Book 1", "progress": 0.01},
        {"sequence": 2, "title": "Second"},
        {"sequence": 2.5, "can_redownload": 1, "store_id": "777"},
    ])
    container, volumes = made["container"], made["volumes"]
    sid = container["store_id"]
    assert sid.isdigit() and len({sid, *(v["store_id"] for v in volumes)}) == 4
    assert volumes[2]["store_id"] == "777"
    assert lib.execute("library", SERIES_COLUMNS) == [
        (container["id"], "Series S", 5, STORE_SERIES, 0, 5, sid, sid, None, None, None, 1),
        (volumes[0]["id"], "Series S 1", 1, STORE_SERIES, 0, 5, volumes[0]["store_id"], sid,
         container["id"], 1, "Book 1", None),
        (volumes[1]["id"], "Second", 1, STORE_SERIES, 0, 5, volumes[1]["store_id"], sid,
         container["id"], 2, None, None),
        (volumes[2]["id"], "Series S 3", 1, STORE_SERIES, 1, 1, "777", sid, container["id"], 2.5, None, None),
    ]
    api = open_api(lib)
    first = api.get_book_by_id(volumes[0]["id"])
    assert (first.series_id, first.series_container_id, first.series_sequence, first.series_label) == (
        sid, container["id"], 1.0, "Book 1")
    assert api.get_book_by_id(container["id"]).series_is_ordered is True
    # 1.10's owned rule: only the redownloadable volume is a library book.
    assert [b.id for b in api.list_books()] == [volumes[2]["id"]]


def test_add_series_options(lib):
    made = lib.add_series("Loose", [
        {"sequence": "nan"},
        {"raw": {"ZSERIESCONTAINER": None, "ZSTOREID": "S-2"}},
        {"data_source": UBIQUITY},
    ], ordered=False, store_id="SERIES-1")
    assert made["container"]["store_id"] == "SERIES-1"
    assert made["volumes"][1]["store_id"] == "S-2"  # as stored, after raw
    rows = lib.execute("library", "SELECT ZSERIESISORDERED, ZSEQUENCENUMBER, ZSERIESCONTAINER, "
                       "ZDATASOURCEIDENTIFIER, ZCANREDOWNLOAD, ZSTATE FROM ZBKLIBRARYASSET ORDER BY Z_PK")
    container_id = made["container"]["id"]
    assert rows == [(0, None, None, STORE_SERIES, 0, 5), (None, "nan", container_id, STORE_SERIES, 0, 5),
                    (None, None, None, STORE_SERIES, 0, 5), (None, None, container_id, UBIQUITY, 1, 1)]
    assert lib.add_series("Empty", [])["volumes"] == []


def test_add_series_checks_volumes_first(lib):
    with pytest.raises(ValueError, match="sequnce"):
        lib.add_series("S", [{"sequence": 1}, {"sequnce": 2}])
    assert lib.execute("library", "SELECT count(*) FROM ZBKLIBRARYASSET") == [(0,)]


def test_container_paths(lib):
    container = lib.root / "Library/Containers/com.apple.iBooksX/Data"
    assert lib.prefs_path == container / "Library/Preferences/com.apple.iBooksX.plist"
    assert lib.book_info_dir == container / "Library/Caches/AEEpubInfoSource"
    assert lib.book_info_dir == dump_schema.book_info_dir(lib.data_dir)


# -- write_prefs


def raw_date(seconds: float) -> bytes:
    """A binary plist date object holding ``seconds`` (Core Data)."""
    return b"\x33" + struct.pack(">d", seconds)


def test_write_prefs_defaults(lib):
    path = lib.write_prefs(finished={"ASSET-A": dt.datetime(2026, 6, 1, 8), "ASSET-B": dt.date(2026, 7, 2)})
    assert path == lib.prefs_path and path.is_file()
    data = path.read_bytes()
    assert data.startswith(b"bplist00") and data.count(raw_date(YEAR_ZERO)) == 1
    with pytest.raises(plistlib.InvalidFileException):
        plistlib.loads(data)  # the year-0 date, as in Books' own file
    without = plistlib.loads(lib.write_prefs(finished={"ASSET-A": dt.datetime(2026, 6, 1, 8)},
                                             year_zero_date=False).read_bytes())
    assert without == {
        "ReadingGoals.BooksFinished": {"goal": 8, "date": dt.datetime(2026, 1, 2, 9)},
        "ReadingGoals.StreakDay": {"goal": 5400.0, "date": dt.datetime(2026, 1, 3, 9)},
        "ReadingHistory.CurrentStreak": 0,
        "BKFinishedAssetsCache": {"ASSET-A": dt.datetime(2026, 6, 1, 8)},
    }


def test_write_prefs_values(lib):
    aware = dt.datetime(2026, 2, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    doc = plistlib.loads(lib.write_prefs(
        books_goal="8", books_goal_set=aware, daily_goal_seconds=None, current_streak={"x": 1},
        finished={}, extra={"Other": [1, "two"], "ReadingHistory.CurrentStreak": 3},
        year_zero_date=False).read_bytes())
    assert doc == {"ReadingGoals.BooksFinished": {"goal": "8", "date": dt.datetime(2026, 2, 1, 10)},
                   "ReadingHistory.CurrentStreak": 3, "BKFinishedAssetsCache": {}, "Other": [1, "two"]}
    assert plistlib.loads(lib.write_prefs(books_goal=None, daily_goal_seconds=None, current_streak=None,
                                          year_zero_date=False).read_bytes()) == {}


def test_write_prefs_raw_dates(lib):
    nan = float("nan")
    data = lib.write_prefs(books_goal_set=nan, daily_goal_set=800000000,
                           finished={"A": YEAR_ZERO, "B": 1e300, "C": dt.datetime(2026, 1, 1)}).read_bytes()
    assert data.count(raw_date(YEAR_ZERO)) == 2 and raw_date(1e300) in data
    assert b"\x33" + struct.pack(">d", nan) in data
    assert data.count(raw_date(800000000.0)) == 1
    with pytest.raises(plistlib.InvalidFileException):
        plistlib.loads(data)
    valid = plistlib.loads(lib.write_prefs(daily_goal_set=800000000.5, year_zero_date=False).read_bytes())
    assert valid["ReadingGoals.StreakDay"]["date"] == dt.datetime(2001, 1, 1) + dt.timedelta(seconds=800000000.5)


def test_write_prefs_is_reproducible(lib, make_library):
    other = make_library()
    kwargs = dict(finished={"A": dt.datetime(2026, 6, 1), "B": float("nan")}, books_goal_set=YEAR_ZERO)
    assert lib.write_prefs(**kwargs).read_bytes() == other.write_prefs(**kwargs).read_bytes()


def test_write_prefs_xml(lib):
    text = lib.write_prefs(fmt="xml", finished={"A": dt.datetime(2026, 6, 1)}).read_text()
    assert text.count("<date>0000-12-30T00:00:00Z</date>") == 1
    with pytest.raises(ValueError):
        plistlib.loads(text.encode())
    doc = plistlib.loads(lib.write_prefs(fmt="xml", year_zero_date=False, daily_goal_set=0).read_bytes())
    assert doc["ReadingGoals.StreakDay"]["date"] == dt.datetime(2001, 1, 1)
    with pytest.raises(ValueError, match="fmt='binary'"):
        lib.write_prefs(fmt="xml", books_goal_set=float("nan"))


@pytest.mark.parametrize("kwargs, error", [
    ({"fmt": "json"}, ValueError),
    ({"books_goal_set": "2026-01-01"}, TypeError),
    ({"finished": {"A": True}}, TypeError),
    ({"extra": {"Clash": dt.datetime(2001, 1, 1, 0, 0, 0, 1)}}, ValueError),  # write_prefs' placeholder
])
def test_write_prefs_rejects(lib, kwargs, error):
    with pytest.raises(error):
        lib.write_prefs(**kwargs)
    assert not lib.prefs_path.exists()


# -- add_book_info_cache


def cache_sql(path) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return sorted(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    finally:
        con.close()


def cache_rows(path, columns="Z_PK, ZDATABASEKEY, ZBOOKTITLE, ZBOOKAUTHOR, ZPUBLISHERYEAR, ZDELETEDFLAG") -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return con.execute(f"SELECT {columns} FROM ZAEBOOKINFO ORDER BY Z_PK").fetchall()
    finally:
        con.close()


def test_add_book_info_cache(lib):
    path = lib.add_book_info_cache([
        {"asset_id": "ASSET-1", "title": "Removed Book", "author": "Someone", "year": "2001"},
        {"asset_id": "ASSET-2", "author": "Only Author", "deleted": 1,
         "raw": {"ZGENRE": "Synthetic", "ZBOOKLANGUAGE": "fr"}},
        {},
    ])
    assert path == lib.book_info_dir / "AEBookInfo-v20250715-26.7.sqlite"
    assert sorted(p.name for p in lib.book_info_dir.iterdir()) == [path.name]  # closed cleanly: no sidecars
    assert path.read_bytes()[18:20] == b"\x02\x02"  # a WAL database
    assert dump_schema.book_info_open_mode(path) == dump_schema.OPEN_WAL_IMMUTABLE
    assert dump_schema.find_book_info(lib.data_dir) == path
    expected_ddl = sqlite3.connect(":memory:")
    expected_ddl.executescript((SCHEMAS_DIR / lib.schema / "AEBookInfo.sql").read_text())
    assert cache_sql(path) == sorted(r[0] for r in expected_ddl.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert cache_rows(path) == [(1, "ASSET-1", "Removed Book", "Someone", "2001", 0),
                                (2, "ASSET-2", None, "Only Author", None, 1),
                                (3, None, None, None, None, 0)]
    assert cache_rows(path, "ZGENRE, ZBOOKLANGUAGE, Z_ENT, Z_OPT") == [
        (None, None, 1, 1), ("Synthetic", "fr", 1, 1), (None, None, 1, 1)]


def test_add_book_info_cache_drift_and_modes(lib):
    narrow = lib.add_book_info_cache([{"asset_id": "A", "title": "T", "author": "dropped"}], version="v1",
                                     columns=["ZDATABASEKEY", "ZBOOKTITLE"], journal_mode="DELETE")
    assert narrow.name == "AEBookInfo-v1.sqlite" and narrow.read_bytes()[18:20] == b"\x01\x01"
    assert dump_schema.book_info_open_mode(narrow) == dump_schema.OPEN_JOURNAL
    assert cache_sql(narrow) == [
        "CREATE INDEX Z_AEBookInfo_byDatabaseKeyIndex ON ZAEBOOKINFO (ZDATABASEKEY COLLATE BINARY ASC)",
        "CREATE TABLE ZAEBOOKINFO ( Z_PK INTEGER PRIMARY KEY, ZBOOKTITLE VARCHAR, ZDATABASEKEY VARCHAR )"]
    assert cache_rows(narrow, "*") == [(1, "T", "A")]
    bare = lib.add_book_info_cache([{"title": "x"}], version="v2", columns=[])
    assert cache_rows(bare, "*") == [(1,)]


def test_add_book_info_cache_falls_back_to_the_newest_ddl(lib, monkeypatch):
    monkeypatch.setattr(lib, "schema", "macos-0-none_books-0-0")  # a schema without AEBookInfo.sql
    path = lib.add_book_info_cache([{"asset_id": "A"}])
    assert cache_rows(path, "ZDATABASEKEY") == [("A",)]


@pytest.mark.parametrize("kwargs, error", [
    ({"rows": [{"titel": "x"}]}, ValueError),
    ({"rows": [{"raw": {"ZNOSUCHCOLUMN": 1}}]}, ValueError),
    ({"columns": ["ZNOSUCHCOLUMN"]}, ValueError),
    ({"columns": "ZBOOKTITLE"}, ValueError),
    ({"journal_mode": "WAL; DROP TABLE x"}, ValueError),
    ({"version": "../escape"}, ValueError),
    ({"version": ""}, ValueError),
])
def test_add_book_info_cache_rejects(lib, kwargs, error):
    kwargs = {"rows": [{"asset_id": "A"}], **kwargs}
    with pytest.raises(error):
        lib.add_book_info_cache(**kwargs)
    assert not lib.book_info_dir.exists() or not any(lib.book_info_dir.iterdir())


def test_add_book_info_cache_never_replaces_a_file(lib):
    path = lib.add_book_info_cache([{"asset_id": "A"}])
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        lib.add_book_info_cache([{"asset_id": "B"}])
    assert path.read_bytes() == before
