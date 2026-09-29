"""Every column the library reads or writes exists in every schema fixture.

The full fixtures under ``py_apple_books/testing/schemas/`` are real
Apple Books schemas (DDL only). ``tests/fixtures/partial/`` holds column
lists published for older versions that only cover some tables; those
are checked for the tables they have.
"""

import configparser
import json
import pathlib
import sqlite3
from typing import Dict, Set

import pytest

from py_apple_books import collection_writer
from py_apple_books.models import base
from py_apple_books.testing import FixtureLibrary, available_schemas

PARTIAL = pathlib.Path(__file__).parent / "fixtures" / "partial"
PARTIAL_FILES = sorted(PARTIAL.glob("*.json"))


def mapped_columns() -> Dict[str, Set[str]]:
    """``{table: {column, ...}}`` for mappings.ini ([Tables], the model
    sections and [book_collection]) plus the collection writer's columns."""
    cfg = configparser.ConfigParser()
    cfg.read(pathlib.Path(base.__file__).parent / "mappings.ini")
    sections = {name.lower(): name for name in cfg.sections()}
    out: Dict[str, Set[str]] = {}
    for key, table in cfg.items("Tables"):
        table = table.split(".")[-1]  # 'anno_db.ZAEANNOTATION'
        out.setdefault(table, set()).update(v for _, v in cfg.items(sections[key]))
    out.setdefault("ZBKCOLLECTION", set()).update(collection_writer._COLLECTION_COLUMNS)
    out.setdefault("ZBKCOLLECTIONMEMBER", set()).update(collection_writer._MEMBER_COLUMNS)
    return out


def missing_columns(lib: FixtureLibrary) -> Dict[str, list]:
    """Mapped columns absent from ``lib``'s stores, by table."""
    missing = {}
    for table, cols in mapped_columns().items():
        path = lib.annotation_path if table == "ZAEANNOTATION" else lib.library_path
        con = sqlite3.connect(path)
        try:
            have = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()
        if cols - have:
            missing[table] = sorted(cols - have)
    return missing


def test_mapping_covers_every_table():
    assert set(mapped_columns()) == {"ZBKLIBRARYASSET", "ZAEANNOTATION", "ZBKCOLLECTION", "ZBKCOLLECTIONMEMBER"}


@pytest.mark.parametrize("schema", available_schemas())
def test_mapped_columns_exist(schema, make_library):
    assert missing_columns(make_library(schema)) == {}


@pytest.mark.parametrize("partial", PARTIAL_FILES, ids=lambda p: p.stem)
def test_mapped_columns_exist_in_partial_ddl(partial):
    ref = json.loads(partial.read_text())
    assert ref["source"].startswith("https://")
    checked = 0
    for table, cols in mapped_columns().items():
        if table in ref["tables"]:
            checked += 1
            assert not cols - set(ref["tables"][table]), f"{table} lacks {sorted(cols - set(ref['tables'][table]))}"
    assert checked == len(ref["tables"])


def test_partial_2023_ddl_is_complete():
    (partial,) = [p for p in PARTIAL_FILES if p.stem.startswith("2023-03")]
    tables = json.loads(partial.read_text())["tables"]
    assert {t: len(set(c)) for t, c in tables.items()} == {"ZAEANNOTATION": 33, "ZBKLIBRARYASSET": 92}


def test_dropped_column_is_reported(make_library):
    lib = make_library()
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZRATING")
    lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZFUTUREPROOFING5")
    assert missing_columns(lib) == {"ZBKLIBRARYASSET": ["ZRATING"], "ZAEANNOTATION": ["ZFUTUREPROOFING5"]}
