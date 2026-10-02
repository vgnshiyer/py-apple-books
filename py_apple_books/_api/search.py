"""The :class:`~py_apple_books.PyAppleBooks` mixin for ranked annotation
search and multi-word book search (new in 1.11).

- ``search_annotations``: ranked search over highlights and notes, on
  the private index in :mod:`py_apple_books.search`.
- ``search_books``: books whose title or author holds every word of a
  query, in one SQL statement.

See ``py_apple_books._api`` for the rules mixin code follows.
"""

import functools
import operator
from typing import List, Optional, Union

from py_apple_books._api._common import _book_arg, _book_scope, strict_limit, strict_offset
from py_apple_books.db.clause import Q, _text
from py_apple_books.db.client import current_library
from py_apple_books.models.book import Book
from py_apple_books.models.manager import ModelIterable
from py_apple_books.search import _MAX_TERMS, AnnotationHit, _plan, _search_annotations
from py_apple_books.text import fold_for_match


class _SearchAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""

    def search_annotations(self, query: str, *, limit: Optional[int] = 20, offset: Optional[int] = None,
                           book_id: Union[int, str, Book, None] = None, require_all: bool = False,
                           include_deleted: bool = False) -> List[AnnotationHit]:
        """Search highlights and notes by relevance, best first.

        Matches the highlighted text, the note and the surrounding text,
        each folded as every search folds it
        (:func:`py_apple_books.text.fold_for_match`: case, accents, quote
        and dash style ignored), in this order:

        1. annotations containing every word of the query, best first
           (full-text ranking: a word in the highlight counts most, one
           in the note less, one only in the surrounding text least;
           English words also match their other forms, "habits" finds
           "habit");
        2. annotations containing the whole query as written;
        3. unless ``require_all``, annotations containing some of the
           words;
        4. only if nothing matched so far, annotations containing every
           word inside a longer word; then, unless ``require_all``, those
           containing every word of 3 or more characters (a query with
           no such word skips this step).

        Equal scores list the newer annotation id first. Common English
        words (the, of, ...) are left out of a multi-word query; double
        quotes search for a phrase. A query in a script written without
        spaces between words (Chinese, Japanese, Korean, Thai, Lao,
        Khmer, Myanmar) or of punctuation only is matched as text
        contained in the annotation. For a query of 3 or more
        characters once folded (a space at either end counts), the
        results with ``limit=None`` include every annotation
        :meth:`search_annotation_by_text` returns.

        Scope as in :meth:`search_annotation_by_text`: user highlights,
        notes and bookmarks, deleted ones only with ``include_deleted``,
        never the reading-position row.

        The search runs on an index of the library's annotations, built
        in memory on first use and rebuilt when they change (checked at
        most once a second; :meth:`close` drops it).

        :param query: the text to look for. '', whitespace, or characters
            that all fold away return ``[]``.
        :param limit: hits to return (an integer >= 1), or None for all.
        :param offset: hits of the ranked list to skip (>= 0), for paging.
        :param book_id: only this book's annotations: its id, or a
            :class:`Book`.
        :param require_all: leave out the hits that match only some of
            the words.
        :param include_deleted: also search annotations deleted in Apple
            Books.
        :returns: :class:`~py_apple_books.search.AnnotationHit` objects.
        :raises InvalidArgumentError: a query longer than
            :data:`~py_apple_books.search.MAX_QUERY_LENGTH` characters,
            or a bad ``limit`` or ``offset``.
        :raises BookNotFoundError: no book has ``book_id``.
        :raises AnnotationStoreNotFoundError: there is no annotation
            store.
        :raises QueryTimeoutError: a statement of the search or of its
            index build ran past the query timeout, or the wait for
            another thread's build did (the timeout bounds each
            statement: use ``query_deadline()`` to bound the whole call).
            A build stopped this way is continued by the next search.
        :raises DBQueryError: the index failed ("Ranked annotation search
            failed."), or reading the store did.
        """
        limit = strict_limit(limit)
        offset = strict_offset(offset) or 0
        plan = _plan(query)
        asset_id = None
        if book_id is not None:
            asset_id = _book_arg(book_id, needs=("asset_id",), get_book=self.get_book_by_id).asset_id
            if asset_id is None:
                return []
        if plan is None:
            return []
        return _search_annotations(current_library(), plan, limit=limit, offset=offset, asset_id=asset_id,
                                   require_all=bool(require_all), include_deleted=bool(include_deleted))

    def search_books(self, query: str, *, limit: Optional[int] = None, order_by: Optional[str] = None,
                     offset: Optional[int] = None, include_store_series: bool = False) -> ModelIterable:
        """Get the books whose title or author contains every word of
        ``query``, each word in either (``"history smith"`` finds a
        history book by an author named Smith), ignoring case, accents and
        quote/dash style as :meth:`get_book_by_title` does. Store series
        items you don't own are left out unless ``include_store_series``.

        The query is folded, then split at spaces; at most 32 distinct
        words are used. Word order doesn't matter, and a word may be part
        of a longer one. '' matches every book with a title or an author;
        a query whose characters all fold away matches none. Where the
        store has no author column, only titles are searched. One SQL
        statement, unordered (storage order) by default, as the other
        book searches.

        :raises InvalidArgumentError: a bad ``limit`` (an integer >= 1,
            or None for all) or ``offset``.
        """
        limit = strict_limit(limit)
        offset = strict_offset(offset)
        fields = ("title", "author") if Book.manager.has_fields("author") else ("title",)

        def any_field(value) -> Q:
            return functools.reduce(operator.or_, (Q(**{f"{field}__search": value}) for field in fields))

        folded = fold_for_match(_text(query))
        words = list(dict.fromkeys(word for word in (folded or "").split(" ") if word))[:_MAX_TERMS]
        if words:
            where = functools.reduce(operator.and_, (any_field(word) for word in words))
        else:
            # '', whitespace, or characters that fold away: the query as
            # given, under the __search lookup's own rules.
            where = any_field(query)
        scope = _book_scope(include_store_series)
        owned = scope.pop("where", None)
        if owned is not None:
            where = owned & where
        return Book.manager.filter(where=where, **scope, limit=limit, order_by=order_by, offset=offset)
