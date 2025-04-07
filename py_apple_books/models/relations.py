from typing import TypeVar, Type

T = TypeVar('T')


# TODO: add validations
class Relation:
    def __init__(self, related_model: Type[T], related_name: str):
        self.related_model = related_model
        self.related_name = related_name


class OneToMany(Relation):
    def __init__(self, related_model: Type[T], related_name: str, foreign_key: str):
        super().__init__(related_model, related_name)
        self.foreign_key = foreign_key


class OneToOne(Relation):
    def __init__(self, related_model: Type[T], related_name: str, foreign_key: str):
        super().__init__(related_model, related_name)
        self.foreign_key = foreign_key


class ManyToMany(Relation):
    def __init__(self, related_model: Type[T], related_name: str, from_key: str, to_key: str, join_table: str):
        super().__init__(related_model, related_name)
        self.from_key = from_key
        self.to_key = to_key
        self.join_table = join_table
