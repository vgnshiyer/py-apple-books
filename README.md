# PyAppleBooks

PyAppleBooks is a Python library designed to streamline interactions with Apple Books data. It offers a straightforward and intuitive interface, empowering developers to effortlessly access, manipulate, and manage Apple Books content through Python-based applications.

[![PyPI](https://img.shields.io/pypi/v/py_apple_books.svg)](https://pypi.org/project/py-apple-books/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Installation

`pip install py_apple_books`

## Getting Started - Examples

Here are some basic examples of how to use PyAppleBooks

#### Using PyAppleBooks

```python
from py_apple_books import PyAppleBooks

api = PyAppleBooks()
```

#### Get list of all books

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
Title: Think & Grow Rich
Author: Napoleon Hill
--------------------------------------------------
Title: Autobiography of a Yogi (Complete Edition)
Author: Paramahansa Yogananda
--------------------------------------------------
Title: Sapiens
Author: Yuval Noah Harari
```

#### Get a list of books along with their highlights 

```python
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

```
# Sample output
--------------------------------------------------
Title: Elon Musk
Author: Walter Isaacson
Annotations:
        Adversity shaped me
        The people who are crazy enough to think they can change the world are the ones who do.

        They’re always trying to save the world, with their underpants on the outside or these skin-tight iron suits, which is really pretty strange when you think about it,
        Reading remained Musk’s psychological retreat. Sometimes he would immerse himself in books all afternoon and most of the night, nine hours at a stretch. When the family went to someone’s house, he would disappear into their host’s library. When they went into town, he would wander off and later be found at a bookstore, sitting on the floor, in his own world.
        Elon developed into a night person, staying up until dawn reading books
        One unfortunate trend in the 1980s was that cars and computers became tightly sealed appliances. It was possible to open up and fiddle with the innards of the Apple II that Steve Wozniak designed in the late 1970s, but you couldn’t do that with the Macintosh, which Steve Jobs in 1984 made almost impossible to open.
```

#### Get a list of collections along with books

```python
collections = api.list_collections(include_books=True)
for collection in collections:
    print('-'*50)
    print(f"Collection: {collection.title}")
    
    print(f"Books in collection: {len(collection.books)}")
    for book in collection.books:
        print(f"\t{book.title}")
```

```
# Sample output
--------------------------------------------------
Collection: Management
Books in collection: 1
        Crucial Conversations
--------------------------------------------------
Collection: Finance
Books in collection: 2
        TheBusinessofthe21stCenturyBook-FirstFiveChapters
        The Richest Man in Babylon
--------------------------------------------------
Collection: biography
Books in collection: 1
        Elon Musk
```

#### Get list of all books by author

```python
books = api.get_books_by_author('Robert C. Martin')
for book in books:
    print('-'*50)
    print(f"Title: {book.title}")
    print(f"Author: {book.author}")
```

```
--------------------------------------------------
Title: Clean Code
Author: Robert C. Martin
--------------------------------------------------
Title: The Clean Coder
Author: Robert C. Martin
```

#### Get all notes

```python
notes = api.get_all_notes()
for note in notes:
    print('-' * 50)
    print(f'Selected text: {note.selected_text}')
    print(f'Note text: {note.note}')
```

```
# sample output
--------------------------------------------------
Selected text: profusion
Note text: Plethora
--------------------------------------------------
Selected text: As multi-leader replication is a somewhat retrofitted feature in many databases, there are often
subtle configuration pitfalls and surprising interactions with other database features. For example,
autoincrementing keys, triggers, and integrity constraints can be problematic. For this reason,
multi-leader replication is often considered dangerous territory that should be avoided if possible
Note text: Retrofitted = add on
--------------------------------------------------
Selected text: By subtracting a follower’s current position from the leader’s
current position, you can measure the amount of replication lag.
Note text: Calculate lag for leader based replication.
```

#### Find all green highlights

```python
notes = api.get_annotation_by_color('green')
for note in notes:
    print('-'*50)
    print(f'Selected text: {note.selected_text}')
    print(f'Color: {note.color}')
```

```
# sample output
--------------------------------------------------
Selected text: The purpose of life is a life of purpose
Color: green
--------------------------------------------------
Selected text: No man is free who is not a master of himself.'
Color: green
--------------------------------------------------
Selected text: The only limits on your life are those that you set yourself.
Color: green
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