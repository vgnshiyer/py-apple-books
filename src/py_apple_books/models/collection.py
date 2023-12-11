from dataclasses import dataclass
from src.py_apple_books.models.book import Book

@dataclass
class Collection:
    id_: str # Z_PK
    title: str # ZTITLE
    books: list[Book]