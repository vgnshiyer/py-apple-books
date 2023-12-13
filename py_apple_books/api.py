from py_apple_books.data import collection_db, book_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from typing import Optional


class BooksApi:

    def list_collections(self, include_books: bool = False) -> list[Collection]:
        list_of_collections = []
        collections = [dict(zip(Collection.__annotations__.keys(), c)) for c in collection_db.find_all()]
        for collection in collections:
            self._populate_books(collection, include_books)
            collection_obj = Collection(**collection)
            list_of_collections.append(collection_obj)
        return list_of_collections

    def _populate_books(self, collection: dict, include_books: bool) -> dict:
        collection['books'] = []
        if include_books:
            books_in_collection = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.find_by_collection_id(collection['id_'])]
            for book in books_in_collection:
                book_obj = Book(**book)
                collection.get('books').append(book_obj)
        return collection

    def get_collection_by_id(self, collection_id: str, include_books: bool = False) -> Optional[Collection]:
        collection = dict(zip(Collection.__annotations__.keys(), collection_db.find_by_id(collection_id)))
        collection = self._populate_books(collection, include_books)
        return Collection(**collection)

    def get_collection_by_name(self, collection_name: str, include_books: bool = False) -> Optional[Collection]:
        collection = dict(zip(Collection.__annotations__.keys(), collection_db.find_by_name(collection_name)))
        collection = self._populate_books(collection, include_books)
        return Collection(**collection)

    def list_books(self):
        list_of_books = []
        books = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.find_all()]
        for book in books:
            book_obj = Book(**book)
            list_of_books.append(book_obj)
        return list_of_books

    def get_book_by_id(self, book_id: str):
        book = dict(zip(Book.__annotations__.keys(), book_db.find_by_id(book_id)))
        return Book(**book)
