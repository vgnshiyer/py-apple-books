from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    notes = booksApi.get_annotations_by_note("Asd")
    print(notes)