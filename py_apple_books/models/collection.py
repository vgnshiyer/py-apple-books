from dataclasses import dataclass, field
from py_apple_books.models.base import AppleBooksModel
from py_apple_books.models.book import Book
from typing import List


@dataclass
class Collection(AppleBooksModel):
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
    # TODO: add relations support
    books: List[Book] = field(default_factory=list)
