"""Test helpers for py-apple-books and the tools built on it (provisional API).

Builds throwaway Apple Books stores from privacy-safe, schema-only
fixtures (``schemas/<macos>_<books>/``: Apple's DDL and Core Data
bookkeeping, no library rows) and fills them with synthetic rows::

    from py_apple_books.testing import FixtureLibrary, seed_demo

    lib = FixtureLibrary.create(tmp_path)
    book = lib.add_book("Synthetic Book", progress=0.4)
    lib.add_annotation(book, "a synthetic highlight")

``FixtureLibrary`` also writes Books' preferences plist and per-book
info caches beside the stores.

``python -m py_apple_books.testing.dump_schema`` produces a new schema
fixture from a real library (see its ``--help``).

This package is not imported by ``py_apple_books`` itself. Its modules
use only the standard library and import each other relatively, so
tools can load this directory on its own without running
``py_apple_books/__init__.py`` (which, up to 1.9, opens the library at
import). Importing it opens no database.
"""

from .demo import seed_demo, write_epub
from .fixture import (
    ANNOTATION_KINDS,
    COLORS,
    DEFAULT_SCHEMA,
    STORE_SERIES,
    SYSTEM_COLLECTIONS,
    UBIQUITY,
    YEAR_ZERO,
    FixtureLibrary,
    available_schemas,
    build_store,
    core_data_time,
    page_location_blob,
)

__all__ = [
    "ANNOTATION_KINDS",
    "COLORS",
    "DEFAULT_SCHEMA",
    "STORE_SERIES",
    "SYSTEM_COLLECTIONS",
    "UBIQUITY",
    "YEAR_ZERO",
    "FixtureLibrary",
    "available_schemas",
    "build_store",
    "core_data_time",
    "page_location_blob",
    "seed_demo",
    "write_epub",
]
