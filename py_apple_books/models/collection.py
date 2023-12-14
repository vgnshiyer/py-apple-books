from dataclasses import dataclass
from py_apple_books.models.book import Book

@dataclass
class Collection:
    # Identifiers
    id: str
    title: str

    # Collection details
    details: str
    books: list[Book]

    # Status
    is_deleted: bool
    is_hidden: bool