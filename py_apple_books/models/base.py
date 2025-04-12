from typing import Any
import pathlib
import configparser
from py_apple_books.models.manager import ModelManager
from py_apple_books.models.relations import OneToMany, OneToOne, ManyToMany


class ModelBase(type):
    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        setattr(cls, 'manager', ModelManager(cls))

        cls.relations = []
        for attribute, value in attrs.items():
            if isinstance(value, OneToMany) or isinstance(value, OneToOne):
                relation_type = value.__class__.__name__
                forward_relation = {
                    'name': attribute,
                    'type': relation_type,
                    'related_model': value.related_model,
                    'foreign_key': value.foreign_key,
                }
                cls.relations.append(forward_relation)

                related_model = value.related_model
                if not hasattr(related_model, 'relations'):
                    related_model.relations = []
                backward_relation = {
                    'name': value.related_name,
                    'type': 'ManyToOne' if relation_type == 'OneToMany' else relation_type,
                    'related_model': cls,
                    'foreign_key': value.foreign_key,
                }
                related_model.relations.append(backward_relation)

            elif isinstance(value, ManyToMany):
                relation_type = value.__class__.__name__
                forward_relation = {
                    'name': attribute,
                    'type': relation_type,
                    'related_model': value.related_model,
                    'from_key': value.from_key,
                    'to_key': value.to_key,
                    'join_table': value.join_table,
                }
                cls.relations.append(forward_relation)

                related_model = value.related_model
                if not hasattr(related_model, 'relations'):
                    related_model.relations = []
                backward_relation = {
                    'name': value.related_name,
                    'type': relation_type,
                    'related_model': cls,
                    'from_key': value.to_key,
                    'to_key': value.from_key,
                    'join_table': value.join_table,
                }
                related_model.relations.append(backward_relation)

        return cls


class Model(metaclass=ModelBase):
    """
    Base class for all Apple Books models.
    """

    @classmethod
    def _get_mappings(cls, section: str, keys: list[str] | None = None) -> dict:
        mappings_path = pathlib.Path(__file__).parent / "mappings.ini"
        config = configparser.ConfigParser()
        config.read(mappings_path)
        if keys is None:
            return dict(config.items(section))
        return {key: config.get(section, key) for key in keys}

    @classmethod
    def from_db(cls, db_data: list[Any]) -> 'Model':
        fields_len = len(cls._get_mappings(cls.__name__))
        obj = cls(**dict(zip(cls._get_mappings(cls.__name__).keys(), db_data[:fields_len])))
        cls.manager.handle_relations(obj)
        return obj

    @classmethod
    def to_db(cls) -> dict:
        pass

    # TODO
    def save(self) -> str:
        """
        Saves the model to the database.
        """
        pass
