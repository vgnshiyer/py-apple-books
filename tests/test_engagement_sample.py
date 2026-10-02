"""``sample_highlights`` (pab-sample-v1) and ``get_highlights_on_this_day``
(1.11). Synthetic data only.

The sample is checked against an independent oracle (this file's own
implementation of the documented algorithm over the raw store rows) and
against golden ids that pin ``pab-sample-v1``.
"""

import datetime as dt
import hashlib
import json
import math
import os
import random
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books import engagement
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import BookNotFoundError, InvalidArgumentError, UnsupportedSchemaError
from py_apple_books.models.manager import ModelIterable
from py_apple_books.testing import seed_demo
from py_apple_books.utils import APPLE_EPOCH_OFFSET
from tests import engagement_helpers

DAY = dt.date(2026, 10, 1)
UTC = dt.timezone.utc


def local(*args) -> float:
    """Core Data seconds of a naive local time."""
    return dt.datetime(*args).timestamp() - APPLE_EPOCH_OFFSET


def ids(result) -> list:
    return [a.id for a in result]


# -- the oracle -----------------------------------------------------------------


def oracle_key(day: dt.date, seed, ident: str, noted: bool) -> int:
    data = f"{day.isoformat()}\x1f{seed or ''}\x1f{ident}".encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(data, digest_size=8, person=b"pab-sample-v1").digest()
    h = (int.from_bytes(digest, "big") >> 11) | 1
    return h << 53 if noted else h * h


def oracle(lib, day=DAY, seed=None, *, per_book=True, include_orphans=False, exclude_short=True,
           asset=None) -> list:
    """The documented sample order, computed from the raw store rows."""
    con = sqlite3.connect(lib.annotation_path)
    try:
        con.execute("ATTACH DATABASE ? AS lib", (str(lib.library_path),))
        books = {r[0] for r in con.execute("SELECT ZASSETID FROM lib.ZBKLIBRARYASSET") if r[0] is not None}
        rows = con.execute(
            "SELECT Z_PK, ZANNOTATIONUUID, ZANNOTATIONASSETID, ZANNOTATIONNOTE, ZANNOTATIONSELECTEDTEXT, "
            "ZANNOTATIONCREATIONDATE FROM ZAEANNOTATION WHERE ZANNOTATIONTYPE = 2 "
            "AND ZANNOTATIONDELETED IS NOT 1").fetchall()
    finally:
        con.close()
    end = dt.datetime(day.year, day.month, day.day).timestamp() + 86400 - APPLE_EPOCH_OFFSET
    from py_apple_books.text import is_short_selection
    scored = []
    for pk, uuid, asset_id, note, text, created in rows:
        if not text or not text.strip() or (created is not None and created >= end):
            continue
        if not include_orphans and asset_id not in books:
            continue
        if asset is not None and asset_id != asset:
            continue
        if exclude_short and is_short_selection(text):
            continue
        ident = uuid.upper() if uuid else f"pk:{pk}"
        scored.append((oracle_key(day, seed, ident, bool(note and note.strip())), pk, asset_id))
    scored.sort(key=lambda s: (-s[0], s[1]))
    if not per_book:
        return [pk for _, pk, _ in scored]
    nth, rounds = {}, []
    for key, pk, asset_id in scored:
        n = nth.get(asset_id, 0)
        nth[asset_id] = n + 1
        rounds.append((n, -key, pk))
    return [pk for _, _, pk in sorted(rounds)]


@pytest.fixture
def populated(library):
    made = library.populate(books=12, annotations_per_book=20)
    return made


# -- the algorithm ------------------------------------------------------------------


class TestAlgorithm:
    def test_name(self):
        assert engagement.SAMPLE_ALGORITHM == "pab-sample-v1"
        assert "SAMPLE_ALGORITHM" in engagement.__all__

    def test_key_golden(self):
        # Pins pab-sample-v1's hash input, personalisation and weighting.
        ident = "5F0C7C4E-2D0B-4C7E-9A52-6A1F3B0E8D11"
        assert engagement._sample_key(DAY, None, ident, False) == oracle_key(DAY, None, ident, False)
        assert engagement._sample_key(DAY, "s", ident, True) == oracle_key(DAY, "s", ident, True)
        assert engagement._sample_key(DAY, None, ident, False) == GOLDEN_KEY

    def test_ident(self):
        assert engagement._sample_ident("ab-cd", 5) == "AB-CD"
        assert engagement._sample_ident(None, 5) == engagement._sample_ident("", 5) == "pk:5"
        assert engagement._sample_ident(b"ab", 5) == "AB"

    def test_integer_order_equals_the_float_order(self):
        rng = random.Random(11)
        cands = []
        for pk in range(50_000):
            h = (rng.getrandbits(64) >> 11) | 1
            noted = rng.random() < 0.2
            cands.append((h << 53 if noted else h * h, pk, h, noted))
        by_int = [pk for _, pk, _, _ in sorted(cands, key=lambda c: (-c[0], c[1]))]
        # Efraimidis-Spirakis: largest u ** (1 / w), i.e. smallest log(-log u) - log w.
        by_float = [pk for _, pk, _, _ in sorted(
            cands, key=lambda c: (math.log(-math.log(c[2] / 2.0 ** 53)) - math.log(2 if c[3] else 1), c[1]))]
        assert by_int == by_float

    @pytest.mark.parametrize("digest", [b"\xff" * 8, b"\x00" * 8])
    def test_extreme_digests(self, monkeypatch, digest):
        class Fake:
            def __init__(self, *a, **k):
                pass

            def digest(self):
                return digest

        monkeypatch.setattr(engagement.hashlib, "blake2b", Fake)
        for noted in (False, True):
            key = engagement._sample_key(DAY, None, "x", noted)
            assert 0 < key < 2 ** 106

    def test_round_robin(self):
        ranked = engagement._rank([(5, 1, "a"), (9, 2, "a"), (7, 3, "b"), (9, 4, None), (1, 5, "b")])
        assert [pk for _, pk, _ in ranked] == [2, 4, 3, 1, 5]
        assert [pk for _, pk, _ in engagement._round_robin(ranked)] == [2, 4, 3, 1, 5]
        ranked = engagement._rank([(9, 1, "a"), (8, 2, "a"), (7, 3, "a"), (1, 4, "b")])
        assert [pk for _, pk, _ in engagement._round_robin(ranked)] == [1, 4, 2, 3]


# Computed once from the documented algorithm (see test_key_golden).
GOLDEN_KEY = 29459765075190644373671580301249
# populate(books=12, annotations_per_book=20), day 2026-10-01, no seed.
GOLDEN_IDS = [82, 147, 229, 235, 215, 53, 164, 156, 172, 93, 138, 218]


class TestGolden:
    def test_populated_golden_ids(self, api, populated, library):
        assert ids(api.sample_highlights(limit=12, on=DAY)) == GOLDEN_IDS
        assert ids(api.sample_highlights(limit=None, on=DAY)) == oracle(library)

    def test_seeded_and_per_book_match_the_oracle(self, api, populated, library):
        assert ids(api.sample_highlights(limit=None, on=DAY, seed="mine")) == oracle(library, seed="mine")
        book = populated["books"][3]
        assert ids(api.sample_highlights(limit=None, on=DAY, book_id=book["id"])) == oracle(
            library, per_book=False, asset=book["asset_id"])


class TestDeterminism:
    def test_two_instances(self, populated, library):
        first, second = PyAppleBooks(data_dir=library.data_dir), PyAppleBooks(data_dir=library.data_dir)
        try:
            assert ids(first.sample_highlights(on=DAY)) == ids(second.sample_highlights(on=DAY))
        finally:
            first.close()
            second.close()

    def test_datetime_on_is_its_day(self, api, populated):
        assert ids(api.sample_highlights(on=dt.datetime(2026, 10, 1, 23, 59))) == ids(
            api.sample_highlights(on=DAY))
        assert ids(api.sample_highlights(on=dt.datetime(2026, 10, 1, 0, 0, 1))) == ids(
            api.sample_highlights(on=DAY))

    def test_day_and_seed_change_the_order(self, api, populated):
        base = ids(api.sample_highlights(limit=None, on=DAY))
        assert ids(api.sample_highlights(limit=None, on=DAY + dt.timedelta(days=1))) != base
        assert ids(api.sample_highlights(limit=None, on=DAY, seed="x")) != base
        assert sorted(ids(api.sample_highlights(limit=None, on=DAY, seed="x"))) == sorted(base)

    def test_another_process_and_hash_seed(self, populated, library):
        script = textwrap.dedent('''
            import datetime as dt, json, sys
            from py_apple_books import PyAppleBooks
            api = PyAppleBooks(data_dir=sys.argv[1])
            print(json.dumps([a.id for a in api.sample_highlights(limit=None, on=dt.date(2026, 10, 1))]))
            api.close()
        ''')
        env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
        outs = []
        for hash_seed in ("1", "12345"):
            env["PYTHONHASHSEED"] = hash_seed
            done = subprocess.run([sys.executable, "-c", script, str(library.data_dir)], env=env,
                                  capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, done.stderr[-2000:]
            outs.append(json.loads(done.stdout))
        assert outs[0] == outs[1] == oracle(library)


class TestOrder:
    def test_spread_across_books(self, api, populated):
        picks = list(api.sample_highlights(limit=12, on=DAY))
        assert len({a.asset_id for a in picks}) == 12

    def test_stable_when_another_book_gains_a_highlight(self, api, populated, library):
        book_a = populated["books"][0]
        before = [a for a in api.sample_highlights(limit=None, on=DAY) if a.asset_id != book_a["asset_id"]]
        library.add_annotation(book_a, "a brand new passage that is long enough to count",
                               created=local(2026, 9, 1))
        after = [a for a in api.sample_highlights(limit=None, on=DAY) if a.asset_id != book_a["asset_id"]]
        assert ids(after) == ids(before)

    def test_pages_slice_the_order(self, api, populated, library):
        order = oracle(library)
        for size in (1, 5, 7):
            pages = []
            for start in range(0, len(order), size):
                pages += ids(api.sample_highlights(limit=size, offset=start, on=DAY))
            assert pages == order
        assert ids(api.sample_highlights(limit=3, offset=len(order) + 5, on=DAY)) == []


class TestResult:
    def test_iterable(self, api, populated, sql_trace):
        result = api.sample_highlights(limit=6, on=DAY)
        assert isinstance(result, ModelIterable)
        assert result.count() == len(result) == 6
        picks = list(result)
        assert result[1:3] == picks[1:3] and result.first() is picks[0]
        assert sum(result.count_by("asset_id").values()) == 6
        assert set(result.count_by("asset_id")) == {a.asset_id for a in picks}
        before = len(sql_trace)
        titles = [a.book.title for a in picks]
        assert all(titles) and len(sql_trace) - before == 1

    def test_full_rows(self, api, populated):
        pick = api.sample_highlights(limit=1, on=DAY)[0]
        assert pick.representative_text and pick.uuid and pick.creation_date and pick.color

    def test_many_picks_reread_the_scope(self, api, library, monkeypatch):
        from py_apple_books._api import engagement as mixin

        library.populate(books=3, annotations_per_book=4)
        monkeypatch.setattr(mixin, "_FETCH_BY_ID_MAX", 2)
        assert ids(api.sample_highlights(limit=None, on=DAY)) == oracle(library)

    def test_empty_library(self, api):
        result = api.sample_highlights(on=DAY)
        assert list(result) == [] and result.count() == 0 and result.first() is None


class TestCandidates:
    @pytest.fixture
    def rows(self, library):
        book, other = library.add_book("Kept"), library.add_book("Other")
        text = "a passage long enough to be sampled, about rivers."
        r = {
            "live": library.add_annotation(book, text, created=local(2026, 5, 1)),
            "noted": library.add_annotation(other, text, kind="note", note="why", created=local(2026, 5, 2)),
            "deleted": library.add_annotation(book, text, deleted=True, created=local(2026, 5, 3)),
            "blank": library.add_annotation(book, "   \n ", created=local(2026, 5, 4)),
            "null": library.add_annotation(book, None, created=local(2026, 5, 5)),
            "short": library.add_annotation(book, "Ephemeral,", created=local(2026, 5, 6)),
            "orphan": library.add_annotation("GONE-ASSET", text, created=local(2026, 5, 7)),
            "later": library.add_annotation(book, text, created=local(2026, 10, 2, 0, 0, 1)),
            "late_today": library.add_annotation(book, text, created=local(2026, 10, 1, 23, 59)),
            "undated": library.add_annotation(book, text, created=None),
            "bookmark": library.add_annotation(book, None, kind="bookmark"),
            "position": library.add_annotation(book, None, kind="reading_position"),
            "tombstone": library.add_annotation(None, None, kind="tombstone"),
        }
        r["book"], r["other"] = book, other
        return r

    def sampled(self, api, **kwargs):
        return set(ids(api.sample_highlights(limit=None, on=DAY, **kwargs)))

    def test_default_candidates(self, api, rows):
        assert self.sampled(api) == {rows[k] for k in ("live", "noted", "late_today", "undated")}

    def test_orphans_and_short(self, api, rows):
        assert rows["orphan"] in self.sampled(api, include_orphans=True)
        assert rows["short"] in self.sampled(api, exclude_short=False)

    def test_on_in_the_past(self, api, rows):
        got = set(ids(api.sample_highlights(limit=None, on=dt.date(2026, 5, 1))))
        assert got == {rows["live"], rows["undated"]}

    def test_window(self, api, rows):
        assert self.sampled(api, after=dt.date(2026, 5, 2), before=dt.date(2026, 10, 1)) == {
            rows["noted"], rows["late_today"]}
        assert self.sampled(api, before=dt.datetime(2026, 5, 1, 0, 0)) == {rows["live"]}

    def test_exclusions(self, api, rows, library):
        assert rows["live"] not in self.sampled(api, exclude_ids=[rows["live"]])
        assert rows["live"] not in self.sampled(api, exclude_ids=(str(rows["live"]),))
        uuid = library.execute("annotations", "SELECT ZANNOTATIONUUID FROM ZAEANNOTATION WHERE Z_PK = ?",
                               (rows["noted"],))[0][0]
        assert rows["noted"] not in self.sampled(api, exclude_uuids=[uuid.lower()])
        assert rows["noted"] not in self.sampled(api, exclude_uuids={uuid})

    @pytest.mark.parametrize("kwargs, match", [
        ({"exclude_ids": ["x"]}, "exclude_ids items"),
        ({"exclude_ids": [True]}, "not bool"),
        ({"exclude_ids": [1.5]}, "not float"),
        ({"exclude_ids": "12"}, "single str"),
        ({"exclude_ids": 12}, "iterable"),
        ({"exclude_uuids": [5]}, "must be strings"),
        ({"exclude_uuids": "ABC"}, "single str"),
        ({"seed": 5}, "seed must be"),
        ({"on": "2026-10-01"}, "on must be"),
        ({"after": 2026}, "after must be"),
        ({"limit": 0}, "limit"),
        ({"limit": -1}, "limit"),
        ({"offset": -1}, "offset"),
    ])
    def test_validation(self, api, kwargs, match):
        with pytest.raises(InvalidArgumentError, match=match):
            api.sample_highlights(**kwargs)

    def test_book_argument(self, api, rows):
        book = rows["book"]
        assert self.sampled(api, book_id=book["id"]) == {rows[k] for k in ("live", "late_today", "undated")}
        assert self.sampled(api, book_id=str(book["id"])) == self.sampled(api, book_id=book["id"])
        assert self.sampled(api, book_id=api.get_book_by_id(book["id"])) == self.sampled(api, book_id=book["id"])
        with pytest.raises(BookNotFoundError):
            api.sample_highlights(book_id=99999)

    def test_weighting(self, api, library):
        book = library.add_book("Weighted")
        noted = {library.add_annotation(book, f"a noted passage of some length, number {i}.", kind="note",
                                        note="n", created=local(2026, 1, 1)) for i in range(10)}
        for i in range(10):
            library.add_annotation(book, f"a plain passage of some length, number {i}.", created=local(2026, 1, 1))
        firsts = sum(api.sample_highlights(limit=1, on=DAY + dt.timedelta(days=d))[0].id in noted
                     for d in range(500))
        # P(first pick has a note) = 2w / (2w + (1 - w)) with w = 0.5: 2/3.
        assert abs(firsts / 500 - 2 / 3) < 0.08

    def test_threads(self, api, populated):
        expected = ids(api.sample_highlights(limit=None, on=DAY))
        results, errors = [], []

        def run():
            try:
                results.append(ids(api.sample_highlights(limit=None, on=DAY)))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors and results == [expected] * 8


class TestDrift:
    def library_with(self, make_library, *alters):
        lib = make_library()
        book = lib.add_book("B")
        for i in range(6):
            lib.add_annotation(book, f"a passage long enough to sample, number {i}.", note="n" if i % 2 else None,
                               created=local(2026, 1, 1 + i))
        for sql in alters:
            lib.execute("annotations", sql)
        return lib

    def sample(self, lib, **kwargs):
        with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
            return ids(PyAppleBooks().sample_highlights(limit=None, on=DAY, **kwargs))

    def test_without_uuids_keys_are_ids(self, make_library):
        lib = self.library_with(make_library, "DROP INDEX Z_AEAnnotation_annotationUuid",
                                "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONUUID")
        rows = lib.execute("annotations", "SELECT Z_PK, ZANNOTATIONNOTE FROM ZAEANNOTATION")
        expected = sorted(rows, key=lambda r: (-oracle_key(DAY, None, f"pk:{r[0]}", bool(r[1])), r[0]))
        assert self.sample(lib) == [r[0] for r in expected]

    def test_without_notes_every_weight_is_one(self, make_library):
        lib = self.library_with(make_library, "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONNOTE")
        rows = lib.execute("annotations", "SELECT Z_PK, ZANNOTATIONUUID FROM ZAEANNOTATION")
        expected = sorted(rows, key=lambda r: (-oracle_key(DAY, None, r[1].upper(), False), r[0]))
        assert self.sample(lib) == [r[0] for r in expected]

    def test_without_text_nothing(self, make_library):
        lib = self.library_with(make_library, "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONSELECTEDTEXT")
        assert self.sample(lib) == []

    def test_without_creation_dates_every_row_counts(self, make_library):
        lib = self.library_with(make_library, "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONCREATIONDATE")
        assert len(self.sample(lib)) == 6
        with pytest.raises(UnsupportedSchemaError):
            self.sample(lib, after=dt.date(2026, 1, 1))


class TestPerformance:
    @pytest.mark.parametrize("count", [10_000] + ([50_000] if engagement_helpers.SLOW else []))
    def test_sample_time(self, api, library, count):
        library.populate(books=100, annotations_per_book=count // 100)
        engagement_helpers.realistic_texts(library)
        api.sample_highlights(on=DAY)  # warm: connection and schema
        start = time.perf_counter()
        picks = list(api.sample_highlights(on=DAY))
        elapsed = time.perf_counter() - start
        assert len(picks) == 5
        budget = (0.15 if count == 10_000 else 1.0) if engagement_helpers.SLOW else 3.0
        assert elapsed < budget, f"{count}: {elapsed:.3f} s"


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


def test_demo_sample_and_on_this_day(api, library, tmp_path):
    made = seed_demo(library, tmp_path)
    a = made["annotations"]
    picks = set(ids(api.sample_highlights(limit=None, on=DAY, exclude_short=False)))
    # Live type-2 rows of books in the library, with text.
    assert picks == {a[k] for k in ("highlight", "note", "underline", "no_file", "apostrophe", "curly")}
