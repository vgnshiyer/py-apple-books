"""G4.3: Core Data timestamps become naive local datetimes.

Deferred past 1.10: no 1.10 stream removes this marker.
"""

import datetime as dt

import pytest

G43 = "G4.3: Core Data dates are naive local datetimes; deferred past 1.10, no stream removes this marker"
CORE_DATA_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


@pytest.mark.xfail(strict=True, reason=G43)
def test_core_data_zero_is_aware_utc_epoch(api, library):
    book = library.add_book("Synthetic Book", created=0.0)
    created = api.get_book_by_id(book["id"]).creation_date
    assert created.tzinfo is not None
    assert created == CORE_DATA_EPOCH
