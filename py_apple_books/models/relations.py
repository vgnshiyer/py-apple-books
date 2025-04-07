from typing import TypeVar, Type

T = TypeVar('T')


class Relation:
    def __init__(self, related_model: Type[T], related_name: str, foreign_key: str):
        self.related_model = related_model
        self.related_name = related_name
        self.foreign_key = foreign_key


class OneToMany(Relation):
    pass


class ManyToMany(Relation):
    pass


class OneToOne(Relation):
    pass
