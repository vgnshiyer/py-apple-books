from py_apple_books.managers.book_manager import BookManager
from py_apple_books.managers.collection_manager import CollectionManager
from py_apple_books.managers.annotation_manager import AnnotationManager
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from py_apple_books.models.annotation import Annotation
from typing import Optional, List


class PyAppleBooks:
    """Facade class for accessing Apple Books data."""

    def __init__(self):
        self._collection_manager = CollectionManager()
        self._book_manager = BookManager()
        self._annotation_manager = AnnotationManager()

    # Collection-related methods
    def list_collections(self, include_books: bool = False) -> List[Collection]:
        """
        Returns a list of Collection objects.

        Args:
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            list[Collection]: list of Collection objects
        """
        return self._collection_manager.list_collections(include_books)

    def get_collection_by_id(self, collection_id: str, include_books: bool = False) -> Optional[Collection]:
        """
        Fetches a Collection object by id.

        Args:
            collection_id (str): collection id
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            Collection: Collection object
        """
        return self._collection_manager.get_collection_by_id(collection_id, include_books)

    def get_collection_by_name(self, collection_name: str, include_books: bool = False) -> Optional[Collection]:
        """
        Fetches a Collection object by name.

        Args:
            collection_name (str): collection name
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            Collection: Collection object
        """
        return self._collection_manager.get_collection_by_name(collection_name, include_books)

    # Book-related methods
    def list_books(self, include_annotations: bool = False) -> List[Book]:
        """
        Returns a list of Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        return self._book_manager.list_books(include_annotations)

    def get_book_by_id(self, book_id: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a Book object by id.

        Args:
            book_id (str): book id
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            Book: Book object
        """
        return self._book_manager.get_book_by_id(book_id, include_annotations)

    def get_book_by_asset_id(self, asset_id: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a Book object by asset id.

        Args:
            asset_id (str): asset id
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            Book: Book object
        """
        return self._book_manager.get_book_by_asset_id(asset_id, include_annotations)

    def get_book_by_title(self, title: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a Book object by title.

        Args:
            title (str): book title
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            Book: Book object
        """
        return self._book_manager.get_book_by_title(title, include_annotations)

    def get_books_by_author(self, author: str, include_annotations: bool = False) -> List[Book]:
        """
        Fetches a list of Book objects by author.

        Args:
            author (str): book author
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        return self._book_manager.get_books_by_author(author, include_annotations)

    def get_books_by_genre(self, genre: str, include_annotations: bool = False) -> List[Book]:
        """
        Fetches a list of Book objects by genre.

        Args:
            genre (str): book genre
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        return self._book_manager.get_books_by_genre(genre, include_annotations)

    def get_finished_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of finished Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        return self._book_manager.get_finished_books(include_annotations)

    def get_unfinished_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of unfinished Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        return self._book_manager.get_unfinished_books(include_annotations)

    # Annotation-related methods
    def list_annotations(self) -> List[Annotation]:
        """
        Fetches a list of Annotation objects.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        return self._annotation_manager.list_annotations(type='all')

    def list_highlights(self) -> List[Annotation]:
        """
        Fetches a list of Highlight objects.

        Returns:
            list[Highlight]: list of Highlight objects
        """
        return self._annotation_manager.list_annotations(type='highlights')

    def list_underlines(self) -> List[Annotation]:
        """
        Fetches a list of Underline objects.

        Returns:
            list[Underline]: list of Underline objects
        """
        return self._annotation_manager.list_annotations(type='underlines')

    def list_all_notes(self) -> list[Annotation]:
        """
        Fetches a list of Annotation objects with notes.
        """
        return self._annotation_manager.list_all_notes()

    def get_annotation_by_id(self, annotation_id: str) -> Optional[Annotation]:
        """
        Fetches an Annotation object by id.

        Args:
            annotation_id (str): annotation id

        Returns:
            Annotation: Annotation object
        """
        return self._annotation_manager.get_annotation_by_id(annotation_id)

    def search_annotations_by_text(self, text: str) -> list[Annotation]:
        """
        Fetches Annotations by text.

        Args:
            text (str): text to search for

        Returns:
            list[Annotation]: list of Annotation objects
        """
        return self._annotation_manager.get_annotations_by_text(text)

    def get_annotations_by_color(self, color: str) -> list[Annotation]:
        """
        Fetches a list of Annotation objects by color.

        Args:
            color (str): color

        Returns:
            list[Annotation]: list of Annotation objects
        """
        return self._annotation_manager.get_annotation_by_color(color)
