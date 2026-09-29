"""Model relations: lazy, non-data descriptors (F02).

* To-many (``Book.annotations``, ``Collection.books``,
  ``Book.collections``): every access returns a new, unevaluated
  :class:`~py_apple_books.models.manager.ModelIterable`; nothing runs
  until it is used.
* To-one (``Annotation.book``): resolved on first access and cached on
  the instance. On a model from a multi-row result, the first access
  loads the related rows of every model of that result at once (one
  ``IN`` query per :data:`BATCH_SIZE` keys) instead of one query per row.

A relation reads the library its instance was read from, even once
that library is closed: the relations of models from a
``with LibraryDB(...)`` block, used after the block, open its
connections again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Generic, Mapping, Optional, Tuple, Type, TypeVar, overload

from py_apple_books.db.clause import Subquery, Where

if TYPE_CHECKING:
    from py_apple_books.models.manager import ModelIterable

T = TypeVar('T')

# Keys per IN list when a to-one relation loads for a whole result: well
# under SQLite's smallest limit on bound parameters (999 before 3.32).
BATCH_SIZE = 500


def _db_of(instance):
    """The library ``instance`` was read from (None: the current one)."""
    return instance.__dict__.get('_ab_db')


class Relation(Generic[T]):
    def __init__(
        self,
        related_model: Type[T],
        related_name: str,
        extra_filters: Optional[Mapping[str, Any]] = None,
    ):
        self.related_model = related_model
        self.related_name = related_name
        # Extra filter kwargs applied whenever this relation is traversed,
        # in addition to the foreign-key match. Useful for relations that
        # should only include a subset of the related rows — e.g. a
        # Book's ``annotations`` relation excluding auto-bookmark rows.
        self.extra_filters = dict(extra_filters or {})
        # The attribute name and the class it is on (set by __set_name__,
        # or by ModelBase for a reverse relation).
        self.name: Optional[str] = None
        self.owner: Optional[type] = None

    def __set_name__(self, owner, name):
        self.owner = owner
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return self._resolve(instance)

    def _resolve(self, instance):
        raise NotImplementedError

    def __repr__(self) -> str:
        owner = getattr(self.owner, '__name__', '?')
        return f"<{type(self).__name__} {owner}.{self.name}>"


class OneToMany(Relation[T]):
    def __init__(
        self,
        related_model: Type[T],
        related_name: str,
        foreign_key: str,
        extra_filters: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(related_model, related_name, extra_filters=extra_filters)
        self.foreign_key = foreign_key

    @overload
    def __get__(self, instance: None, owner: Optional[type] = None) -> 'OneToMany[T]': ...

    @overload
    def __get__(self, instance: object, owner: Optional[type] = None) -> 'ModelIterable[T]': ...

    def __get__(self, instance, owner=None):
        return super().__get__(instance, owner)

    def _resolve(self, instance):
        value = getattr(instance, self.foreign_key)
        return self.related_model.manager._relation_iterable(
            _db_of(instance), **{self.foreign_key: value, **self.extra_filters})


# The to-one relation names of each model class, for _release_siblings.
_TO_ONE_NAMES: Dict[type, Tuple[str, ...]] = {}


def _to_one_names(cls: type) -> Tuple[str, ...]:
    names = _TO_ONE_NAMES.get(cls)
    if names is None:
        names = tuple(dict.fromkeys(
            name for klass in cls.__mro__ for name, attr in vars(klass).items()
            if isinstance(attr, _ToOne)))
        _TO_ONE_NAMES[cls] = names
    return names


def _release_siblings(objs) -> None:
    """Drop the sibling list from each model whose to-one relations are
    all resolved: it is only kept to load them."""
    for obj in objs:
        state = obj.__dict__
        if all(name in state for name in _to_one_names(type(obj))):
            state.pop('_ab_siblings', None)


class _ToOne(Relation[T]):
    """A relation to one model, found by ``foreign_key`` (the same field
    name on both models), or None."""

    foreign_key: str

    @overload
    def __get__(self, instance: None, owner: Optional[type] = None) -> '_ToOne[T]': ...

    @overload
    def __get__(self, instance: object, owner: Optional[type] = None) -> Optional[T]: ...

    def __get__(self, instance, owner=None):
        return super().__get__(instance, owner)

    def _filters(self) -> Mapping[str, Any]:
        return self.extra_filters

    def _resolve(self, instance):
        state = instance.__dict__
        key = self.foreign_key
        manager = self.related_model.manager
        value = getattr(instance, key)
        if value is None:
            state[self.name] = None
            _release_siblings((instance,))
            return None
        siblings = state.get('_ab_siblings')
        if not siblings:
            found = manager._relation_iterable(_db_of(instance), **{key: value, **self._filters()}).first()
            state[self.name] = found
            return found
        # Load the related rows of every unresolved model of the result,
        # lowest primary key first per key, as the single lookup does.
        pending = [obj for obj in siblings if self.name not in obj.__dict__]
        keys = list(dict.fromkeys(v for v in (getattr(obj, key) for obj in pending) if v is not None))
        related: dict = {}
        for start in range(0, len(keys), BATCH_SIZE):
            batch = manager._relation_iterable(
                _db_of(instance), order_by='id',
                **{f"{key}__in": keys[start:start + BATCH_SIZE], **self._filters()})
            for obj in batch:
                related.setdefault(getattr(obj, key), obj)
        for obj in pending:
            obj.__dict__[self.name] = related.get(getattr(obj, key))
        _release_siblings(pending)
        return state[self.name]


class OneToOne(_ToOne[T]):
    def __init__(
        self,
        related_model: Type[T],
        related_name: str,
        foreign_key: str,
        extra_filters: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(related_model, related_name, extra_filters=extra_filters)
        self.foreign_key = foreign_key


class ReverseToOne(_ToOne[T]):
    """The model on the other side of a :class:`OneToMany` or
    :class:`OneToOne` (``Annotation.book``); ModelBase installs it.

    Unfiltered: the forward relation's ``extra_filters`` apply to that
    direction only.
    """

    def __init__(self, forward: Relation, owner_model: Type[T]):
        super().__init__(owner_model, forward.name)
        self.forward = forward
        self.foreign_key = forward.foreign_key
        self.owner = forward.related_model
        self.name = forward.related_name

    def _filters(self) -> Mapping[str, Any]:
        return {}


class ManyToMany(Relation[T]):
    def __init__(
        self,
        related_model: Type[T],
        related_name: str,
        from_key: str,
        to_key: str,
        join_table: str,
        extra_filters: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(related_model, related_name, extra_filters=extra_filters)
        self.from_key = from_key
        self.to_key = to_key
        self.join_table = join_table

    @overload
    def __get__(self, instance: None, owner: Optional[type] = None) -> 'ManyToMany[T]': ...

    @overload
    def __get__(self, instance: object, owner: Optional[type] = None) -> 'ModelIterable[T]': ...

    def __get__(self, instance, owner=None):
        return super().__get__(instance, owner)

    def _resolve(self, instance):
        # The join table is read in the same statement, as a subquery
        # (its columns are checked when the query compiles).
        model = self.related_model
        keys = model._get_mappings(self.join_table)
        members = Subquery(model._get_mappings('Tables')[self.join_table], keys[self.to_key],
                           [Where(keys[self.from_key], getattr(instance, self.from_key))])
        return model.manager._relation_iterable(
            _db_of(instance), **{f"{self.to_key}__in": members, **self.extra_filters})


class ReverseManyToMany(ManyToMany[T]):
    """The other side of a :class:`ManyToMany` (``Book.collections``);
    ModelBase installs it. Unfiltered, like :class:`ReverseToOne`."""

    def __init__(self, forward: ManyToMany, owner_model: Type[T]):
        super().__init__(owner_model, forward.name, forward.to_key, forward.from_key,
                         forward.join_table)
        self.forward = forward
        self.owner = forward.related_model
        self.name = forward.related_name
