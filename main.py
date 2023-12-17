from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    annos = booksApi.list_annotations()
    print(annos)