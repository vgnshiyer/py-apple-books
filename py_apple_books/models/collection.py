from dataclasses import dataclass
from py_apple_books.models.book import Book

@dataclass
class Collection:
    id_: str
    title: str
    books: list[Book]