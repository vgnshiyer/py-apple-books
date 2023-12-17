from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    annos = booksApi.get_annotations_with_notes()
    for anno in annos:
        print(anno.note)