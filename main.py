from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    collections = booksApi.list_collections(True)
    for col in collections:
        print('-'*50)
        print(col.title)
        for book in col.books:
            print(book.title)
        print()