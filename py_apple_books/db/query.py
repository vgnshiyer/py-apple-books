from py_apple_books.db.clause import Clause, Where
from typing import TYPE_CHECKING, List, NamedTuple, Optional, Any, Sequence, Tuple, Union
import numbers
import operator

if TYPE_CHECKING:
    from py_apple_books.db.client import DBClient

_INT64_MIN = -2**63
_INT64_MAX = 2**63 - 1

# Types sqlite3 binds as they are (bool is handled as an int).
_NATIVE = (int, float, str, bytes, bytearray, memoryview)


def __getattr__(name):
    # db.client imports adapt_params from this module, so DBClient (a
    # module attribute before 1.10) is imported on first access.
    if name == 'DBClient':
        from py_apple_books.db.client import DBClient
        return DBClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _as_number(value):
    """``value`` as an ``int`` or ``float`` if it is a number sqlite3
    can't bind, otherwise ``value`` unchanged."""
    try:
        return operator.index(value)
    except TypeError:
        pass
    if isinstance(value, numbers.Real) or (
            isinstance(value, numbers.Number) and not isinstance(value, numbers.Complex)):
        # Real, or registered only as a Number (Decimal).
        try:
            as_int = int(value)
            return as_int if as_int == value else float(value)
        except (TypeError, ValueError, OverflowError):
            pass  # NaN or infinite: sqlite3 reports it, as it did before
    return value


def adapt_params(params: Sequence[Any]) -> tuple:
    """Make query parameters safe to bind with :mod:`sqlite3`.

    Every read query binds its parameters through this function:

    * A number of a type sqlite3 can't bind, which pre-1.10 releases
      wrote into the SQL as ``str(value)``, becomes an ``int`` or a
      ``float``: an object with ``__index__`` (such as
      ``numpy.int64``) through ``operator.index``, and a real number
      such as ``Decimal`` or ``Fraction`` as an ``int`` when it is
      integral, else as a ``float``. The rules below then apply.
    * ``bool`` becomes ``int`` (``True`` → 1).
    * An ``int`` outside SQLite's 64-bit range [-2**63, 2**63 - 1] is
      bound as its decimal ``str``. sqlite3 would raise
      ``OverflowError`` binding it; as text, SQLite applies the
      column's numeric affinity and turns it into a REAL, exactly as
      it read the literal pre-1.10 releases wrote into the SQL. Such a
      value therefore matches no integer id and never raises.
    * A ``str`` that can't be encoded as UTF-8 (it holds lone
      surrogates) gets U+FFFD in their place (encoded with
      ``surrogatepass``, decoded with ``replace``, as
      :func:`py_apple_books.text.fold_for_match` does), instead of
      sqlite3 raising ``UnicodeEncodeError``.
    * Anything else is passed through unchanged.
    """
    out = []
    for value in params:
        if value is not None and not isinstance(value, _NATIVE):
            value = _as_number(value)
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, int):
            if value < _INT64_MIN or value > _INT64_MAX:
                value = str(value)
        elif isinstance(value, str):
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                value = value.encode('utf-8', 'surrogatepass').decode('utf-8', 'replace')
        out.append(value)
    return tuple(out)


class CompiledQuery(NamedTuple):
    """A SQL statement with ``?`` placeholders and the values to bind."""

    sql: str
    params: tuple

    def to_sql(self) -> Tuple[str, list]:
        # Also lets a CompiledQuery stand in for a Subquery.
        return self.sql, list(self.params)


class QueryCompiler:
    """SQL query compiler"""

    def __init__(self, client: 'DBClient'):
        self.client = client

    def execute(self, query: str, params: Sequence[Any] = ()) -> list[Any]:
        return self.client.execute(query, params)


class Query:
    """SQL query builder"""

    # TODO: add join query support (single and multi-level)

    @staticmethod
    def compile(table_name: str,
                fields: Union[List[str], str] = '*',
                where: Optional[Union[List[Clause], Clause]] = None,
                order_by: Optional[Union[List[str], str]] = None,
                limit: Optional[int] = None,
                offset: Optional[int] = None,
                use_or: bool = False) -> CompiledQuery:
        """
        Build a parameterized SELECT query

        Args:
            table_name: The name of the table
            fields: The columns to select (a list, or a string used as is)
            where: Clauses joined with AND (OR when ``use_or``); a group
                is parenthesized
            order_by: Rendered terms (``'COL ASC'``), as a list or a string
            limit: Maximum number of rows; None for all
            offset: Rows to skip
            use_or: Join the top-level WHERE clauses with OR
        """
        fields_str = ', '.join(fields) if isinstance(fields, list) else fields
        sql = f"SELECT {fields_str} FROM {table_name}"
        params: list = []

        if isinstance(where, Clause):
            where = [where]
        if where:
            parts = []
            for clause in where:
                part, part_params = clause.to_sql()
                parts.append(part)
                params.extend(part_params)
            sql += " WHERE " + (" OR " if use_or else " AND ").join(parts)

        if order_by:
            sql += " ORDER BY " + (order_by if isinstance(order_by, str) else ", ".join(order_by))

        if limit is not None or (offset or 0) > 0:
            # SQLite has no OFFSET without LIMIT; -1 means no limit.
            sql += " LIMIT ?"
            params.append(-1 if limit is None else limit)
            if (offset or 0) > 0:
                sql += " OFFSET ?"
                params.append(offset)

        return CompiledQuery(sql, tuple(params))

    @staticmethod
    def count(inner: CompiledQuery) -> CompiledQuery:
        """``SELECT COUNT(*)`` over the rows ``inner`` returns."""
        return CompiledQuery(f"SELECT COUNT(*) FROM ({inner.sql})", tuple(inner.params))

    @staticmethod
    def select(table_name: str,
               fields: Union[List[str], str] = '*',
               where: Optional[List[Where]] = None,
               order_by: Optional[str] = None,
               limit: Optional[int] = None,
               use_or: bool = False) -> str:
        """
        Build a SELECT query as literal SQL text (pre-1.10 form).

        Kept for backward compatibility and debugging only: the library
        executes :meth:`compile`'s parameterized form instead.

        Args:
            table_name: The name of the table
            fields: The fields to select
            where: The WHERE clause
            order_by: The ORDER BY clause
            limit: The LIMIT clause
        """
        if isinstance(fields, list):
            fields_str = ', '.join(fields)
        else:
            fields_str = fields

        query = f"SELECT {fields_str} FROM {table_name}"

        if where:
            where_clauses = [str(clause) for clause in where]
            if use_or:
                query += f" WHERE {' OR '.join(where_clauses)}"
            else:
                query += f" WHERE {' AND '.join(where_clauses)}"

        if order_by:
            query += f" ORDER BY {order_by}"

        if limit:
            query += f" LIMIT {limit}"

        return query

    # NOTE: insert/update/delete string-builders were removed on purpose.
    # They interpolated values into SQL without escaping (a title with an
    # apostrophe would break the statement), and the read path they belong
    # to now opens the database read-only anyway. Write operations live in
    # :mod:`py_apple_books.collection_writer`, which uses parameterized
    # queries on a dedicated guarded connection.
