from py_apple_books.api import BooksApi

if __name__ == '__main__':
    booksApi = BooksApi()

    # Get all books
    books = booksApi.list_books()
    print(f"Found {len(books)} books.")

    # Get all collections
    collections = booksApi.list_collections()
    print(f"Found {len(collections)} collections.")

    # Get a collection by ID
    collection = booksApi.get_collection_by_id(collections[0].id)
    print(f"Found collection with ID {collection.id}.")

    # Get a collection by name
    collection = booksApi.get_collection_by_name(collections[0].title)

    # Get a book by ID
    book = booksApi.get_book_by_id(books[0].id)
    print(f"Found book with ID {book.id}.")

    # Get all finished books
    finished_books = booksApi.get_finished_books()
    print(f"Found {len(finished_books)} finished books.")

    # Get all unfinished books
    unfinished_books = booksApi.get_unfinished_books()
    print(f"Found {len(unfinished_books)} unfinished books.")

    # Get all explicit books
    explicit_books = booksApi.get_explicit_books()
    print(f"Found {len(explicit_books)} explicit books.")

    # Get all locked books
    locked_books = booksApi.get_locked_books()
    print(f"Found {len(locked_books)} locked books.")

    # Get all ephemeral books
    ephemeral_books = booksApi.get_ephemeral_books()
    print(f"Found {len(ephemeral_books)} ephemeral books.")

    # Get all hidden books
    hidden_books = booksApi.get_hidden_books()
    print(f"Found {len(hidden_books)} hidden books.")

    # Get all sample books
    sample_books = booksApi.get_sample_books()
    print(f"Found {len(sample_books)} sample books.")

    # Get all books with a rating of 5
    five_star_books = booksApi.get_books_by_rating(5)
    print(f"Found {len(five_star_books)} five star books.")

    # Get all books in a collection
    collection_books = booksApi.get_books_by_collection_id(8)
    print(f"Found {len(collection_books)} books in collection {collections[0].title}.")