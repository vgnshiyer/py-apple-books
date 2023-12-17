from py_apple_books.data import collection_db, book_db, annotation_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from py_apple_books.models.highlight import Highlight
from py_apple_books.models.underline import Underline
from py_apple_books.models.annotation import Annotation
from py_apple_books.exceptions import CollectionNotFoundError, BookNotFoundError, AnnotationNotFoundError
from py_apple_books.utils import get_mappings
from typing import Optional


class PyAppleBooks:

    def __init__(self):
        self._book_mappings = get_mappings('Book')
        self._collection_mappings = get_mappings('Collection')
        self._annotation_mappings = get_mappings('Annotation')

        self._style_mappings = {
            1: 'Green',
            2: 'Blue',
            3: 'Yellow',
            4: 'Pink',
            5: 'Purple',
        }

        self._book_map = {}
        self._collection_map = {}

    def _get_or_create_book(self, book_params: dict) -> Book:
        """
        Returns a Book object if it exists in the book map, otherwise creates a new Book object and adds it to the book map.

        Args:
            book_params (dict): dictionary of book parameters
            
        Returns:
            Book: Book object
        """
        id_ = book_params['id']
        if id_ not in self._book_map:
            self._book_map[id_] = Book(**book_params)
        return self._book_map[id_]

    def _get_or_create_collection(self, collection_params: dict) -> Collection:
        """
        Returns a Collection object if it exists in the collection map, otherwise creates a new Collection object and adds it to the collection map.

        Args:
            collection_params (dict): dictionary of collection parameters

        Returns:    
            Collection: Collection object
        """
        id_ = collection_params['id']
        if id_ not in self._collection_map:
            self._collection_map[id_] = Collection(**collection_params)
        return self._collection_map[id_]

    def _create_annotation_object(self, annotation: dict) -> Annotation:
        """
        Returns an Annotation object.

        Args:
            annotation (dict): dictionary of annotation parameters

        Returns:
            Annotation: Annotation object
        """
        is_underline = annotation.pop('is_underline')
        style = annotation.pop('style')
        if is_underline:
            return Underline(**annotation)
        elif style in self._style_mappings:
            annotation['color'] = self._style_mappings[style]
            return Highlight(**annotation)
        else:
            return Annotation(**annotation)

    def _create_book_object(self, raw_book_data: list) -> Book:
        """
        Returns a Book object.

        Args:
            raw_book_data (list): list of book data

        Returns:
            Book: Book object
        """
        book_fields_len = len(self._book_mappings)
        book_dict = dict(zip(self._book_mappings.keys(), raw_book_data[:book_fields_len]))
        book = self._get_or_create_book(book_dict)
        if raw_book_data[book_fields_len:]:
            annotation_dict = dict(zip(self._annotation_mappings.keys(), raw_book_data[book_fields_len:]))
            if annotation_dict['id']:
                book.add_annotation(self._create_annotation_object(annotation_dict))
        return book

    def _create_collection_object(self, raw_collection_data: list) -> Collection:
        """
        Returns a Collection object.

        Args:
            raw_collection_data (list): list of collection data

        Returns:
            Collection: Collection object
        """
        collection_fields_len = len(self._collection_mappings)
        collection_dict = dict(zip(self._collection_mappings.keys(), raw_collection_data[:collection_fields_len]))
        collection = self._get_or_create_collection(collection_dict)
        if raw_collection_data[collection_fields_len:]:
            book_dict = dict(zip(self._book_mappings.keys(), raw_collection_data[collection_fields_len:]))
            if book_dict['id']:
                collection.add_book(Book(**book_dict))
        return collection

    def list_collections(self, include_books: bool = False) -> list[Collection]:
        """
        Returns a list of Collection objects.

        Args:
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            list[Collection]: list of Collection objects
        """
        list_of_collections = set()
        raw_collection_data = collection_db.find_all(include_books)
        if not raw_collection_data: return []
        for collection_data in raw_collection_data:
            list_of_collections.add(self._create_collection_object(collection_data))
        return list(list_of_collections)

    def get_collection_by_id(self, collection_id: str, include_books: bool = False) -> Optional[Collection]:
        """
        Fetches a Collection object by id.

        Args:
            collection_id (str): collection id
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            Collection: Collection object
        """
        raw_collection_data = collection_db.find_by_id(collection_id)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_id=collection_id)
        raw_collection_data = raw_collection_data[0]
        return self._create_collection_object(raw_collection_data)

    def get_collection_by_name(self, collection_name: str, include_books: bool = False) -> Optional[Collection]:
        """
        Fetches a Collection object by name.

        Args:
            collection_name (str): collection name
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            Collection: Collection object
        """
        raw_collection_data = collection_db.find_by_name(collection_name)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_name=collection_name)
        raw_collection_data = raw_collection_data[0]
        return self._create_collection_object(raw_collection_data)

    def list_books(self, include_annotations=False) -> list[Book]:
        """
        Returns a list of Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_all(include_annotations)
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_book_by_id(self, book_id: str, include_annotations=False) -> Optional[Book]:
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

    def get_books_by_author(self, author: str, include_annotations: bool = False) -> Optional[Book]:
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

    def get_books_by_genre(self, genre: str, include_annotations: bool = False) -> Optional[Book]:
        """
        Fetches a list of Book objects by genre.

        Args:
            genre (str): book genre
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_by_genre(genre)
        if not raw_book_data:
            raise BookNotFoundError(genre=genre)
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
        raw_book_data = book_db.find_finished_books()
        if not raw_book_data: return []
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
        raw_book_data = book_db.find_unfinished_books()
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_explicit_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of explicit Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_explicit_books()
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_locked_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of locked Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_locked_books()
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_ephemeral_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of ephemeral Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_ephemeral_books()
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_hidden_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of hidden Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_hidden_books()
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_sample_books(self, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of sample Book objects.

        Args:
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_sample_books()
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_books_by_rating(self, rating: int, include_annotations: bool = False) -> list[Book]:
        """
        Fetches a list of Book objects by rating.

        Args:
            rating (int): book rating
            include_annotations (bool, optional): if True, includes annotations in the book. Defaults to False.

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_by_rating(rating)
        if not raw_book_data: return []
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def get_books_by_collection_id(self, collection_id: str) -> list[Book]:
        """
        Fetches a list of Book objects by collection id.

        Args:
            collection_id (str): collection id

        Returns:
            list[Book]: list of Book objects
        """
        list_of_books = set()
        raw_book_data = book_db.find_by_collection_id(collection_id)
        if not raw_book_data:
            raise BookNotFoundError(collection_id=collection_id)
        for book_data in raw_book_data:
            list_of_books.add(self._create_book_object(book_data))
        return list(list_of_books)

    def list_annotations(self) -> list[Annotation]:
        """
        Fetches a list of Annotation objects.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_all()
        if not raw_annotation_data: return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def list_highlights(self) -> list[Annotation]:
        """
        Fetches a list of Highlight objects.

        Returns:
            list[Highlight]: list of Highlight objects
        """
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_highlights()
        if not raw_annotation_data: return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def list_underlines(self) -> list[Annotation]:
        """
        Fetches a list of Underline objects.

        Returns:
            list[Underline]: list of Underline objects
        """
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_underlines()
        if not raw_annotation_data: return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def get_all_notes(self) -> list[Annotation]:
        """
        Fetches a list of Annotation objects with notes.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_notes()
        if not raw_annotation_data: return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def get_annotation_by_id(self, annotation_id: str) -> Optional[Annotation]:
        """
        Fetches an Annotation object by id.

        Args:
            annotation_id (str): annotation id

        Returns:
            Annotation: Annotation object
        """
        raw_annotation_data = annotation_db.find_by_id(annotation_id)
        if not raw_annotation_data:
            raise AnnotationNotFoundError(annotation_id=annotation_id)
        raw_annotation_data = raw_annotation_data[0]
        annotation = dict(zip(self._annotation_mappings.keys(), raw_annotation_data))
        return self._create_annotation_object(annotation)

    def get_annotations_by_book_id(self, asset_id: str) -> Optional[Annotation]:
        """
        Fetches Annotations by book id.

        Args:
            asset_id (str): book id

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_by_book_id(asset_id)
        if not raw_annotation_data:
            raise AnnotationNotFoundError(asset_id=asset_id)
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        list_of_annotations = []
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_representative_text(self, representative_text: str) -> Optional[Annotation]:
        """
        Fetches a list of Annotation objects by representative text.

        Args:
            representative_text (str): representative text

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_by_representative_text(representative_text)
        if not raw_annotation_data: return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_deleted_annotations(self) -> list[Annotation]:
        """
        Fetches a list of deleted Annotation objects.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_deleted()
        if not raw_annotation_data: return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_not_deleted_annotations(self) -> list[Annotation]:
        """
        Fetches a list of not deleted Annotation objects.

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_not_deleted()
        if not raw_annotation_data: return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_chapter(self, chapter: str) -> list[Annotation]:
        """
        Fetches a list of Annotation objects by chapter.

        Args:
            chapter (str): chapter

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_by_chapter(chapter)
        if not raw_annotation_data: return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotation_by_location(self, location: str) -> list[Annotation]:
        """
        Fetches an Annotation object by location.

        Args:
            location (str): location

        Returns:
            Annotation: Annotation object
        """
        raw_annotation_data = annotation_db.find_by_location(location)
        if not raw_annotation_data: raise AnnotationNotFoundError(location=location)
        raw_annotation_data = raw_annotation_data[0]
        annotation = dict(zip(self._annotation_mappings.keys(), raw_annotation_data))
        return self._create_annotation_object(annotation)

    def get_annotations_by_note(self, note: str) -> list[Annotation]:
        """
        Fetches a list of Annotation objects by note.

        Args:
            note (str): note

        Returns:
            list[Annotation]: list of Annotation objects
        """
        raw_annotation_data = annotation_db.find_by_note_text(note)
        if not raw_annotation_data: return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_selected_text(self, selected_text: str) -> list[Annotation]:
        """
        Fetches a list of Annotation objects by selected text.

        Args:
            selected_text (str): selected text

        Returns:
            list[Annotation]: list of Annotation objects
        """
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_by_selected_text(selected_text)
        if not raw_annotation_data: return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations