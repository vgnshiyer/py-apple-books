"""The columns the library can't work without exist in every schema fixture.

Required columns are those of ``REQUIRED_FIELDS`` plus the collection
member table's join columns (``[book_collection]``). Every other mapped
column is optional: a store without it reads the field as None (see
``tests/test_schema_drift.py``), so an older or newer fixture may lack it.

The full fixtures under ``py_apple_books/testing/schemas/`` are real
Apple Books schemas (DDL only). ``tests/fixtures/partial/`` holds column
lists published for older versions that only cover some tables; those
are checked for the tables they have. The collection writer checks its
own exact columns before every write, and they are pinned for the full
fixtures here.
"""

import configparser
import json
import pathlib
import sqlite3
from typing import Dict, Set

import pytest

from py_apple_books import collection_writer
from py_apple_books.models import base
from py_apple_books.models.manager import REQUIRED_FIELDS
from py_apple_books.testing import FixtureLibrary, available_schemas

PARTIAL = pathlib.Path(__file__).parent / "fixtures" / "partial"
PARTIAL_FILES = sorted(PARTIAL.glob("*.json"))


def _config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(pathlib.Path(base.__file__).parent / "mappings.ini")
    return cfg


def _tables() -> Dict[str, str]:
    """``{mappings.ini section: bare table name}``."""
    cfg = _config()
    sections = {name.lower(): name for name in cfg.sections()}
    return {sections[key]: table.split(".")[-1] for key, table in cfg.items("Tables")}


def mapped_columns() -> Dict[str, Set[str]]:
    """``{table: {column, ...}}`` for mappings.ini ([Tables], the model
    sections and [book_collection]) plus the collection writer's columns."""
    cfg = _config()
    out: Dict[str, Set[str]] = {}
    for section, table in _tables().items():
        out.setdefault(table, set()).update(v for _, v in cfg.items(section))
    out.setdefault("ZBKCOLLECTION", set()).update(collection_writer._COLLECTION_COLUMNS)
    out.setdefault("ZBKCOLLECTIONMEMBER", set()).update(collection_writer._MEMBER_COLUMNS)
    return out


def required_columns() -> Dict[str, Set[str]]:
    """``{table: {column, ...}}`` the read path needs: the columns of
    REQUIRED_FIELDS and the member table's join columns."""
    cfg = _config()
    tables = _tables()
    out: Dict[str, Set[str]] = {}
    for model, fields in REQUIRED_FIELDS.items():
        out.setdefault(tables[model], set()).update(cfg.get(model, field) for field in fields)
    out.setdefault(tables["book_collection"], set()).update(v for _, v in cfg.items("book_collection"))
    return out


def store_columns(lib: FixtureLibrary, table: str) -> Set[str]:
    path = lib.annotation_path if table == "ZAEANNOTATION" else lib.library_path
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def missing_columns(lib: FixtureLibrary, wanted: Dict[str, Set[str]] = None) -> Dict[str, list]:
    """Columns of ``wanted`` (default: every mapped column) absent from
    ``lib``'s stores, by table."""
    missing = {}
    for table, cols in (wanted or mapped_columns()).items():
        lacking = cols - store_columns(lib, table)
        if lacking:
            missing[table] = sorted(lacking)
    return missing


def test_mapping_covers_every_table():
    assert set(mapped_columns()) == {"ZBKLIBRARYASSET", "ZAEANNOTATION", "ZBKCOLLECTION", "ZBKCOLLECTIONMEMBER"}


def test_required_columns():
    assert required_columns() == {
        "ZBKLIBRARYASSET": {"Z_PK", "ZASSETID"},
        "ZAEANNOTATION": {"Z_PK", "ZANNOTATIONASSETID"},
        "ZBKCOLLECTION": {"Z_PK"},
        "ZBKCOLLECTIONMEMBER": {"ZASSETID", "ZCOLLECTION"},
    }


@pytest.mark.parametrize("schema", available_schemas())
def test_required_columns_exist(schema, make_library):
    assert missing_columns(make_library(schema), required_columns()) == {}


@pytest.mark.parametrize("schema", available_schemas())
def test_writer_columns_exist(schema, make_library):
    writer = {"ZBKCOLLECTION": set(collection_writer._COLLECTION_COLUMNS),
              "ZBKCOLLECTIONMEMBER": set(collection_writer._MEMBER_COLUMNS)}
    assert missing_columns(make_library(schema), writer) == {}


@pytest.mark.parametrize("partial", PARTIAL_FILES, ids=lambda p: p.stem)
def test_required_columns_exist_in_partial_ddl(partial):
    ref = json.loads(partial.read_text())
    assert ref["source"].startswith("https://")
    checked = 0
    for table, cols in required_columns().items():
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
    assert missing_columns(lib, required_columns()) == {}
