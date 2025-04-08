from py_apple_books.models import Book, Collection, Annotation, AnnotationColor
from py_apple_books.models.manager import ModelIterable


class PyAppleBooks:
    """Facade class for accessing Apple Books data."""

    # -- collection actions --
    def list_collections(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all collections."""
        return Collection.manager.all(limit=limit, order_by=order_by)

    def get_collection_by_id(self, collection_id: str) -> Collection:
        """Get a collection and its books."""
        return Collection.manager.filter(id=collection_id)[0]

    def get_collection_by_title(self, title: str) -> ModelIterable:
        """Get a collection and its books."""
        return Collection.manager.filter(title__contains=title)

    # -- book actions --
    def list_books(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all books."""
        return Book.manager.all(limit=limit, order_by=order_by)

    def get_book_by_id(self, book_id: str) -> Book:
        """Get a book and its annotations."""
        return Book.manager.filter(id=book_id)[0]

    def get_book_by_title(self, title: str) -> ModelIterable:
        """Get a book by title."""
        return Book.manager.filter(title__contains=title)

    # -- annotation actions --
    def list_annotations(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all annotations."""
        return Annotation.manager.all(limit=limit, order_by=order_by)

    def get_annotation_by_id(self, annotation_id: str) -> Annotation:
        """Get an annotation by id."""
        return Annotation.manager.filter(id=annotation_id)[0]

    def get_annotations_by_color(self, color: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get annotations by color."""
        style = AnnotationColor[color.upper()].value
        return Annotation.manager.filter(style=style, limit=limit, order_by=order_by)

    def search_annotation_by_highlighted_text(self, text: str,
                                              limit: int = None, order_by: str = None) -> ModelIterable:
        """Search for annotations by highlighted text."""
        return Annotation.manager.filter(selected_text__contains=text, limit=limit, order_by=order_by)

    def search_annotation_by_note(self, note: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Search for annotations by note."""
        return Annotation.manager.filter(note__contains=note, limit=limit, order_by=order_by)

    def search_annotation_by_text(self, text: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Search for annotations by any text that contains the given text."""
        return Annotation.manager.filter(selected_text__contains=text,
                                         representative_text__contains=text,
                                         note__contains=text,
                                         use_or=True,
                                         limit=limit,
                                         order_by=order_by)
