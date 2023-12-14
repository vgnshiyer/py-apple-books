from py_apple_books.data import collection_db, book_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from py_apple_books.exceptions import CollectionNotFoundError, BookNotFoundError
from py_apple_books.utils import get_mappings
from typing import Optional


class BooksApi:

    def __init__(self):
        self.book_mappings = get_mappings('Book')
        self.collection_mappings = get_mappings('Collection')

    def list_collections(self, include_books: bool = False) -> list[Collection]:
        raw_collection_data = collection_db.find_all()
        if not raw_collection_data:
            return []
        list_of_collections = []
        collections = [dict(zip(self.collection_mappings.keys(), c)) for c in raw_collection_data]
        print(collections)
        for collection in collections:
            self._populate_books(collection, include_books)
            collection_obj = Collection(**collection)
            list_of_collections.append(collection_obj)
        return list_of_collections

    def _populate_books(self, collection: dict, include_books: bool) -> dict:
        collection['books'] = []
        if include_books:
            raw_books_in_collection = book_db.find_by_collection_id(collection['id'])
            books_in_collection = [dict(zip(self.book_mappings.keys(), b)) for b in raw_books_in_collection]
            for book in books_in_collection:
                book_obj = Book(**book)
                collection.get('books').append(book_obj)
        return collection

    def get_collection_by_id(self, collection_id: str, include_books: bool = False) -> Optional[Collection]:
        raw_collection_data = collection_db.find_by_id(collection_id)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_id=collection_id)
        collection = dict(zip(self.collection_mappings.keys(), raw_collection_data))
        collection = self._populate_books(collection, include_books)
        return Collection(**collection)

    def get_collection_by_name(self, collection_name: str, include_books: bool = False) -> Optional[Collection]:
        raw_collection_data = collection_db.find_by_name(collection_name)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_name=collection_name)
        collection = dict(zip(self.collection_mappings.keys(), raw_collection_data))
        collection = self._populate_books(collection, include_books)
        return Collection(**collection)

    def list_books(self) -> list[Book]:
        raw_book_data = book_db.find_all()
        if not raw_book_data:
            return []
        list_of_books = []
        books = [dict(zip(self.book_mappings.keys(), b)) for b in raw_book_data]
        for book in books:
            book_obj = Book(**book)
            list_of_books.append(book_obj)
        return list_of_books

    def get_book_by_id(self, book_id: str) -> Optional[Book]:
        raw_book_data = book_db.find_by_id(book_id)
        if not raw_book_data:
            raise BookNotFoundError(book_id=book_id)
        book = dict(zip(self.book_mappings.keys(), raw_book_data))
        return Book(**book)

    def get_book_by_asset_id(self, asset_id: str) -> Optional[Book]:
        raw_book_data = book_db.find_by_asset_id(asset_id)
        if not raw_book_data:
            raise BookNotFoundError(asset_id=asset_id)
        book = dict(zip(self.book_mappings.keys(), raw_book_data))
        return Book(**book)

    def get_book_by_title(self, title: str) -> Optional[Book]:
        raw_book_data = book_db.find_by_title(title)
        if not raw_book_data:
            raise BookNotFoundError(title=title)
        book = dict(zip(self.book_mappings.keys(), raw_book_data))
        return Book(**book)

    def get_book_by_author(self, author: str) -> Optional[Book]:
        raw_book_data = book_db.find_by_author(author)
        if not raw_book_data:
            raise BookNotFoundError(author=author)
        book = dict(zip(self.book_mappings.keys(), raw_book_data))
        return Book(**book)

    def get_book_by_genre(self, genre: str) -> Optional[Book]:
        raw_book_data = book_db.find_by_genre(genre)
        if not raw_book_data:
            raise BookNotFoundError(genre=genre)
        book = dict(zip(self.book_mappings.keys(), raw_book_data))
        return Book(**book)

    def get_books_by_collection_id(self, collection_id: str) -> list[Book]:
        raw_book_data = book_db.find_by_collection_id(collection_id)
        if not raw_book_data:
            raise BookNotFoundError(collection_id=collection_id)
        list_of_books = []
        books = [dict(zip(self.book_mappings.keys(), b)) for b in raw_book_data]
        for book in books:
            book_obj = Book(**book)
            list_of_books.append(book_obj)
        return list_of_books