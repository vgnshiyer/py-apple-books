"""SQL clause primitives for the read path.

Since 1.10 values are always bound as parameters: :meth:`Clause.to_sql`
returns ``(sql, params)``, and only column names (which come from
``mappings.ini``) are written into the SQL text. ``str(clause)`` keeps
the pre-1.10 literal rendering for debugging and backward compatibility
only; the library never executes it.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from py_apple_books.text import fold_for_match

# Operators Where.to_sql() accepts (compared after strip() and upper()).
# CONTAINS and SEARCH are the manager's ``__contains`` and ``__search``
# lookups; the rest are SQL operators.
OPERATORS = frozenset({
    '=', '!=', '<>', '<', '<=', '>', '>=', 'IS', 'IS NOT',
    'LIKE', 'NOT LIKE', 'GLOB', 'NOT GLOB', 'IN', 'NOT IN',
    'CONTAINS', 'SEARCH',
})

LIKE_ESCAPE = '\\'


def escape_like(s: str) -> str:
    """Escape ``s`` for a LIKE pattern with ``ESCAPE '\\'``, so ``%``,
    ``_`` and backslash match themselves (for a ``LIKE`` clause of your
    own; ``__contains`` doesn't use patterns)."""
    return (s.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
             .replace('%', LIKE_ESCAPE + '%')
             .replace('_', LIKE_ESCAPE + '_'))


def _is_subquery(value) -> bool:
    """Whether ``value`` is the right-hand side of ``IN (SELECT ...)``.

    Checked by type, not by a ``to_sql`` attribute: a pandas Series has
    a ``to_sql`` method, and is a list of values here.
    """
    from py_apple_books.db.query import CompiledQuery  # query imports this module
    return isinstance(value, (Subquery, CompiledQuery, Clause))


class Clause:
    def to_sql(self) -> Tuple[str, list]:
        """``(sql, params)``: SQL text with ``?`` placeholders and the
        values to bind to them, in order."""
        raise NotImplementedError

    def columns(self) -> set:
        """Columns of the queried table this clause reads."""
        return set()

    def __str__(self):
        pass

    # Unused since 1.10 (kept until 2.0); see escape_like().
    def _escape_like(self, value: str) -> str:
        escaped_value = value.replace('%', r'\%').replace('_', r'\_')
        return f"'{escaped_value}'"

    def _escape_value(self, value) -> str:
        # Legacy literal rendering, for __str__ only.
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return str(value)


class Join(Clause):
    def __init__(self, table: str, on: str, type: str = ''):
        self.table = table
        self.on = on
        self.type = type

    def __str__(self):
        return f"{self.type} JOIN {self.table} ON {self.on}"


class Where(Clause):
    """``field <operator> value``.

    Construction accepts any operator, as before 1.10; :meth:`to_sql`
    raises ``ValueError`` for one outside :data:`OPERATORS`.
    """

    def __init__(self, field: str, value, operator: str = '='):
        self.field = field
        self.value = value
        self.operator = operator
        self.is_list = isinstance(value, (list, tuple))

    def columns(self) -> set:
        return {self.field}

    def to_sql(self) -> Tuple[str, list]:
        field, value = self.field, self.value
        op = str(self.operator).strip().upper()
        if op not in OPERATORS:
            raise ValueError(
                f"Operator {self.operator!r} is not supported for parameterized queries; "
                "use a manager lookup such as __gte/__lte")
        if op in ('IN', 'NOT IN'):
            if _is_subquery(value):
                sub_sql, sub_params = value.to_sql()
                return f"{field} {op} ({sub_sql})", list(sub_params)
            if isinstance(value, (str, bytes, bytearray)):
                # Before 1.10 a string was pasted into the SQL as is.
                raise TypeError(f"{op} takes a list of values: pass a list or a Subquery, "
                                f"not {type(value).__name__}")
            items = list(value)
            return f"{field} {op} ({', '.join('?' * len(items))})", items
        if op in ('IS', 'IS NOT') and (value is None or value == 'NULL'):
            return f"{field} {op} NULL", []
        if op == 'CONTAINS':
            # Literal substring, case-insensitive for ASCII letters only,
            # like LIKE. instr() compares whole values; LIKE stops reading
            # both sides at a NUL character.
            return f"instr(upper({field}), upper(?)) > 0", [str(value)]
        if op == 'SEARCH':
            raw = str(value)
            needle = fold_for_match(raw)
            if raw and not needle:
                # Only characters folding drops (combining accents,
                # zero-width characters, soft hyphen): nothing to find.
                # An empty needle still matches every non-NULL row.
                return "0", []
            # abk_fold is registered on every read connection (db.client).
            # It gets the column's bytes, so a row that isn't valid UTF-8
            # folds with U+FFFD instead of failing the whole query.
            return f"instr(abk_fold(CAST({field} AS BLOB)), ?) > 0", [needle]
        return f"{field} {op} ?", [value]

    def __str__(self):
        if self.operator == 'IN':
            # Handle lists for IN operator
            if isinstance(self.value, str):
                # If it's already a comma-separated string, use it directly
                formatted_value = f"({self.value})"
            elif _is_subquery(self.value):
                formatted_value = f"({self.value})"
            else:
                # Format each item separately and join them
                formatted_items = [self._escape_value(item) for item in self.value]
                formatted_value = f"({', '.join(formatted_items)})"
            return f"{self.field} {self.operator} {formatted_value}"
        if self.operator in ('IS', 'IS NOT') and (
            self.value is None or self.value == 'NULL'
        ):
            # SQL keyword NULL — must not be quoted. Without this
            # special case, ``__isnull`` filters render as
            # ``col IS 'NULL'`` (literal string), which matches nothing
            # on numeric columns and every row with the literal string
            # ``'NULL'`` on text columns — silently broken.
            return f"{self.field} {self.operator} NULL"
        # Handle regular operators
        return f"{self.field} {self.operator} {self._escape_value(self.value)}"


class Subquery:
    """``SELECT column FROM table [WHERE ...]``, for the right-hand side
    of ``IN`` (``field__in=Subquery(...)``). ``where`` clauses are ANDed."""

    def __init__(self, table: str, column: str, where: Sequence[Clause] = ()):
        self.table = table
        self.column = column
        self.where = list(where)

    def needed_columns(self) -> set:
        """Columns of ``table`` the subquery reads."""
        cols = {self.column}
        for clause in self.where:
            cols |= clause.columns()
        return cols

    def to_sql(self) -> Tuple[str, list]:
        sql = f"SELECT {self.column} FROM {self.table}"
        parts, params = _render(self.where)
        if parts:
            sql += " WHERE " + " AND ".join(parts)
        return sql, params

    def __str__(self):
        # Literal rendering, for debugging only.
        sql = f"SELECT {self.column} FROM {self.table}"
        if self.where:
            sql += " WHERE " + " AND ".join(str(clause) for clause in self.where)
        return sql

    def __repr__(self):
        return f"Subquery({self.table!r}, {self.column!r}, {self.where!r})"


def _render(clauses: Iterable[Clause]) -> Tuple[List[str], list]:
    parts, params = [], []
    for clause in clauses:
        sql, p = clause.to_sql()
        parts.append(sql)
        params.extend(p)
    return parts, params


class WhereGroup(Clause):
    """Clauses joined by ``connector`` (``'AND'`` or ``'OR'``), in
    parentheses.

    ``negated=True`` renders ``(<group>) IS NOT 1``: true unless the
    group is true, so rows where it is NULL (a NULL column) are kept,
    unlike SQL ``NOT``. An empty group is ``1`` (``0`` when negated).
    """

    def __init__(self, children: Iterable[Clause], connector: str = 'AND', negated: bool = False):
        self.children = list(children)
        connector = str(connector).strip().upper()
        if connector not in ('AND', 'OR'):
            raise ValueError(f"connector must be 'AND' or 'OR', not {connector!r}")
        self.connector = connector
        self.negated = negated

    def columns(self) -> set:
        cols = set()
        for child in self.children:
            cols |= child.columns()
        return cols

    def to_sql(self) -> Tuple[str, list]:
        parts, params = _render(self.children)
        return self._join(parts), params

    def _join(self, parts: List[str]) -> str:
        if not parts:
            return '0' if self.negated else '1'
        sql = f"({f' {self.connector} '.join(parts)})"
        return sql + " IS NOT 1" if self.negated else sql

    def __str__(self):
        # Literal rendering, for debugging only.
        return self._join([str(child) for child in self.children])


class Not(WhereGroup):
    """NULL-safe negation: true unless ``inner`` is true."""

    def __init__(self, inner: Clause):
        super().__init__([inner], negated=True)


class Q:
    """Composable manager lookups for ``filter(where=...)``.

    ``Q(title__search='x', is_deleted=0)`` ANDs its lookups; ``a | b``
    and ``a & b`` combine, ``~a`` negates (NULL-safe, see
    :class:`WhereGroup`)::

        Q(selected_text__search=t) | Q(note__search=t)
    """

    def __init__(self, **lookups):
        self.children: list = list(lookups.items())
        self.connector = 'AND'
        self.negated = False

    @classmethod
    def _group(cls, children: list, connector: str, negated: bool = False) -> 'Q':
        q = cls()
        q.children, q.connector, q.negated = children, connector, negated
        return q

    def _combine(self, other, connector: str) -> 'Q':
        if not isinstance(other, Q):
            return NotImplemented
        children = []
        for q in (self, other):
            # Flatten a | b | c into one group; an empty Q() adds nothing.
            if not q.negated and (q.connector == connector or len(q.children) <= 1):
                children.extend(q.children)
            else:
                children.append(q)
        return Q._group(children, connector)

    def __or__(self, other) -> 'Q':
        return self._combine(other, 'OR')

    def __and__(self, other) -> 'Q':
        return self._combine(other, 'AND')

    def __invert__(self) -> 'Q':
        if self.negated:
            return Q._group([self], 'AND', negated=True)
        return Q._group(list(self.children), self.connector, negated=True)

    def resolve(self, manager) -> Clause:
        """The clause for ``manager``'s model (field names are mapped to
        columns; an unknown one raises ``UnknownFieldError``)."""
        clauses = [child.resolve(manager) if isinstance(child, Q)
                   else manager._lookup_to_where(*child)
                   for child in self.children]
        if len(clauses) == 1 and not self.negated:
            return clauses[0]
        return WhereGroup(clauses, self.connector, self.negated)

    def __repr__(self):
        inner = ', '.join(repr(c) if isinstance(c, Q) else f"{c[0]}={c[1]!r}" for c in self.children)
        text = f"Q({inner})" if self.connector == 'AND' else f"Q[{self.connector}]({inner})"
        return f"~{text}" if self.negated else text
