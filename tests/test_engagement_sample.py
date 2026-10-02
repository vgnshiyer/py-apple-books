"""``get_highlights_on_this_day`` (1.11). Synthetic data only."""

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import InvalidArgumentError, UnsupportedSchemaError
from py_apple_books.utils import APPLE_EPOCH_OFFSET

DAY = dt.date(2026, 10, 1)
UTC = dt.timezone.utc


def local(*args) -> float:
    """Core Data seconds of a naive local time."""
    return dt.datetime(*args).timestamp() - APPLE_EPOCH_OFFSET


def ids(result) -> list:
    return [a.id for a in result]


# -- on this day --------------------------------------------------------------------


class TestOnThisDay:
    @pytest.fixture
    def days(self, library):
        book = library.add_book("Days")
        r = {
            "midnight": library.add_annotation(book, "m", created=local(2024, 10, 1, 0, 0)),
            "noon": library.add_annotation(book, "n", kind="note", note="x", created=local(2023, 10, 1, 12)),
            "late": library.add_annotation(book, "l", created=local(2022, 10, 1, 23, 59)),
            "next": library.add_annotation(book, "x", created=local(2024, 10, 2, 0, 0)),
            "eve": library.add_annotation(book, "e", created=local(2024, 9, 30, 23, 59, 59)),
            "this_year": library.add_annotation(book, "t", created=local(2026, 10, 1, 9)),
            "deleted": library.add_annotation(book, "d", deleted=True, created=local(2021, 10, 1, 9)),
            "orphan": library.add_annotation("GONE-ASSET", "o", created=local(2020, 10, 1, 9)),
            "bookmark": library.add_annotation(book, None, kind="bookmark", created=local(2019, 10, 1, 9)),
            "undated": library.add_annotation(book, "u", created=None),
        }
        return r

    def got(self, api, *args, **kwargs):
        return ids(api.get_highlights_on_this_day(*args, **kwargs))

    def test_earlier_years_newest_first(self, api, days):
        assert self.got(api, DAY) == [days[k] for k in ("midnight", "noon", "late", "orphan")]

    def test_scope_options(self, api, days):
        assert days["orphan"] not in self.got(api, DAY, include_orphans=False)
        assert days["deleted"] in self.got(api, DAY, include_deleted=True)

    def test_datetime_on(self, api, days):
        assert self.got(api, dt.datetime(2026, 10, 1, 23, 59)) == self.got(api, DAY)

    def test_today_by_default(self, api, library):
        book = library.add_book("Now")
        today = dt.date.today()
        try:
            last_year = today.replace(year=today.year - 1)
        except ValueError:  # 29 February
            pytest.skip("no 29 February last year")
        row = library.add_annotation(book, "then", created=local(last_year.year, last_year.month, last_year.day, 9))
        library.add_annotation(book, "now", created=local(today.year, today.month, today.day, 0, 0, 1))
        assert self.got(api) == [row]

    def test_paging_and_counts(self, api, days):
        result = api.get_highlights_on_this_day(DAY)
        assert result.count() == len(result) == 4
        assert self.got(api, DAY, 2) + self.got(api, DAY, 2, offset=2) == self.got(api, DAY)
        assert sorted(self.got(api, DAY, order_by=None)) == sorted(self.got(api, DAY))
        assert sum(result.count_by("asset_id").values()) == 4

    def test_leap_day(self, api, library):
        book = library.add_book("Leap")
        leap = library.add_annotation(book, "leap", created=local(2024, 2, 29, 10))
        library.add_annotation(book, "march", created=local(2023, 3, 1, 10))
        library.add_annotation(book, "feb 28", created=local(2023, 2, 28, 10))
        assert self.got(api, dt.date(2028, 2, 29)) == [leap]
        assert leap not in self.got(api, dt.date(2027, 2, 28)) and leap not in self.got(api, dt.date(2027, 3, 1))

    def test_extreme_days(self, api, library):
        book = library.add_book("Old")
        jan = library.add_annotation(book, "jan", created=local(2005, 1, 1, 10))
        library.add_annotation(book, "june", created=local(2005, 6, 1, 10))
        assert self.got(api, dt.date(2001, 6, 1)) == []
        assert self.got(api, dt.date(1999, 1, 1)) == []
        assert self.got(api, dt.date(9999, 1, 1)) == [jan]
        assert self.got(api, dt.date.min) == []

    def test_equals_the_union_of_yearly_windows(self, api, days, library):
        for day in (DAY, dt.date(2026, 10, 2), dt.date(2026, 9, 30)):
            union = set()
            for year in range(2001, day.year):
                one = dt.date(year, day.month, day.day)
                union |= {a.id for a in api.get_annotations_by_date_range(one, one) if a.type == 2}
            assert set(self.got(api, day)) == union

    def test_validation(self, api):
        for kwargs in ({"on": "today"}, {"limit": 0}, {"offset": -2}):
            with pytest.raises(InvalidArgumentError):
                api.get_highlights_on_this_day(**kwargs)

    def test_without_a_creation_date_column(self, make_library):
        lib = make_library()
        lib.add_annotation(lib.add_book("B"), "x")
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONCREATIONDATE")
        with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
            with pytest.raises(UnsupportedSchemaError, match="ZANNOTATIONCREATIONDATE"):
                list(PyAppleBooks().get_highlights_on_this_day(DAY))


_DST_SCRIPT = textwrap.dedent(r'''
    import datetime as dt, json, sys, tempfile, shutil, pathlib
    from py_apple_books import PyAppleBooks
    from py_apple_books.testing import FixtureLibrary
    from py_apple_books.utils import APPLE_EPOCH_OFFSET

    def local(day, *time):
        return dt.datetime(day.year, day.month, day.day, *time).timestamp() - APPLE_EPOCH_OFFSET

    root = pathlib.Path(tempfile.mkdtemp(dir=sys.argv[2]))
    try:
        lib = FixtureLibrary.create(root)
        book = lib.add_book("Zoned")
        change = dt.date(*json.loads(sys.argv[1]))   # a DST change day
        one = dt.timedelta(days=1)
        early = lib.add_annotation(book, "early", created=local(change, 0, 30))
        late = lib.add_annotation(book, "late", created=local(change, 23, 30))
        lib.add_annotation(book, "eve", created=local(change - one, 23, 30))
        lib.add_annotation(book, "next", created=local(change + one, 0, 30))
        api = PyAppleBooks(data_dir=lib.data_dir)
        on = change.replace(year=change.year + 1)
        print(json.dumps({"got": [a.id for a in api.get_highlights_on_this_day(on)], "want": [late, early]}))
        api.close()
    finally:
        shutil.rmtree(root)
''')


@pytest.mark.parametrize("tz, change", [
    ("Europe/Berlin", (2026, 3, 29)), ("Europe/Berlin", (2026, 10, 25)),
    ("America/New_York", (2026, 3, 8)), ("America/New_York", (2026, 11, 1)),
])
def test_on_this_day_across_dst(tmp_path, tz, change):
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env["TZ"] = tz
    done = subprocess.run([sys.executable, "-c", _DST_SCRIPT, json.dumps(change), str(tmp_path)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert out["got"] == out["want"]
