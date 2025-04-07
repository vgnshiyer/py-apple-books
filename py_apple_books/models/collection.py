from dataclasses import dataclass
from py_apple_books.models.base import Model
from py_apple_books.models.book import Book
from py_apple_books.models.relations import ManyToMany


@dataclass
class Collection(Model):
    """
    Represents a collection in the Apple Books library.
    """
    # Identifiers
    id: str
    title: str

    # Status
    is_deleted: bool
    is_hidden: bool

    # Collection details
    details: str
    books = ManyToMany(
        related_model=Book,
        related_name='collections',
        foreign_key='book_id'
    )
