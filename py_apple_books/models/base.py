from typing import Any
import pathlib
import configparser
from py_apple_books.models.manager import ModelManager


class ModelBase(type):
    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        setattr(cls, 'manager', ModelManager(cls))
        return cls


class AppleBooksModel(metaclass=ModelBase):
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
    def from_db(cls, db_data: list[Any]) -> 'AppleBooksModel':
        fields_len = len(cls._get_mappings(cls.__name__))
        return cls(**dict(zip(cls._get_mappings(cls.__name__).keys(), db_data[:fields_len])))

    @classmethod
    def to_db(cls) -> dict:
        pass

    # TODO
    def save(self) -> str:
        """
        Saves the model to the database.
        """
        pass
