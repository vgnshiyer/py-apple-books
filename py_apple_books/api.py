from py_apple_books.models import Book, Collection, Annotation
from py_apple_books.models.manager import ModelIterable


class PyAppleBooks:
    """Facade class for accessing Apple Books data."""

    # -- collection actions --
    def list_collections(self) -> ModelIterable:
        """List all collections."""
        return Collection.manager.all()

    def get_collection_by_id(self, collection_id: str) -> Collection:
        """Get a collection and its books."""
        return Collection.manager.filter(id=collection_id)[0]

    def get_collection_by_title(self, title: str) -> Collection:
        """Get a collection and its books."""
        return Collection.manager.filter(title=title)[0]

    # -- book actions --
    def list_books(self) -> ModelIterable:
        """List all books."""
        return Book.manager.all()

    def get_book_by_id(self, book_id: str) -> Book:
        """Get a book and its annotations."""
        return Book.manager.filter(id=book_id)[0]

    # -- annotation actions --
    def list_annotations(self) -> ModelIterable:
        """List all annotations."""
        return Annotation.manager.all()

    def get_annotation_by_id(self, annotation_id: str) -> Annotation:
        """Get an annotation by id."""
        return Annotation.manager.filter(id=annotation_id)[0]
