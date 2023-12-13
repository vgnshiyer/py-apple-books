from py_apple_books.data import collection_db, book_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection

class BooksApi:

    @staticmethod
    def list_collections():
        list_of_collections = []

        collections = [dict(zip(Collection.__annotations__.keys(), c)) for c in collection_db.get_all_collections()]

        for c in collections:
            books_in_collection = [dict(zip(Book.__annotations__.keys(), b)) for b in book_db.get_books_in_collection(c['id_'])]

            c['books'] = []
            for b in books_in_collection:
                book = Book(**b)

                c.get('books').append(book)

            collection = Collection(**c)

            list_of_collections.append(collection)

        return list_of_collections