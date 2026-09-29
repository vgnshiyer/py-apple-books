from py_apple_books.db import QueryCompiler
from py_apple_books.db import AppleBooksDBClient
from py_apple_books.db import Query
from py_apple_books.db.clause import Clause, Not, Q, Subquery, Where, WhereGroup
from py_apple_books.db.query import CompiledQuery
from py_apple_books.exceptions import InvalidArgumentError, UnknownFieldError
from typing import Callable, Any, List, Optional, Union
from functools import partial
import operator
import sys
import warnings

_INT64_MAX = 2**63 - 1

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

    Accepts an int, an integral float or a numeric string. A limit
    above 2**63 - 1 means all rows. A limit <= 0 also means all rows,
    as before 1.10, with a DeprecationWarning: 2.0 will raise.
    Anything else raises :class:`InvalidArgumentError`.
    """
    if limit is None:
        return None
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except ValueError:
            raise InvalidArgumentError(f"limit must be an integer or None, not {limit!r}") from None
    elif isinstance(limit, float):
        if not limit.is_integer():
            raise InvalidArgumentError(f"limit must be an integer or None, not {limit!r}")
        limit = int(limit)
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


class ModelIterable:
    def __init__(self, callable: Callable[[], list[Any]], model_class):
        self.run_query = callable
        self.model_class = model_class

    def __iter__(self):
        results = self.run_query()
        for result in results:
            yield self.model_class.from_db(result)

    def __len__(self):
        results = self.run_query()
        return len(results)

    def __getitem__(self, index: int) -> Any:
        results = self.run_query()
        return self.model_class.from_db(results[index])


class ModelManager:
    def __init__(self, model_class):
        self.model_class = model_class
        self.compiler = QueryCompiler(AppleBooksDBClient())

        self.model_name = self.model_class.__name__
        self.table_name = None
        if self.model_name != 'Model':
            self.table_name = self.model_class._get_mappings('Tables')[self.model_name.lower()]

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
        fields = list(self.model_class._get_mappings(self.model_name).values())
        if only:
            fields = [field for field in fields if field in only]
        return fields

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

    def _query(self, fields: List[str], where: List[Clause], use_or: bool, limit, order_by,
               offset) -> ModelIterable:
        limit = normalize_limit(limit)
        offset = normalize_offset(offset)
        order = self._order_terms(order_by, paging=offset is not None)
        query = Query.compile(self.table_name, fields=fields, where=where, order_by=order,
                              limit=limit, offset=offset, use_or=use_or)
        return ModelIterable(callable=self._create_callable(query), model_class=self.model_class)

    def all(self, only: List[str] = None, limit: int = None, order_by: str = None,
            offset: int = None) -> ModelIterable:
        fields = self._get_fields(only)
        return self._query(fields, [], False, limit, order_by, offset)

    def filter(self, only: List[str] = None, use_or: bool = False, limit: int = None,
               order_by: str = None, offset: int = None,
               where: Optional[Union[Q, Clause]] = None, **filters) -> ModelIterable:
        """Rows matching ``field__<lookup>=value`` keywords (see
        :data:`LOOKUPS`), joined with AND (OR if ``use_or``), and ANDed
        with ``where`` (a :class:`~py_apple_books.db.clause.Q` or a
        clause), if given."""
        fields = self._get_fields(only)
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
        return self._query(fields, where_clauses, use_or, limit, order_by, offset)

    def has_fields(self, *fields: str) -> bool:
        """True if the store has the column of every named field.

        Lets callers skip a filter on a column an older or newer Apple
        Books version doesn't have. An unknown field name raises
        :class:`UnknownFieldError`. Reads the table's schema on each call.
        """
        columns = [self._get_db_field(field) for field in fields]
        schema, _, table = self.table_name.rpartition('.')
        pragma = f"PRAGMA {schema}.table_info({table})" if schema else f"PRAGMA table_info({table})"
        existing = {row[1].upper() for row in self.compiler.execute(pragma)}
        return all(column.upper() in existing for column in columns)

    def handle_relations(self, model_object):
        """
        Handle the relations for a model object.
        """
        for relation in self.model_class.relations:
            extra = relation.get('extra_filters', {}) or {}

            # handle one-to-many relations
            if relation['type'] == 'OneToMany':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                setattr(model_object, relation['name'],
                        related_model.manager.filter(**{foreign_key: val, **extra}))

            # handle many-to-one relations
            elif relation['type'] == 'ManyToOne':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                try:
                    setattr(model_object, relation['name'],
                            related_model.manager.filter(**{foreign_key: val, **extra})[0])
                except IndexError:
                    setattr(model_object, relation['name'], None)

            # handle one-to-one relations
            elif relation['type'] == 'OneToOne':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                try:
                    setattr(model_object, relation['name'],
                            related_model.manager.filter(**{foreign_key: val, **extra})[0])
                except IndexError:
                    setattr(model_object, relation['name'], None)

            # handle many-to-many relations: the join table is read in
            # the same statement, as a subquery, rather than as an id list
            elif relation['type'] == 'ManyToMany':
                related_model = relation['related_model']
                join_table = self.model_class._get_mappings('Tables')[relation['join_table']]
                keys = self.model_class._get_mappings(relation['join_table'])
                val = getattr(model_object, relation['from_key'])
                members = Subquery(join_table, keys[relation['to_key']],
                                   [Where(keys[relation['from_key']], val)])
                setattr(
                    model_object,
                    relation['name'],
                    related_model.manager.filter(
                        **{f"{relation['to_key']}__in": members, **extra}
                    ),
                )

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
        related_ids = self.compiler.execute(query.sql, query.params)
        return [row[0] for row in related_ids]

    # Write operations intentionally do NOT live on the manager: the
    # manager's connection is read-only by design. See
    # py_apple_books.collection_writer for the guarded write path.
