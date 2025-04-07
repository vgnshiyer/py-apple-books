# PyAppleBooks

PyAppleBooks is a Python API library to access your Apple Books Data.

[![PyPI](https://img.shields.io/pypi/v/py_apple_books.svg)](https://pypi.org/project/py-apple-books/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![](https://img.shields.io/badge/Follow-vgnshiyer-0A66C2?logo=linkedin)](https://www.linkedin.com/comm/mynetwork/discovery-see-all?usecase=PEOPLE_FOLLOWS&followMember=vgnshiyer)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Donate-yellow.svg?logo=buymeacoffee)](https://www.buymeacoffee.com/vgnshiyer)

## Installation

`pip install py_apple_books`

## Available Functions

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| list_collections() | List all collections in Apple Books | None | ModelIterable |
| get_collection_by_id(collection_id) | Get a collection by its ID | collection_id: str | Collection |
| get_collection_by_title(title) | Get a collection by its title | title: str | Collection |
| list_books() | List all books in the Apple Books library | None | ModelIterable |
| get_book_by_id(book_id) | Get a book by its ID | book_id: str | Book |
| list_annotations() | List all annotations across all books | None | ModelIterable |
| get_annotation_by_id(annotation_id) | Get an annotation by its ID | annotation_id: str | Annotation |
| get_annotations_by_color(color) | Get annotations by color | color: str | ModelIterable |
| search_annotation_by_highlighted_text(text) | Search for annotations by highlighted text | text: str | ModelIterable |
| search_annotation_by_note(note) | Search for annotations by note | note: str | ModelIterable |
| search_annotation_by_text(text) | Search for annotations by any text that contains the given text | text: str | ModelIterable |

## Examples

#### Creating a client

```python
from py_apple_books import PyAppleBooks

api = PyAppleBooks()
```

#### Get all books from your library

```python
books = api.list_books()
for book in books:
    print('-'*50)
    print(f"Title: {book.title}")
    print(f"Author: {book.author}")
```

```
# Sample output
--------------------------------------------------
Title: The Rational Optimist
Author: Matt Ridley
--------------------------------------------------
Title: Einstein: His Life and Universe
Author: Walter Isaacson
--------------------------------------------------
Title: Steve Jobs
Author: Walter Isaacson
```

#### Get all annotations

```python
annotations = api.list_annotations()
for annotation in annotations:
    print('-' * 50)
    print(f'Selected text: {annotation.selected_text}')
```

```
# sample output
--------------------------------------------------
Selected text: Genomes contain the instructions for building an organism.
--------------------------------------------------
Selected text: Uniqueness is a commodity in oversupply.
```

#### Get annotations by color

```python
annotations = api.get_annotations_by_color('green')
for annotation in annotations:
    print('-' * 50)
    print(f'Selected text: {annotation.selected_text}')
    print(f'Color: {annotation.color}')
```

```
# sample output
--------------------------------------------------
Selected text: Many things are obvious in retrospect, but still take a flash of genius to become plain.
Color: GREEN
--------------------------------------------------
Selected text: An electron does not have a definite position or path until we observe it.
Color: GREEN
```

#### Search for annotations by highlighted text

```python
annotations = api.search_annotation_by_highlighted_text('genomes')
for annotation in annotations:
    print('-' * 50)
    print(f'Selected text: {annotation.selected_text}')
    print(f'Color: {annotation.color}')
```

```
# sample output
--------------------------------------------------
Selected text: The human genome - the complete set of human genes - comes packaged in twenty-three separate pairs of chromosomes
Color: YELLOW
--------------------------------------------------
Selected text: Natural selection is the process by which genes change their sequences
Color: GREEN
```

#### Get all collections and books

```python
collections = api.list_collections()
for collection in collections:
    print('-'*50)
    print(f"Collection: {collection.title}")
    print(f"Number of books: {len(collection.books)}")
    for book in collection.books:
        print(f"  - {book.title}")
```

```
# Sample output
--------------------------------------------------
Collection: Management
Number of books: 1
  - Crucial Conversations
--------------------------------------------------
Collection: Finance
Number of books: 2
  - The Business of the 21st Century Book
  - The Richest Man in Babylon
--------------------------------------------------
Collection: biography
Number of books: 3
  - Elon Musk
  - Steve Jobs
  - Einstein: His Life and Universe
```

## Upcoming Features

- [ ] Adding a book to collection
- [ ] Removing a book from collection
- [ ] Updating annotations

## Contribution

Thank you for considering contributing to this project! Your help is greatly appreciated.

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
