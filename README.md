# PyAppleBooks

PyAppleBooks is a Python API library to access your Apple Books data.

[![PyPI](https://img.shields.io/pypi/v/py_apple_books.svg)](https://pypi.org/project/py-apple-books/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![](https://img.shields.io/badge/Follow-vgnshiyer-0A66C2?logo=linkedin)](https://www.linkedin.com/comm/mynetwork/discovery-see-all?usecase=PEOPLE_FOLLOWS&followMember=vgnshiyer)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Donate-yellow.svg?logo=buymeacoffee)](https://www.buymeacoffee.com/vgnshiyer)

## Installation

```bash
pip install py_apple_books
```

## Available Functions

### Collections

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `list_collections()` | List all collections | `limit?`, `order_by?` | ModelIterable |
| `get_collection_by_id(collection_id)` | Get a collection by its ID | `collection_id: str` | Collection |
| `get_collection_by_title(title)` | Search collections by title substring | `title: str` | ModelIterable |

### Books

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `list_books()` | List all books | `limit?`, `order_by?` | ModelIterable |
| `get_book_by_id(book_id)` | Get a book by its ID | `book_id: str` | Book |
| `get_book_by_title(title)` | Search books by title substring | `title: str` | ModelIterable |
| `get_books_by_genre(genre)` | Search books by genre substring | `genre: str`, `limit?`, `order_by?` | ModelIterable |

### Reading Progress

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_books_in_progress()` | Books currently being read (0% < progress < 100%) | `limit?`, `order_by?` | ModelIterable |
| `get_finished_books()` | Books marked as finished | `limit?`, `order_by?` | ModelIterable |
| `get_unstarted_books()` | Books not yet started | `limit?`, `order_by?` | ModelIterable |
| `get_recently_read_books()` | Books ordered by last-opened date, most recent first | `limit?` (default 10) | ModelIterable |

### Annotations

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `list_annotations()` | List all annotations | `limit?`, `order_by?` | ModelIterable |
| `get_annotation_by_id(annotation_id)` | Get an annotation by its ID | `annotation_id: str` | Annotation |
| `get_annotations_by_color(color)` | Filter annotations by highlight color | `color: str`, `limit?`, `order_by?` | ModelIterable |
| `search_annotation_by_highlighted_text(text)` | Substring search in highlighted text | `text: str`, `limit?`, `order_by?` | ModelIterable |
| `search_annotation_by_note(note)` | Substring search in user notes | `note: str`, `limit?`, `order_by?` | ModelIterable |
| `search_annotation_by_text(text)` | Substring search across highlighted text, representative text, and notes | `text: str`, `limit?`, `order_by?` | ModelIterable |
| `get_annotations_by_date_range(after?, before?)` | Filter annotations by creation date | `after?: datetime`, `before?: datetime`, `limit?`, `order_by?` | ModelIterable |

### Book Content (v1.7.0+)

Read the full text of your non-DRM EPUBs. Powered by [ebooklib](https://pypi.org/project/EbookLib/) and [beautifulsoup4](https://pypi.org/project/beautifulsoup4/).

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_book_content(book_id)` | Return a `BookContent` handle after verifying the book is downloaded and not DRM-protected | `book_id: int` | BookContent |
| `get_current_reading_location(book_id)` | Apple Books' auto-tracked "current reading position" bookmark (a zero-width annotation with a CFI) | `book_id: int` | Optional[Annotation] |
| `get_current_reading_chapter(book_id)` | Convenience: resolve the bookmark's CFI to a `Chapter` | `book_id: int` | Optional[Chapter] |

`BookContent` methods:

| Method | Description | Return Type |
|--------|-------------|-------------|
| `list_chapters()` | Flattened table of contents with title, href, fragment, order, depth | `list[Chapter]` |
| `get_chapter_content(chapter_id)` | Plain text of a chapter, scoped to its fragment anchor | `str` |
| `chapter_at_cfi(cfi)` | Resolve an EPUB CFI to the containing `Chapter` | `Optional[Chapter]` |

`BookContent` properties:

| Property | Description |
|----------|-------------|
| `is_epub` | True if the path is an EPUB bundle directory |
| `is_pdf` | True if the path is a single PDF file |
| `is_downloaded` | True if locally materialized (not an iCloud placeholder) — does not trigger hydration |
| `is_drm_protected` | True if the EPUB has `META-INF/encryption.xml` (Apple Books Store FairPlay-protected book) |

### Exceptions

Content-access methods may raise:

| Exception | When |
|-----------|------|
| `BookNotDownloadedError` | The book has no local file (never downloaded) **or** the file is an iCloud placeholder |
| `DRMProtectedError` | The book is FairPlay-protected (Apple Books Store purchase) |
| `AppleBooksError` | Base class for all exceptions above, plus EPUB-parsing errors |

## Examples

### Creating a client

```python
from py_apple_books import PyAppleBooks

api = PyAppleBooks()
```

### List books

```python
for book in api.list_books():
    print(f"{book.title} — {book.author}")
```

### Get annotations

```python
for a in api.list_annotations(limit=5):
    print(f"[{a.color}] {a.selected_text}")
```

### Search highlights

```python
for a in api.search_annotation_by_highlighted_text('genome'):
    print(a.selected_text)
```

### Filter by color

```python
for a in api.get_annotations_by_color('yellow'):
    print(a.selected_text)
```

### Reading progress

```python
for book in api.get_books_in_progress(limit=5):
    print(f"{book.title}: {book.reading_progress:.1f}%")
```

### Read chapter content

```python
from py_apple_books.exceptions import BookNotDownloadedError, DRMProtectedError

book = api.get_book_by_id(42)
try:
    content = api.get_book_content(book.id)
except BookNotDownloadedError:
    print("Open the book in Apple Books first to download it.")
except DRMProtectedError:
    print("DRM-protected Store purchase — text content isn't readable.")
else:
    for ch in content.list_chapters():
        print(f"{'  ' * ch.depth}[{ch.order}] {ch.title}")

    # Read the first substantive chapter
    text = content.get_chapter_content(content.list_chapters()[0].id)
    print(text[:500])
```

### See what the user is currently reading

```python
# The book they most recently opened
for book in api.get_recently_read_books(limit=1):
    ch = api.get_current_reading_chapter(book.id)
    if ch is None:
        continue
    content = api.get_book_content(book.id)
    text = content.get_chapter_content(ch.id)
    print(f"Currently reading: {book.title}")
    print(f"Progress: {book.reading_progress:.1f}%")
    print(f"Chapter {ch.order}: {ch.title}")
    print(text[:500])
```

### Get all collections and books

```python
for collection in api.list_collections():
    print(f"{collection.title}: {len(collection.books)} books")
    for book in collection.books:
        print(f"  - {book.title}")
```

## How content access handles Apple Books' quirks

- **iCloud placeholders** — books you've imported but haven't opened lately can live only in iCloud. `is_downloaded` detects this via `os.stat` (for files) or `du -sk` (for bundle directories) without triggering a download. `get_book_content` raises `BookNotDownloadedError` so you can prompt the user to open the book in Apple Books.
- **DRM'd Store purchases** — FairPlay-encrypted EPUBs have `META-INF/encryption.xml` and their chapter bodies are opaque ciphertext. Detected up-front; callers get a clear `DRMProtectedError`.
- **Non-standard EPUB layouts** — OPFs at unusual paths, NCX files not declared in `<spine toc=…>` (e.g. _The 4-Hour Workweek_), EPUB3 books with nav docs only (e.g. _On Numbers and Games_), URL-encoded href characters (`%21` → `!`), duplicate navPoint ids — all handled, with a stdlib NCX override for the ebooklib blind spots.
- **Fragment-scoped chapters** — Project Gutenberg EPUBs often put multiple sections in one XHTML file, separated by anchors. `get_chapter_content` returns only the requested section, not the whole file.

## Running the tests

```bash
pip install -e '.[dev]'
pytest
```

The test suite covers placeholder/DRM detection, NCX parsing, XHTML text extraction, fragment scoping, and end-to-end BookContent behavior against generated EPUB fixtures. No real Apple Books library required.

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
2️⃣ Create a new branch for your changes (`git checkout -b feature/my-new-feature`). \
3️⃣ Make your changes and test them thoroughly. \
4️⃣ Push your changes and open a Pull Request to `main`.

*Please provide a clear title and description of your changes.*

## License

PyAppleBooks is licensed under the MIT license. See the LICENSE file for details.
