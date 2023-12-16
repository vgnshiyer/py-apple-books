from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    book = booksApi.get_book_by_id('38', True)