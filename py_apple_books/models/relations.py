from typing import TypeVar, Type, Optional, Mapping, Any

T = TypeVar('T')


# TODO: add validations
class Relation:
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


class OneToMany(Relation):
    def __init__(
        self,
        related_model: Type[T],
        related_name: str,
        foreign_key: str,
        extra_filters: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(related_model, related_name, extra_filters=extra_filters)
        self.foreign_key = foreign_key


class OneToOne(Relation):
    def __init__(
        self,
        related_model: Type[T],
        related_name: str,
        foreign_key: str,
        extra_filters: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(related_model, related_name, extra_filters=extra_filters)
        self.foreign_key = foreign_key


class ManyToMany(Relation):
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
