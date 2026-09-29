from __future__ import annotations

from py_apple_books.db import QueryCompiler
from py_apple_books.db import AppleBooksDBClient
from py_apple_books.db import Query
from py_apple_books.db.client import (
    ANNOTATION_SCHEMA, ANNOTATIONS_NOT_FOUND, NOT_A_DATABASE, current_library, use_library,
)
from py_apple_books.db.clause import Clause, Not, Q, Subquery, Where, WhereGroup
from py_apple_books.db.query import CompiledQuery
from py_apple_books.models.relations import _to_one_names
from py_apple_books.exceptions import (
    AnnotationStoreNotFoundError,
    DBError,
    DBQueryError,
    InvalidArgumentError,
    LibraryNotFoundError,
    UnknownFieldError,
    UnsupportedSchemaError,
)
from typing import (
    TYPE_CHECKING, Any, Callable, Dict, FrozenSet, Generic, Iterable, Iterator, List, Optional,
    Tuple, TypeVar, Union, overload,
)
from dataclasses import dataclass, replace
from functools import lru_cache, partial
import inspect
import numbers
import operator
import re
import sys
import warnings

if TYPE_CHECKING:
    from py_apple_books.db.client import LibraryDB

M = TypeVar("M")

# The fields a model can't be read without: every query selects their
# columns, and a store that lacks one raises UnsupportedSchemaError for
# that model only. Every other mapped column is optional: read as None
# where the store doesn't have it (an older or newer Apple Books), and an
# UnsupportedSchemaError only for a query that filters or sorts on it.
# The member table's columns ([book_collection]) are needed only by the
# collection relations, whose subquery checks them.
REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    'Book': ('id', 'asset_id'),
    'Annotation': ('id', 'asset_id'),
    'Collection': ('id',),
}

_INT64_MAX = 2**63 - 1

# A statement error that means the cached schema is out of date.
_STALE_SCHEMA = re.compile(r'no such (column|table)')

# A plain column name, which the compiler checks against the schema. A
# raw ``where=`` clause may name an expression instead (``lower(ZTITLE)``),
# which is left to SQLite.
_COLUMN_NAME = re.compile(r'\w+')

# Filter lookups: ``field__<suffix>=value`` -> Where operator. ``isnull``
# is special (truthy -> IS NULL, falsy -> IS NOT NULL), and
# ``field__not_<suffix>`` negates any other lookup NULL-safely (a row
# whose column is NULL is kept). A key without a known suffix is an
# exact match.
LOOKUPS = {
    'exact': '=',
    'ne': '!=',
    'gt': '>',
    'gte': '>=',
    'lt': '<',
    'lte': '<=',
    'in': 'IN',
    'notin': 'NOT IN',
    'contains': 'CONTAINS',
    'search': 'SEARCH',
    'is': 'IS',
    'isnot': 'IS NOT',
    'isnull': None,
}


def _caller_stacklevel() -> int:
    """``stacklevel`` for a ``warnings.warn`` made by this function's
    caller that attributes the warning to the first frame outside
    py_apple_books (the user's code)."""
    frame = sys._getframe(1)
    level = 1
    while frame is not None and frame.f_globals.get('__name__', '').startswith('py_apple_books'):
        frame = frame.f_back
        level += 1
    return level


def normalize_limit(limit) -> Optional[int]:
    """Validate a ``limit`` argument; None means all rows.

    Accepts an int, an integral float or Decimal, or a numeric string.
    A limit above 2**63 - 1 means all rows. A limit <= 0 also means all
    rows, as before 1.10, with a DeprecationWarning: 2.0 will raise.
    Anything else raises :class:`InvalidArgumentError`.
    """
    if limit is None:
        return None
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except ValueError:
            raise InvalidArgumentError(f"limit must be an integer or None, not {limit!r}") from None
    elif isinstance(limit, numbers.Number) and not isinstance(limit, numbers.Integral):
        # float, Decimal, Fraction: accepted when integral.
        try:
            as_int = int(limit)
        except (TypeError, ValueError, OverflowError):
            as_int = None
        if as_int is None or as_int != limit:
            raise InvalidArgumentError(f"limit must be an integer or None, not {limit!r}")
        limit = as_int
    else:
        try:
            limit = operator.index(limit)
        except TypeError:
            raise InvalidArgumentError(f"limit must be an integer or None, not {limit!r}") from None
    if limit > _INT64_MAX:
        return None
    if limit <= 0:
        warnings.warn('limit <= 0 means no limit and will raise in py-apple-books 2.0; pass None',
                      DeprecationWarning, stacklevel=_caller_stacklevel())
        return None
    return limit


def normalize_offset(offset) -> Optional[int]:
    """Validate an ``offset`` argument: None, or an int >= 0 (values
    above 2**63 - 1 are clamped to it). 0 is kept: any offset, 0
    included, orders an unordered query by primary key."""
    if offset is None:
        return None
    try:
        offset = operator.index(offset)
    except TypeError:
        raise InvalidArgumentError(f"offset must be an integer or None, not {offset!r}") from None
    if offset < 0:
        raise InvalidArgumentError(f"offset must be >= 0, not {offset}")
    return min(offset, _INT64_MAX)


@lru_cache(maxsize=64)
def _upper(columns: FrozenSet[str]) -> FrozenSet[str]:
    """``columns`` upper-cased: SQLite column names ignore case."""
    return frozenset(column.upper() for column in columns)


def _display(table: str) -> str:
    """``table`` without its schema prefix (``anno_db.``), for messages."""
    return table.rpartition('.')[2]


def _missing_column(model_name: str, field: Optional[str], table: str, column: str) -> UnsupportedSchemaError:
    needed = f"needed for {model_name}.{field}" if field else "needed by this query"
    return UnsupportedSchemaError(
        f"This Apple Books version has no column {column} in {_display(table)} ({needed}).",
        table=_display(table), column=column)


def _from_schema(error: DBError) -> bool:
    """Whether a compile error came from the cached schema, which a new
    read may change: a missing column, or no table for the model. Not a
    missing store or annotation store, nor a file that isn't a database:
    reading the schema again would only find them again."""
    if isinstance(error, UnsupportedSchemaError):
        return True
    return (not isinstance(error, AnnotationStoreNotFoundError)
            and getattr(error, 'path', None) is None and str(error) != NOT_A_DATABASE)


def _clause_columns(clause) -> set:
    columns = getattr(clause, 'columns', None)
    return set(columns()) if callable(columns) else set()


def _subqueries(clause) -> Iterator[Subquery]:
    """The :class:`Subquery` values in ``clause`` (an ``__in`` lookup's
    right-hand side), nested groups included."""
    if isinstance(clause, WhereGroup):
        for child in clause.children:
            yield from _subqueries(child)
    elif isinstance(clause, Where) and isinstance(clause.value, Subquery):
        yield clause.value


def _check_subquery(schema: dict, sub: Subquery) -> None:
    """Raise UnsupportedSchemaError if ``sub`` reads a column its table lacks."""
    columns = schema.get(sub.table)
    if not columns:
        raise UnsupportedSchemaError(
            f"This Apple Books version has no {_display(sub.table)} table (needed by this query).",
            table=_display(sub.table))
    have = _upper(columns)
    for column in sorted(sub.needed_columns()):
        if column.upper() not in have:
            raise _missing_column('', None, sub.table, column)


def _order_items(order_by) -> tuple:
    """``order_by`` (None, a string or a list) as a tuple of its items."""
    if not order_by:
        return ()
    return tuple(order_by) if isinstance(order_by, (list, tuple)) else (order_by,)


@dataclass(frozen=True)
class _Spec:
    """What a manager-built :class:`ModelIterable` reads: resolved
    clauses rather than SQL, compiled against the store's schema each
    time a query runs (G3.1).

    ``only`` is a set of field names or None; ``order`` holds the
    ``order_by`` items as given; ``limit`` and ``offset`` are normalized.
    """

    manager: 'ModelManager'
    only: Optional[FrozenSet[str]]
    where: tuple
    use_or: bool
    order: tuple
    limit: Optional[int]
    offset: Optional[int]

    @property
    def storage_window(self) -> bool:
        """Whether this is a limit without ``order_by`` or offset: the
        first rows SQLite reads (storage or index order, R23), which a
        query of its own, ordered by primary key, would not pick again."""
        return self.limit is not None and self.offset is None and not self.order

    def reads(self, field: str) -> bool:
        """Whether rows of this query hold ``field``'s column (``only``
        and the required fields), where the store has it."""
        return self.only is None or field in self.only or field in self.manager.required_fields

    def sliced(self, start: int, stop: Optional[int]) -> '_Spec':
        """Rows ``[start:stop]`` of this query (``start`` and ``stop`` >= 0),
        composed onto its own limit and offset. The offset is always set,
        0 included, so an unordered slice is ordered by primary key (R23).
        On a :attr:`storage_window` that picks other rows than the
        window's, so only an answer that doesn't depend on which rows
        (``exists()``) may use it there."""
        limit = None if stop is None else max(stop - start, 0)
        if self.limit is not None:
            remaining = max(self.limit - start, 0)
            limit = remaining if limit is None else min(limit, remaining)
        if limit is not None and limit > _INT64_MAX:
            limit = None
        offset = min((self.offset or 0) + start, _INT64_MAX)
        return replace(self, limit=limit, offset=offset)

    def compile(self, db: 'LibraryDB', fields: Optional[List[str]] = None,
                ordered: bool = True) -> CompiledQuery:
        """The SELECT for ``db``'s schema.

        Selects every mapped column the store has (or those of ``only``
        and the required fields) in mapping order, with a bare ``NULL``
        in place of the rest, so rows line up with the model's fields.
        ``fields`` replaces the column list (``['1']`` for a count).
        ``ordered=False`` leaves out the ORDER BY, for a count, which
        doesn't depend on it; its columns are still checked.

        :raises AnnotationStoreNotFoundError: an annotation query without
            an annotation store.
        :raises LibraryNotFoundError: the store has no table for the model.
        :raises UnsupportedSchemaError: the store lacks a required column,
            or a column the query filters or sorts on.
        """
        mgr = self.manager
        table = mgr.table_name
        if table.startswith(ANNOTATION_SCHEMA + '.') and not db.has_annotations():
            raise AnnotationStoreNotFoundError(ANNOTATIONS_NOT_FOUND)
        schema = db.schema()
        columns = schema.get(table)
        if not columns:
            raise LibraryNotFoundError(
                f"The Apple Books store has no {_display(table)} table: "
                "it is empty or not an Apple Books library.")
        have = _upper(columns)
        mapping = mgr.model_class._get_mappings(mgr.model_name)
        required = mgr.required_fields
        select = []
        for field, column in mapping.items():
            if column.upper() in have:
                select.append(column if self.reads(field) else 'NULL')
            elif field in required:
                raise _missing_column(mgr.model_name, field, table, column)
            else:
                select.append('NULL')
        order = mgr._order_terms(self.order, paging=self.offset is not None)
        used = {term.rsplit(' ', 1)[0] for term in order}
        for clause in self.where:
            used |= _clause_columns(clause)
            for sub in _subqueries(clause):
                _check_subquery(schema, sub)
        by_column = {column.upper(): field for field, column in mapping.items()}
        for column in sorted(used):
            if column.upper() not in have and _COLUMN_NAME.fullmatch(column):
                raise _missing_column(mgr.model_name, by_column.get(column.upper()), table, column)
        return Query.compile(table, list(fields) if fields is not None else select, list(self.where),
                             order if ordered else None, self.limit, self.offset, self.use_or)


class ModelIterable(Generic[M]):
    """The models a manager query or a to-many relation returns.

    Lazy, and evaluated once: the query runs on first use (iteration,
    ``len()``, ``bool()``, an index) and later uses read that result.
    Every manager call and every relation access returns a new
    iterable, so a new call sees new data.

    On an unevaluated iterable, ``count()``, ``exists()``, ``first()``
    and a slice ``[a:b]`` (bounds >= 0, no step) each run one query of
    their own (``COUNT(*)``, ``LIMIT``/``OFFSET``) and leave it
    unevaluated. A slice returns a list. An unordered slice is ordered
    by primary key, so consecutive slices page through the rows without
    gaps or repeats; once evaluated, slices and indexes follow the
    order of the rows read. Unordered, ``first()`` is the lowest
    primary key either way.

    The exception is a ``limit`` without ``order_by`` or ``offset``: its
    rows are the first SQLite reads, in storage (or index) order, which
    a query of their own can't pick again. There ``first()``, a slice
    and ``count_by()`` evaluate the iterable and answer from its rows.

    ``ModelIterable(callable, model_class)``, the pre-1.10 form, wraps a
    function that returns raw rows; its models' relations read the
    library current when it was created.

    Models keep the library they were read from: relations used after
    it is closed (a ``with LibraryDB(...)`` block's models, after the
    block) open its connections again.
    """

    def __init__(self, callable: Optional[Callable[[], list]] = None, model_class=None, *,
                 spec: Optional[_Spec] = None, db: Optional['LibraryDB'] = None):
        self._callable = callable
        self.model_class = model_class
        self._spec = spec
        if db is None and callable is not None:
            # The rows come from the caller's library (the facade reads
            # them before wrapping them), which their relations then read.
            db = current_library()
        # The library the query reads (None: the current one when it runs).
        self._db = db
        self._rows: Optional[list] = None
        self._objs: Optional[list] = None

    # -- evaluation ---------------------------------------------------------

    def _execute(self, build: Callable[['LibraryDB'], CompiledQuery]) -> list:
        """Compile ``build(db)`` and run it through the manager's compiler.

        Queries compile against the cached schema, which may predate a
        change to the store (a table or column added or dropped, a store
        replaced or an annotation store found). So when compiling fails
        on a missing table or column, or the statement fails with 'no
        such table' or 'no such column', the schema is read again and the
        query compiled (and run) once more. The retry runs outside the
        first error's handler, so a lasting failure raises one error.
        """
        db = self._db if self._db is not None else current_library()
        compiler = self._spec.manager.compiler
        with use_library(db):
            try:
                query = build(db)
            except (LibraryNotFoundError, UnsupportedSchemaError) as e:
                if not _from_schema(e):
                    raise
                query = None
            if query is None:
                db.invalidate_schema()
                query = build(db)
            try:
                return compiler.execute(query.sql, query.params)
            except DBQueryError as e:
                if not _STALE_SCHEMA.search(str(e)):
                    raise
            db.invalidate_schema()
            query = build(db)
            return compiler.execute(query.sql, query.params)

    def run_query(self) -> list:
        """Run the query and return its raw rows, uncached (pre-1.10
        attribute: the iterable itself runs its query once)."""
        if self._spec is not None:
            return self._execute(self._spec.compile)
        if self._callable is None:
            raise TypeError("this ModelIterable has no query to run")
        return self._callable()

    def _fetch(self) -> list:
        if self._rows is None:
            self._rows = list(self.run_query())
        return self._rows

    def _materialize(self) -> list:
        if self._objs is None:
            rows = self._fetch()
            from_db = self.model_class.from_db
            if _takes_db(from_db):
                objs = [from_db(row, db=self._db) for row in rows]
            else:
                # A pre-1.10 override without db=: the library is
                # recorded afterwards.
                objs = [from_db(row) for row in rows]
                if self._db is not None:
                    for obj in objs:
                        state = getattr(obj, '__dict__', None)
                        if state is not None and state.get('_ab_db') is None:
                            state['_ab_db'] = self._db
            self._objs = _link(objs)
        return self._objs

    @classmethod
    def _from_objects(cls, model_class, objs: Iterable, db: Optional['LibraryDB'] = None) -> 'ModelIterable':
        """An evaluated iterable over models already built (a result
        sorted or filtered in Python)."""
        iterable = cls(model_class=model_class, db=db)
        iterable._objs = _link(list(objs))
        return iterable

    # -- access ---------------------------------------------------------------

    def __iter__(self) -> Iterator[M]:
        return iter(self._materialize())

    def __len__(self) -> int:
        # Counts rows; builds no models.
        return len(self._objs) if self._objs is not None else len(self._fetch())

    def __bool__(self) -> bool:
        return bool(self._objs) if self._objs is not None else bool(self._fetch())

    @overload
    def __getitem__(self, index: int) -> M: ...

    @overload
    def __getitem__(self, index: slice) -> List[M]: ...

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = (None if v is None else operator.index(v)
                                 for v in (index.start, index.stop, index.step))
            if (self._spec is not None and not self._spec.storage_window
                    and self._rows is None and self._objs is None
                    and step in (None, 1) and (start is None or start >= 0)
                    and (stop is None or stop >= 0)):
                page = ModelIterable(model_class=self.model_class,
                                     spec=self._spec.sliced(start or 0, stop), db=self._db)
                return list(page._materialize())
        return self._materialize()[index]

    def __repr__(self) -> str:
        name = getattr(self.model_class, '__name__', '?')
        rows = self._objs if self._objs is not None else self._rows
        state = 'unevaluated' if rows is None else f"{len(rows)} rows"
        return f"<ModelIterable[{name}]: {state}>"

    # -- aggregates -----------------------------------------------------------

    def count(self) -> int:
        """The number of rows: ``len()`` once evaluated, else a
        ``COUNT(*)`` query that leaves this iterable unevaluated."""
        if self._objs is not None or self._rows is not None or self._spec is None:
            return len(self)
        spec = self._spec
        return self._execute(lambda db: Query.count(spec.compile(db, fields=['1'], ordered=False)))[0][0]

    def exists(self) -> bool:
        """Whether there is any row (a ``LIMIT 1`` query unless evaluated)."""
        if self._objs is not None or self._rows is not None or self._spec is None:
            return bool(self)
        spec = self._spec.sliced(0, 1)
        return bool(self._execute(lambda db: spec.compile(db, fields=['1'], ordered=False)))

    def first(self) -> Optional[M]:
        """The first model, or None. Without ``order_by``, that is the one
        with the lowest primary key, evaluated or not (under a limit
        without an offset, of the rows the iterable holds)."""
        spec = self._spec
        if spec is not None and not spec.order and (
                self._objs is not None or self._rows is not None or spec.storage_window):
            objs = self._materialize()
            return min(objs, key=operator.attrgetter('id')) if objs else None
        page = self[0:1]
        return page[0] if page else None

    def count_by(self, field: str) -> dict:
        """``{value: number of rows}`` for a model field, grouped in SQL:
        the raw values the iterable's rows hold (e.g. Core Data seconds
        for dates), so a column the store lacks, or a field ``only=``
        leaves out, counts as None.

        Under a limit without ``order_by`` or offset (see the class), and
        on an iterable over a pre-1.10 callable, the raw rows are grouped
        in Python instead, which evaluates the iterable. One built from
        models (``_from_objects``) counts their attribute values.
        """
        spec = self._spec
        if spec is None or spec.storage_window:
            mgr = self.model_class.manager if spec is None else spec.manager
            mgr._get_db_field(field)  # an unknown field raises
            if self._rows is None and self._objs is not None:
                values = [getattr(obj, field) for obj in self._objs]
            else:
                i = list(self.model_class._get_mappings(mgr.model_name)).index(field)
                values = [row[i] if i < len(row) else None for row in self._fetch()]
            counts: dict = {}
            for value in values:
                counts[value] = counts.get(value, 0) + 1
            return counts
        mgr = spec.manager
        column = mgr._get_db_field(field)
        # The order only matters for which rows a limit or offset keeps.
        ordered = spec.limit is not None or bool(spec.offset)

        def build(db):
            present = spec.reads(field) and column.upper() in _upper(
                db.schema().get(mgr.table_name) or frozenset())
            inner = spec.compile(db, fields=[f"{column if present else 'NULL'} AS _k"], ordered=ordered)
            return CompiledQuery(f"SELECT _k, COUNT(*) FROM ({inner.sql}) GROUP BY _k", inner.params)

        return {key: n for key, n in self._execute(build)}


@lru_cache(maxsize=64)
def _takes_db(from_db) -> bool:
    """Whether a model's ``from_db`` accepts ``db=``: an override written
    before 1.10 may be ``from_db(cls, row)``."""
    try:
        parameters = inspect.signature(from_db).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD or (p.name == 'db' and p.kind is not p.POSITIONAL_ONLY)
               for p in parameters)


def _link(objs: list) -> list:
    """Give each model of a multi-row result the whole list, so a
    to-one relation (``Annotation.book``) loads for all of them at once.
    Models without one (books, collections) don't get it: it would only
    keep the result, and its library, alive."""
    if len(objs) > 1 and hasattr(objs[0], '__dict__') and _to_one_names(type(objs[0])):
        for obj in objs:
            obj.__dict__['_ab_siblings'] = objs
    return objs


class ModelManager:
    def __init__(self, model_class):
        self.model_class = model_class
        # Every model query runs through compiler.execute(sql, params), in
        # the library it reads, so a replacement compiler sees them all.
        self.compiler = QueryCompiler(AppleBooksDBClient())

        self.model_name = self.model_class.__name__
        self.table_name = None
        if self.model_name != 'Model':
            self.table_name = self.model_class._get_mappings('Tables')[self.model_name.lower()]

    @property
    def required_fields(self) -> Tuple[str, ...]:
        """The model's fields in :data:`REQUIRED_FIELDS`."""
        return REQUIRED_FIELDS.get(self.model_name, ())

    def _create_callable(self, query: Union[CompiledQuery, str]) -> Callable[[], list[Any]]:
        if isinstance(query, CompiledQuery):
            return partial(self.compiler.execute, query.sql, query.params)
        return partial(self.compiler.execute, query)

    def _get_db_field(self, field: str) -> str:
        mappings = self.model_class._get_mappings(self.model_name)
        try:
            return mappings[field]
        except KeyError:
            raise UnknownFieldError(self.model_class, field, sorted(mappings)) from None

    def _get_fields(self, only: List[str] = None) -> List[str]:
        # Pre-1.10 helper (column names only); queries use _Spec.compile.
        fields = list(self.model_class._get_mappings(self.model_name).values())
        if only:
            fields = [field for field in fields if field in only]
        return fields

    def _only(self, only) -> Optional[FrozenSet[str]]:
        """``only`` (field or column names) as a set of field names;
        None or empty means every field."""
        if not only:
            return None
        if isinstance(only, str):
            only = [only]
        mapping = self.model_class._get_mappings(self.model_name)
        by_column = {column.upper(): field for field, column in mapping.items()}
        fields = set()
        for name in only:
            if name in mapping:
                fields.add(name)
            elif isinstance(name, str) and name.upper() in by_column:
                fields.add(by_column[name.upper()])
            else:
                raise UnknownFieldError(self.model_class, name, sorted(mapping))
        return frozenset(fields)

    def _format_order_by(self, order_by: str) -> str:
        # Pre-1.10 renderer, kept for compatibility; queries use _order_terms.
        if order_by:
            field = order_by[1:] if order_by.startswith('-') else order_by
            direction = ' DESC' if order_by.startswith('-') else ''
            order_by = self._get_db_field(field) + direction
        return order_by

    def _order_terms(self, order_by, paging: bool = False) -> List[str]:
        """ORDER BY terms (``'COL ASC'``/``'COL DESC'``) for ``order_by``.

        ``order_by`` is a field name (``'-name'`` for descending), a
        comma-separated string of them, or a list or tuple. Ordered
        queries get the primary key as a final tie-break, so equal keys
        come back in a stable order. Without ``order_by``, a paged query
        (``paging``: an offset, 0 included) is ordered by primary key;
        anything else keeps storage order, as before 1.10.
        """
        items = []
        if order_by:
            items = list(order_by) if isinstance(order_by, (list, tuple)) else [order_by]
        terms, columns = [], set()
        for item in items:
            for part in str(item).split(','):
                part = part.strip()
                if not part:
                    continue
                descending = part.startswith('-')
                column = self._get_db_field(part[1:].strip() if descending else part)
                terms.append(f"{column} {'DESC' if descending else 'ASC'}")
                columns.add(column)
        pk = self._get_db_field('id')
        if terms:
            if pk not in columns:
                terms.append(f"{pk} ASC")
        elif paging:
            terms = [f"{pk} ASC"]
        return terms

    def _lookup_to_where(self, key: str, value) -> Clause:
        """The clause for one ``filter`` keyword (see :data:`LOOKUPS`)."""
        field, sep, lookup = key.rpartition('__')
        if not sep:
            return Where(self._get_db_field(key), value, operator='=')
        if lookup.startswith('not_') and lookup[4:] in LOOKUPS and lookup[4:] != 'isnull':
            return Not(self._lookup_to_where(f"{field}__{lookup[4:]}", value))
        if lookup == 'isnull':
            return Where(self._get_db_field(field), None, operator='IS' if value else 'IS NOT')
        if lookup in LOOKUPS:
            return Where(self._get_db_field(field), value, operator=LOOKUPS[lookup])
        return Where(self._get_db_field(key), value, operator='=')

    def _iterable(self, where: List[Clause], use_or: bool, only, limit, order_by,
                  offset) -> ModelIterable:
        limit = normalize_limit(limit)
        offset = normalize_offset(offset)
        order = _order_items(order_by)
        self._order_terms(order)  # an unknown field raises here, not when the query runs
        spec = _Spec(self, self._only(only), tuple(where), use_or, order, limit, offset)
        return ModelIterable(model_class=self.model_class, spec=spec, db=current_library())

    def all(self, only: List[str] = None, limit: int = None, order_by: str = None,
            offset: int = None) -> ModelIterable:
        """Every row. ``only`` names the fields (or columns) to read; the
        others are None."""
        return self._iterable([], False, only, limit, order_by, offset)

    def filter(self, only: List[str] = None, use_or: bool = False, limit: int = None,
               order_by: str = None, offset: int = None,
               where: Optional[Union[Q, Clause]] = None, **filters) -> ModelIterable:
        """Rows matching ``field__<lookup>=value`` keywords (see
        :data:`LOOKUPS`), joined with AND (OR if ``use_or``), and ANDed
        with ``where`` (a :class:`~py_apple_books.db.clause.Q` or a
        clause), if given."""
        where_clauses = [self._lookup_to_where(key, value) for key, value in filters.items()]
        if where is not None:
            if isinstance(where, Q):
                where = where.resolve(self)
            elif not hasattr(where, 'to_sql'):
                raise TypeError(f"where must be a Q or a Clause, not {type(where).__name__}")
            if use_or and len(where_clauses) > 1:
                where_clauses = [WhereGroup(where_clauses, 'OR')]
            where_clauses.append(where)
            use_or = False
        return self._iterable(where_clauses, use_or, only, limit, order_by, offset)

    def count(self, **filters) -> int:
        """The number of rows ``filter(**filters)`` returns, counted in SQL."""
        return self.filter(**filters).count()

    def has_fields(self, *fields: str) -> bool:
        """True if the store has the column of every named field.

        Lets callers skip a filter on a column an older or newer Apple
        Books version doesn't have. An unknown field name raises
        :class:`UnknownFieldError`. Reads the current library's cached
        schema (:meth:`LibraryDB.schema`), without a query of its own.
        """
        columns = [self._get_db_field(field) for field in fields]
        have = _upper(current_library().schema().get(self.table_name, frozenset()))
        return all(column.upper() in have for column in columns)

    def _relation_iterable(self, db: Optional['LibraryDB'], **filters) -> ModelIterable:
        """``filter(**filters)`` reading ``db`` (None: the current library)."""
        with use_library(db):
            return self.filter(**filters)

    def handle_relations(self, model_object):
        """Load every relation of ``model_object`` now and set the results
        on it, as ``from_db`` did before 1.10. Relations load on first
        access since then, so this is only needed to load them eagerly.
        """
        for relation in self.model_class.relations:
            name = relation['name']
            setattr(model_object, name, getattr(model_object, name))

    def get_related_ids(self, model_object, relation: dict) -> list[str]:
        """
        Get related IDs for many-to-many relationships.
        """
        # This method is a minor contrivance to avoid multi-level join
        join_table = self.model_class._get_mappings('Tables')[relation['join_table']]
        from_key = self.model_class._get_mappings(relation['join_table'])[relation['from_key']]
        to_key = self.model_class._get_mappings(relation['join_table'])[relation['to_key']]
        val = getattr(model_object, relation['from_key'])

        query = Query.compile(join_table, fields=[to_key], where=[Where(from_key, val, operator='=')])
        with use_library(model_object.__dict__.get('_ab_db')):
            related_ids = self.compiler.execute(query.sql, query.params)
        return [row[0] for row in related_ids]

    # Write operations intentionally do NOT live on the manager: the
    # manager's connection is read-only by design. See
    # py_apple_books.collection_writer for the guarded write path.
