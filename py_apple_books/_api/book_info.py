"""The :class:`~py_apple_books.PyAppleBooks` mixin for titles and authors of removed books,
from Apple Books' caches (new in 1.11).

- ``get_cached_book_info``: title, author, language, publisher and year
  of books by asset id, from :mod:`py_apple_books.book_info`.

See ``py_apple_books._api`` for the rules mixin code follows.
"""

from typing import Dict, Iterable, Optional, Union

from py_apple_books.book_info import CachedBookInfo, _asset_id_list, _lookup
from py_apple_books.db.client import current_library


class _BookInfoAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""

    def get_cached_book_info(self, asset_ids: Union[str, Iterable[Optional[str]]]
                             ) -> Dict[str, CachedBookInfo]:
        """Title, author, language, publisher and year of books as Apple
        Books cached them, by asset id: meant for highlights and notes
        whose book is no longer in the library (``annotation.book`` is
        None), to name the book they came from.

        Apple Books keeps what it parsed from each book in caches next to
        the library, one per Books version, and keeps the rows of books
        since removed. Every cache is searched, newest Books version
        first; for each id, the newest cache with a title wins, else the
        newest with an author. Each value is what Books cached when it
        last parsed the book, and
        :attr:`~py_apple_books.book_info.CachedBookInfo.source` names the
        cache file it came from.

        Best effort: the result shrinks as macOS purges the caches. A
        missing, unreadable, locked, changing or unfamiliar cache never
        raises (nor does a timeout); the call returns what it read
        within its budget, possibly ``{}``. The budget: 0.25 s waiting
        for a lock Books holds and 1 s per cache file, 2 s per call,
        and no more than the library's ``query_timeout`` or an active
        :meth:`query_deadline`. Files not reached are read at a later
        call. Up to 1,024 ids are remembered per cache file (fewer if
        their values are unusually long): a call with more is not fully
        remembered, so a repeat reads the caches again and may not
        reach the oldest within its budget. Pass the distinct asset ids
        of annotations whose book is gone, not every annotation's.

        No library store is needed: the caches of the Books container
        holding this instance's library are read (its ``Documents``
        folder, or the folder of a library store given as
        ``.../Documents/BKLibrary/<file>``); other layouts, and caches in
        iCloud Drive or a cloud-storage folder, give ``{}``. The caches
        are read-only to this call, and no connection to them stays open
        after it.

        Results are remembered by this instance's library (:meth:`close`
        forgets them): the cache folder is listed and its files stat'ed
        again at most every
        :data:`~py_apple_books.book_info.BOOK_INFO_RECHECK` seconds, and
        only changed files are read again. Thread-safe. As for every
        library call, fork only while no other thread is inside this
        method: SQLite's internal locks are copied in whatever state
        they were in, and a call in the child could then wait forever,
        beyond any budget.

        Privacy: the answer also tells whether Books ever opened a book
        with a given id, including books never highlighted. Pass ids
        taken from the user's own annotations (``annotation.asset_id``);
        don't offer it as a lookup by arbitrary id.

        :param asset_ids: one asset id (a ``str``), or an iterable of
            them; None and '' items are skipped. An id no asset id can
            match (over 1,024 bytes in UTF-8, or holding a lone
            surrogate) is never found, and hides no other id.
        :returns: ``{asset id: CachedBookInfo}`` for the ids found, in
            the order the ids were first given.
        :raises InvalidArgumentError: ``asset_ids`` is neither a ``str``
            nor an iterable, or holds an item that is neither a ``str``
            nor None (the message names types, never values).
        """
        ids = _asset_id_list(asset_ids)
        if not ids:
            return {}
        return _lookup(current_library(), ids)
