from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    collections = booksApi.list_collections()

    for c in collections:
        print(c)

    books = booksApi.list_books()

    for b in books:
        print(b)