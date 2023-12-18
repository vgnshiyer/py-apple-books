# PyAppleBooks

PyAppleBooks is a Python library designed to streamline interactions with Apple Books data. It offers a straightforward and intuitive interface, empowering developers to effortlessly access, manipulate, and manage Apple Books content through Python-based applications.

## Installation

`pip install py_apple_books`

## Getting Started

Here are some basic examples of how to use PyAppleBooks

```python
from py_apple_books import PyAppleBooks

api = PyAppleBooks()
```

```python
# Get list of all books
books = api.list_books()
for book in books:
    print('-'*50)
    print(f"Title: {book.title}")
    print(f"Author: {book.author}")
```

```python
# Get a list of books along with their highlights 
books_with_highlights = api.list_books(include_annotations=True)
for book in books_with_highlights:
    print('-'*50)
    print(f"Title: {book.title}")
    print(f"Author: {book.author}")
    
    print("Annotations:")
    for annotation in book.highlights:
        print(f"\t{annotation.selected_text}")
        if annotation.note:
            print(f"\tNote: {annotation.note}")
```

```python
# Get a list of collections along with books
collections = api.list_collections(include_books=True)
for collection in collections:
    print('-'*50)
    print(f"Collection: {collection.title}")
    
    print(f"Books in collection: {len(collection.books)}")
    for book in collection.books:
        print(f"\t{book.title}")
```

## Contribution

Thank you for considering contributing to this project! Your help is greatly appreciated.

To contribute to this project, please follow these guidelines:

### Opening Issues
If you encounter a bug, have a feature request, or want to discuss something related to the project, please open an issue on the GitHub repository. When opening an issue, please provide:

**Bug Reports**: Describe the issue in detail. Include steps to reproduce the bug if possible, along with any error messages or screenshots.

**Feature Requests**: Clearly explain the new feature you'd like to see added to the project. Provide context on why this feature would be beneficial.

**General Discussions**: Feel free to start discussions on broader topics related to the project.

### Contributing

1️⃣ Fork the GitHub repository https://github.com/vgnshiyer/py-apple-books \
2️⃣ Create a new branch for your changes (git checkout -b feature/my-new-feature). \
3️⃣ Make your changes and test them thoroughly. \
4️⃣ Push your changes and open a Pull Request to `main`.

*Please provide a clear title and description of your changes.*

## License

PyAppleBooks is licensed under the MIT license. See the LICENSE file for details.