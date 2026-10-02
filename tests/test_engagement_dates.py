"""The 1.11 date rules (R9): ``on`` days, ``after``/``before`` windows,
date bounds on ``get_annotations_by_date_range`` and the finish-date
window on ``get_finished_books``. Synthetic data only.

Local-time cases that depend on a time zone (DST days, an aware UTC
instant near midnight) run in a subprocess with ``TZ`` set.
"""

import datetime as dt
import inspect
import json
import os
import subprocess
import sys
import textwrap

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.engagement import (
    _day_end,
    _day_start,
    _local_day,
    _resolve_window,
    _window_filters,
)
from py_apple_books.exceptions import InvalidArgumentError, UnsupportedSchemaError
from py_apple_books.testing import STORE_SERIES
from py_apple_books.utils import APPLE_EPOCH_OFFSET

UTC = dt.timezone.utc


def local(*args) -> float:
    """Core Data seconds of a naive local time."""
    return dt.datetime(*args).timestamp() - APPLE_EPOCH_OFFSET


class TestLocalDay:
    def test_none_is_today(self):
        assert _local_day(None) == dt.date.today()

    def test_date_and_naive_datetime(self):
        day = dt.date(2026, 10, 1)
        assert _local_day(day) == day
        assert _local_day(dt.datetime(2026, 10, 1, 23, 59)) == _local_day(day)
        assert _local_day(dt.datetime(2026, 10, 1, 0, 0)) == day
        assert type(_local_day(dt.datetime(2026, 10, 1))) is dt.date

    def test_aware_datetime_is_its_local_date(self):
        moment = dt.datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
        assert _local_day(moment) == moment.astimezone().date()

    @pytest.mark.parametrize("value", ["2026-10-01", 20261001, True, 1.5, object()])
    def test_other_types_raise(self, value):
        with pytest.raises(InvalidArgumentError) as exc:
            _local_day(value)
        assert type(value).__name__ in str(exc.value)
        assert "2026" not in str(exc.value)

    def test_name_is_used_in_messages(self):
        with pytest.raises(InvalidArgumentError, match="^when must be"):
            _local_day("x", name="when")

    @pytest.mark.parametrize("value", [
        # Instants before 0001-01-01 or after 9999-12-31 UTC: out of range
        # in every local time zone (UTC itself included).
        dt.datetime.min.replace(tzinfo=dt.timezone(dt.timedelta(hours=23, minutes=59))),
        dt.datetime.max.replace(tzinfo=dt.timezone(-dt.timedelta(hours=23, minutes=59))),
    ])
    def test_out_of_range_aware_datetime(self, value):
        with pytest.raises(InvalidArgumentError, match="out of the range"):
            _local_day(value)


class TestWindow:
    def test_no_bounds(self):
        assert _resolve_window(None, None) == (None, None, True)
        assert _window_filters("creation_date", None, None) == {}

    def test_datetime_bounds_are_inclusive_instants(self):
        after, before = dt.datetime(2026, 1, 1, 8), dt.datetime(2026, 1, 2, 9, 30)
        lo, hi, inclusive = _resolve_window(after, before)
        assert (lo, hi, inclusive) == (after.timestamp() - APPLE_EPOCH_OFFSET,
                                       before.timestamp() - APPLE_EPOCH_OFFSET, True)
        aware = dt.datetime(2026, 1, 1, 8, tzinfo=UTC)
        assert _resolve_window(aware, None)[0] == aware.timestamp() - APPLE_EPOCH_OFFSET

    def test_date_bounds_cover_whole_local_days(self):
        lo, hi, inclusive = _resolve_window(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        assert lo == local(2026, 1, 1) and hi == local(2027, 1, 1) and inclusive is False
        assert _window_filters("finished_date", dt.date(2026, 1, 1), dt.date(2026, 12, 31)) == {
            "finished_date__gte": local(2026, 1, 1), "finished_date__lt": local(2027, 1, 1)}

    def test_extreme_days(self):
        assert _day_end(dt.date.max) == float("inf")
        assert _day_start(dt.date.min) == float("-inf")
        assert _resolve_window(dt.datetime.min, None)[0] == float("-inf")

    @pytest.mark.parametrize("bad", ["2026", 5, False, 1.0])
    def test_other_types_raise_with_the_parameter_name(self, bad):
        with pytest.raises(InvalidArgumentError, match="^after must be"):
            _resolve_window(bad, None)
        with pytest.raises(InvalidArgumentError, match="^finished_before must be"):
            _resolve_window(None, bad, names=("finished_after", "finished_before"))


@pytest.fixture
def day_rows(library):
    """Highlights at the edges of 1 October 2026, local time."""
    book = library.add_book("Dated")
    rows = {}
    for name, when in (("before", local(2026, 9, 30, 23, 59, 59, 999000)),
                       ("midnight", local(2026, 10, 1)),
                       ("noon", local(2026, 10, 1, 12)),
                       ("late", local(2026, 10, 1, 23, 59, 59, 999000)),
                       ("next", local(2026, 10, 2))):
        rows[name] = library.add_annotation(book, f"text {name}", created=when)
    return rows


class TestDateRangeAcceptsDates:
    def ids(self, result):
        return sorted(a.id for a in result)

    def test_a_date_covers_the_whole_local_day(self, api, day_rows):
        day = dt.date(2026, 10, 1)
        r = day_rows
        assert self.ids(api.get_annotations_by_date_range(after=day, before=day)) == sorted(
            [r["midnight"], r["noon"], r["late"]])
        assert self.ids(api.get_annotations_by_date_range(after=day)) == sorted(
            [r["midnight"], r["noon"], r["late"], r["next"]])
        assert self.ids(api.get_annotations_by_date_range(before=day)) == sorted(
            [r["before"], r["midnight"], r["noon"], r["late"]])

    def test_datetime_bounds_are_unchanged(self, api, day_rows, sql_trace):
        after, before = dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 1, 12)
        r = day_rows
        assert self.ids(api.get_annotations_by_date_range(after=after, before=before)) == sorted(
            [r["midnight"], r["noon"]])
        sql, params = sql_trace[-1]
        assert ">= ?" in sql and "<= ?" in sql
        assert after.timestamp() - APPLE_EPOCH_OFFSET in params
        assert before.timestamp() - APPLE_EPOCH_OFFSET in params

    def test_date_equals_its_datetime_window(self, api, day_rows):
        by_date = api.get_annotations_by_date_range(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        by_datetime = api.get_annotations_by_date_range(dt.datetime(2026, 1, 1),
                                                        dt.datetime(2026, 12, 31, 23, 59, 59, 999999))
        assert by_date.count() == by_datetime.count() == len(day_rows)

    def test_a_string_still_raises_attribute_error(self, api, day_rows):
        with pytest.raises(AttributeError):
            api.get_annotations_by_date_range(after="2026-10-01")


@pytest.fixture
def finished(library):
    rows = {
        "jan": library.add_book("January", finished=True, finished_date=local(2026, 1, 1)),
        "mid": library.add_book("Midyear", finished=True, finished_date=local(2026, 6, 15, 12)),
        "dec": library.add_book("December", finished=True,
                                finished_date=local(2026, 12, 31, 23, 59, 59, 999000)),
        "last_year": library.add_book("Last Year", finished=True, finished_date=local(2025, 12, 31, 23)),
        "undated": library.add_book("Undated", finished=True),
        "stray": library.add_book("Stray date", finished_date=local(2026, 3, 1)),
        "unowned": library.add_book("Unowned volume", finished=True, data_source=STORE_SERIES,
                                    finished_date=local(2026, 3, 1)),
    }
    return {k: v["id"] for k, v in rows.items()}


class TestFinishedWindow:
    def ids(self, result):
        return sorted(b.id for b in result)

    def test_unbounded_is_the_1_10_list(self, api, finished, sql_trace):
        result = api.get_finished_books()
        assert self.ids(result) == sorted(finished[k] for k in ("jan", "mid", "dec", "last_year", "undated"))
        assert "ZDATEFINISHED" not in sql_trace[-1][0].partition(" WHERE ")[2]

    def test_a_year_of_dates(self, api, finished):
        result = api.get_finished_books(finished_after=dt.date(2026, 1, 1), finished_before=dt.date(2026, 12, 31))
        assert self.ids(result) == sorted(finished[k] for k in ("jan", "mid", "dec"))
        assert result.count() == len(result) == 3

    def test_datetime_bounds_are_inclusive(self, api, finished):
        bound = dt.datetime.fromtimestamp(local(2026, 6, 15, 12) + APPLE_EPOCH_OFFSET)
        assert self.ids(api.get_finished_books(finished_after=bound)) == sorted(
            [finished["mid"], finished["dec"]])
        assert self.ids(api.get_finished_books(finished_before=bound)) == sorted(
            [finished["jan"], finished["mid"], finished["last_year"]])

    def test_order_limit_and_offset(self, api, finished):
        result = api.get_finished_books(order_by="finished_date", finished_after=dt.date(2025, 1, 1))
        assert [b.id for b in result] == [finished[k] for k in ("last_year", "jan", "mid", "dec")]
        page = api.get_finished_books(2, "-finished_date", offset=1, finished_after=dt.date(2025, 1, 1))
        assert [b.id for b in page] == [finished["mid"], finished["jan"]]

    def test_bounds_are_keyword_only_after_offset(self):
        params = list(inspect.signature(PyAppleBooks.get_finished_books).parameters.values())
        assert [p.name for p in params] == ["self", "limit", "order_by", "offset", "finished_after",
                                            "finished_before"]
        assert all(p.kind is p.KEYWORD_ONLY and p.default is None for p in params[3:])

    def test_bad_bound(self, api, finished):
        with pytest.raises(InvalidArgumentError, match="^finished_after must be a date"):
            api.get_finished_books(finished_after="2026")

    def test_drift_without_the_finish_date_column(self, make_library):
        lib = make_library()
        lib.add_book("Done", finished=True, finished_date=local(2026, 2, 1))
        lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZDATEFINISHED")
        with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
            api = PyAppleBooks()
            assert [b.title for b in api.get_finished_books()] == ["Done"]
            with pytest.raises(UnsupportedSchemaError, match="ZDATEFINISHED"):
                list(api.get_finished_books(finished_after=dt.date(2026, 1, 1)))


# -- time zones (subprocess) --------------------------------------------------

_TZ_SCRIPT = textwrap.dedent(r'''
    import datetime as dt, json, sys, tempfile, shutil, pathlib
    from py_apple_books import PyAppleBooks
    from py_apple_books.engagement import _local_day
    from py_apple_books.testing import FixtureLibrary
    from py_apple_books.utils import APPLE_EPOCH_OFFSET

    def local(*a):
        return dt.datetime(*a).timestamp() - APPLE_EPOCH_OFFSET

    spec = json.loads(sys.argv[1])
    root = pathlib.Path(tempfile.mkdtemp(dir=sys.argv[2]))
    try:
        lib = FixtureLibrary.create(root)
        book = lib.add_book("Zoned")
        ids = {name: lib.add_annotation(book, name, created=local(*when))
               for name, when in spec["rows"].items()}
        api = PyAppleBooks(data_dir=lib.data_dir)
        day = dt.date(*spec["day"])
        out = {
            "ids": ids,
            "range": sorted(a.id for a in api.get_annotations_by_date_range(after=day, before=day)),
            "aware_day": _local_day(dt.datetime(2026, 10, 1, 2, 0, tzinfo=dt.timezone.utc)).isoformat(),
            "day_hours": (local(*spec["next"]) - local(*spec["day"])) / 3600,
        }
        api.close()
        print(json.dumps(out))
    finally:
        shutil.rmtree(root)
''')


def run_in_zone(tz: str, spec: dict, tmp_path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env["TZ"] = tz
    done = subprocess.run([sys.executable, "-c", _TZ_SCRIPT, json.dumps(spec), str(tmp_path)],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


@pytest.mark.parametrize("tz, prev_day, day, next_day, hours", [
    ("Europe/Berlin", (2026, 3, 28), (2026, 3, 29), (2026, 3, 30), 23),     # spring forward
    ("Europe/Berlin", (2026, 10, 24), (2026, 10, 25), (2026, 10, 26), 25),  # fall back
    ("America/New_York", (2026, 3, 7), (2026, 3, 8), (2026, 3, 9), 23),
    ("America/New_York", (2026, 10, 31), (2026, 11, 1), (2026, 11, 2), 25),
])
def test_date_bounds_on_dst_days(tmp_path, tz, prev_day, day, next_day, hours):
    spec = {"day": day, "next": next_day, "rows": {
        "early": [*day, 0, 30], "late": [*day, 23, 30], "after": [*next_day, 0, 30],
        "before": [*prev_day, 23, 30]}}
    out = run_in_zone(tz, spec, tmp_path)
    assert out["day_hours"] == hours
    assert out["range"] == sorted([out["ids"]["early"], out["ids"]["late"]])


def test_aware_utc_instant_is_the_local_day(tmp_path):
    spec = {"day": (2026, 10, 1), "next": (2026, 10, 2), "rows": {}}
    assert run_in_zone("America/Los_Angeles", spec, tmp_path)["aware_day"] == "2026-09-30"
    assert run_in_zone("Asia/Tokyo", spec, tmp_path)["aware_day"] == "2026-10-01"
