from py_apple_books.api import BooksApi

if __name__ == '__main__':
    collections = BooksApi.list_collections()

    for c in collections:
        print(c)