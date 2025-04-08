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
        from_key='id',
        to_key='asset_id',
        join_table='book_collection'
    )

    def __str__(self):
        return f"ID: {self.id}\nTitle: {self.title}\nDetails: {self.details}"
