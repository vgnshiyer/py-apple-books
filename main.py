from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    books = booksApi.get_finished_books(include_annotations=False)
    print(books)