from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    annotations = booksApi.list_annotations()
    for annotation in annotations:
        print(annotation.note)