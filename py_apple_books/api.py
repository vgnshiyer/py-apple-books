from py_apple_books.data import collection_db, book_db, annotation_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from py_apple_books.models.highlight import Highlight
from py_apple_books.models.underline import Underline
from py_apple_books.models.annotation import Annotation
from py_apple_books.exceptions import CollectionNotFoundError, BookNotFoundError
from py_apple_books.utils import get_mappings
from typing import Optional


class BooksApi:

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

    def _create_annotation_object(self, annotation: dict) -> Annotation:
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
        book_fields_len = len(self._book_mappings)
        book_dict = dict(zip(self._book_mappings.keys(), raw_book_data[:book_fields_len]))
        book = Book(**book_dict)
        if raw_book_data[book_fields_len:]:
            annotation_dict = dict(zip(self._annotation_mappings.keys(), raw_book_data[book_fields_len:]))
            if annotation_dict['id']:
                book.add_annotation(self._create_annotation_object(annotation_dict))
        return book

    def _populate_books(self, collection: dict, include_books: bool) -> dict:
        collection['books'] = []
        if include_books:
            raw_books_in_collection = book_db.find_by_collection_id(collection['id'])
            books_in_collection = [dict(zip(self._book_mappings.keys(), b)) for b in raw_books_in_collection]
            for book in books_in_collection:
                book_obj = Book(**book)
                collection.get('books').append(book_obj)

    def list_collections(self, include_books: bool = False) -> list[Collection]:
        if include_books == False:
            raw_collection_data = collection_db.find_all()
            if not raw_collection_data:
                return []
            list_of_collections = []
            collections = [dict(zip(self._collection_mappings.keys(), c)) for c in raw_collection_data]
            for collection in collections:
                collection_obj = Collection(**collection)
                list_of_collections.append(collection_obj)
            
        return list_of_collections

    def get_collection_by_id(self, collection_id: str, include_books: bool = False) -> Optional[Collection]:
        raw_collection_data = collection_db.find_by_id(collection_id)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_id=collection_id)
        raw_collection_data = raw_collection_data[0]
        collection = dict(zip(self._collection_mappings.keys(), raw_collection_data))
        self._populate_books(collection, include_books)
        return Collection(**collection)

    def get_collection_by_name(self, collection_name: str, include_books: bool = False) -> Optional[Collection]:
        raw_collection_data = collection_db.find_by_name(collection_name)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_name=collection_name)
        raw_collection_data = raw_collection_data[0]
        collection = dict(zip(self._collection_mappings.keys(), raw_collection_data))
        self._populate_books(collection, include_books)
        return Collection(**collection)

    def list_books(self, include_annotations=False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_all(include_annotations)
        if not raw_book_data: return []
        book_fields_len = len(self._book_mappings)
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_book_by_id(self, book_id: str, include_annotations=False) -> Optional[Book]:
        raw_book_data = book_db.find_by_id(book_id, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(book_id=book_id)
        raw_book_data = raw_book_data[0]
        return self._create_book_object(raw_book_data)

    def get_book_by_asset_id(self, asset_id: str, include_annotations: bool = False) -> Optional[Book]:
        raw_book_data = book_db.find_by_asset_id(asset_id, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(asset_id=asset_id)
        raw_book_data = raw_book_data[0]
        return self._create_book_object(raw_book_data)

    def get_book_by_title(self, title: str, include_annotations: bool = False) -> Optional[Book]:
        raw_book_data = book_db.find_by_title(title, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(title=title)
        raw_book_data = raw_book_data[0]
        return self._create_book_object(raw_book_data)

    def get_books_by_author(self, author: str, include_annotations: bool = False) -> Optional[Book]:
        list_of_books = []
        raw_book_data = book_db.find_by_author(author, include_annotations)
        if not raw_book_data:
            raise BookNotFoundError(author=author)
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_books_by_genre(self, genre: str, include_annotations: bool = False) -> Optional[Book]:
        list_of_books = []
        raw_book_data = book_db.find_by_genre(genre)
        if not raw_book_data:
            raise BookNotFoundError(genre=genre)
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_finished_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_finished_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_unfinished_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_unfinished_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_explicit_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_explicit_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_locked_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_locked_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_ephemeral_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_ephemeral_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_hidden_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_hidden_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_sample_books(self, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_sample_books()
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_books_by_rating(self, rating: int, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_by_rating(rating)
        if not raw_book_data:
            return []
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def get_books_by_collection_id(self, collection_id: str, include_annotations: bool = False) -> list[Book]:
        list_of_books = []
        raw_book_data = book_db.find_by_collection_id(collection_id)
        if not raw_book_data:
            raise BookNotFoundError(collection_id=collection_id)
        for book_data in raw_book_data:
            list_of_books.append(self._create_book_object(book_data))
        return list_of_books

    def list_annotations(self) -> list[Annotation]:
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_all()
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def list_highlights(self) -> list[Annotation]:
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_highlights()
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def list_underlines(self) -> list[Annotation]:
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_underlines()
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def get_annotations_with_notes(self) -> list[Annotation]:
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_notes()
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations

    def get_annotations_by_id(self, annotation_id: str) -> Optional[Annotation]:
        raw_annotation_data = annotation_db.find_by_id(annotation_id)
        if not raw_annotation_data:
            return None
        raw_annotation_data = raw_annotation_data[0]
        annotation = dict(zip(self._annotation_mappings.keys(), raw_annotation_data))
        return self._create_annotation_object(annotation)

    def get_annotations_by_book_id(self, asset_id: str) -> Optional[Annotation]:
        raw_annotation_data = annotation_db.find_by_book_id(asset_id)
        if not raw_annotation_data:
            return None
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        list_of_annotations = []
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_representative_text(self, representative_text: str) -> Optional[Annotation]:
        raw_annotation_data = annotation_db.find_by_representative_text(representative_text)
        if not raw_annotation_data:
            return None
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_deleted_annotations(self) -> list[Annotation]:
        raw_annotation_data = annotation_db.find_deleted()
        if not raw_annotation_data:
            return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_not_deleted_annotations(self) -> list[Annotation]:
        raw_annotation_data = annotation_db.find_not_deleted()
        if not raw_annotation_data:
            return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_chapter(self, chapter: str) -> list[Annotation]:
        raw_annotation_data = annotation_db.find_by_chapter(chapter)
        if not raw_annotation_data:
            return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_location(self, location: str) -> list[Annotation]:
        raw_annotation_data = annotation_db.find_by_location(location)
        if not raw_annotation_data:
            return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_note(self, note: str) -> list[Annotation]:
        raw_annotation_data = annotation_db.find_by_note_text(note)
        if not raw_annotation_data:
            return []
        list_of_annotations = []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            annotation_obj = self._create_annotation_object(annotation)
            list_of_annotations.append(annotation_obj)
        return list_of_annotations

    def get_annotations_by_selected_text(self, selected_text: str) -> list[Annotation]:
        list_of_annotations = []
        raw_annotation_data = annotation_db.find_by_selected_text(selected_text)
        if not raw_annotation_data:
            return []
        annotations = [dict(zip(self._annotation_mappings.keys(), a)) for a in raw_annotation_data]
        for annotation in annotations:
            list_of_annotations.append(self._create_annotation_object(annotation))
        return list_of_annotations