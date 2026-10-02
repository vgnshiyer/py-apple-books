"""Engagement: resurfacing highlights, the days they were made, and
highlighted words (1.11).

The :class:`~py_apple_books.PyAppleBooks` methods built on this module
(``get_underlines``, ``sample_highlights``,
``get_highlights_on_this_day``) read the library and annotation
databases only: no book file is opened. This module holds what they
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
from datetime import date, datetime, timedelta
from typing import Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.utils import APPLE_EPOCH_OFFSET

__all__ = ["SAMPLE_ALGORITHM"]

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
