from py_apple_books.data import collection_db, book_db
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection

class BooksApi:

    COLLECTION_FIELDS = [
        'Z_PK',
        'ZTITLE'
    ]

    BOOK_FIELDS = [
        'Z_PK',
        'ZTITLE',
        'ZAUTHOR',
        'ZBOOKDESCRIPTION',
        'ZGENRE',
        'ZPATH'
    ]

    @staticmethod
    def list_collections():
        list_of_collections = []

        collections = [dict(zip(BooksApi.COLLECTION_FIELDS, collection)) for collection in collection_db.get_all_collections(BooksApi.COLLECTION_FIELDS)]

        for c in collections:
            books_in_collection = [dict(zip(BooksApi.BOOK_FIELDS, book)) for book in book_db.get_books_in_collection(c['Z_PK'], BooksApi.BOOK_FIELDS)]

            for b in books_in_collection:
                book = Book(
                    id_=b['Z_PK'],
                    asset_id=b['ZASSETID'],
                    title=b['ZTITLE'],
                    author=b['ZAUTHOR'],
                    description=b['ZDESCRIPTION'],
                    genre=b['ZGENRE'],
                    path=b['ZPATH'],
                )

                c.get('books', []).append(book)

            collection = Collection(
                id_=c['Z_PK'],
                title=c['ZTITLE'],
                books=c.get('books', [])
            )

            list_of_collections.append(collection)

        return list_of_collections