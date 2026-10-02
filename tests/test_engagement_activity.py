"""``get_highlight_activity`` and ``get_highlight_streaks`` (1.11, Tier
B). Synthetic data only; time-zone cases run in a subprocess with ``TZ``
set."""

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from py_apple_books.engagement import ActivityPeriod, HighlightActivity, HighlightStreaks, _streaks
from py_apple_books.exceptions import BookNotFoundError, InvalidArgumentError, InvalidChoiceError
from py_apple_books.utils import APPLE_EPOCH_OFFSET
from tests import engagement_helpers

D = dt.date


def local(*args) -> float:
    return dt.datetime(*args).timestamp() - APPLE_EPOCH_OFFSET


@pytest.fixture
def activity(library):
    one, two = library.add_book("One"), library.add_book("Two")
    r = {"one": one, "two": two}
    for name, book, when, kind in (
            ("a", one, (2026, 12, 31, 10), "highlight"),       # Thursday of ISO week 53 of 2026
            ("b", one, (2027, 1, 2, 9), "note"),                # same ISO week, next year
            ("c", two, (2027, 1, 2, 22), "highlight"),
            ("d", two, (2027, 1, 4, 8), "highlight"),           # Monday: the next week
            ("e", "GONE-A", (2027, 2, 1, 12), "highlight"),     # removed books
            ("f", "GONE-B", (2027, 2, 1, 13), "note"),
            ("g", "GONE-B", (2027, 3, 15, 13), "underline")):
        r[name] = library.add_annotation(book, f"text {name}", kind=kind, created=local(*when),
                                         note="a note" if kind == "note" else None)
    r["deleted"] = library.add_annotation(one, "x", deleted=True, created=local(2027, 1, 5))
    r["undated"] = library.add_annotation(one, "y", created=None)
    r["bookmark"] = library.add_annotation(one, None, kind="bookmark", created=local(2027, 1, 6))
    r["position"] = library.add_annotation(one, None, kind="reading_position", created=local(2027, 1, 6))
    r["tombstone"] = library.add_annotation(None, None, kind="tombstone")
    return r


def check_invariants(result: HighlightActivity):
    assert len(result.per_book) + result.orphan_books == result.books
    assert sum(n for _, _, n in result.per_book) + result.orphan_highlights == result.highlights
    if result.granularity is not None:
        assert sum(p.highlights for p in result.periods) == result.highlights
        assert sum(p.notes for p in result.periods) == result.notes
        assert all(p.start < p.end for p in result.periods)
        assert [p.start for p in result.periods] == sorted(p.start for p in result.periods)
    else:
        assert result.periods == ()


class TestActivity:
    def test_totals(self, api, activity):
        result = api.get_highlight_activity()
        check_invariants(result)
        assert (result.highlights, result.notes, result.active_days) == (7, 2, 5)
        assert (result.books, result.orphan_books, result.orphan_highlights) == (4, 2, 3)
        assert result.per_book == ((activity["one"]["id"], "One", 2), (activity["two"]["id"], "Two", 2))
        assert result.first == dt.datetime(2026, 12, 31, 10) and result.last == dt.datetime(2027, 3, 15, 13)
        assert (result.after, result.before, result.granularity) == (None, None, "month")

    def test_months(self, api, activity):
        periods = api.get_highlight_activity().periods
        assert periods == (
            ActivityPeriod(D(2026, 12, 1), D(2027, 1, 1), highlights=1, notes=0, books=1, active_days=1),
            ActivityPeriod(D(2027, 1, 1), D(2027, 2, 1), highlights=3, notes=1, books=2, active_days=2),
            ActivityPeriod(D(2027, 2, 1), D(2027, 3, 1), highlights=2, notes=1, books=2, active_days=1),
            ActivityPeriod(D(2027, 3, 1), D(2027, 4, 1), highlights=1, notes=0, books=1, active_days=1),
        )

    def test_iso_weeks_across_the_year(self, api, activity):
        periods = api.get_highlight_activity(granularity="week").periods
        assert [(p.start, p.end, p.highlights) for p in periods] == [
            (D(2026, 12, 28), D(2027, 1, 4), 3), (D(2027, 1, 4), D(2027, 1, 11), 1),
            (D(2027, 2, 1), D(2027, 2, 8), 2), (D(2027, 3, 15), D(2027, 3, 22), 1)]
        assert D(2026, 12, 28).isocalendar()[1] == 53

    def test_days_and_years(self, api, activity):
        days = api.get_highlight_activity(granularity="day").periods
        assert [(p.start, p.highlights) for p in days] == [
            (D(2026, 12, 31), 1), (D(2027, 1, 2), 2), (D(2027, 1, 4), 1), (D(2027, 2, 1), 2), (D(2027, 3, 15), 1)]
        assert all(p.end == p.start + dt.timedelta(days=1) for p in days)
        years = api.get_highlight_activity(granularity="year").periods
        assert [(p.start, p.end, p.highlights) for p in years] == [
            (D(2026, 1, 1), D(2027, 1, 1), 1), (D(2027, 1, 1), D(2028, 1, 1), 6)]
        none = api.get_highlight_activity(granularity=None)
        check_invariants(none)
        assert none.highlights == 7

    def test_window(self, api, activity):
        result = api.get_highlight_activity(after=D(2027, 1, 1), before=D(2027, 1, 31))
        check_invariants(result)
        assert result.highlights == 3 and result.books == 2 and result.orphan_books == 0
        assert (result.after, result.before) == (D(2027, 1, 1), D(2027, 1, 31))

    @pytest.mark.parametrize("after, before", [
        (D(2027, 1, 1), D(2027, 2, 1)), (D(2026, 12, 31), None), (None, D(2027, 1, 2)),
        (dt.datetime(2027, 1, 2, 9), dt.datetime(2027, 2, 1, 12)), (dt.datetime(2027, 1, 2, 9, 0, 1), None),
    ])
    def test_matches_the_date_range(self, api, activity, after, before):
        result = api.get_highlight_activity(after=after, before=before)
        check_invariants(result)
        listed = [a for a in api.get_annotations_by_date_range(after, before) if a.type == 2]
        assert result.highlights == len(listed)

    def test_unreadable_date_in_the_window(self, api, library):
        # The SQL window keeps a stored date beyond datetime's range; the
        # model reads it as None and the activity does not count it.
        book = library.add_book("B")
        library.add_annotation(book, "dated", created=local(2027, 1, 2, 9))
        library.add_annotation(book, "far", created=1e300)
        listed = [a for a in api.get_annotations_by_date_range(D(2020, 1, 1)) if a.type == 2]
        assert len(listed) == 2
        result = api.get_highlight_activity(after=D(2020, 1, 1))
        check_invariants(result)
        assert result.highlights == len([a for a in listed if a.creation_date is not None]) == 1

    def test_book(self, api, activity):
        result = api.get_highlight_activity(book_id=activity["two"]["id"], granularity="day")
        check_invariants(result)
        assert result.highlights == 2 and result.per_book == ((activity["two"]["id"], "Two", 2),)
        assert api.get_highlight_activity(book_id=api.get_book_by_id(activity["one"]["id"])).highlights == 2
        with pytest.raises(BookNotFoundError):
            api.get_highlight_activity(book_id=999)

    def test_per_book_ties_and_shared_asset(self, api, library):
        first = library.add_book("First", asset_id="SHARED")
        library.add_book("Second row", asset_id="SHARED")
        other = library.add_book("Other")
        library.add_annotation(other, "o", created=local(2027, 1, 1))
        library.add_annotation(first, "s", created=local(2027, 1, 1))
        assert api.get_highlight_activity().per_book == ((first["id"], "First", 1), (other["id"], "Other", 1))

    def test_empty(self, api):
        result = api.get_highlight_activity()
        check_invariants(result)
        assert (result.highlights, result.books, result.first, result.last, result.periods) == (0, 0, None, None, ())

    def test_validation(self, api):
        with pytest.raises(InvalidChoiceError):
            api.get_highlight_activity(granularity="quarter")
        with pytest.raises(InvalidChoiceError):
            api.get_highlight_activity(granularity=7)
        with pytest.raises(InvalidArgumentError):
            api.get_highlight_activity(after="2027")
        with pytest.raises(TypeError):
            api.get_highlight_activity(D(2027, 1, 1))   # keyword-only

    @pytest.mark.parametrize("count", [10_000] + ([50_000] if engagement_helpers.SLOW else []))
    def test_time(self, api, library, count):
        library.populate(books=100, annotations_per_book=count // 100)
        api.get_highlight_activity()
        start = time.perf_counter()
        result = api.get_highlight_activity(granularity="day")
        elapsed = time.perf_counter() - start
        assert result.highlights == count
        assert elapsed < (0.5 if engagement_helpers.SLOW else 3.0), f"{elapsed:.3f} s"


class TestStreaks:
    def test_pure_rule(self):
        days = [D(2027, 1, 1), D(2027, 1, 2), D(2027, 1, 3), D(2027, 1, 10), D(2027, 1, 11), D(2027, 1, 12)]
        s = _streaks(days, D(2027, 1, 13))
        assert (s.current, s.current_start) == (3, D(2027, 1, 10))          # alive through yesterday
        assert (s.longest, s.longest_start, s.longest_end) == (3, D(2027, 1, 1), D(2027, 1, 3))  # earliest
        assert (s.last_active, s.active_days, s.on) == (D(2027, 1, 12), 6, D(2027, 1, 13))
        assert _streaks(days, D(2027, 1, 12)).current == 3
        assert _streaks(days, D(2027, 1, 14)).current == 0            # a gap
        assert _streaks(days, D(2027, 1, 14)).current_start is None
        past = _streaks(days, D(2027, 1, 2))
        assert (past.current, past.longest, past.active_days, past.last_active) == (2, 2, 2, D(2027, 1, 2))
        empty = _streaks([], D(2027, 1, 1))
        assert empty == HighlightStreaks(D(2027, 1, 1), 0, None, 0, None, None, None, 0)
        assert _streaks([D(1, 1, 1)], D.min).current == 1

    def test_from_the_library(self, api, activity):
        s = api.get_highlight_streaks(on=D(2027, 1, 5))
        assert (s.current, s.current_start, s.last_active) == (1, D(2027, 1, 4), D(2027, 1, 4))
        assert (s.longest, s.longest_start, s.longest_end) == (1, D(2026, 12, 31), D(2026, 12, 31))
        assert s.active_days == 3
        assert api.get_highlight_streaks(on=dt.datetime(2027, 1, 5, 23, 59)) == s
        later = api.get_highlight_streaks(on=D(2027, 6, 1))
        assert later.current == 0 and later.active_days == 5 and later.last_active == D(2027, 3, 15)

    def test_runs(self, api, library):
        book = library.add_book("Daily")
        for day in (1, 2, 3, 3, 5, 6):
            library.add_annotation(book, "x", created=local(2027, 1, day, 12))
        library.add_annotation(book, "gone", deleted=True, created=local(2027, 1, 4, 12))
        library.add_annotation(book, None, kind="bookmark", created=local(2027, 1, 4, 12))
        s = api.get_highlight_streaks(on=D(2027, 1, 7))
        assert (s.current, s.current_start, s.longest, s.longest_start) == (2, D(2027, 1, 5), 3, D(2027, 1, 1))

    def test_today_by_default(self, api, library):
        book = library.add_book("Now")
        now = dt.datetime.now()
        library.add_annotation(book, "x", created=now.timestamp() - APPLE_EPOCH_OFFSET - 1)
        s = api.get_highlight_streaks()
        assert s.on == D.today() and s.current >= 1

    def test_empty_and_validation(self, api):
        s = api.get_highlight_streaks(on=D(2027, 1, 1))
        assert (s.current, s.longest, s.active_days, s.last_active) == (0, 0, 0, None)
        with pytest.raises(InvalidArgumentError):
            api.get_highlight_streaks(on="today")


_TZ_SCRIPT = textwrap.dedent(r'''
    import datetime as dt, json, sys, tempfile, shutil, pathlib
    from py_apple_books import PyAppleBooks
    from py_apple_books.testing import FixtureLibrary
    from py_apple_books.utils import APPLE_EPOCH_OFFSET

    root = pathlib.Path(tempfile.mkdtemp(dir=sys.argv[1]))
    try:
        lib = FixtureLibrary.create(root)
        book = lib.add_book("Zoned")
        for when in ((2026, 3, 29, 0, 30), (2026, 3, 28, 23, 30), (2026, 10, 25, 0, 30), (2026, 10, 25, 23, 30)):
            lib.add_annotation(book, "x", created=dt.datetime(*when).timestamp() - APPLE_EPOCH_OFFSET)
        api = PyAppleBooks(data_dir=lib.data_dir)
        days = api.get_highlight_activity(granularity="day").periods
        streaks = api.get_highlight_streaks(on=dt.date(2026, 10, 26))
        print(json.dumps({"days": [[p.start.isoformat(), p.highlights] for p in days],
                          "current": streaks.current, "longest": streaks.longest}))
        api.close()
    finally:
        shutil.rmtree(root)
''')


def test_local_days_across_dst(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env["TZ"] = "Europe/Berlin"
    done = subprocess.run([sys.executable, "-c", _TZ_SCRIPT, str(tmp_path)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert out["days"] == [["2026-03-28", 1], ["2026-03-29", 1], ["2026-10-25", 2]]
    assert (out["current"], out["longest"]) == (1, 2)
