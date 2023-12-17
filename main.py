from py_apple_books.api import PyAppleBooks

if __name__ == '__main__':
    booksApi = PyAppleBooks()

    annos = booksApi.get_annotations_with_notes()
    for anno in annos:
        print(anno.note)