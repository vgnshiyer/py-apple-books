# Changelog

## 1.10.0 (2026-09-30)

1.10 rebuilds the read path. Queries bind their values as SQL parameters.
The library is found on first use and read through a pool of read-only
connections that works from any thread. Results are evaluated once, and
relations load lazily. The public API only grows: the new parameters of
the existing `PyAppleBooks` methods are keyword-only, and every new
exception keeps the built-in base 1.9 raised.

apple-books-mcp 0.8.2 accepts any `py-apple-books>=1.9.1,<2`, so its users
get 1.10.0 on their next fresh install. The changes they are likely to
notice are marked **(MCP)**, and the
[README](README.md#note-for-apple-books-mcp-082-users) sums them up.
Maintainer-library figures below are ratios; they differ from library to
library.

### Behavior changes

- **(MCP)** Book lists and searches leave out Apple Books Store series
  entries you don't own: series containers, and volumes of a series that
  have no redownload (ownership) flag. That was about 13% of the rows in the
  maintainer's library. It covers `list_books`, `get_book_by_title`,
  `get_books_by_genre`, the three reading-status lists and
  `get_recently_read_books`. `include_store_series=True` brings them back on
  the first three. `get_book_by_id` and the relations (`collection.books`)
  still resolve every row. If the store lacks a column the rule needs, the
  rows that column would hide are shown, as in 1.9.
- **(MCP)** `get_books_in_progress`, `get_finished_books` and
  `get_unstarted_books` follow one rule, finished first. A book marked
  finished is only finished, whatever its progress. Otherwise it is in
  progress above 0%, and unstarted at 0% or with no progress recorded (1.9
  dropped those). The three lists no longer overlap, and together they are
  `list_books()`.
- **(MCP)** Annotation lists, searches and `book.annotations` leave out
  annotations deleted in Apple Books and the empty tombstones Books keeps for
  iCloud sync (about 1% of the rows in the maintainer's library).
  `include_deleted=True` restores them on the six annotation list and search
  methods. User bookmarks stay. `get_annotation_by_id` still returns any
  row.
- **(MCP)** Text searches match through a fold on both sides. They ignore
  case (non-ASCII included), Latin accents, quote, dash and prime style,
  ligatures and other compatibility forms, zero-width characters and runs of
  whitespace. So `don't` finds `don’t`, `Godel` finds `Gödel` and a
  highlight's own text, line breaks and all, finds it. This covers
  `get_book_by_title`, `get_collection_by_title`, `get_books_by_genre` and
  the three annotation text searches. A needle whose visible characters all
  fold away finds nothing.
- **(MCP)** `%` and `_` in a search match themselves; they used to be LIKE
  wildcards.
- **(MCP)** `get_annotations_by_color` and the three annotation text searches
  are newest first by default (`order_by='-creation_date'`). With a `limit`
  they return the most recent matches instead of the oldest rows, which were
  mostly orphans of removed books. Unlimited calls return the same rows,
  reordered. Pass `order_by=None` for the 1.9 storage order.
  `get_annotations_by_date_range` still has no default order.
- **(MCP)** `get_recently_read_books` orders by the new
  `Book.last_read_date`, the later of the last-opened and last-engaged
  dates. The last-opened date alone goes stale while a book stays open.
  `order_by='-last_opened_date'` gives the 1.9 order. MCP 0.8.2's
  'Last Read' shows the same date (`Book.format_progress_summary`).
- **(MCP)** Not-found and bad-input errors are typed, and some messages
  change. `get_book_by_id` raises `BookNotFoundError` ("No book with id
  N."), `get_annotation_by_id` raises `AnnotationNotFoundError`, and both
  are still `IndexError`s. An unknown color raises `InvalidChoiceError`
  ("Unknown highlight color 'orange'. Valid colors: …"), still a `KeyError`.
  An unknown chapter id raises `ChapterNotFoundError`, still an
  `AppleBooksError`, and its message no longer names `list_chapters()`.
  `get_book_content` keeps raising a bare `IndexError` for an unknown id.
- **(MCP)** `get_book_content` on an unowned Store series entry without a
  file raises `NotInLibraryError`, a `BookNotDownloadedError`, which says
  there is no book file to read instead of suggesting a download.
- **(MCP)** Odd arguments no longer fail. A `limit` beyond SQLite's integer
  range returns every row instead of raising `DBQueryError`. A search with a
  lone surrogate or a NUL character no longer raises. Ids beyond that range
  are not found, as before. `limit=-1` on `search_annotation_by_text` no
  longer drops the last match.
- **(MCP)** Annotation text searches fold in Python, which costs about 0.2 s
  per 10k annotations. A limited search now sorts every match, and MCP 0.8.2
  then formats recent annotations of books still in the library, so
  limited color and search calls take longer through the MCP.
- **(MCP)** Every statement stops after 30 s by default with
  `QueryTimeoutError`. See "Changed" for how to raise or disable the limit.
- `DBError` is now an `AppleBooksError`. An `except AppleBooksError` block
  now also catches database errors.
- Writes resolve their target store strictly and refuse to guess: see
  "Changed".

### Added

- `PyAppleBooks(data_dir=None, *, library_db=None, annotation_db=None,
  query_timeout=...)` reads a library of its own: a Books Documents folder,
  or the store files. Its results and their relations keep reading that
  library. Construction does no I/O.
- `PyAppleBooks.close()` and `PyAppleBooks.query_deadline(seconds)`.
- `PyAppleBooks.store_info()`: the store files in use, the other `*.sqlite`
  files next to them, the mapped columns this Apple Books version lacks, the
  SQLite version, the query timeout and the backup folder.
- Counts in SQL: `count_annotations(book_id=None)`, `count_books_by_status()`
  and `get_library_stats() -> LibraryStats` (exported from
  `py_apple_books`). The whole library takes five statements, and each count
  equals the length of the list it counts.
- `offset=` on every list and search method. `limit=`, `order_by=` and
  `offset=` on `get_book_by_title` and `get_collection_by_title`.
  `include_store_series=` on `list_books`, `get_book_by_title` and
  `get_books_by_genre`. `include_deleted=` on `list_annotations`,
  `get_annotations_by_color`, the three annotation text searches and
  `get_annotations_by_date_range`. All keyword-only.
- Environment variables, read on first use, never at import:
  `APPLE_BOOKS_DATA_DIR`, `APPLE_BOOKS_LIBRARY_DB`,
  `APPLE_BOOKS_ANNOTATION_DB` (which stores `PyAppleBooks()` reads),
  `APPLE_BOOKS_QUERY_TIMEOUT` and `APPLE_BOOKS_MODEL_CHECK`. Precedence:
  constructor arguments always win. The three location variables apply only
  to a library built without `data_dir`, `library_db` and `annotation_db`
  (`PyAppleBooks()`, and the collection writes and `write_safety` defaults
  that go with it). `APPLE_BOOKS_QUERY_TIMEOUT` applies to any library built
  without `query_timeout`. The default library keeps the stores and the
  timeout it found on first use for the rest of the process (it finds the
  stores again only when a store file is replaced).
- `ModelIterable`: `count()`, `exists()`, `first()`, `count_by(field)`,
  slicing (returns a list), `bool()`, a `repr()`, and typing as
  `ModelIterable[Book]` and so on. `Model.manager` gains `count(**filters)`,
  `has_fields(*fields)` and `required_fields`; `filter()` takes `offset=`
  and `where=Q(...)`, and `all()` takes `offset=`.
- Lookups `__is`, `__isnot`, `__notin`, `__search` (folded match) and
  `__not_<lookup>`. `__in` takes any iterable or a `Subquery`. `order_by`
  takes `'a,-b'` or a list. `only=` works with field or column names.
- `Book` fields `store_id`, `data_source`, `can_redownload`, `state` and
  `last_engaged_date`. `Book` properties `last_read_date`, `reading_status`,
  `is_series_container`, `is_store_series_item`, `is_cloud_only` and
  `deep_link`. `Annotation` fields `uuid` and `position`, and
  `Annotation.deep_link`. The enums `ReadingStatus` and `AnnotationType`
  (in `py_apple_books.models`). `Location.spine_index` and
  `Location.sort_key` put annotations in reading order without opening the
  book. The account-identifying purchaser id is deliberately not mapped.
- Models can be pickled and copied.
- `py_apple_books.db`: `LibraryDB`, `StorePaths`, `locate_store`,
  `default_library`, `current_library`, `use_library`, `query_deadline`,
  `USE_DEFAULT`, `DEFAULT_QUERY_TIMEOUT` (30.0), `CompiledQuery`,
  `Query.compile`, `Query.count` and `adapt_params`. In `db.clause`: `Q`,
  `WhereGroup`, `Not`, `Subquery`, `escape_like` and `Where.to_sql`. In
  `db.metadata`: `read_store_metadata`, `StoreMetadata` and
  `read_only_uri`.
- `py_apple_books.text.fold_for_match`, the fold the searches use.
- Exceptions: `NotFoundError`, the new base of `BookNotFoundError` and
  `CollectionNotFoundError` (both in 1.9, raised by the writes;
  `get_book_by_id` and `count_annotations(book_id)` now raise
  `BookNotFoundError` too). Also `AnnotationNotFoundError` (an
  `IndexError`, like those two), `ChapterNotFoundError`,
  `InvalidArgumentError` (a `ValueError`), `InvalidChoiceError` (a
  `KeyError`), `UnknownFieldError`, `NotInLibraryError`,
  `LibraryNotFoundError`, `AnnotationStoreNotFoundError`,
  `LibraryAccessDeniedError`, `UnsupportedSchemaError`, `QueryTimeoutError`,
  `BackupValidationError`, `LibraryBusyError` (also a
  `sqlite3.OperationalError`) and `AmbiguousStoreError`. The tree is in the
  README.
- Writes: `write_safety.verify_backup`, `list_backups`, `check_model_hashes`,
  `resolve_model_check`, `MODEL_CHECK_ENV`, and
  `validate_table_columns(..., allow_extra_nullable=False)`.
  `restore_library`'s `db_path` is optional (the Books library by default),
  it gains keyword-only `force`, `snapshot` and `backup_dir`, and it returns
  the pre-restore snapshot's path (it returned None).
  `WriteSession(..., model_check=None)` and `collection_writer.BUSY_TIMEOUT`.
- `py_apple_books.testing` (provisional API, standard library only):
  `FixtureLibrary` builds synthetic Apple Books stores from a committed,
  schema-only fixture. `seed_demo` fills them with a small demo library.
  `python -m py_apple_books.testing.dump_schema` writes a privacy-safe
  schema fixture from your library (`--compare`, and `--census` for a
  count-only table of book rows).

### Changed

- **Timeouts.** Every statement has a 30 s limit (`DEFAULT_QUERY_TIMEOUT`).
  Raise or disable it with `PyAppleBooks(query_timeout=...)` (`None` or `0`
  disables it) or `APPLE_BOOKS_QUERY_TIMEOUT` (seconds; `0`, `none` or
  `off` disable it). `query_deadline(seconds)` shortens it for a block.
  Waiting for a pooled connection counts against the limit.
- **(MCP)** **Store discovery.** The stores are found on first use, not at
  import. Apple's canonical file is used when its Core Data metadata shows
  it is an Apple Books store. A `… copy.sqlite` that sorts first is no
  longer read, and a replaced store file is picked up by the next call. A
  missing annotation store leaves books and collections readable and raises
  `AnnotationStoreNotFoundError` for annotation queries.
- **Schema drift.** A mapped column missing from the store (an older or
  newer Apple Books) reads as None. Filtering or sorting on it raises
  `UnsupportedSchemaError`. Only a model's id and asset id columns (a
  collection's id) are required.
- **Evaluated once.** A `ModelIterable` runs its query once; `len()`,
  iteration, indexing and `bool()` share the rows. 1.9 re-ran the query on
  every use. Call the method again for fresh rows.
- **Lazy relations.** `book.annotations`, `collection.books` and
  `book.collections` query on access. `annotation.book` loads the books of a
  whole result at once. `from_db` no longer loads relations, and
  `run_query` is now a method (`run_query.args` is gone).
- **Deprecated: `AppleBooksDBClient.conn` and `.cursor`.** They are lazy
  compatibility properties that emit a `DeprecationWarning`; 2.0 removes
  them. `AppleBooksDBClient` runs every query on the current `LibraryDB`.
- **Deprecated: `limit` ≤ 0** still means all rows, but warns; 2.0 raises.
- **Stricter: `Where.to_sql()`** accepts only the operators in
  `db.clause.OPERATORS` (`GLOB` and `NOT GLOB` are new) and raises
  `ValueError` for any other, `BETWEEN` included (use `__gte`/`__lte`).
  Constructing such a `Where` and `str()` still work. The manager's filters
  compile through `to_sql()`.
- **Stricter: `__in` with a string** raises `TypeError` when the query
  compiles; 1.9 pasted the string into the SQL. Pass a list or a
  `Subquery`. `__in` binds one parameter per item, so a list longer than
  SQLite's bound-variable limit (32,766 by default since SQLite 3.32, 999
  before; some builds allow more) raises `DBQueryError` ("too many SQL
  variables"); 1.9 wrote the items into the SQL. Pass a `Subquery` for
  large id sets. The library's own relations stay under the limit.
- **Stricter arguments.** An unknown field in a filter, `order_by`, `only=`
  or `has_fields` raises `UnknownFieldError` (a `KeyError`, as 1.9 raised,
  and a `ValueError`). A `limit` that isn't integral raises
  `InvalidArgumentError`; 1.9 pasted it into the SQL, which usually raised
  `DBQueryError`. Numeric strings (`'3'`) and integral floats are still
  accepted as a `limit`. An `offset` that isn't an `int`, or a negative
  one, raises `InvalidArgumentError` too. A `date` filter value raises
  `DBQueryError`, as a `datetime` already did. 1.9 wrote a `date` into the
  SQL as arithmetic (`2000-01-01` became 1998), so such a filter matched
  every row or none. Pass Core Data seconds, as
  `get_annotations_by_date_range` does.
- **`manager.compiler.execute`** receives `(sql, params)`; it received
  literal SQL. `manager.compiler` is still an assignable attribute, and every
  model statement goes through it.
- **`__contains`** is a literal substring match (ASCII case-insensitive,
  as before); `%`, `_` and NUL are ordinary characters.
- **Writes.** They go to the library store the instance reads, resolved
  strictly. Several candidate stores with no canonical one, a canonical
  store that can't be read, only a copy or backup, or a store other than
  the one being read raise `AmbiguousStoreError`. `PyAppleBooks()` follows
  `APPLE_BOOKS_LIBRARY_DB` and `APPLE_BOOKS_DATA_DIR` for writes too.
- **(MCP)** **Write checks (F37).** Before each write the collection and
  membership tables must have exactly the columns and declared types this
  version was verified against, and the check runs inside the write
  transaction. The Core Data model version of both entities is compared
  with the verified one. `APPLE_BOOKS_MODEL_CHECK=warn` (the default) logs
  a difference and writes, `enforce` refuses, and `off` skips that check
  and also allows new nullable columns (left empty). Missing or retyped
  columns always refuse. A refusal raises `SchemaValidationError`, which
  MCP 0.8.2 reports as "Write aborted for safety". macOS 27 is unverified.
- **Backups per store.** The current user's library still backs up into
  `~/.py_apple_books/backups/`. Any other store (named by `data_dir`,
  `library_db` or the location variables) backs up into a folder of its
  own under `libraries/` there (`store_info().backup_dir`). A store's backups
  are matched by their whole name. `list_backups()` and `restore_library()`
  without `db_path` follow the same rule.
- **Restore (F45).** `restore_library` checks the backup first
  (`verify_backup`: integrity, same store, same data model) unless
  `force=True`. It snapshots the current library, and the snapshot is never
  reused as a pre-write backup. It prunes old backups but keeps the one
  restored. Over a damaged library, `force=True` or `snapshot=False` without
  `db_path` targets the canonical store file even if it can't be read. The
  restore goes through SQLite, so a file SQLite rejects outright
  (overwritten or truncated) still has to be replaced by hand while Books
  is quit.
- **Packaging.** Builds from `pyproject.toml` (PEP 621 and PEP 639), with
  `License-Expression: MIT` and the `Programming Language :: Python :: 3 ::
  Only` classifier. The runtime dependencies are unchanged. The `[dev]`
  extra is replaced by the `dev` dependency group
  (`pip install -e . --group dev`, pip ≥ 25.1, or `uv sync`). The wheel adds
  `text.py`, `db/metadata.py` and `testing/` with its schema fixture.
- **Releases.** Built and tested in a job without publishing credentials,
  then uploaded by a separate job in the `pypi` environment that can
  authenticate with PyPI Trusted Publishing (OIDC) and attach PEP 740
  attestations. Until the maintainer registers the publisher on pypi.org and
  deletes the `PYPI_API_TOKEN` secret, uploads keep using the token. The
  release tag must be `v` + `__version__` (for example `v1.10.0`).
- **CI.** Tests on macOS with Python 3.10 to 3.14 (all blocking), plus the
  newest 3.15 pre-release for information. Also a lowest-dependency run,
  wheel and sdist checks against 1.9.1, a workflow security lint, and
  apple-books-mcp 0.8.2 and latest driven over stdio against the built
  wheel and a synthetic library. Actions are pinned by commit SHA.
  Dependabot also updates GitHub Actions.

### Fixed

- **(MCP)** Importing `py_apple_books` opened the library. With no Books
  data (a fresh Mac, CI, Docker) or no Full Disk Access, it failed at
  import, and so did `apple-books-mcp --help`. Imports now do no I/O. On
  first use the errors are typed: `LibraryNotFoundError` names
  `APPLE_BOOKS_DATA_DIR`, and `LibraryAccessDeniedError` gives a Full Disk
  Access hint.
- Queries from another thread (`threading`, `anyio.to_thread`, mcp 2.x)
  failed with "SQLite objects created in a thread…". Connections are pooled
  per library and usable from any thread; one process holds far fewer
  SQLite file handles. Threads waiting for a connection are served first
  come first served, so under heavy load none waits until its deadline.
- The store lookup took the first `*.sqlite` by name, so a stale copy could
  silently replace the live library for reads and collection writes.
- A store file replaced while the process ran was read stale until
  restart.
- **(MCP)** One missing column broke most read tools; it now reads as None.
- **(MCP)** The ORM loaded relations eagerly, one or two statements per row,
  so MCP 0.8.2's `get_library_stats`, `describe_book` and annotation lists
  ran about two statements per annotation. `get_library_stats` now runs
  three statements (one more per 500 books the annotations point to) and
  `describe_book` two, and `annotation.book` loads the books of a whole
  result at once.
- `ModelIterable` slicing raised `TypeError`, `only=` raised, and `list()`
  ran the query twice.
- The write schema check couldn't detect Core Data model drift (Apple's
  store declares no NOT NULL columns). The exact column check and the model
  hash check above catch it.
- **(MCP)** Writes: lock contention past 5 s raises `LibraryBusyError`
  ("…busy…; nothing was changed") instead of a raw "database is locked". So
  does a commit blocked by another program's read. A read-only store raises
  `WriteError`. A store that vanished between resolution and the write is no
  longer created as an empty file. Books opened while the backup was taken
  is noticed (`BooksAppRunningError`). Drift in the tables the writer only
  reads raises `SchemaValidationError` before the write starts.
- A backup of a store whose name extends another's (`BKLibrary-1.sqlite`)
  could be reused or pruned as the other store's backup.
- `search_annotation_by_text` over-fetched, filtered in Python and dropped
  the last match under `limit=-1`. It is one SQL query now.
- `search_annotation_by_text` kept annotations whose type is NULL; it now
  uses the same scope as the other annotation searches.
- Reading-status corner cases: finished books at under 100% showed as in
  progress or unstarted, and books with no recorded progress were in no
  list.
- README: `BookContent.get_chapter` was documented as
  `get_chapter_content`, and a `chapter_at_cfi` method that doesn't exist was
  listed. Every Python example in the README now runs against a synthetic
  library.

### Security

- Every read query binds its values as parameters; only column names from
  the mapping file are written into SQL. Before, crafted ids and search
  strings could change a query (`get_book_by_id("1' OR '1'='1")` returned a
  book, and a crafted collection search listed deleted collections). UNION
  and recursive-CTE payloads are plain text now. Values SQLite can't bind
  are adapted: an integer beyond 64 bits matches nothing, as 1.9's literal
  did, and lone surrogates become U+FFFD.
- Read connections are opened read-only and also set `query_only`.
- Release builds run without publishing credentials, the `pypi` job has only
  `id-token: write`, workflow tokens are read-only, and checkouts don't keep
  credentials.

### Upgrading from 1.9

Most code needs no change. Check these:

- Lists hide unowned Store series entries and deleted annotations, the
  status lists partition the library, and color and text searches are
  newest first. Use `include_store_series=True`, `include_deleted=True` or
  pass `order_by` (`None` for storage order,
  `'-last_opened_date'` for the old recency order) where you relied on the
  old results.
- `except AppleBooksError` now also catches `DBError`. Put
  `except DBError` first if database failures must propagate.
- Results no longer re-query. Call the method again to see new data. Code
  that read `obj.__dict__['annotations']` after `from_db`, or
  `run_query.args`, needs updating.
- Paging: pass `offset=` (`offset=0` for the first page) for a stable
  primary-key order across pages, or slice a result.
- `limit` ≤ 0 warns; pass `None` for all rows.
- Custom `manager.compiler` replacements receive `(sql, params)`.
- Filters with a `date` value, a string for `__in`, or a `Where` operator
  outside `OPERATORS` now raise; 1.9 wrote them into the SQL, where a
  `date` became arithmetic and matched every row or none. An `__in` list
  longer than SQLite's bound-variable limit (32,766 since SQLite 3.32, 999
  before) raises `DBQueryError`; pass a `Subquery` for large id sets. A
  `limit` that isn't integral raises `InvalidArgumentError` instead of
  `DBQueryError`.
- Set the location and timeout variables before the first query: the
  default library keeps what it found then.
- Long queries stop after 30 s; set `query_timeout=None` or
  `APPLE_BOOKS_QUERY_TIMEOUT=0` for batch jobs that need longer.
- Errors surface on first use instead of at import.
- If you set `APPLE_BOOKS_DATA_DIR`, `APPLE_BOOKS_LIBRARY_DB` or
  `APPLE_BOOKS_ANNOTATION_DB` for another purpose, `PyAppleBooks()` now
  reads (and writes) the stores they name.
- Writes refuse ambiguous stores with `AmbiguousStoreError`, and a
  read-only store raises `WriteError`, which isn't a `sqlite3` error.
  `LibraryBusyError` is still a `sqlite3.OperationalError`.
- `pip install -e '.[dev]'` no longer installs the test tools; use
  `pip install -e . --group dev` (pip ≥ 25.1) or `uv sync`.

### If 1.10 breaks your setup

- Pin the previous release: `pip install 'py-apple-books<1.10'`. For
  apple-books-mcp run through uvx, add the pin to its arguments:
  `uvx --with 'py-apple-books<1.10' apple-books-mcp` (in a Claude Desktop
  config: `"args": ["--with", "py-apple-books<1.10", "apple-books-mcp"]`).
- Please [open an issue](https://github.com/vgnshiyer/py-apple-books/issues).
  A fix ships as 1.10.x. uvx keeps the environment it resolved, so after a
  fix, drop the pin and refresh it once with `uvx apple-books-mcp@latest`
  (or `uv cache clean`).

## Earlier releases

See the [GitHub releases](https://github.com/vgnshiyer/py-apple-books/releases).
