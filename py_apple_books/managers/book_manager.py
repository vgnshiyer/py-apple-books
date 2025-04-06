from py_apple_books.data import book_db
from py_apple_books.models.book import Book
from py_apple_books.exceptions import BookNotFoundError, CollectionNotFoundError
from py_apple_books.managers.base import ManagerBase
from py_apple_books.utils import get_mappings
from typing import Optional, List, Dict


class BookManager(ManagerBase):
    """Manages book-related operations."""

    def __init__(self):
        super().__init__()
        self._book_mappings = get_mappings('Book')
        self._annotation_mappings = get_mappings('Annotation')
        self._book_map: Dict[str, Book] = {}

    def _get_or_create_book(self, book_params: dict) -> Book:
        id_ = book_params['id']
        if id_ not in self._book_map:
            self._book_map[id_] = Book(**book_params)
        return self._book_map[id_]

    def _create_book_object(self, raw_book_data: list) -> Book:
        book_fields_len = len(self._book_mappings)
        book_dict = dict(zip(self._book_mappings.keys(), raw_book_data[:book_fields_len]))
        book = self._get_or_create_book(book_dict)
        return book

    def list_books(self, include_annotations: bool = False) -> List[Book]:
        """
        Returns a list of Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_all(include_annotations)
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_book_by_id(self, book_id: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a Book object by id.

        Args:
            book_id (str): book id
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            Book: Book object
        """
        raw_book_data = book_db.find_by_id(book_id, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(book_id=book_id)
        raw_book_data = raw_book_data[0]
        return self._create_book_object(raw_book_data)

    def get_book_by_asset_id(self, asset_id: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a Book object by asset id.

        Args:
            asset_id (str): asset id
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            Book: Book object
        """
        raw_book_data = book_db.find_by_asset_id(asset_id, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(asset_id=asset_id)
        raw_book_data = raw_book_data[0]
        return self._create_book_object(raw_book_data)

    def get_book_by_title(self, title: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a Book object by title.

        Args:
            title (str): book title
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            Book: Book object
        """
        raw_book_data = book_db.find_by_title(title, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(title=title)
        raw_book_data = raw_book_data[0]
        return self._create_book_object(raw_book_data)

    def get_books_by_author(self, author: str, include_annotations: bool = False) -> List[Book]:
        """
        Fetches a list of Book objects by author.

        Args:
            author (str): book author
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_by_author(author, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(author=author)
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_books_by_genre(self, genre: str, include_annotations: bool = False) -> List[Book]:
        """
        Fetches a list of Book objects by genre.

        Args:
            genre (str): book genre
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_by_genre(genre, include_annotations)
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_finished_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of finished Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_finished_books(include_annotations)
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_unfinished_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of unfinished Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_unfinished_books(include_annotations)
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)