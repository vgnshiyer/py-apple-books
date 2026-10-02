"""Drift cases for stream 2.5 (engagement): the new engagement methods,
and the 1.11 arguments of ``get_finished_books`` and
``get_annotations_by_date_range``. Fixed days, so results don't depend
on the date the suite runs."""

import datetime as dt

UTC = dt.timezone.utc
DAY = dt.date(2026, 10, 1)


def annotations(result) -> list:
    return [(a.id, a.selected_text, a.note, getattr(a.book, "id", None)) for a in result]


def seed(lib, rows: dict) -> dict:
    """An underline, a noted word and a noted passage on the finished
    book, and its finish date."""
    asset = lib.execute("library", "SELECT ZASSETID FROM ZBKLIBRARYASSET WHERE Z_PK = ?", (rows["done"],))[0][0]
    lib.execute("library", "UPDATE ZBKLIBRARYASSET SET ZDATEFINISHED = ? WHERE Z_PK = ?",
                (dt.datetime(2026, 3, 1, 12, tzinfo=UTC).timestamp() - 978307200, rows["done"]))
    return {
        "eng_underline": lib.add_annotation(asset, "Laconic,", kind="underline",
                                            created=dt.datetime(2025, 10, 1, 12, tzinfo=UTC)),
        "eng_word": lib.add_annotation(asset, "ephemeral", kind="note", note="fleeting",
                                       created=dt.datetime(2024, 10, 1, 12, tzinfo=UTC)),
        "eng_passage": lib.add_annotation(asset, "a passage long enough to be sampled on its own.",
                                          kind="note", note="why", color="pink",
                                          created=dt.datetime(2025, 6, 1, 12, tzinfo=UTC)),
    }


CASES = {
    "get_underlines": lambda api, rows: annotations(api.get_underlines()),
    "get_highlights_on_this_day": lambda api, rows: annotations(api.get_highlights_on_this_day(DAY)),
    "sample_highlights": lambda api, rows: annotations(api.sample_highlights(limit=None, on=DAY)),
    "sample_highlights(all)": lambda api, rows: annotations(api.sample_highlights(
        limit=None, on=DAY, seed="drift", include_orphans=True, exclude_short=False)),
    "sample_highlights(book)": lambda api, rows: annotations(api.sample_highlights(
        limit=2, on=DAY, book_id=rows["done"], exclude_short=False)),
    "get_finished_books(window)": lambda api, rows: [b.id for b in api.get_finished_books(
        finished_after=dt.date(2026, 1, 1), finished_before=dt.datetime(2026, 12, 31, 23, 59))],
    "get_annotations_by_date_range(dates)": lambda api, rows: annotations(api.get_annotations_by_date_range(
        dt.date(2024, 1, 1), dt.date(2026, 9, 30), order_by="-creation_date")),
    # Tier B
    "get_vocabulary": lambda api, rows: [(e.term, e.key, e.count, e.notes, e.context, annotations(e.annotations))
                                         for e in api.get_vocabulary()],
    "get_vocabulary(underlines)": lambda api, rows: [(e.term, e.count) for e in api.get_vocabulary(
        underline_only=True, order_by="term")],
    "get_highlight_activity": lambda api, rows: api.get_highlight_activity(),
    "get_highlight_activity(book)": lambda api, rows: api.get_highlight_activity(
        book_id=rows["done"], after=dt.date(2024, 1, 1), before=dt.date(2026, 9, 30), granularity="week"),
    "get_highlight_streaks": lambda api, rows: api.get_highlight_streaks(on=DAY),
}
