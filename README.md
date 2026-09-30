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

Upgrading from 1.9? See the [changelog](https://github.com/vgnshiyer/py-apple-books/blob/main/CHANGELOG.md), and the
[note for apple-books-mcp users](#note-for-apple-books-mcp-082-users).

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

The variables are read when the library is used, never at import.

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

## Available Functions

New parameters since 1.9 are keyword-only.

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

### Reading Progress

| Function | Description | Parameters | Return Type |
|----------|-------------|------------|-------------|
| `get_books_in_progress()` | Books not marked finished, with progress above 0% | `limit?`, `order_by?`, `offset?` | ModelIterable |
| `get_finished_books()` | Books marked as finished, whatever their progress | `limit?`, `order_by?`, `offset?` | ModelIterable |
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
| `get_annotations_by_date_range(after?, before?)` | Filter annotations by creation date | `after?: datetime`, `before?: datetime`, `limit?`, `order_by?`, `offset?`, `include_deleted?` | ModelIterable |

### Counts and library info (v1.10.0+)

| Function | Description | Return Type |
|----------|-------------|-------------|
| `count_books_by_status()` | The lengths of the three reading-status lists | `dict[ReadingStatus, int]` |
| `count_annotations(book_id?)` | The length of `list_annotations()`, or of one book's annotations | int |
| `get_library_stats()` | Book and annotation totals, orphans, and annotations per book, in five statements | LibraryStats |
| `store_info()` | The store files in use, other `*.sqlite` files next to them, mapped columns this Books version lacks, the SQLite version, the query timeout and the backup folder | StoreInfo |
| `query_deadline(seconds)` | A context manager that stops queries still running after `seconds` | context manager |
| `close()` | Close the idle connections (the next call reconnects) | None |

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
| `get_chapter(chapter_id)` | Plain text of a chapter, scoped to its fragment anchor | `str` |

`BookContent` properties:

| Property | Description |
|----------|-------------|
| `is_epub` | True if the path is an EPUB bundle directory |
| `is_pdf` | True if the path is a single PDF file |
| `is_downloaded` | True if locally materialized (not an iCloud placeholder) — does not trigger hydration |
| `is_drm_protected` | True if the EPUB carries FairPlay `META-INF/sinf.xml`, Adobe `rights.xml`, or an `encryption.xml` that encrypts more than fonts (font obfuscation alone doesn't count; an unparseable file does) |

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
| `get_recently_read_books` | `'-last_read_date'`: the later of last opened and last engaged, newest first (sorted in Python) |
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

## Exceptions

Every exception derives from `py_apple_books.exceptions.AppleBooksError`.
The new ones keep the built-in base 1.9 raised, so `except IndexError`,
`except KeyError` and `except sqlite3.OperationalError` handlers written for
1.9 still work.

| Exception | Also a | When |
|-----------|--------|------|
| `BookNotFoundError` | `IndexError`, `WriteError` | `get_book_by_id`, `count_annotations(book_id)` or a write: no book has that id |
| `CollectionNotFoundError` | `IndexError`, `WriteError` | No (live) collection has that id |
| `AnnotationNotFoundError` | `IndexError` | `get_annotation_by_id`: no annotation has that id |
| `ChapterNotFoundError` | | No chapter or spine entry has that id |
| `InvalidArgumentError` | `ValueError` | A bad argument, such as a negative `offset` |
| `InvalidChoiceError` | `KeyError`, `ValueError` | A value outside a fixed set, such as an unknown highlight color (`.value`, `.valid`) |
| `UnknownFieldError` | `KeyError`, `ValueError` | A model has no such field (in a filter, `order_by` or `only=`) |
| `BookNotDownloadedError` | | The book has no local file (never downloaded) **or** the file is an iCloud placeholder |
| `NotInLibraryError` | `BookNotDownloadedError` | The row is an unowned Store series entry with no file |
| `DRMProtectedError` | | The book is DRM-protected — usually an Apple Books Store purchase (FairPlay), occasionally an encrypted imported EPUB |
| `UnsafeEpubEntryError` | | A file in the EPUB bundle resolves outside it (absolute/`../` href, symlink), isn't a regular file, or is implausibly large; the book is refused |
| `DBError` | | Base of the database errors (an `AppleBooksError` since 1.10) |
| `LibraryNotFoundError` | `DBConnectionError` | No Apple Books store found, or the file isn't one (`.path`) |
| `AnnotationStoreNotFoundError` | `LibraryNotFoundError` | An annotation query without an annotation store |
| `LibraryAccessDeniedError` | `DBConnectionError` | macOS refused access: grant Full Disk Access to the app running your code (`.path`) |
| `UnsupportedSchemaError` | `DBQueryError` | The store lacks a column or table a query needs (`.table`, `.column`) |
| `QueryTimeoutError` | `DBQueryError` | A query ran past its deadline (`.timeout`) |
| `WriteError` | | Base of the write errors |
| `BooksAppRunningError` | `WriteError` | A write or restore while Books is running |
| `SchemaValidationError` | `WriteError` | The collection tables don't match the verified schema |
| `SystemCollectionError` | `WriteError` | An edit to a built-in collection |
| `LibraryBusyError` | `WriteError`, `sqlite3.OperationalError` | Another program held the library's lock too long; nothing was changed |
| `AmbiguousStoreError` | `WriteError`, `DBConnectionError` | A write can't tell for sure which store Books uses |
| `BackupValidationError` | `WriteError` | A backup failed a pre-restore check (`.reason`) |

`get_book_content` raises a bare `IndexError`, not an `AppleBooksError`,
for an unknown book id throughout 1.x: apple-books-mcp 0.8.2 catches
`AppleBooksError` before `IndexError` around it. 2.0 will raise
`BookNotFoundError`.

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
(WAL-inclusive). Writes within 5 minutes of each other share the backup
taken before the first of them, and the newest 10 backups of each store are
kept. Backups of the current user's library go to
`~/.py_apple_books/backups/`. Any other store (named by `data_dir`,
`library_db` or the location variables) gets a folder of its own under
`~/.py_apple_books/backups/libraries/`; `store_info().backup_dir` names it.

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
else the current user's store). To restore over a damaged library, pass
`force=True`: the target is then the canonical store file even if it can't
be read. For another store, pass `db_path=`; its backups are looked up in
its own folder.

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
    ch = api.get_current_reading_chapter(book.id)
    if ch is None:
        continue
    content = api.get_book_content(book.id)
    text = content.get_chapter(ch.id)
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
your schema. This writes about 11 KB of SQL describing the database layout
and holds no library contents (no titles, highlights, paths or account ids;
the output is checked for that before it's saved):

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

Stricter already in 1.10:

- `Where(...)` still accepts any operator, but `to_sql()`, which every
  manager filter goes through, rejects one outside
  `py_apple_books.db.clause.OPERATORS` with `ValueError` (`BETWEEN`
  included: use `__gte`/`__lte`). `IN` with a string raises `TypeError`.
- `manager.compiler` is still an assignable attribute, and every model
  statement runs through it, but its `execute` now receives `(sql, params)`
  instead of literal SQL.

## Note for apple-books-mcp 0.8.2 users

apple-books-mcp 0.8.2 accepts any `py-apple-books>=1.9.1,<2`, so a fresh
install picks up 1.10. What you may notice:

- **Book lists** leave out Apple Books Store series entries you don't own
  (series containers and unowned volumes): about 13% of the rows in the
  maintainer's library. Total book counts drop accordingly.
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

Opt-in, read-only check against your own library (prints only counts and
exception type names):

```bash
APPLE_BOOKS_LIVE_TESTS=1 python -m pytest -q -s tests/test_live_library.py
```

### apple-books-mcp compatibility

`tests/mcp_compat/run.py` drives a released apple-books-mcp over stdio
against a demo library and compares every read tool's output with the
committed goldens:

```bash
python tests/mcp_compat/run.py --mcp-version 0.8.2 --check                  # via uvx, Python 3.12
python tests/mcp_compat/run.py --mcp-version 0.8.2 --uvx-python 3.14 --lib dist/py_apple_books-*.whl --check
python tests/mcp_compat/run.py --mcp-version latest                          # no goldens; fails on crashes
```

`--update` rewrites the goldens. The golden rule: update them only for an
intended change to what MCP users see, and map every changed file to that
change in the pull request.

### Real-library regression (maintainers)

`scripts/mcp_regress/` compares apple-books-mcp 0.8.2's output for every
read tool between the released 1.9.1 and a candidate, on a read-only
snapshot of a real library, and checks each difference against rule-based
expectations; [its README](https://github.com/vgnshiyer/py-apple-books/blob/main/scripts/mcp_regress/README.md)
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
