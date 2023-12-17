from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    books = booksApi.list_books(True)
    for h in books[15].highlights:
        print(h)