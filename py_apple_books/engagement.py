"""Engagement: resurfacing highlights, the days they were made,
highlighted words, highlight activity and reading goals (1.11).

The engagement methods of :class:`~py_apple_books.PyAppleBooks` read the
Apple Books databases (and, for reading goals, Books' preferences
file), never a book file. This module holds their types and what they
share: the date rules of the 1.11 methods and the sampling algorithm.

Dates in new methods (1.11):

- ``after``/``before`` bounds (and ``finished_after``/``finished_before``
  on :meth:`~py_apple_books.PyAppleBooks.get_finished_books`) are
  inclusive. A ``datetime`` is an instant: a naive one is local time, an
  aware one is converted. A ``date`` covers that whole local day:
  ``after=date(2026, 1, 1)`` starts at local midnight, and
  ``before=date(2026, 12, 31)`` ends just before the next local
  midnight. Anything else raises
  :class:`~py_apple_books.exceptions.InvalidArgumentError`.
- ``on`` names a local calendar day: None is today, a ``date`` is that
  day, a naive ``datetime`` its date, an aware one the local date of
  that instant. Its time of day is never used.

"Local" is this Mac's time zone, as for every date the library returns.

:data:`SAMPLE_ALGORITHM` names the algorithm
:meth:`~py_apple_books.PyAppleBooks.sample_highlights` ranks with. The
same candidates, day and seed give the same order on every platform and
Python version; a new algorithm gets a new name.
"""

import hashlib
import math
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Dict, Hashable, Iterable, List, Optional, Sequence, Set, Tuple, Union

from py_apple_books.exceptions import InvalidArgumentError, InvalidChoiceError
from py_apple_books.text import _coerce_text, fold_for_match, selection_core
from py_apple_books.utils import APPLE_EPOCH_OFFSET

if TYPE_CHECKING:
    from py_apple_books.models.annotation import Annotation

__all__ = ["SAMPLE_ALGORITHM", "VocabularyEntry", "ActivityPeriod", "HighlightActivity", "HighlightStreaks",
           "ReadingGoals"]

#: The ranking algorithm of :meth:`PyAppleBooks.sample_highlights`.
#: Each candidate gets a key from a keyed BLAKE2b hash of the day, the
#: seed and the highlight's uuid (``pk:<id>`` without one), and a
#: highlight with a note counts twice (weighted Efraimidis-Spirakis
#: sampling, compared in exact integers). Without ``book_id`` the picks
#: then go round-robin across books. Changing any of this needs a new
#: name.
SAMPLE_ALGORITHM = "pab-sample-v1"

_SAMPLE_PERSON = SAMPLE_ALGORITHM.encode("ascii")


# -- dates (R9) ---------------------------------------------------------------


def _type_error(name: str, value) -> InvalidArgumentError:
    return InvalidArgumentError(f"{name} must be a date, a datetime or None, not {type(value).__name__}.")


def _local_day(on, *, name: str = "on") -> date:
    """The local calendar day ``on`` names (see the module docstring).

    :raises InvalidArgumentError: ``on`` is not None, a ``date`` or a
        ``datetime``, or an aware datetime outside the local range.
    """
    if on is None:
        return date.today()
    if isinstance(on, datetime):
        if on.tzinfo is not None and on.utcoffset() is not None:
            try:
                on = on.astimezone()
            except (OverflowError, OSError, ValueError):
                raise InvalidArgumentError(f"{name} is out of the range of local dates.") from None
        return date(on.year, on.month, on.day)
    if isinstance(on, date):
        return date(on.year, on.month, on.day)
    raise _type_error(name, on)


def _instant(value: datetime) -> float:
    """Core Data seconds of a datetime (naive is local time). One too far
    from today for the platform to convert is before or after every
    stored date: -inf or +inf."""
    try:
        return value.timestamp() - APPLE_EPOCH_OFFSET
    except (OverflowError, OSError, ValueError):
        return -math.inf if value.year < 1970 else math.inf


def _day_start(day: date) -> float:
    """Core Data seconds of the local midnight that starts ``day``."""
    return _instant(datetime(day.year, day.month, day.day))


def _day_end(day: date) -> float:
    """Core Data seconds of the local midnight that ends ``day`` (the
    start of the next day; +inf after the last representable day)."""
    try:
        following = day + timedelta(days=1)
    except OverflowError:
        return math.inf
    return _day_start(following)


def _is_day(value) -> bool:
    """A ``date`` that isn't a ``datetime``."""
    return isinstance(value, date) and not isinstance(value, datetime)


def _resolve_window(after, before, *,
                    names: Tuple[str, str] = ("after", "before")) -> Tuple[Optional[float], Optional[float], bool]:
    """``(lo, hi, hi_inclusive)`` in Core Data seconds for an
    ``after``/``before`` window (see the module docstring); None for a
    missing bound. ``lo`` is inclusive; ``hi`` is inclusive for a
    datetime and exclusive (the next local midnight) for a date.

    :raises InvalidArgumentError: a bound that is neither None, a date
        nor a datetime (the message names the parameter, not the value).
    """
    lo: Optional[float] = None
    hi: Optional[float] = None
    hi_inclusive = True
    if after is not None:
        if isinstance(after, datetime):
            lo = _instant(after)
        elif isinstance(after, date):
            lo = _day_start(after)
        else:
            raise _type_error(names[0], after)
    if before is not None:
        if isinstance(before, datetime):
            hi = _instant(before)
        elif isinstance(before, date):
            hi, hi_inclusive = _day_end(before), False
        else:
            raise _type_error(names[1], before)
    return lo, hi, hi_inclusive


def _window_filters(field: str, after, before, *,
                    names: Tuple[str, str] = ("after", "before")) -> Dict[str, float]:
    """Manager filter keywords bounding ``field`` (a date field) by an
    ``after``/``before`` window; empty without bounds."""
    lo, hi, hi_inclusive = _resolve_window(after, before, names=names)
    filters: Dict[str, float] = {}
    if lo is not None:
        filters[f"{field}__gte"] = lo
    if hi is not None:
        filters[f"{field}__lte" if hi_inclusive else f"{field}__lt"] = hi
    return filters


# -- sampling (pab-sample-v1) -------------------------------------------------


def _sample_ident(uuid, pk) -> str:
    """What a highlight is hashed by: its uuid in upper case, else
    ``pk:<id>``."""
    if isinstance(uuid, (bytes, bytearray)):
        uuid = bytes(uuid).decode("utf-8", "replace")
    if uuid is not None and not isinstance(uuid, str):
        uuid = str(uuid)
    return uuid.upper() if uuid else f"pk:{pk}"


def _sample_key(day: date, seed: Optional[str], ident: str, noted: bool) -> int:
    """The ``pab-sample-v1`` key of one highlight; larger ranks first.

    ``H`` is the 64-bit BLAKE2b digest (personalised with the algorithm
    name) of ``'<day ISO>\\x1f<seed>\\x1f<ident>'`` (``ident`` from
    :func:`_sample_ident`) and
    ``h = (H >> 11) | 1``, so ``u = h / 2**53`` lies strictly inside
    (0, 1). The weighted key ``u ** (1 / w)`` (w = 2 for a highlight with
    a note, else 1) is compared squared, in integers: ``h << 53`` for a
    note, ``h * h`` otherwise.
    """
    data = f"{day.isoformat()}\x1f{seed or ''}\x1f{ident}".encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(data, digest_size=8, person=_SAMPLE_PERSON).digest()
    h = (int.from_bytes(digest, "big") >> 11) | 1
    return h << 53 if noted else h * h


def _rank(candidates: Iterable[Tuple[int, int, Hashable]]) -> List[Tuple[int, int, Hashable]]:
    """``(key, id, asset)`` candidates by key, largest first, ties by id."""
    return sorted(candidates, key=lambda c: (-c[0], c[1]))


def _round_robin(ranked: Sequence[Tuple[int, int, Hashable]]) -> List[Tuple[int, int, Hashable]]:
    """``ranked`` (from :func:`_rank`) in rounds: each round takes every
    asset's next-best candidate, the round ordered by key (ties by id).
    Each distinct asset value (None included) is one group."""
    nth: Dict[Hashable, int] = {}
    rounds = []
    for key, pk, asset in ranked:
        n = nth.get(asset, 0)
        nth[asset] = n + 1
        rounds.append((n, -key, pk, (key, pk, asset)))
    rounds.sort(key=lambda r: r[:3])
    return [r[3] for r in rounds]


# -- vocabulary ---------------------------------------------------------------


def _newest_first(annotations: Iterable["Annotation"]) -> Tuple["Annotation", ...]:
    """``annotations`` newest first (by creation date; undated last),
    ties by higher id."""
    return tuple(sorted(annotations, key=lambda a: (a.creation_date is not None,
                                                    a.creation_date or datetime.min, a.id),
                        reverse=True))


@dataclass(frozen=True)
class VocabularyEntry:
    """A word or short phrase you highlighted, with every highlight of it
    (from :meth:`PyAppleBooks.get_vocabulary`).

    Highlights group by ``key``: their text trimmed to its word or phrase
    (:func:`py_apple_books.text.selection_core`) and folded for matching
    (:func:`py_apple_books.text.fold_for_match`: case, accents, quote
    style and ligatures). There is no stemming, so inflected forms are
    separate entries.

    Not hashable: it holds models.
    """

    #: The word or phrase as shown: the trimmed text of the newest highlight.
    term: str
    #: What the highlights were grouped by (folded; for sorting and lookups).
    key: str
    #: The highlights, newest first.
    annotations: Tuple["Annotation", ...]

    __hash__ = None  # type: ignore[assignment]

    @property
    def count(self) -> int:
        """How many times you highlighted it."""
        return len(self.annotations)

    @property
    def notes(self) -> Tuple[str, ...]:
        """The distinct notes on its highlights (trimmed, blank ones left
        out), newest first."""
        seen: Dict[str, None] = {}
        for annotation in self.annotations:
            note = _coerce_text(annotation.note)
            if note and note.strip():
                seen.setdefault(note.strip(), None)
        return tuple(seen)

    @property
    def first_highlighted(self) -> Optional[datetime]:
        """When you first highlighted it, or None if no highlight is dated."""
        dates = [a.creation_date for a in self.annotations if a.creation_date is not None]
        return min(dates) if dates else None

    @property
    def last_highlighted(self) -> Optional[datetime]:
        """When you last highlighted it, or None if no highlight is dated."""
        dates = [a.creation_date for a in self.annotations if a.creation_date is not None]
        return max(dates) if dates else None

    @property
    def asset_ids(self) -> Tuple[str, ...]:
        """The books it was highlighted in (asset ids, newest first)."""
        seen: Dict[str, None] = {}
        for annotation in self.annotations:
            if annotation.asset_id is not None:
                seen.setdefault(annotation.asset_id, None)
        return tuple(seen)

    @property
    def context(self) -> Optional[str]:
        """A sentence it was used in: the surrounding text Books keeps
        (``representative_text``) of the newest highlight whose surrounding
        text contains the term as a whole word or phrase (not inside a
        longer word; in Chinese, Japanese, Thai and other scripts written
        without spaces, anywhere) and says more than the term itself;
        None if there is none."""
        for annotation in self.annotations:
            text = _coerce_text(annotation.representative_text)
            if not text or not text.strip():
                continue
            folded = fold_for_match(selection_core(text))
            if folded != self.key and _has_word(folded, self.key):
                return text.strip()
        return None


# Scripts written without spaces between words, where a word can sit
# inside a longer run of letters: Thai, Lao, Myanmar, Khmer (East Asian
# wide characters are recognised by their width).
_UNSPACED_SCRIPTS = ((0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF))


def _joins_word(ch: str) -> bool:
    """Whether ``ch`` would continue a word next to it: a letter, number,
    mark or underscore of a script that separates words with spaces."""
    if ch == "_":
        return True
    if unicodedata.category(ch)[0] not in "LNM" or unicodedata.east_asian_width(ch) in ("W", "F"):
        return False
    cp = ord(ch)
    return not any(low <= cp <= high for low, high in _UNSPACED_SCRIPTS)


def _has_word(text: str, key: str) -> bool:
    """Whether ``key`` occurs in ``text`` (both folded) as a whole word or
    phrase: no occurrence counts whose neighbour would continue its first
    or last word ('art' is not in 'started')."""
    if not key:
        return False
    start = text.find(key)
    while start != -1:
        end = start + len(key)
        joined_before = start > 0 and _joins_word(key[0]) and _joins_word(text[start - 1])
        joined_after = end < len(text) and _joins_word(key[-1]) and _joins_word(text[end])
        if not joined_before and not joined_after:
            return True
        start = text.find(key, start + 1)
    return False


# get_vocabulary's orders: entry attribute, and whether None can occur.
_VOCABULARY_ORDERS = ("last_highlighted", "first_highlighted", "term", "count")


def _vocabulary_order(order_by) -> Tuple[str, bool]:
    """``(field, descending)`` for a ``get_vocabulary`` ``order_by``.

    :raises InvalidChoiceError: not one of the orders, with or without '-'.
    """
    valid = [*_VOCABULARY_ORDERS, *(f"-{name}" for name in _VOCABULARY_ORDERS)]
    if isinstance(order_by, str):
        name = order_by.strip()
        field = name[1:] if name.startswith("-") else name
        if field in _VOCABULARY_ORDERS:
            return field, name.startswith("-")
    shown = order_by if isinstance(order_by, str) and len(order_by) <= 40 else type(order_by).__name__
    raise InvalidChoiceError(f"Unknown vocabulary order {shown!r}. Valid orders: {', '.join(valid)}.",
                             value=order_by, valid=valid)


def _vocabulary(annotations: Iterable["Annotation"]) -> List[VocabularyEntry]:
    """Group short-selection highlights into entries (in key order)."""
    groups: Dict[str, list] = {}
    for annotation in annotations:
        key = fold_for_match(selection_core(annotation.selected_text))
        if key:
            groups.setdefault(key, []).append(annotation)
    entries = []
    for key in sorted(groups):
        members = _newest_first(groups[key])
        entries.append(VocabularyEntry(term=selection_core(members[0].selected_text), key=key,
                                       annotations=members))
    return entries


def _sort_vocabulary(entries: List[VocabularyEntry], field: str, descending: bool) -> List[VocabularyEntry]:
    """``entries`` (in key order) by ``field``; ties by key, entries
    without a value (no dated highlight) last in either direction."""
    def value(entry):
        return entry.key if field == "term" else getattr(entry, field)

    present = [e for e in entries if value(e) is not None]
    absent = [e for e in entries if value(e) is None]
    present.sort(key=value, reverse=descending)  # stable: ties keep key order
    return present + absent


# -- highlight activity -------------------------------------------------------

_GRANULARITIES = ("day", "week", "month", "year")


@dataclass(frozen=True)
class ActivityPeriod:
    """One day, ISO week, month or year of
    :attr:`HighlightActivity.periods` (only periods with highlights are
    listed)."""

    #: The first day of the period (a Monday for a week).
    start: date
    #: The day after the period (exclusive).
    end: date
    #: Highlights and notes made in it.
    highlights: int
    #: Of those, the ones with a note.
    notes: int
    #: Distinct books (asset ids) highlighted in it, removed books included.
    books: int
    #: Days of it with at least one highlight.
    active_days: int


@dataclass(frozen=True)
class HighlightActivity:
    """How much you highlighted over a window, from
    :meth:`PyAppleBooks.get_highlight_activity`.

    It measures highlighting, not reading: days you read without
    highlighting don't show, and reading time isn't available.

    ``books`` counts distinct asset ids, removed books included:
    ``len(per_book) + orphan_books == books``, the ``per_book`` counts
    plus ``orphan_highlights`` add up to ``highlights``, and so do the
    ``periods``' (when a granularity is set).
    """

    #: The window, as passed.
    after: Optional[Union[date, datetime]]
    before: Optional[Union[date, datetime]]
    #: ``'day'``, ``'week'`` (ISO, from Monday), ``'month'``, ``'year'`` or None.
    granularity: Optional[str]
    #: Highlights and notes made in the window.
    highlights: int
    #: Of those, the ones with a note.
    notes: int
    #: Days with at least one highlight.
    active_days: int
    #: Distinct books highlighted (asset ids), removed books included.
    books: int
    #: Of those, books no longer in the library.
    orphan_books: int
    #: Highlights of books no longer in the library.
    orphan_highlights: int
    #: The first and last highlight's creation date (None without any).
    first: Optional[datetime]
    last: Optional[datetime]
    #: The periods with highlights, oldest first (empty without a granularity).
    periods: Tuple[ActivityPeriod, ...] = ()
    #: ``(book id, title, highlights)`` for each book in the library,
    #: most highlighted first, ties by id.
    per_book: Tuple[Tuple[int, Optional[str], int], ...] = ()


@dataclass(frozen=True)
class HighlightStreaks:
    """Runs of consecutive days on which you highlighted, from
    :meth:`PyAppleBooks.get_highlight_streaks` (local days, up to and
    including ``on``). Highlighting, not reading: a day read without a
    highlight breaks a streak."""

    #: The day the streaks are counted up to.
    on: date
    #: The run that ends on ``on``, or yesterday if nothing is highlighted
    #: on ``on`` yet; 0 otherwise.
    current: int
    current_start: Optional[date]
    #: The longest run (the earliest of equally long ones).
    longest: int
    longest_start: Optional[date]
    longest_end: Optional[date]
    #: The last day with a highlight.
    last_active: Optional[date]
    #: Days with at least one highlight.
    active_days: int


def _granularity(value) -> Optional[str]:
    """A ``get_highlight_activity`` granularity, checked.

    :raises InvalidChoiceError: not one of the granularities or None.
    """
    if value is None or (isinstance(value, str) and value in _GRANULARITIES):
        return value
    shown = value if isinstance(value, str) and len(value) <= 40 else type(value).__name__
    raise InvalidChoiceError(f"Unknown granularity {shown!r}. Valid: {', '.join(_GRANULARITIES)}, or None.",
                             value=value, valid=_GRANULARITIES)


def _period(day: date, granularity: str) -> Tuple[date, date]:
    """``(start, end)`` of the period holding ``day`` (end exclusive;
    ``date.max`` past the last representable day)."""
    if granularity == "day":
        start = day
    elif granularity == "week":
        start = day - timedelta(days=day.weekday())
    elif granularity == "month":
        start = day.replace(day=1)
    else:
        start = date(day.year, 1, 1)
    try:
        if granularity == "day":
            end = start + timedelta(days=1)
        elif granularity == "week":
            end = start + timedelta(days=7)
        elif granularity == "month":
            end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
        else:
            end = date(start.year + 1, 1, 1)
    except (OverflowError, ValueError):
        end = date.max
    return start, end


def _activity(rows: Iterable[Tuple[Hashable, datetime, bool]], books: Dict, *, after, before,
              granularity: Optional[str]) -> HighlightActivity:
    """A :class:`HighlightActivity` from ``(asset id, creation date,
    has a note)`` rows; ``books`` maps asset ids to the library's books
    (``_common._books_by_asset``)."""
    rows = list(rows)
    per_asset: Dict[Hashable, int] = {}
    days: Set[date] = set()
    buckets: Dict[date, list] = {}
    for asset, created, noted in rows:
        per_asset[asset] = per_asset.get(asset, 0) + 1
        day = created.date()
        days.add(day)
        if granularity is not None:
            start, end = _period(day, granularity)
            bucket = buckets.setdefault(start, [end, 0, 0, set(), set()])
            bucket[1] += 1
            bucket[2] += noted
            bucket[3].add(asset)
            bucket[4].add(day)
    in_library = [(books[asset].id, books[asset].title, n) for asset, n in per_asset.items() if asset in books]
    orphans = [n for asset, n in per_asset.items() if asset not in books]
    created = [c for _, c, _ in rows]
    return HighlightActivity(
        after=after, before=before, granularity=granularity,
        highlights=len(rows),
        notes=sum(1 for _, _, noted in rows if noted),
        active_days=len(days),
        books=len(per_asset),
        orphan_books=len(orphans),
        orphan_highlights=sum(orphans),
        first=min(created) if created else None,
        last=max(created) if created else None,
        periods=tuple(ActivityPeriod(start=start, end=b[0], highlights=b[1], notes=b[2], books=len(b[3]),
                                     active_days=len(b[4]))
                      for start, b in sorted(buckets.items())),
        per_book=tuple(sorted(in_library, key=lambda entry: (-entry[2], entry[0]))),
    )


def _streaks(days: Iterable[date], on: date) -> HighlightStreaks:
    """:class:`HighlightStreaks` over the active ``days`` up to ``on``."""
    ordered = sorted(d for d in set(days) if d <= on)
    longest, longest_start, longest_end = 0, None, None
    run, run_start, previous = 0, None, None
    for day in ordered:
        if previous is not None and (day - previous).days == 1:
            run += 1
        else:
            run, run_start = 1, day
        if run > longest:  # strictly longer: ties keep the earliest run
            longest, longest_start, longest_end = run, run_start, day
        previous = day
    alive = bool(ordered) and (on - ordered[-1]).days <= 1
    return HighlightStreaks(
        on=on,
        current=run if alive else 0,
        current_start=run_start if alive else None,
        longest=longest, longest_start=longest_start, longest_end=longest_end,
        last_active=ordered[-1] if ordered else None,
        active_days=len(ordered),
    )


# -- reading goals ------------------------------------------------------------


@dataclass(frozen=True)
class ReadingGoals:
    """Books' reading goals, from its preferences file, as Books last
    saved them (:meth:`PyAppleBooks.get_reading_goals`; ``modified`` is
    when). Each field is None (or empty) when the file doesn't hold it
    in the expected form. Dates are naive local time, like the model
    dates.

    Not available from Books' files: minutes read per day, pages read,
    Books' streak history or longest streak, and whether today's goal was
    met.
    """

    #: The yearly goal: books to finish.
    books_per_year: Optional[int] = None
    #: When that goal was set.
    books_goal_set: Optional[datetime] = None
    #: The daily reading goal, in seconds.
    daily_goal_seconds: Optional[float] = None
    #: When that goal was set.
    daily_goal_set: Optional[datetime] = None
    #: Books' own current streak of days meeting the daily goal, as it
    #: last saved it (not computed here).
    apple_current_streak: Optional[int] = None
    #: ``(asset id, finish date)`` for the books Books counts as finished
    #: towards the yearly goal: oldest first, entries without a readable
    #: date last. Books' own list, which can differ from
    #: :meth:`PyAppleBooks.get_finished_books`.
    finished_assets: Tuple[Tuple[str, Optional[datetime]], ...] = ()
    #: When the preferences file was last written.
    modified: Optional[datetime] = None

    @property
    def daily_goal_minutes(self) -> Optional[float]:
        """The daily reading goal in minutes."""
        return None if self.daily_goal_seconds is None else self.daily_goal_seconds / 60

    def books_finished_in(self, year: int) -> int:
        """How many of :attr:`finished_assets` have a finish date in
        ``year`` (local time).

        :raises InvalidArgumentError: ``year`` isn't an int.
        """
        if isinstance(year, bool) or not isinstance(year, int):
            raise InvalidArgumentError(f"year must be an int, not {type(year).__name__}.")
        return sum(1 for _, when in self.finished_assets if when is not None and when.year == year)
