from dataclasses import dataclass, field
from py_apple_books.models.book import Book
from typing import List

@dataclass
class Collection:
    # Identifiers
    id: str
    title: str

    # Status
    is_deleted: bool
    is_hidden: bool

    # Collection details
    details: str
    books: List[Book] = field(default_factory=list)

    def add_book(self, book: Book) -> None:
        """
        Adds a book to the collection.

        Args:
            book (Book): book to add to the collection
        """
        self.books.append(book)

    def __hash__(self) -> int:
        """
        Returns a hash value for the Collection object.

        Returns:
            int: hash value for the Collection object
        """
        return hash(self.id)