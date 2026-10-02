"""Engagement: resurfacing highlights, the days they were made, and
highlighted words (1.11).

The engagement methods of :class:`~py_apple_books.PyAppleBooks` read the
Apple Books databases, never a book file. This module holds their types
and what they share: the date rules of the 1.11 methods and the
sampling algorithm.

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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

from py_apple_books.exceptions import InvalidArgumentError, InvalidChoiceError
from py_apple_books.text import _coerce_text, fold_for_match, selection_core
from py_apple_books.utils import APPLE_EPOCH_OFFSET

if TYPE_CHECKING:
    from py_apple_books.models.annotation import Annotation

__all__ = ["SAMPLE_ALGORITHM", "VocabularyEntry"]

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
        text contains the term and says more than the term itself; None
        if there is none."""
        for annotation in self.annotations:
            text = _coerce_text(annotation.representative_text)
            if not text or not text.strip():
                continue
            folded = fold_for_match(selection_core(text))
            if self.key in folded and folded != self.key:
                return text.strip()
        return None


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
