from py_apple_books.data import collection_db, book_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection

class BooksApi:

    @staticmethod
    def list_collections():
        list_of_collections = []

        collections = [dict(zip(Collection.__annotations__.keys(), c)) for c in collection_db.find_all()]

        for collection in collections:
            books_in_collection = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.find_books_in_collection(collection['id_'])]

            collection['books'] = []
            for book in books_in_collection:
                book_obj = Book(**book)

                collection.get('books').append(book_obj)

            collection_obj = Collection(**collection)

            list_of_collections.append(collection_obj)

        return list_of_collections

    @staticmethod
    def get_collection_by_id(collection_id: str):
        collection = dict(zip(Collection.__annotations__.keys(), collection_db.find_by_id(collection_id)))

        books_in_collection = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.find_books_in_collection(collection['id_'])]

        collection['books'] = []
        for book in books_in_collection:
            book_obj = Book(**book)

            collection.get('books').append(book_obj)

        collection_obj = Collection(**collection)

        return collection_obj

    @staticmethod
    def get_collection_by_name(collection_name: str):
        collection = dict(zip(Collection.__annotations__.keys(), collection_db.find_by_name(collection_name)))

        books_in_collection = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.find_books_in_collection(collection['id_'])]

        collection['books'] = []
        for book in books_in_collection:
            book_obj = Book(**book)

            collection.get('books').append(book_obj)

        collection_obj = Collection(**collection)

        return collection_obj

    @staticmethod
    def list_books():
        list_of_books = []

        books = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.find_all()]

        for book in books:
            book_obj = Book(**book)

            list_of_books.append(book_obj)

        return list_of_books
