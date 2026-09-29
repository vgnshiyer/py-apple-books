"""Tests for ``dump_schema --census``: the count-only book-row census that
shows how the 1.10 owned-books rule treats a library."""

import pytest

from py_apple_books.testing import STORE_SERIES, UBIQUITY, dump_schema

TITLES = ["Owned One", "Owned Two", "Unowned Volume", "Redownloadable Volume", "Series Box", "Private Source"]


@pytest.fixture
def census_library(make_library):
    lib = make_library()
    lib.add_book(TITLES[0])
    lib.add_book(TITLES[1])
    lib.add_book(TITLES[2], data_source=STORE_SERIES, state=5)  # can_redownload defaults to 0
    lib.add_book(TITLES[3], data_source=STORE_SERIES, can_redownload=1)
    lib.add_book(TITLES[4], data_source=STORE_SERIES, content_type=5, state=5)
    lib.add_book(TITLES[5], data_source="x.private")
    return lib


def run_census(lib, capsys, *extra) -> list:
    assert dump_schema.main(["--census", "--data-dir", str(lib.data_dir), *extra]) == 0
    captured = capsys.readouterr()
    for title in TITLES:
        assert title not in captured.out + captured.err
    return captured.out.splitlines()


def table_rows(lines) -> dict:
    """``{(data_source, can_redownload, content_type, state): rows}`` from the table."""
    header = lines.index(next(l for l in lines if l.startswith("data_source")))
    rows = {}
    for line in lines[header + 1:]:
        cells = line.split()
        if len(cells) != 5:
            break
        rows[tuple(cells[:4])] = int(cells[4])
    return rows


def test_census_counts(census_library, capsys):
    lines = run_census(census_library, capsys)
    assert lines[0].startswith("Census of ZBKLIBRARYASSET: 6 rows")
    assert table_rows(lines) == {
        (STORE_SERIES, "0", "1", "5"): 1,
        (STORE_SERIES, "0", "5", "5"): 1,
        (STORE_SERIES, "1", "1", "1"): 1,
        (UBIQUITY, "1", "1", "1"): 2,
        ("<other>", "1", "1", "1"): 1,
    }
    assert "hidden_by_1.10_rule = 2" in lines
    assert "series_rows_kept_because_redownloadable = 1" in lines


def test_census_hides_non_apple_identifiers(census_library, capsys):
    out = "\n".join(run_census(census_library, capsys))
    assert "x.private" not in out and "<other>" in out
    census_library.execute("library", "UPDATE ZBKLIBRARYASSET SET ZDATASOURCEIDENTIFIER = ? WHERE Z_PK = 1",
                           ("com.apple.ibooks.datasource.ubiquity/1234567890",))
    out = "\n".join(run_census(census_library, capsys))
    assert "1234567890" not in out


def test_census_is_never_written_to_out(census_library, capsys, tmp_path):
    out = tmp_path / "out"
    run_census(census_library, capsys, "--out", str(out))
    written = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    assert b"hidden_by" not in written and b"Census" not in written


def test_census_without_a_rule_column(census_library, capsys):
    census_library.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZCANREDOWNLOAD")
    lines = run_census(census_library, capsys)
    assert "column ZCANREDOWNLOAD is missing" in lines
    assert not any(l.startswith(("hidden_by", "series_rows_kept")) for l in lines)
    assert table_rows(lines)[(UBIQUITY, "-", "1", "1")] == 2


def test_census_needs_a_store(tmp_path, capsys):
    assert dump_schema.main(["--census", "--data-dir", str(tmp_path)]) == 1
    assert "No BKLibrary store" in capsys.readouterr().err
