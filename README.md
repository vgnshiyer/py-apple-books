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

Requires Python 3.10+ on macOS.

Upgrading from 1.10 or 1.9? See the [changelog](https://github.com/vgnshiyer/py-apple-books/blob/main/CHANGELOG.md), and the
[note for apple-books-mcp users](#note-for-apple-books-mcp-082090-users).

## Quick start

```python
from py_apple_books import PyAppleBooks

api = PyAppleBooks()

for book in api.list_books(limit=5):
    print(f"{book.title} — {book.author or 'Unknown Author'}")
```

## Configuration

### Which library is read

`PyAppleBooks()` reads the current user's Apple Books library. Pass a
location to read another one, such as a copy of the Books `Documents` folder
from another Mac:

```python
from py_apple_books import PyAppleBooks

# A Documents folder holding BKLibrary/ and AEAnnotation/
other = PyAppleBooks(data_dir="~/Desktop/Books copy")
print(other.store_info().library_path.name)
other.close()
```

`PyAppleBooks(data_dir=None, *, library_db=None, annotation_db=None, query_timeout=...)`:

- `data_dir`: a Books `Documents` folder; both stores are looked up in it.
- `library_db` and `annotation_db`: the store files themselves.
- `query_timeout`: see [Query timeout](#query-timeout).

An instance built with any of these has a library of its own. Its results,
and their relations, keep reading that library after the call returns;
`close()` it when done.

Which stores are read, in order of precedence:

1. **Constructor arguments always win.** With `data_dir`, `library_db` or
   `annotation_db`, the location variables below are ignored. A store not
   given as a file is found in `data_dir`, else in the Apple Books
   container.
2. **Environment variables apply to the default library only**:
   `PyAppleBooks()` (or `PyAppleBooks(query_timeout=...)`) and the
   `write_safety` defaults.
   - Library store: `APPLE_BOOKS_LIBRARY_DB`, else found in
     `APPLE_BOOKS_DATA_DIR`, else in the default folder.
   - Annotation store: `APPLE_BOOKS_ANNOTATION_DB`, else found in
     `APPLE_BOOKS_DATA_DIR`, else in the default folder.
3. The default folder is
   `~/Library/Containers/com.apple.iBooksX/Data/Documents`.

In a folder, the stores are found by content, not by name. Apple's canonical
file is used when its Core Data metadata shows it is an Apple Books store,
and stray copies (`… copy.sqlite`) are ignored. A store file replaced while
your program runs is picked up by the next call. Without an annotation store
(Books creates it when a book is first opened), books and collections are
still readable. Collection writes go to the library store the instance reads
(see [Writes](#writes)).

| Variable | Meaning |
|----------|---------|
| `APPLE_BOOKS_DATA_DIR` | Books `Documents` folder for the default library |
| `APPLE_BOOKS_LIBRARY_DB` | Library store file (`BKLibrary…sqlite`) for the default library |
| `APPLE_BOOKS_ANNOTATION_DB` | Annotation store file (`AEAnnotation…sqlite`) for the default library |
| `APPLE_BOOKS_QUERY_TIMEOUT` | Seconds a query may run, for libraries built without `query_timeout`; `0`, `none` or `off` for no limit (default 30) |
| `APPLE_BOOKS_MODEL_CHECK` | `warn` (default), `enforce` or `off`: see [Writes](#writes) |

The variables are read on first use, never at import. The default library
keeps the stores and the timeout it found then for the rest of the process
(it finds the stores again only when a store file is replaced), so set the
variables before the first query. A `PyAppleBooks(query_timeout=...)`
built later reads them again.

### Query timeout

Every SQL statement has a deadline, 30 s by default. A statement still
running then is stopped with `QueryTimeoutError`, and so is a wait for a
pooled connection. (A wait for another program's lock on the store gives up
after 5 s, as in 1.9, or at the deadline if that is sooner.) Set
`query_timeout=` on the instance (`None` or `0` for no limit), or
`APPLE_BOOKS_QUERY_TIMEOUT`.
`query_deadline(seconds)` shortens the limit for a block, including work the
block hands to other threads through `anyio.to_thread` or
`contextvars.copy_context()`:

```python
batch = PyAppleBooks(query_timeout=None)   # no limit for this instance
everything = list(batch.list_annotations())
batch.close()

with api.query_deadline(2):                # stop any query in the block after 2 s
    titles = [book.title for book in api.list_books()]
```

### Imports, threads and closing

- Importing `py_apple_books` does no filesystem access. The library is found
  on first use, so a missing library or a macOS privacy denial surfaces as
  a typed error from that call (`LibraryNotFoundError`,
  `LibraryAccessDeniedError`), not at import.
- A `PyAppleBooks` and its results can be used from any thread, including
  `anyio.to_thread` workers. Each library keeps a pool of read-only
  connections (at most 8 open at once), and a connection is used by one
  thread at a time.
- `close()` closes the idle connections; the next call reconnects. After a
  `fork()`, the child makes its own connections. Fork only while no other
  thread is running a query.
- Text that isn't valid UTF-8 in the stores reads with U+FFFD in place of
  the invalid bytes (a warning is logged once per library) instead of
  failing the query. The connections `LibraryDB.connection()` and
  `LibraryDB.open_connection()` hand out decode text as `sqlite3` does by
  default.
- What the library derives from book files (chapter lists, spines, book
  metadata) is cached in memory, process-wide and bounded; the ranked
  search index is kept per library and freed by `close()`. See
  [Caches and memory](#how-content-access-handles-apple-books-quirks).

## Available Functions

Parameters added to existing methods since 1.9 are keyword-only. Methods
added in 1.11 take a book as `book_id`: an id or a `Book`.

### Collections

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `list_collections()` | List all collections (deleted ones excluded) | `limit?`, `order_by?`, `offset?` | ModelIterable |
| `get_collection_by_id(collection_id)` | Get a collection by its ID | `collection_id: str` | Collection |
| `get_collection_by_title(title)` | Search collections by title ([folded](#searching) substring) | `title: str`, `limit?`, `order_by?`, `offset?` | ModelIterable |

### Collection writes (v1.9.0+)

Apple exposes no automation API for collections, so these write directly to
the library's SQLite store, behind guard rails (see [Writes](#writes)). Only
user-created collections can be renamed or deleted; membership edits also
work on "Want to Read".

⚠️ With iCloud "Collections, bookmarks and highlights" sync enabled, direct
writes may not propagate to other devices and can be reverted by a cloud
re-sync. Verify a small edit cross-device before relying on it.

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `create_collection(title, details?)` | Create a user collection | `title: str`, `details?: str` | Collection |
| `rename_collection(collection_id, new_title)` | Rename a user collection | `collection_id`, `new_title: str` | Collection |
| `delete_collection(collection_id)` | Soft-delete a user collection (books untouched) | `collection_id` | None |
| `add_book_to_collection(collection_id, book_id)` | Add a book (idempotent) | `collection_id`, `book_id` | bool |
| `remove_book_from_collection(collection_id, book_id)` | Remove a book (idempotent) | `collection_id`, `book_id` | bool |

### Books

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `list_books()` | List the books in your library | `limit?`, `order_by?`, `offset?`, `include_store_series?` | ModelIterable |
| `get_book_by_id(book_id)` | Get a book by its ID (any row, Store series entries included) | `book_id: str` | Book |
| `get_book_by_title(title)` | Search books by title ([folded](#searching) substring) | `title: str`, `limit?`, `order_by?`, `offset?`, `include_store_series?` | ModelIterable |
| `get_books_by_genre(genre)` | Search books by genre ([folded](#searching) substring) | `genre: str`, `limit?`, `order_by?`, `offset?`, `include_store_series?` | ModelIterable |
| `search_books(query)` (v1.11.0+) | Books whose title or author contains every word of the query ([folded](#searching)) | `query: str`, `limit?`, `order_by?`, `offset?`, `include_store_series?` | ModelIterable |
| `get_books_by_subject(subject)` (v1.11.0+) | Books whose genre or any subject in the book file contains `subject` (a superset of `get_books_by_genre`); never downloads an iCloud book | `subject: str`, `limit?`, `order_by?`, `offset?`, `include_store_series?`, `read_files?` (default True) | ModelIterable |
| `get_book_metadata(book_id)` (v1.11.0+) | [Metadata](#metadata-and-series-v1110): language, publisher, publication date, ISBN, subjects, description, cover path and series, from the library, completed from the book's package document | `book_id`, `read_files?` (default True) | BookMetadata |
| `get_series(book_id)` (v1.11.0+) | The Store series a book belongs to, with every known volume (provisional) | `book_id` | Optional[Series] |
| `list_series()` (v1.11.0+) | Every Store series the library records, by title (provisional) | `started_only?`, `limit?`, `offset?` | List[Series] |

### Reading Progress

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_books_in_progress()` | Books not marked finished, with progress above 0% | `limit?`, `order_by?`, `offset?` | ModelIterable |
| `get_finished_books()` | Books marked as finished, whatever their progress; `finished_after`/`finished_before` (v1.11.0+) keep a window of finish dates | `limit?`, `order_by?`, `offset?`, `finished_after?`, `finished_before?` | ModelIterable |
| `get_unstarted_books()` | Books not marked finished, at 0% or with no progress | `limit?`, `order_by?`, `offset?` | ModelIterable |
| `get_recently_read_books()` | Opened books by `Book.last_read_date`, most recent first | `limit?` (default 10), `order_by?`, `offset?` | ModelIterable |

### Annotations

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `list_annotations()` | List your highlights, notes and bookmarks | `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |
| `get_annotation_by_id(annotation_id)` | Get an annotation by its ID (any row, deleted ones included) | `annotation_id: str` | Annotation |
| `get_annotations_by_color(color)` | Highlights of one color, newest first | `color: str`, `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |
| `search_annotation_by_highlighted_text(text)` | [Folded](#searching) search in highlighted text, newest first | `text: str`, `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |
| `search_annotation_by_note(note)` | [Folded](#searching) search in your notes, newest first | `note: str`, `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |
| `search_annotation_by_text(text)` | [Folded](#searching) search across highlighted text, surrounding text and notes, newest first | `text: str`, `limit?`, `order_by?`, `offset?`, `include_deleted?` | list |
| `search_annotations(query)` (v1.11.0+) | Highlights and notes [ranked](#ranked-search-v1110) by relevance, best first | `query: str`, `limit?` (default 20), `offset?`, `book_id?`, `require_all?`, `include_deleted?` | List[AnnotationHit] |
| `get_annotations_by_date_range(after?, before?)` | Filter annotations by creation date; a `date` (v1.11.0+) covers that whole local day | `after?: datetime \| date`, `before?: datetime \| date`, `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |

### Reading position and context (v1.11.0+)

See [Where am I in a book](#where-am-i-in-a-book-v1110) and
[Spoiler-safe reading](#spoiler-safe-reading-v1110).

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_reading_position(book_id)` | Where you are in a book: the reading-position bookmark, else inferred from the newest located annotation, with its chapter, fraction and (PDFs) page | `book_id`, `resolve_chapter?` (default True), `infer?` (default True) | Optional[ReadingPosition] |
| `get_annotation_locations(annotations)` | The spine file and chapter of many annotations at once; each book read once | `annotations` | `dict[id, ResolvedLocation]` |
| `get_annotation_context(annotation_id)` | A highlight with the text before and after it, or an error that says why there is none | `annotation_id` (or an `Annotation`), `chars_before?` (300), `chars_after?` (300) | AnnotationContext |
| `get_read_boundary(book_id)` | How far a book has been read, from the library database alone | `book_id`, `basis?` (`'position'` or `'furthest'`) | ReadBoundary |

### Engagement methods (v1.11.0+)

See [Engagement](#engagement-v1110). Every method here reads the Apple
Books databases only, except `get_reading_goals`, which reads Books'
preferences file; no book file is opened.

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_underlines()` | Your underlines, newest first | `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |
| `sample_highlights(limit=5)` | A varied, repeatable sample of your highlights for a day | `limit?` (default 5), `offset?`, `on?`, `seed?`, `book_id?`, `after?`, `before?`, `exclude_ids?`, `exclude_uuids?`, `exclude_short?` (default True), `include_orphans?` | ModelIterable |
| `get_highlights_on_this_day(on?)` | Highlights and notes made on this calendar day in earlier years, newest first | `on?`, `limit?`, `order_by?`, `offset?`, `include_deleted?`, `include_orphans?` (default True) | ModelIterable |
| `get_vocabulary()` | The words and short phrases you highlighted, grouped across books, most recently highlighted first | `limit?`, `order_by?`, `offset?`, `book_id?`, `after?`, `before?`, `underline_only?` | List[VocabularyEntry] |
| `get_highlight_activity()` | How much you highlighted in a window, by day, ISO week, month or year | `after?`, `before?`, `book_id?`, `granularity?` (default `'month'`) | HighlightActivity |
| `get_highlight_streaks()` | Runs of consecutive days with a highlight | `on?` | HighlightStreaks |
| `get_reading_goals()` | Books' reading goals as it last saved them | `prefs_path?` | Optional[ReadingGoals] |

### Counts and library info (v1.10.0+)

| Function | Description | Return Type |
|----------|-------------|-------------|
| `count_books_by_status()` | The lengths of the three reading-status lists | `dict[ReadingStatus, int]` |
| `count_annotations(book_id?)` | The length of `list_annotations()`, or of one book's annotations | int |
| `get_library_stats()` | Book and annotation totals, orphans (and, v1.11.0+, `orphan_assets`: annotations per asset id with no book), and annotations per book, in five statements | LibraryStats |
| `get_cached_book_info(asset_ids)` (v1.11.0+) | Title, author, language, publisher and year of [removed books](#removed-books-v1110), from Apple Books' caches, by asset id | `dict[str, CachedBookInfo]` |
| `store_info()` | The store files in use, other `*.sqlite` files next to them, mapped columns this Books version lacks, the SQLite version, the query timeout and the backup folder | StoreInfo |
| `query_deadline(seconds)` | A context manager that stops queries still running after `seconds` | context manager |
| `close()` | Close the idle connections (the next call reconnects) | None |

### Book Content (v1.7.0+)

Read the full text of your non-DRM EPUBs. Powered by [ebooklib](https://pypi.org/project/EbookLib/) and [beautifulsoup4](https://pypi.org/project/beautifulsoup4/).

The library never downloads a book. A book stored only in iCloud, or
partly in iCloud, is refused with `BookNotDownloadedError`: open it in
Apple Books first (see
[how content access handles Apple Books' quirks](#how-content-access-handles-apple-books-quirks)).

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_book_content(book_id)` | Return a `BookContent` handle after verifying the book is downloaded and not DRM-protected | `book_id: int`, or a `Book` (v1.11.0+) | BookContent |
| `get_current_reading_location(book_id)` | Apple Books' auto-tracked "current reading position" bookmark (a zero-width annotation with a CFI); superseded by `get_reading_position()` | `book_id: int` | Optional[Annotation] |
| `get_current_reading_chapter(book_id)` | Convenience: resolve the bookmark's CFI to a `Chapter`; superseded by `get_reading_position()` | `book_id: int` | Optional[Chapter] |
| `get_annotation_surrounding_text(annotation_id)` | The text around a highlight, `""` when it can't be found; superseded by `get_annotation_context()`, which says why no text is given and finds more highlights | `annotation_id: int`, `chars_before?` (300), `chars_after?` (300) | str |

`BookContent` methods:

| Method | Description | Return Type |
|--------|-------------|-------------|
| `list_chapters()` | Flattened table of contents with title, href, fragment, order, depth; `Chapter.spine_index` (v1.11.0+) is the chapter file's spine position | `list[Chapter]` |
| `get_chapter(chapter_id, *, span='file', normalize_unicode=False)` | Plain text of a chapter. `span='file'` (default): the 1.10 text, from the entry's fragment to where another entry of that file begins. v1.11.0+: `span='section'` runs to the next entry of any depth, across files; `span='chapter'` to the next entry of the same depth or shallower (a part includes its chapters) | `str` |
| `list_spine_items()` (v1.11.0+) | The book's reading order: index, manifest id, href, media type, `linear`, `is_toc_page`, `readable`, and the chapters in each file | `list[SpineItem]` |
| `get_spine_item_text(item, *, normalize_unicode=False)` (v1.11.0+) | The text of one whole spine file, by manifest id (`str`, such as `Location.chapter_id`) or spine index (`int`) | `str` |
| `iter_spine_text(*, start, until, include_nonlinear, include_toc_pages)` (v1.11.0+) | The book's text one spine item at a time, from a `TextPosition` up to a `TextPosition` or a `ResolvedBoundary` | `Iterator[SpineText]` |
| `resolve(location)` (v1.11.0+) | The spine file and chapter of a `Location`, CFI or spine index | `ResolvedLocation \| None` |
| `search(query, *, start, until, limit, chars_before, chars_after, count_total, count_withheld, include_nonlinear, include_toc_pages)` (v1.11.0+) | [Folded](#searching) search of the book's text in reading order, optionally stopping at a boundary | `TextSearchResult` |
| `resolve_boundary(boundary, *, precision='item')` (v1.11.0+) | A `ReadBoundary` placed in the book's text | `ResolvedBoundary` |
| `position_at_percent(percent)` (v1.11.0+) | The start of the spine item that holds that share of the book's linear text | `TextPosition` |

`BookContent` properties:

| Property | Description |
|----------|-------------|
| `is_epub` | True if the path is an EPUB bundle directory |
| `is_pdf` | True if the path is a single PDF file (compare `Book.is_pdf`, decided from the database alone) |
| `is_downloaded` | True if locally materialized (not an iCloud placeholder) — does not trigger hydration |
| `is_drm_protected` | True if the EPUB carries FairPlay `META-INF/sinf.xml`, Adobe `rights.xml`, or an `encryption.xml` that encrypts more than fonts (font obfuscation alone doesn't count; an unparseable file does) |
| `book_id` (v1.11.0+) | The library id `get_book_content()` opened it for; `BookContent(path, *, book_id=None)` |

Import `Chapter`, `SpineItem`, `SpineText`, `TextHit` and `TextSearchResult`
from `py_apple_books.content`, and `ReadingPosition`, `ResolvedLocation`,
`AnnotationContext`, `ChapterMatch`, `UnavailableReason`, `TextPosition`,
`ReadBoundary` and `ResolvedBoundary` from `py_apple_books.positions`.
Offsets in `get_spine_item_text()` text are the offsets of a
`TextPosition`; they are not offsets into `get_chapter()` text.
`py_apple_books.content.clear_content_cache()` drops everything cached
from book files; call it after changing a book's files in place.
A `BookContent` can be shared between threads and pickled.

In the `span` modes, `chapter_id` is looked up as a chapter's order
(`"5"`) first, then as a chapter id, then as a manifest id. A location's
`chapter_id` (`Location.chapter_id`) names the file the location is in; in
books whose converter split chapters across files, that file's id can also
be the id of the next chapter, which begins later in the file, so
`get_chapter(location.chapter_id, span=...)` can return the next chapter.
Read a location's own file with `get_spine_item_text`, or find its chapter
with `resolve()` and pass that chapter's order with `span`.

### Text helpers (v1.11.0+)

Pure functions in `py_apple_books.text`; they read nothing.

| Function | Description | Return Type |
|----------|-------------|-------------|
| `text.fold_for_match(s)` | The fold every search applies to both sides (see [Searching](#searching)) | `Optional[str]` |
| `text.finditer_folded(text, query)` | Every folded match of `query` in `text`, as non-overlapping `(start, end)` offsets into `text`, in order | `Iterator[Tuple[int, int]]` |
| `text.find_folded(text, query, start=0, end=None)` | The first folded match in `text[start:end]`, offsets into `text` | `Optional[Tuple[int, int]]` |
| `text.normalize_unicode(s)` | `s` without soft hyphens, zero-width spaces and BOMs, in NFC | `Optional[str]` |
| `text.snap_break(text, pos, *, lookback=300, floor=0)` | An end offset `b` with `floor < b <= pos`: after the last newline, else after the last breaking space or sentence end, else at a character boundary, else `pos` | `int` |
| `text.selection_core(text)` | A highlight trimmed of edge punctuation, symbols and invisible characters, whitespace collapsed (for display) | `str` |
| `text.is_short_selection(text)` | Whether a highlight is a word or a short phrase (at most 64 characters raw, 30 trimmed, 3 words; 8 characters in scripts written without spaces; no sentence punctuation) | `bool` |

Folding, letters and character boundaries follow the running Python's
Unicode database, so a character added in a recent Unicode version can be
classified differently on older Pythons. `snap_break` approximates
Unicode's grapheme cluster rules with the standard library only.

## Searching

The title, genre, collection-title and annotation text searches compare a
folded form of both the query and the stored text
(`py_apple_books.text.fold_for_match`). The fold ignores:

- case, non-ASCII included (`ß` matches `ss`);
- Latin accents (`Godel` finds `Gödel`); other scripts keep their marks, so
  `が` and `か` stay distinct;
- quote, prime and dash style (`don't` finds `don’t`, `-` finds `–` and `—`);
- ligatures and compatibility forms (`find` finds `ﬁnd`, `...` finds `…`);
- soft hyphens and zero-width characters;
- runs of whitespace, line breaks included, so a highlight's own text finds
  it.

`%` and `_` match themselves; they are not wildcards. An empty query matches
every row whose field is set. A query whose visible characters all fold away
matches nothing.

The fold is public: `py_apple_books.text.fold_for_match`. It is
deterministic, idempotent and never raises; a minor release may only make it
fold more, applied to both sides of every search. To find a query inside a
longer text (a chapter, for example) and get the match's position in the
original, use `finditer_folded(text, query)` or
`find_folded(text, query, start=0, end=None)`; a query that folds to nothing
(empty, whitespace only) finds nothing there.

```python
# Straight or curly apostrophe, any case
for a in api.search_annotation_by_text("DON'T"):
    print(a.selected_text)
```

The fold runs in Python for every row searched: an annotation text search
costs about 0.2 s per 10k annotations. The annotation searches and
`get_annotations_by_color` return the newest matches first, so a `limit`
keeps the most recent ones; pass `order_by=None` for storage order.

Through the model managers, `__search` is the folded match and `__contains`
a literal substring match (ASCII case-insensitive, as `LIKE` was).

`search_books` (v1.11.0+) returns the books whose title or author contains
every word of the query, each word in either, in any order
(`"history smith"` finds a history book by an author named Smith). The query
is folded, then split at spaces; at most 32 distinct words are used. `''`
matches every book with a title or author. The author searched is the one
`Book.author` shows: a book Apple Books lists as "Unknown Author" has none.
Where the store has no author column, only titles are searched.

## Ranked search (v1.11.0+)

`search_annotations` ranks highlights and notes against a query and returns
`py_apple_books.search.AnnotationHit` objects, best first:

1. annotations containing every word, ranked by SQLite FTS5's bm25: a word
   in the highlighted text counts most, one in the note less, one only in
   the surrounding text least. English words match their other forms
   ("habits" finds "habit"); stemming is English-only;
2. annotations containing the whole query as written (folded);
3. unless `require_all=True`, annotations containing some of the words
   (`hit.matched_all` is False for these, and they always come last);
4. only when nothing matched, annotations containing every word inside a
   longer word (an attached article or prefix), then, unless
   `require_all=True`, those containing every word of 3 or more characters
   (`hit.matched_all` is False for these when a shorter word was left out).
   A query with no word of 3 or more characters skips this step.

Equal scores list the newer annotation first. `hit.method` says how a hit
matched (`MatchMethod.FTS` or `MatchMethod.SUBSTRING`); `hit.score` is
higher for better hits and comparable only between hits of the same method.
Common English words (the, of, about, ...) are left out of a multi-word
query; put words in double quotes to search for a phrase. Both sides are
folded as in [Searching](#searching). A query in a script written without
spaces between words (Chinese, Japanese, Korean, Thai, Lao, Khmer, Myanmar),
or of punctuation only, is matched as text contained in the annotation.

For any query of 3 or more characters once folded (a space at either end
counts), the results with `limit=None` include every annotation
`search_annotation_by_text` returns, so it is a safe default. `''`,
whitespace, or characters that all fold away return `[]`. A query longer
than `py_apple_books.search.MAX_QUERY_LENGTH` (10,000 characters) raises
`InvalidArgumentError`.

```python
for hit in api.search_annotations("synthetic highlight", limit=10):
    print(f"{hit.score:6.2f}", hit.matched_all, hit.annotation.selected_text)
```

The search runs on an index of the library's highlighted text, notes and
surrounding text, built in memory on the first search (no file is written)
and rebuilt when the annotations change, which is checked at most once a
second; Apple Books' reading-position updates don't count as a change. It
holds folded text only, never shown. It takes about 0.7 KB per annotation
(about 7 MB for 10,000 annotations of typical length, measured on synthetic
text). Each `PyAppleBooks` instance with a library of its own builds its own
index, and `close()` frees it (searches still running finish first); prefer
one instance per process. During a rebuild, the old index is freed as soon
as no search uses it. Waiting for another thread's build ends at the query
timeout; a search's own build runs many statements, each bounded by the
query timeout, so use `query_deadline()` to bound the whole call. A build
stopped by a timeout is continued by the next search rather than started
again. If your Python's SQLite lacks FTS5
(`py_apple_books.search.fts5_available()` is False), every tier matches
substrings instead (weaker ranking). As with any query, don't fork while
another thread is searching: the first search and each rebuild run much
longer than other queries.

## Pagination and counting

Every list and search method takes `limit` and `offset`. With an `offset`
(including `offset=0`) and no `order_by`, results are ordered by primary key
(`Z_PK`), so consecutive pages never overlap or skip rows. Pass `offset=0`
for the first page. A `limit` alone keeps the storage order 1.9 returned.
Ordered queries, including the methods with a default order, use the
primary key as a tie-break.

```python
page_1 = list(api.list_annotations(limit=50, offset=0))
page_2 = list(api.list_annotations(limit=50, offset=50))
```

The list methods return a `ModelIterable`. It runs its query once, on the
first `len()`, `bool()`, iteration or index, and later uses read the same
rows. Call the method again for fresh data. It also offers:

```python
annotations = api.list_annotations()

print(annotations.count())        # SELECT COUNT(*), without fetching the rows
print(annotations.exists())       # LIMIT 1
print(annotations.first())        # lowest primary key unless ordered; None if empty
first_ten = annotations[:10]      # a slice is a list (one LIMIT/OFFSET query)
print(annotations.count_by("style"))  # {raw column value: rows}
```

Counts use the same filters as the lists they count, so a count always
equals the length of its list:

```python
from py_apple_books.models import ReadingStatus

counts = api.count_books_by_status()
print(counts[ReadingStatus.FINISHED], counts["in_progress"], counts["unstarted"])
print(api.count_annotations())

stats = api.get_library_stats()
print(stats.total_books, stats.finished_books, stats.in_progress_books, stats.unstarted_books)
print(stats.total_annotations, stats.orphan_annotations)
for book_id, title, n in stats.annotations_per_book[:5]:
    print(f"{n:5}  {title}")
```

`limit=None` means all rows. A `limit` of 0 or below also means all rows but
is deprecated (see [Deprecations](#deprecations)); a negative `offset` raises
`InvalidArgumentError`.

**Methods added in 1.11** take `limit=None` for all rows (unless a default
is documented: `search_annotations` returns 20, `sample_highlights` 5) and
raise `InvalidArgumentError` for a `limit` below 1, a `bool` or a value that
isn't integral (no deprecated "0 means all"), and for a negative `offset`.
`search_annotations` pages through its ranked order (pages are consistent
while the annotations don't change). Some of them page in Python, after
reading and ordering every match, and return a list rather than a
`ModelIterable`: `list_series` (ordered by title), `get_vocabulary` (for a
total, call it with `limit=None` and count the list), and
`get_books_by_subject` with `read_files=True` (it returns a
`ModelIterable` whose `count()` and slices are computed from that scanned
list without another query; with a `limit`, the scan stops once
`offset + limit` books have matched).

## What the lists return

**Your books.** Apple Books also keeps rows for Store series it knows about:
series containers, and volumes of a series you don't own. The book lists and
searches leave those out. A Series row counts as owned when Books records it
as redownloadable (`Book.can_redownload == 1`); a row with that flag unset
counts as not owned. Series containers (`content_type` 5) never count as
owned. `include_store_series=True` brings these rows back on `list_books`,
`get_book_by_title` and `get_books_by_genre`. The status and recency lists
always leave them out. `get_book_by_id` and relations such as
`collection.books` resolve every row. If your Apple Books version lacks a
column this rule needs, the rows that column would hide are shown.

**Reading status.** One rule, finished first (`Book.reading_status`):
finished if Books marked the book finished, whatever its progress;
otherwise in progress above 0%, and unstarted at 0% or with no progress. The
three lists never overlap and together equal `list_books()`.

**Annotations.** The annotation lists, searches and `book.annotations`
return live highlights, notes and bookmarks. They leave out Apple Books'
automatic reading-position row (see `get_current_reading_location`),
annotations deleted in Books, and the empty tombstones Books keeps for
iCloud sync. `include_deleted=True` brings the deleted rows and tombstones
back; `get_annotation_by_id` returns any row.

**Default orders.**

| Method | Default order |
|--------|---------------|
| `get_annotations_by_color`, the three annotation text searches | newest first (`'-creation_date'`) |
| `get_underlines`, `get_highlights_on_this_day` (v1.11.0+) | newest first (`'-creation_date'`) |
| `get_recently_read_books` | `'-last_read_date'`: the later of last opened and last engaged, newest first (sorted in Python) |
| `search_annotations` (v1.11.0+) | best match first (see [Ranked search](#ranked-search-v1110)) |
| `sample_highlights` (v1.11.0+) | sample order (`py_apple_books.engagement.SAMPLE_ALGORITHM`) |
| `get_vocabulary` (v1.11.0+) | most recently highlighted first (`'-last_highlighted'`) |
| `list_series` (v1.11.0+) | series title |
| Everything else | storage order; primary key when paging with `offset` |

`order_by` takes a field (`'title'`), a descending field (`'-title'`), a
comma-separated list (`'author,-title'`) or a list. `order_by=None` gives
storage order, and `get_recently_read_books(order_by='-last_opened_date')`
the 1.9 recency order.

```python
everything = api.list_books(include_store_series=True)
with_deleted = api.list_annotations(include_deleted=True)
storage_order = api.get_annotations_by_color("yellow", order_by=None)
```

## Model data

New in 1.10:

| Model | Field or property | Meaning |
|-------|-------------------|---------|
| `Book` | `reading_status` | `ReadingStatus.FINISHED`, `IN_PROGRESS` or `UNSTARTED` (the lists' rule) |
| `Book` | `last_read_date` | The later of `last_opened_date` and `last_engaged_date` |
| `Book` | `last_engaged_date` | Advances with reading activity; unset on many books |
| `Book` | `can_redownload`, `data_source`, `store_id` | Books' ownership flag, the source of the row, and the Store id (Store rows only) |
| `Book` | `is_store_series_item`, `is_series_container` | An unowned Store series entry, or a series container |
| `Book` | `state`, `is_cloud_only` | Books' cached download state; the file system stays authoritative (`BookContent.is_downloaded`) |
| `Book` | `deep_link` | `ibooks://assetid/<asset id>`, which opens the book in Books |
| `Annotation` | `uuid` | Stable across devices and re-syncs |
| `Annotation` | `position` | The chapter's spine index, so `order_by='position'` sorts by book order |
| `Annotation` | `deep_link` | The book link plus `#<CFI>`. Whether Books jumps to the highlight is unverified |
| `Location` | `spine_index`, `sort_key` | Sort annotations in reading order without opening the book |

New in 1.11:

| Model | Field or property | Meaning |
|-------|-------------------|---------|
| `Book` | `language`, `year`, `release_date` | The language, publication year and Store release date Books records |
| `Book` | `series_id`, `series_container_id`, `series_sequence`, `series_label`, `series_is_ordered` | Where the book sits in a Store series (provisional) |
| `Book` | `high_water_progress` | The furthest point Books recorded, in percent like `reading_progress`; can be ahead of where you are |
| `Book` | `is_pdf` | A PDF, by the library's content type or the file name (no file access; `BookContent.is_pdf` looks at the file) |
| `Annotation` | `position_fraction`, `furthest_fraction` | Bookmarks and the reading-position row: where you are and the furthest point read, 0..1 |
| `Annotation` | `page_location` | Bookmarks and the reading-position row: a `PageLocation` with `page` (PDFs) and `ordinal` (the spine item) |
| `Annotation` | `location_data` | The raw record `page_location` is decoded from (left out of `repr`) |
| `Annotation` | `is_short_selection` | A word or short phrase rather than a passage |
| `Location` | `end_sort_key` | Where a range ends, in `sort_key` order |
| `Chapter` | `spine_index` | The spine position of the chapter's file (not part of `==` or `repr`) |

Books stores the two fractions as text: filtering or sorting on them in a
query compares text (`'0.10'` sorts before `'0.9'`), so compare the floats in
Python. They are read only when the row's `type` is read too. A date Books
stored in a form that can't be read reads as None.

`py_apple_books.models` also exports `PageLocation` and the
[metadata and series](#metadata-and-series-v1110) types `BookMetadata`,
`MetadataFileState`, `Series` and `SeriesVolume` (v1.11.0+).

`AnnotationType` names the annotation kinds: `TOMBSTONE` (0), `BOOKMARK` (1),
`HIGHLIGHT` (2; a note is a highlight with a note body) and
`READING_POSITION` (3). A mapped column your Apple Books version lacks reads
as None (`store_info().missing_columns` lists them); filtering or sorting on
it raises `UnsupportedSchemaError`. Models can be pickled.

```python
from py_apple_books.models import ReadingStatus

for book in api.list_books():
    if book.reading_status is ReadingStatus.IN_PROGRESS:
        print(book.title, book.last_read_date, book.deep_link)

# A book's highlights in reading order
for book in api.get_recently_read_books(limit=1):
    located = [a for a in book.annotations if a.location and a.location.sort_key]
    for a in sorted(located, key=lambda a: a.location.sort_key):
        print(a.location.spine_index, a.selected_text)
```

## Where am I in a book (v1.11.0+)

`get_reading_position(book_id)` says where you are in a book. It reads
Apple Books' reading-position bookmark (`source` `bookmark`), else, with
`infer=True`, the newest highlight or bookmark with a location
(`recent_annotation`). It returns a `ReadingPosition` with the location, its
spine file, its chapter (`chapter`, `match`, `total_chapters`; skipped with
`resolve_chapter=False`, which touches no file), and, where Books records
them, the position as a fraction of the book (`fraction`), the furthest point
read (`furthest_fraction`) and, for PDFs, the page and page count. A book
that can't be read (not downloaded, DRM-protected, a PDF) still gets a
position, with the reason in `unavailable`. It runs at most three queries
and returns None when nothing records a position.

```python
for book in api.get_books_in_progress(limit=3):
    pos = api.get_reading_position(book)
    if pos is None:
        continue
    if pos.chapter is not None:
        print(f"{book.title}: chapter {pos.chapter.order} of {pos.total_chapters}, {pos.chapter.title}")
    else:
        print(f"{book.title}: {pos.unavailable or pos.match}")
```

`get_annotation_locations(annotations)` places many annotations at once
(`{annotation.id: ResolvedLocation}`), reading each book once, and
`get_annotation_context(annotation_id)` returns a highlight with the text
around it. `str()` of an `AnnotationContext` is the passage on one line,
with `…` where it was cut, and equals what `get_annotation_surrounding_text`
returns whenever that finds the highlight (unless the context chose a later
occurrence, `disambiguated`). It also finds highlights across soft hyphens
and zero-width spaces, and with different case, accents, quotes or dashes
(`text_match`). Instead of an empty string it raises: a
`ContextUnavailableError` whose `reason` says what is missing, or the error
the book raises.

```python
from py_apple_books.exceptions import AppleBooksError
from py_apple_books.positions import UnavailableReason

annotations = list(api.list_annotations(limit=20))
where = api.get_annotation_locations(annotations)
for a in annotations:
    loc = where.get(a.id)
    chapter = loc.chapter.title if loc and loc.chapter else None
    try:
        ctx = api.get_annotation_context(a, chars_before=60, chars_after=60)
    except AppleBooksError as e:
        print(a.id, chapter, "no context:", UnavailableReason.of(e))
    else:
        print(a.id, chapter, ctx)
```

`BookContent.resolve(location)` does the same for one location (a
`Location`, a CFI or a spine index). `ResolvedLocation.match` says how the
chapter was chosen:

| `ChapterMatch` | Meaning |
|----------------|---------|
| `file` | The location's file holds one table-of-contents entry |
| `anchor` | The file holds several; the last one starting at or before the location, compared with where each entry's anchor is in the file |
| `preceding` | The file holds none, or the location is before all of them: the last entry earlier in the book |
| `front_matter` | No entry starts before it (no chapter yet) |
| `section_unknown` | The file holds several entries but where they start couldn't be read; the file is known, the section isn't, and no chapter is guessed |

These methods never run `du` or walk a book folder, and never download a
file: a book or file that isn't on this Mac is reported as
`not_downloaded`. Import the types from `py_apple_books.positions`; they are
not in the top-level namespace:

| Type | What it is |
|------|------------|
| `ChapterMatch` | How a location was placed in the table of contents (above) |
| `ResolvedLocation` | A location's `chapter`, `match`, `spine_index` and `item_id`, or the `unavailable` reason |
| `UnavailableReason` | Why a chapter, position or context couldn't be given; `UnavailableReason.of(exc)` maps a library error to one |
| `ReadingPosition` | Where you are in a book: `source` (`bookmark` or the inferred `recent_annotation`), `location`, `chapter`, `total_chapters`, `fraction`, `furthest_fraction`, `page`, `page_count` |
| `AnnotationContext` | A highlight with the text `before` and `after` it; `str()` is the passage with `…` where it was cut |
| `TextPosition` | A point in a book's text, written `"N:M"` (spine item and offset); `TextPosition.parse()` reads it back |
| `ReadBoundary` | How far you have read, from the library alone; `includes(location)` tells whether a location is known to be read |
| `ResolvedBoundary` | That boundary placed in the book's text: the text before `position` has been read |

Each is an immutable, hashable and picklable dataclass, or an enum whose
members are strings (`str()` gives the value). A `TextPosition` is valid for
one book file and one library version; it is not an offset into
`get_chapter()` text.

## Spoiler-safe reading (v1.11.0+)

`get_read_boundary(book_id)` says how far a book has been read, from the
library database alone (no book file is read; at most three statements).
The first source the library has places it: Apple Books' reading-position
bookmark (the earliest one if a book has several, so the boundary is never
past any of them), else the newest highlight, note or bookmark with a
location, else the reading progress, else nothing (`source` is
`reading_position`, `recent_highlight`, `progress` or `none`).
`basis='furthest'` uses the furthest point read when it is past the reading
progress. A missing column on an older or newer Books database skips the
sources that need it (`schema_missing_columns` in `warnings`).
`boundary.includes(annotation.location)` tells from locations alone whether
a highlight is known to be read.

`BookContent.resolve_boundary(boundary)` places it in the book's text, at
the start of the spine item the boundary falls in (`precision='item'`, the
only precision in this release), never later than the point it stands for.
A bookmark it can't place falls back to the highlight, then the progress,
then the start of the book, with a warning code. `search(query, until=...)`
then searches only the text before it: no match runs across the boundary
and no snippet shows anything past it. `count_withheld=True` counts the
matches past it without returning them, and `next_start` pages through the
hits.

```python
# book_id: a book you are reading, e.g. api.get_books_in_progress(limit=1).first().id
boundary = api.get_read_boundary(book_id)
content = api.get_book_content(book_id)
read = content.resolve_boundary(boundary)
result = content.search("words", until=read, count_withheld=True)
for hit in result.hits:
    print(hit.start, hit.snippet)
print(result.withheld_in_item, result.withheld_later, "matches later in the book")

# The highlights you have already read past
book = api.get_book_by_id(book_id)
seen = [a for a in book.annotations if boundary.includes(a.location)]
```

`iter_spine_text(until=read)` reads the text before the boundary one spine
item at a time, and `position_at_percent(percent)` gives the start of the
item that holds that share of the book. `get_read_boundary` and
`get_reading_position` can name different bookmarks when a book has several
live reading-position rows: the boundary takes the earliest, the position the
newest. A `ResolvedBoundary` is valid for one book file and one library
version; a boundary for another book raises `InvalidArgumentError` on a
`BookContent` from `get_book_content()`.

## Metadata and series (v1.11.0+)

`get_book_metadata(book_id)` returns a `py_apple_books.models.BookMetadata`:
language (a normalised BCP 47 tag such as `en-US`), publisher, publication
date (`published`, `'YYYY'`, `'YYYY-MM'` or `'YYYY-MM-DD'`, and `year`),
ISBN (checksum-verified; an ISBN-13 when the book has one), subjects,
description (plain text), the cover image's path inside the book
(`cover_href`), and a series named in the book file (`series_title`,
`series_sequence`). What the library records comes first; the book's own
package document (OPF) fills in the rest, and `book_file_fields` says which
fields came from it. Every text value is untrusted text from the library or
the book file.

`file_state` (`MetadataFileState`) is `read`, `not_requested`
(`read_files=False`: library values only, no file access), `no_file`,
`not_epub`, `not_downloaded` or `unreadable`; file problems never raise. A
book stored only in iCloud, or partly evicted, is never downloaded: it is
reported as `not_downloaded`. Results read from book files are cached in
memory while the files are unchanged.

```python
meta = api.get_book_metadata(book_id)
print(meta.language, meta.publisher, meta.published, meta.isbn, meta.subjects, meta.file_state)
```

`get_series(book_id)` and `list_series()` (provisional: they may change in
1.12) return the Apple Books Store series the library records, as `Series`
(`title`, `series_id`, the series `container` row, `is_ordered`, `volumes`)
of `SeriesVolume`s (`book`, `ids`, `sequence`, `label`, `in_library`,
`reading_status`). `get_series` accepts a volume, the series container, or a
copy of a volume in your library that shares only its Store id; None when
Books records no series for the book. `volumes` are the volumes Books knows
about, owned or not, never the length of the series. `Series.current` is the
volume in progress with the highest sequence, `up_next` the volume after the
furthest one started or finished, and `volume_for(book_id)` and
`next_after(book_id)` look volumes up. `SeriesVolume.in_library` follows
`list_books()`'s rule. Both read the library database only (`get_series` at
most 4 statements, `list_series` at most 2).

```python
for series in api.list_series(started_only=True):
    nxt = series.up_next
    print(series.title, len(series.volumes), "next:", nxt.label if nxt else None)
```

`get_books_by_subject(subject)` is `get_books_by_genre` plus the subjects in
each book's package document, so books whose genre doesn't mention a topic
but whose subjects do are found too; `read_files=False` is
`get_books_by_genre` itself. Only unzipped EPUBs on this Mac are read, and
only their `container.xml` and package document; books stored only in
iCloud, books without a file and PDFs are skipped without touching the disk.
The database lookup finishes before any file is read, and one deadline (the
library's `query_timeout` and any `query_deadline()`) covers the whole call,
raising `QueryTimeoutError`.

## Engagement (v1.11.0+)

`sample_highlights(limit=5)` resurfaces highlights: a varied sample for a
day, the same on every platform for the same day and `seed`, and new the
next day. Highlights with a note count twice, and without `book_id` the picks
go round-robin across books, so the first ones come from different books.
Words and short phrases are left out unless `exclude_short=False`. It is
resurfacing, not spaced repetition: nothing is stored, so a caller that
keeps its own "already shown" list passes it in `exclude_ids` or
`exclude_uuids`. The algorithm is named by
`py_apple_books.engagement.SAMPLE_ALGORITHM`. PDF highlights are not in the
annotation database, so they are not sampled.

```python
import datetime as dt

for a in api.sample_highlights(3):
    print(a.selected_text)
for a in api.get_highlights_on_this_day(limit=5):
    print(a.creation_date.year, a.selected_text)
streaks = api.get_highlight_streaks()
print("highlight streak:", streaks.current, "longest:", streaks.longest)
this_year = api.get_finished_books(finished_after=dt.date(dt.date.today().year, 1, 1))
```

`get_vocabulary()` groups the words and short phrases you highlighted across
books ("Ephemeral," and "ephemeral" are one entry; no stemming), each with
its highlights, `count`, `notes`, dates, `asset_ids` and a `context`
sentence from the text Books keeps around the highlight. It returns a list.
`get_underlines()` returns underlines; on a Books version without the
underline flag, highlights without a color.

**Dates.** The methods added in 1.11 take dates by one rule. `after` and
`before` (and `get_finished_books`' `finished_after` and `finished_before`)
are inclusive and keyword-only; a `datetime` is an instant (a naive one is
local time), a `date` covers that whole local day. `on` names a local
calendar day; its time of day is ignored. Any other type raises
`InvalidArgumentError`. `get_annotations_by_date_range` also accepts dates.

**What can and can't be derived.** `get_highlight_activity` and
`get_highlight_streaks` count highlights, not reading time: reading minutes,
pages read per day and Books' streak history are not stored in a form the
library can read. Books records the date a book was marked as finished, so
books marked in bulk share one finish date. `get_reading_goals()` reads
Books' preferences file as Books last saved it (`ReadingGoals.modified`):
the yearly books goal, the daily reading goal, Books' own current streak and
its list of books finished towards the goal, which is a separate record from
`get_finished_books` and can differ. The file is read only if it is a
regular local file of at most 8 MiB, outside iCloud Drive and other cloud
folders; otherwise, or if it is missing, the result is None.

## Removed books (v1.11.0+)

Highlights and notes outlive their book: when a book is removed from the
library, its annotations stay, and `annotation.book` is None
(`get_library_stats().orphan_annotations` counts them, and `orphan_assets`
counts them per asset id). Apple Books keeps what it parsed from each book in
caches next to the library, one per Books version, including books since
removed, and `get_cached_book_info` reads them:

```python
orphans = [a for a in api.list_annotations() if a.book is None]
names = api.get_cached_book_info(a.asset_id for a in orphans)
for a in orphans:
    info = names.get(a.asset_id)
    print(info.title if info and info.title else "Removed book", "|", a.selected_text)
```

It returns `py_apple_books.book_info.CachedBookInfo` objects (`title`,
`author`, `language`, `publisher`, `year` as text, any of them None, though
never both title and author) for the ids found. Values are what Books cached
when it last parsed the book; `info.source` names the cache file they came
from, which names the Books version that wrote it. The answer is best
effort: macOS may purge these caches, so a title found today may be gone
later. The call never raises for a cache problem and returns what it read
within a few seconds, possibly `{}` (SQLite may wait about 10 s on a cache
another process keeps locked).

The caches are read-only to this call: each is opened according to its
journal mode, so the cache, its journal and its `-wal` are never written.
Reading a WAL-mode cache may update its `-shm` file (SQLite's shared-memory
index) in place, as reads of the library stores do. No file is created or
removed, except in one narrow race: if Books closes a WAL-mode cache while
it is being read, SQLite may leave an empty `-wal` and a `-shm` beside it,
which SQLite ignores or rebuilds. Caches are found from the library's
`Documents` folder (or a library store given as
`.../Documents/BKLibrary/<file>`); other layouts, and folders in iCloud Drive
or cloud storage, give `{}`. Results are remembered until `close()`: the
cache folder is looked at again at most every
`py_apple_books.book_info.BOOK_INFO_RECHECK` seconds (30), and only changed
files are read again. Up to 1,024 ids are remembered per cache file (fewer if
their values are unusually long), so pass the asset ids of the annotations
whose book is gone (as above; repeated ids count once), not the ids of every
annotation.

Privacy: the answer also tells whether Books ever opened a book with a given
id, including books you never highlighted. Pass ids from your own
annotations; an application shouldn't offer it as a lookup by arbitrary id.

## Exceptions

Every exception derives from `py_apple_books.exceptions.AppleBooksError`.
The new ones keep the built-in base 1.9 raised, so `except IndexError`,
`except KeyError` and `except sqlite3.OperationalError` handlers written for
1.9 still work. One exception: a write to a read-only store raised
`sqlite3.OperationalError` in 1.9 and now raises `WriteError`, which is not
a `sqlite3` error.

| Exception | Also a | When |
|-----------|--------|------|
| `BookNotFoundError` | `IndexError`, `WriteError` | `get_book_by_id`, `count_annotations(book_id)`, the `book_id` of a method added in 1.11, or a write: no book has that id |
| `CollectionNotFoundError` | `IndexError`, `WriteError` | No (live) collection has that id |
| `AnnotationNotFoundError` | `IndexError` | `get_annotation_by_id`: no annotation has that id |
| `ChapterNotFoundError` | | No chapter or spine entry has that id |
| `InvalidArgumentError` | `ValueError` | A bad argument, such as a negative `offset`, a `limit` below 1 in a method added in 1.11, or a query longer than `py_apple_books.search.MAX_QUERY_LENGTH` |
| `InvalidChoiceError` | `KeyError`, `ValueError` | A value outside a fixed set, such as an unknown highlight color (`.value`, `.valid`) |
| `UnknownFieldError` | `KeyError`, `ValueError` | A model has no such field (in a filter, `order_by` or `only=`) |
| `BookNotDownloadedError` | | The book has no local file (never downloaded), the file is an iCloud placeholder, Books records it as stored only in iCloud, **or** (v1.11.0+) part of it is stored only in iCloud; the library never downloads it |
| `NotInLibraryError` | `BookNotDownloadedError` | The row is an unowned Store series entry with no file |
| `DRMProtectedError` | | The book is DRM-protected — usually an Apple Books Store purchase (FairPlay), occasionally an encrypted imported EPUB |
| `NotEpubError` (v1.11.0+) | | A chapter method of `BookContent` on a PDF or another non-EPUB file (same messages as 1.10) |
| `ContextUnavailableError` (v1.11.0+) | | `get_annotation_context` has no text to give (`.reason`: `no_location`, `no_highlight_text`, `orphaned`, `empty_chapter`, `highlight_not_found`; `.annotation_id`) |
| `UnsafeEpubEntryError` | | A file in the EPUB bundle resolves outside it (absolute/`../` href, symlink), isn't a regular file, or is implausibly large; the book is refused (`.entry`, v1.11.0+, is the full entry name) |
| `DBError` | | Base of the database errors (an `AppleBooksError` since 1.10) |
| `LibraryNotFoundError` | `DBConnectionError` | No Apple Books store found, or the file isn't one (`.path`) |
| `AnnotationStoreNotFoundError` | `LibraryNotFoundError` | An annotation query without an annotation store |
| `LibraryAccessDeniedError` | `DBConnectionError` | macOS refused access: grant Full Disk Access to the app running your code (`.path`) |
| `UnsupportedSchemaError` | `DBQueryError` | The store lacks a column or table a query needs (`.table`, `.column`) |
| `QueryTimeoutError` | `DBQueryError` | A query ran past its deadline (`.timeout`), including a search waiting for the ranked-search index build |
| `WriteError` | | Base of the write errors |
| `BooksAppRunningError` | `WriteError` | A write or restore while Books is running |
| `SchemaValidationError` | `WriteError` | The collection tables don't match the verified schema |
| `SystemCollectionError` | `WriteError` | An edit to a built-in collection |
| `LibraryBusyError` | `WriteError`, `sqlite3.OperationalError` | Another program held the library's lock, or another write held its backup folder, too long; nothing was changed |
| `AmbiguousStoreError` | `WriteError`, `DBConnectionError` | A write can't tell for sure which store Books uses |
| `BackupValidationError` | `WriteError` | A backup failed a pre-restore check (`.reason`) |

`get_book_content` raises a bare `IndexError`, not an `AppleBooksError`,
for an unknown book id throughout 1.x: apple-books-mcp 0.8.2 catches
`AppleBooksError` before `IndexError` around it. 2.0 will raise
`BookNotFoundError`.

`py_apple_books.positions.UnavailableReason.of(exc)` (v1.11.0+) maps any
error the content and context APIs raise to one reason: `NotInLibraryError`
to `not_owned`, `BookNotDownloadedError` to `not_downloaded`,
`DRMProtectedError` to `drm`, `NotEpubError` to `not_epub`,
`ChapterNotFoundError` to `chapter_not_found`, `ContextUnavailableError` to
its `reason`, and any other library error to `unreadable`. It gives None for
database errors and anything else, and never raises. `search_annotations`
raises `DBQueryError('Ranked annotation search failed.')` if its private
index fails; the message never includes the query.

```python
from py_apple_books.exceptions import BookNotFoundError

try:
    api.get_book_by_id(999999999)
except BookNotFoundError as e:
    print(e)  # No book with id 999999999.
```

## Writes

Every collection write:

- refuses while Books.app is running (`BooksAppRunningError`), checked
  before the backup and again once the write lock is held. Writes also
  refuse when Books.app's state can't be verified: off macOS (e.g. in
  Docker), or if `pgrep` fails;
- takes a WAL-inclusive backup first (see [Backups and restore](#backups-and-restore));
- runs in a single transaction that maintains Core Data's bookkeeping;
- goes to the library store the instance reads. That store is resolved
  strictly: several candidate stores and no canonical one, a canonical store
  that can't be read, only a copy or backup, or a store other than the one
  being read raise `AmbiguousStoreError`, and nothing is written. A missing
  store file is never created.

Before each write, the collection and membership tables must have exactly
the columns and column types this version was verified against; otherwise
the write is refused (`SchemaValidationError`) and nothing changes. The Core
Data model version of those two entities is also compared with the verified
one. `APPLE_BOOKS_MODEL_CHECK` decides what a difference does:

| Mode | Unknown model version | New optional (nullable) column | Missing or retyped column, new required column |
|------|-----------------------|--------------------------------|-----------------------------------------------|
| `warn` (default) | logs a warning, then writes | refuses | refuses |
| `enforce` | refuses | refuses | refuses |
| `off` | not checked | logs a warning, then writes and leaves it empty | refuses |

`WriteSession(model_check=...)` overrides the variable. Writes are verified
on macOS 26.7 with Books 8.5. **macOS 27 is unverified**: if it adds or
changes columns in these tables, writes refuse until a release is verified
against it. If a write is refused, please report your schema (see
[Report a new version](#report-a-new-version)).

If another program holds the library's write lock for more than 5 s
(`collection_writer.BUSY_TIMEOUT`), or blocks the commit, the write raises
`LibraryBusyError` and nothing changes; try again in a moment. A read-only
store raises `WriteError`.

```python
# Quit Books first.
shelf = api.create_collection("Beach reads")
book = api.list_books(limit=1).first()
api.add_book_to_collection(shelf.id, book.id)
print([b.title for b in api.get_collection_by_id(shelf.id).books])
```

## Backups and restore

Each write backs up the library store first, with the SQLite backup API
(WAL-inclusive). A write reuses the newest backup if it was taken less than
5 minutes ago, and otherwise takes a new one; the newest 10 backups of each
store are kept. Backups of the current user's library go to
`~/.py_apple_books/backups/`. Any other store (named by `data_dir`,
`library_db` or the location variables) gets a folder of its own under
`~/.py_apple_books/backups/libraries/`; `store_info().backup_dir` names it.

Writes running at the same time, in any threads or processes using 1.11 or
later, share one backup: the backup folder is locked while a write checks
for a recent backup, takes one and prunes old ones, and while a restore
runs. A write that waits more than 15 s for it
(`write_safety.BACKUP_LOCK_TIMEOUT`, or less inside `query_deadline`)
raises `LibraryBusyError` and nothing changes. Backups are created readable
by you only (0600), and new backup folders with 0700.

`write_safety.restore_library(backup)` puts a backup back:

- It refuses while Books is running.
- It checks the backup first (`verify_backup`: it opens, passes an integrity
  check, is a backup of this store and was written by the same Books data
  model) unless `force=True`.
- It snapshots the current library, and returns the snapshot's path:
  restoring the snapshot undoes the restore.
- **It replaces the whole library database**: collections go back, and so do
  books added and reading progress recorded since the backup. Highlights and
  notes live in the other store and are not touched.
- With iCloud collection sync on, a restore (like any write) may not reach
  other devices, and a later sync may bring the old state back.

```python
from py_apple_books.write_safety import list_backups, restore_library

# Quit Books first. Restores the newest backup; the returned snapshot undoes it.
backups = list_backups()          # newest first
snapshot = restore_library(backups[0])
```

Without `db_path` both follow the default library (the location variables,
else the current user's store). For another store, pass `db_path=`; its
backups are looked up in its own folder.

To restore over a damaged library, pass `force=True`: it skips the backup
check, and the target is the canonical store file even when that file fails
the check for a readable Books store (damaged metadata or pages).
`snapshot=False` skips the pre-restore snapshot when it can't be taken; the
restore then can't be undone. The restore goes through SQLite, so a file
SQLite rejects outright (overwritten or truncated) can't be restored over
this way; it has to be replaced by hand while Books is quit.

## Examples

These assume `api = PyAppleBooks()`, as in [Quick start](#quick-start).

### Get annotations

```python
for a in api.list_annotations(limit=5):
    print(f"[{a.color}] {a.selected_text}")
```

### Search highlights

```python
for a in api.search_annotation_by_highlighted_text('synthetic'):
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

book = api.list_books(limit=1).first()
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
    text = content.get_chapter(content.list_chapters()[0].id)
    print(text[:500])
```

### See what the user is currently reading

```python
# The book they most recently read
for book in api.get_recently_read_books(limit=1):
    pos = api.get_reading_position(book)
    if pos is None or pos.chapter is None:
        continue
    content = api.get_book_content(book)
    text = content.get_chapter(str(pos.chapter.order), span="chapter")
    print(f"Currently reading: {book.title}")
    print(f"Progress: {book.reading_progress:.1f}%")
    print(f"Chapter {pos.chapter.order} of {pos.total_chapters}: {pos.chapter.title}")
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

- **iCloud placeholders and partial downloads** — books you've imported but haven't opened lately can live partly or wholly in iCloud, and the library never downloads them. `get_book_content` refuses, before any download check: a book Books records as stored only in iCloud (no file is touched), a book whose file or folder is itself only in iCloud or has an iCloud stub next to it, and (v1.11.0+) a book with any folder or file inside it only in iCloud (each folder is checked before it is listed). Then `is_downloaded` (`os.stat` for files, `du -sk` for bundle directories) and the DRM check run as before. Each raises `BookNotDownloadedError`, so you can prompt the user to open the book in Apple Books.
- **No download on read (v1.11.0+)** — every read of a book's files checks each folder and file first, and runs with macOS's download of iCloud-only files turned off for the reading thread, so a file moved to iCloud after those checks fails with `BookNotDownloadedError` ("Part of this book is stored only in iCloud. Open it in Apple Books to download it, then try again.") instead of being downloaded. Off macOS that switch doesn't exist and the checks alone apply. The 1.11 content methods (spine, `resolve`, `search`, boundaries, positions, context) read only `container.xml`, the package document and the navigation files, then each file whose text is asked for, each checked first, and never run `du` or walk the book folder.
- **Book metadata (v1.11.0+)** — `get_book_metadata` reads only `container.xml` and the package document. It checks, in order and without touching the disk for the first three: a file recorded, an `.epub` path, Books not recording the book as stored only in iCloud; then that the book folder is on this Mac with no iCloud stub next to it, that every folder and file in it is on this Mac (each folder checked before it is listed, as `get_book_content` checks), `is_downloaded`, and every folder and file on the way to the package document, each before anything inside it is looked up or read. Anything not on this Mac, including a partly evicted book, is reported as `not_downloaded`, never downloaded. The package document is untrusted input: a DOCTYPE with an internal subset is refused, external entities are never fetched, parsing is bounded and stops once the needed parts are read, and symlinks inside the book folder are refused.
- **Caches and memory (v1.11.0+)** — chapter lists and spines are kept in memory, process-wide, keyed by file metadata (identities, sizes and modification times; at most about 32 MiB and 4,096 books). Only each book's table of contents (chapter titles and its navigation files), its spine and manifest (the ids, paths and media types of its files) and the positions of the anchors in its chapter files are kept; the package's metadata is not kept there, chapter text is not cached across `BookContent` instances, and nothing is written to disk. Book metadata and subjects read for `get_book_metadata` and `get_books_by_subject` are cached the same way (at most 512 books and 16 MiB, and 20,000 books and 8 MiB). `content.clear_content_cache()` drops all of these. The ranked-search index (about 0.7 KB per annotation) and the removed-books memo belong to a library and are freed by `close()`.
- **DRM'd Store purchases** — FairPlay-encrypted EPUBs carry `META-INF/sinf.xml` / an `encryption.xml` covering their content, and their chapter bodies are opaque ciphertext. Detected up-front from that evidence; callers get a clear `DRMProtectedError`. EPUBs whose `encryption.xml` only obfuscates fonts are readable.
- **Untrusted book files** — imported EPUBs are stored as unzipped bundles, so every entry is read through a contained reader: anything resolving outside the bundle, non-regular files and oversized entries raise `UnsafeEpubEntryError` instead of exposing unrelated local files.
- **Missing metadata** — Apple's unknown-author placeholder is returned as `author=None`, and the `ZPAGECOUNT` placeholder (0 or 1) as `page_count=None`.
- **Non-standard EPUB layouts** — OPFs at unusual paths, NCX files not declared in `<spine toc=…>`, EPUB3 books with nav docs only, URL-encoded href characters (`%21` → `!`), duplicate navPoint ids — all handled, with a stdlib NCX override for the ebooklib blind spots.
- **Fragment-scoped chapters** — Project Gutenberg EPUBs often put multiple sections in one XHTML file, separated by anchors. `get_chapter` returns only the requested section, not the whole file.

## Supported versions

| macOS | Apple Books | Status |
|-------|-------------|--------|
| 26.7 (25G229) | 8.5 (6570) | Verified on a live library; full schema fixture |
| unknown (early 2023) | unknown | Partial: annotation and book-asset column names from public DDL (March 2023); column-presence check only |
| 12–15 | — | Not verified |
| 27 | — | Not verified |

Reads adapt to the columns a store has: a missing optional column reads as
None. Collection writes need the exact verified schema (see [Writes](#writes)).

### Report a new version

If you run a macOS or Apple Books version not listed above, please send us
your schema. This writes about 14 KB of SQL describing the database layout
and, when Books has one, its book-info cache, and holds no library contents
(no titles, highlights, paths or account ids; the output is checked for that
before it's saved):

```sh
python -m py_apple_books.testing.dump_schema --out ~/Desktop/apple-books-schema --compare
```

Attach the `macos-…_books-…` folder it creates to an issue. `--compare`
prints the column and entity-hash differences from the nearest committed
fixture. The stores are opened read-only.

**If you bought books or series in the Apple Books Store,** please also check
how 1.10's owned-books rule treats your library. This prints a count-only
table of book rows by data source, redownload flag, content type and state,
to the terminal only (no titles, ids, paths or account ids):

```sh
python -m py_apple_books.testing.dump_schema --census
```

`hidden_by_1.10_rule` counts the rows the book lists leave out. If it
includes books you own, please open an issue with the table.

## Deprecations

Nothing is removed in 1.x. These go in 2.0:

| What | In 1.10 | In 2.0 |
|------|---------|--------|
| `limit` ≤ 0 | Means all rows, with a `DeprecationWarning` | Raises; pass `None` for all rows |
| `db.client.find_sqlite_file` | Kept; for `BKLibrary`/`AEAnnotation` folders it uses the new store discovery | Removed; use `locate_store` or `LibraryDB().paths()` |
| Legacy SQL renderers: `Query.select`, `str(Where(...))` | Kept for debugging; the library never executes them | Removed; use `Query.compile` and `Clause.to_sql()` (parameterized) |
| `AppleBooksDBClient.conn` / `.cursor` | Lazy compatibility properties with a `DeprecationWarning` | Removed; use `LibraryDB.open_connection()` or `LibraryDB.execute()` |
| `DBClient`, `utils.get_mappings`, `Model.to_db`/`save`, `Clause._escape_like` | Kept, unused | Removed |
| `get_book_content` raising a bare `IndexError` | Kept for apple-books-mcp 0.8.2 | Raises `BookNotFoundError` |

Superseded in 1.11, kept: these stay, unchanged and without a warning, and
nothing removes them in 1.x. New code should use their replacements.

| Method | Use instead |
|--------|-------------|
| `get_current_reading_location(book_id)`, `get_current_reading_chapter(book_id)` | `get_reading_position(book_id)`: the bookmark and its chapter, an inferred position when there is no bookmark, fractions and pages, and the reason when the book can't be read |
| `get_annotation_surrounding_text(annotation_id)` | `get_annotation_context(annotation_id)`: the same text as `str(ctx)`, plus the chapter, and an error that says why when there is no text |

Stricter already in 1.10:

- `Where(...)` still accepts any operator, but `to_sql()`, which every
  manager filter goes through, rejects one outside
  `py_apple_books.db.clause.OPERATORS` with `ValueError` (`BETWEEN`
  included: use `__gte`/`__lte`). `IN` with a string raises `TypeError`.
- `__in` binds one parameter per item, so a list longer than SQLite's
  bound-variable limit (32,766 by default since SQLite 3.32, 999 before)
  raises `DBQueryError`. Pass a `Subquery` for large id sets.
- `manager.compiler` is still an assignable attribute, and every model
  statement runs through it, but its `execute` now receives `(sql, params)`
  instead of literal SQL.

## Note for apple-books-mcp 0.8.2–0.9.0 users

apple-books-mcp 0.8.2 accepts any `py-apple-books>=1.9.1,<2` and 0.9.0 any
`py-apple-books>=1.10,<2`, so a fresh install picks up the newest 1.x
release.

### What 1.11 changes

On a library whose books are fully downloaded, nothing the MCP shows
changes. Elsewhere you may notice:

- **Books in iCloud**: the chapter, context and current-chapter tools report
  a book stored partly in iCloud as not downloaded ("Part of this book is
  stored only in iCloud. Open it in Apple Books to download it, then try
  again."), and a book Apple Books records as stored only in iCloud with the
  existing "stored in iCloud" message. The library never downloads such a
  book; open it in Apple Books first.
- **Unreadable text and dates**: a title, author or highlight that isn't
  valid UTF-8, or a date stored in a form that can't be read, no longer
  makes every list fail. Such text shows U+FFFD (�) in place of the invalid
  bytes, and such a date as missing. Error messages no longer quote such
  text.
- **Messages**: file names over 80 characters in content errors are
  shortened, and operating-system errors in them name only a file's base
  name, never its path.
- **Collection writes**: writes running at the same time share one backup,
  and a write that waits more than 15 s for another write's backup reports
  that the library is busy and changes nothing. New backups are readable by
  you only.
- **`--doctor`** (0.9.0): its NOTE line can list the new optional fields on
  older Books versions.
- Chapter lists are faster on repeat calls.

To keep 1.10, pin it: `uvx --with 'py-apple-books<1.11' apple-books-mcp`, or
in a Claude Desktop config
`"args": ["--with", "py-apple-books<1.11", "apple-books-mcp"]`.

<a id="note-for-apple-books-mcp-082-users"></a>

### What 1.10 changed (0.8.2 users coming from 1.9)

- **Book lists** leave out Apple Books Store series entries you don't own
  (series containers and unowned volumes), so total book counts can drop.
- **Reading status**: the in-progress, finished and unstarted lists no
  longer overlap and add up to the book list; the library stats agree with
  them.
- **Annotations** deleted in Apple Books, and empty deletion tombstones, no
  longer appear in lists, searches, counts or the "book no longer in
  library" group.
- **Searches** work with apostrophes (they used to fail), ignore case,
  accents, quote and dash style and line breaks, and treat `%` and `_`
  literally. Annotation searches take about 0.2 s longer per 10k
  annotations.
- **Color and text searches** show the newest matches first, so a limited
  call returns recent highlights instead of old ones from removed books.
- **'Last Read'** and the recently-read order use the later of the
  last-opened and last-engaged dates.
- **Messages**: an unknown highlight color lists the valid ones; a chapter
  that isn't found no longer mentions `list_chapters()`; content tools on an
  unowned Store series entry say it isn't in your library.
- **Missing or protected library**: the server starts even without Books
  data, and each tool then reports that no library was found. Without Full
  Disk Access, the error says so. Without an annotation store, book and
  collection tools still work.
- **Collection writes**: a busy library or a read-only store gives a
  "nothing was changed" message instead of a raw SQLite error, and a changed
  collection schema refuses the write ("Write aborted for safety") instead
  of writing.
- Many tools run faster (far fewer SQL statements), and every query stops
  after 30 s (`APPLE_BOOKS_QUERY_TIMEOUT` changes that).

apple-books-mcp 0.8.2 has no switch for the old behavior. To keep 1.9,
pin it: `uvx --with 'py-apple-books<1.10' apple-books-mcp`, or in a Claude
Desktop config `"args": ["--with", "py-apple-books<1.10", "apple-books-mcp"]`.
Code using the library directly can ask for the old results per call:
`include_store_series=True`, `include_deleted=True`, or a chosen `order_by`
(`None` for storage order,
`get_recently_read_books(order_by='-last_opened_date')`).

## Development

### Installation

```bash
uv sync                              # creates .venv with the dev group
# or, with pip >= 25.1:
pip install -e . --group dev
```

The test tools are the `dev` dependency group (`pytest`, `coverage`,
`anyio`). The old `[dev]` extra is gone: `pip install -e '.[dev]'` installs
only the runtime dependencies. With an older pip, upgrade it or install the
group's requirements by hand.

### Running the tests

```bash
python -m pytest -q
```

The suite never opens your Apple Books library and doesn't need one. It
removes your `APPLE_BOOKS_*` settings and points `HOME` at a synthetic library
built from the committed schema fixture (`py_apple_books.testing`), for the
whole run. It covers the read path in the call shapes apple-books-mcp 0.8.2
uses, schema drift, threads and deadlines, SQL binding (with a fuzzer:
`APPLE_BOOKS_FUZZ_ITERATIONS`, 300 by default), collection writes, backups
and restore, and content access against generated EPUB fixtures.

The ranked-search index notices a change to the stores within about a
second. A test that changes a store and searches again at once should
search a new library, or call `close()` on the instance first.

Opt-in, read-only check against your own library (prints only counts and
exception type names; it reads only books stored as a folder that is wholly
on disk, and turns off downloads of iCloud-only files for its own process):

```bash
APPLE_BOOKS_LIVE_TESTS=1 python -m pytest -q -s tests/test_live_library.py
```

### Testing helpers

`py_apple_books.testing` (provisional API, standard library only) builds
synthetic libraries for your own tests: `FixtureLibrary` creates Apple Books
stores from the committed schema fixture, and `seed_demo` fills them with a
small demo library. New in 1.11:

- `write_epub_bundle(dest, files, toc=(), *, title, author, language,
  identifier, nav, opf_dir, extra_items, spine_xml, guide, landmarks,
  metadata_xml)`: unzipped EPUB bundles in real-world shapes (the package
  document in any folder, a nav document, an NCX, both or neither,
  non-linear or malformed spine entries, other media types), byte for byte
  the same on every run, for content tests.
- `FixtureLibrary.add_series`, `write_prefs` / `prefs_path`,
  `add_book_info_cache` / `book_info_dir`, the `add_book` keywords
  `finished_date=` and `last_engaged=`, the `add_annotation` keywords
  `user_data=`, `position_fraction=` and `furthest_fraction=`, and
  `page_location_blob` and `YEAR_ZERO`.

Call `py_apple_books.content.clear_content_cache()` between tests that
rewrite a book folder in place: chapter lists, spines and book metadata are
cached process-wide by file metadata.

### apple-books-mcp compatibility

`tests/mcp_compat/run.py` drives a released apple-books-mcp over stdio
against a demo library and compares every read tool's output with the
committed goldens:

```bash
python tests/mcp_compat/run.py --mcp-version 0.8.2 --check                  # via uvx, Python 3.12
python tests/mcp_compat/run.py --mcp-version 0.9.0 --check
python tests/mcp_compat/run.py --mcp-version 0.8.2 --uvx-python 3.14 --lib dist/py_apple_books-*.whl --check
python tests/mcp_compat/run.py --mcp-version latest                          # no goldens; fails on crashes
```

`--update` rewrites the goldens. The golden rule: update them only for an
intended change to what MCP users see, and map every changed file to that
change in the pull request.

### Real-library regression (maintainers)

`scripts/mcp_regress/` compares apple-books-mcp 0.8.2's or 0.9.0's output
for every read tool between the released 1.10.0 and a candidate, on a
read-only snapshot of a real library, and checks each difference against
rule-based expectations (none are expected on a fully downloaded library);
[its README](https://github.com/vgnshiyer/py-apple-books/blob/main/scripts/mcp_regress/README.md)
has the procedure. Its outputs hold private library data: keep them outside
the repository, and report only the totals.

### Building

```bash
uv build
python scripts/check_dist.py --baseline-wheel <previous release wheel>
```

To release, set `__version__` in `py_apple_books/__init__.py` and publish a
GitHub release tagged `v<__version__>`.

## Upcoming Features

- [x] Adding a book to collection
- [x] Removing a book from collection
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
