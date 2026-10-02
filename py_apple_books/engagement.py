"""Engagement: resurfacing highlights, the days they were made, and
highlighted words (1.11).

The :class:`~py_apple_books.PyAppleBooks` methods built on this module
read the library and annotation databases only: no book file is opened.
This module holds what they share, starting with the date rules of the
1.11 methods.

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
"""

import math
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.utils import APPLE_EPOCH_OFFSET

__all__: list = []


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
