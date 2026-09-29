from typing import Any
import functools
import pathlib
import configparser
from py_apple_books.models.manager import ModelIterable, ModelManager
from py_apple_books.models.relations import (
    ManyToMany, OneToMany, OneToOne, ReverseManyToMany, ReverseToOne,
)


@functools.cache
def _load_mappings() -> dict[str, dict[str, str]]:
    """Parse mappings.ini once per process.

    ``Model._get_mappings`` runs several times per row materialized, so
    re-reading the file on every call dominated query time on large
    libraries. Callers get copies via ``_get_mappings``; never mutate the
    cached dicts directly.
    """
    mappings_path = pathlib.Path(__file__).parent / "mappings.ini"
    config = configparser.ConfigParser()
    config.read(mappings_path)
    return {section: dict(config.items(section)) for section in config.sections()}


@functools.cache
def _field_names(section: str) -> tuple:
    """The field names of a mappings.ini section, in file order."""
    return tuple(_load_mappings()[section])


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
                    'extra_filters': dict(value.extra_filters),
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
                    # Reverse relations don't inherit extra_filters —
                    # filtering is one-directional by definition.
                    'extra_filters': {},
                }
                related_model.relations.append(backward_relation)
                setattr(related_model, value.related_name, ReverseToOne(value, cls))

            elif isinstance(value, ManyToMany):
                relation_type = value.__class__.__name__
                forward_relation = {
                    'name': attribute,
                    'type': relation_type,
                    'related_model': value.related_model,
                    'from_key': value.from_key,
                    'to_key': value.to_key,
                    'join_table': value.join_table,
                    'extra_filters': dict(value.extra_filters),
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
                    'extra_filters': {},
                }
                related_model.relations.append(backward_relation)
                setattr(related_model, value.related_name, ReverseManyToMany(value, cls))

        return cls


class Model(metaclass=ModelBase):
    """
    Base class for all Apple Books models.
    """

    @classmethod
    def _get_mappings(cls, section: str, keys: list[str] | None = None) -> dict:
        mappings = _load_mappings()[section]
        if keys is None:
            return dict(mappings)
        return {key: mappings[key] for key in keys}

    @classmethod
    def from_db(cls, db_data: list[Any], db=None) -> 'Model':
        """The model for a row of its mapped columns, in mappings.ini
        order. ``db`` is the library the row came from, which its
        relations read. Loads no relation: they load on first access."""
        keys = _field_names(cls.__name__)
        obj = cls(**dict(zip(keys, db_data[:len(keys)])))
        obj.__dict__['_ab_db'] = db
        return obj

    def __getstate__(self):
        # For pickle and copy: the library and the sibling list are not
        # part of a model's data, nor is a to-many relation's result.
        return {key: value for key, value in self.__dict__.items()
                if not key.startswith('_ab_') and not isinstance(value, ModelIterable)}

    @classmethod
    def to_db(cls) -> dict:
        pass

    # TODO
    def save(self) -> str:
        """
        Saves the model to the database.
        """
        pass
