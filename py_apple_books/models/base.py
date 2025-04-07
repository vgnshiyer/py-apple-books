from typing import Any
import pathlib
import configparser
from py_apple_books.models.manager import ModelManager
from py_apple_books.models.relations import Relation


class ModelBase(type):
    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        setattr(cls, 'manager', ModelManager(cls))

        cls.relations = []
        for attribute, value in attrs.items():
            if isinstance(value, Relation):
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
        cls._handle_relations(obj)
        return obj

    @classmethod
    def _handle_relations(cls, model_object: 'Model'):
        for relation in cls.relations:
            # handle one-to-many relations
            if relation['type'] == 'OneToMany':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                setattr(model_object, relation['name'], related_model.manager.filter(**{foreign_key: val}))

            # handle many-to-one relations
            if relation['type'] == 'ManyToOne':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                setattr(model_object, relation['name'], related_model.manager.filter(**{foreign_key: val})[0])

            # TODO: handle many-to-many relations

    @classmethod
    def to_db(cls) -> dict:
        pass

    # TODO
    def save(self) -> str:
        """
        Saves the model to the database.
        """
        pass
