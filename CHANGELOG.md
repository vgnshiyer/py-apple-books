# Changelog

## 1.11.0 (unreleased)

The release candidate is `1.11.0rc1`. Installers only pick a pre-release
when asked for it by version (`pip install py-apple-books==1.11.0rc1`), so
requirements such as apple-books-mcp 0.8.2's `py-apple-books>=1.9.1,<2` and
0.9.0's `py-apple-books>=1.10,<2` keep resolving 1.10.0 until 1.11.0 is
out.

1.11 adds new ways to read a library: where you are in a book and in which
chapter each highlight sits, a highlight with its context, spoiler-safe
reading and search of a book's text, ranked annotation search, book
metadata and Store series, engagement features (a daily sample of
highlights, on this day, vocabulary, activity, streaks, reading goals) and
the titles of removed books. It also makes the read path safer: reading a
book never downloads a file macOS has moved to iCloud. The public API only
grows: every 1.10 method keeps its signature and its results on a library
whose books are fully downloaded, new parameters of existing methods are
keyword-only, and the new types live in their own modules (the top-level
`py_apple_books` namespace is unchanged).

apple-books-mcp 0.8.2 and 0.9.0 accept 1.11, so their users get 1.11.0 on
their next fresh install. On a library whose books are fully on disk, what
they see doesn't change. The changes they may notice elsewhere are marked
**(MCP)**, and the [README](README.md#note-for-apple-books-mcp-082090-users)
sums them up.

### Behavior changes

- **(MCP)** **No read downloads an iCloud file.** macOS can move books in
  iCloud Drive partly or wholly to iCloud. Every read of a book's files now
  checks each folder and file first and never looks inside one that is only
  in iCloud, and reads run with downloads of such files turned off for the
  reading thread (macOS only), so anything missed fails instead of being
  downloaded. Such reads raise `BookNotDownloadedError`: "Part of this book
  is stored only in iCloud. Open it in Apple Books to download it, then try
  again." This covers the chapter methods, `get_annotation_surrounding_text`
  and `get_current_reading_chapter`. Books that are fully on disk read
  exactly as before.
- **(MCP)** **`get_book_content()` refuses iCloud books up front.** A book
  Apple Books records as stored only in iCloud is refused before any of its
  files is touched, and a book whose file or folder is itself only in
  iCloud is refused before the download check runs, both with the existing
  "stored in iCloud" message. A book with only some of its files in iCloud
  is refused with the message above.
- **(MCP)** **Text that isn't valid UTF-8** in the Apple Books stores (a
  title, an author, a highlight) no longer makes every list or search that
  reads it fail with `DBQueryError`. It reads with U+FFFD (�) in place of
  the invalid bytes, and a warning is logged once per library (logger
  `py_apple_books.db`). Valid text reads exactly as before. Error messages
  no longer include the text of such a cell; they name the column only.
  Connections handed out by `LibraryDB.connection()` and
  `LibraryDB.open_connection()` still decode text as `sqlite3` does by
  default.
- **(MCP)** **Unreadable dates read as None.** A date Books stored in a form
  that can't be read (text, NaN, infinity, or far outside the range a date
  can hold) no longer makes every list containing that book or annotation
  fail. Valid dates convert exactly as before.
- **(MCP)** **Shorter, path-free error messages.** Messages about a book's
  files shorten the names of files inside the book, and the book's file
  name, when longer than 80 characters (to their first 60 and last 20
  characters), and operating-system errors in them name only a file's base
  name, never its path. Names of up to 80 characters read as before. Book
  titles in `get_book_content()` messages are unchanged.
- **(MCP)** **Concurrent writes share one backup.** Writes running at the
  same time, in several threads or several processes, share one pre-write
  backup. `backup_library` locks the backup folder while it checks for a
  recent backup, takes one and prunes old ones, so a burst of writes no
  longer takes a backup per write, and no longer prunes away the backups
  taken before the burst. `restore_library` holds the same lock, on the
  folder its snapshot goes to and on the backup's own folder (for a
  symbolic link, also the folder of the file it points to), from its check
  of the backup to the end, so no concurrent write can prune the backup
  being restored. The lock is a `flock` on the folder itself (no lock file
  is created), so it applies however the folder's path is written. Where
  the file system doesn't support it, writes within one process are still
  serialized.
- **(MCP)** A write that waits more than `write_safety.BACKUP_LOCK_TIMEOUT`
  (15 s) for the backup folder, or past the end of an enclosing
  `query_deadline` if that is sooner, raises `LibraryBusyError` ("Another
  write is backing up or restoring this library; nothing was changed. Try
  again in a moment.") and changes nothing.
- `restore_library` checks again that the backup exists once it holds the
  lock, before it takes its snapshot. With its snapshot (the default), it
  now creates the snapshot folder before it checks the backup, because the
  lock needs the folder to exist: a restore refused by that check, or one
  that waits too long for the lock, may leave that new, empty folder
  behind. A snapshot folder that can't be created is now reported before a
  backup that fails its check.
- **`list_chapters()` is cached** (faster on repeat; same values). A
  `BookContent` computes its chapter list once, and the list is kept
  process-wide, keyed by file metadata (the book folder's identity and
  modification time, and the identity, size and modification time of its
  `container.xml`, package document and navigation files), so a repeat call
  on any instance (including a new one from `get_book_content()`) reads
  only file metadata instead of the whole book.
  `get_current_reading_chapter()` benefits too. The first call on a book
  still reads it the 1.10 way (with the same errors), and the cached list
  is used only after it matched that read. Later calls reuse the list while
  `container.xml`, the package document and the navigation files are
  unchanged, so a chapter file that has since gone missing or become
  unreadable is reported by `get_chapter()` rather than by
  `list_chapters()`. Each call returns a new list; the `Chapter` objects in
  it are shared (they are immutable). Call `content.clear_content_cache()`
  after changing a book's files in place.

### Added

**Where you are in a book** (`py_apple_books.positions`)

- `PyAppleBooks.get_reading_position(book_id, *, resolve_chapter=True,
  infer=True)`: where the reader is, as a `ReadingPosition`: from Apple
  Books' reading-position bookmark (its CFI, or its page data: the page of
  a PDF, the spine file of an EPUB), else inferred from the newest
  highlight or bookmark with a location (`source='recent_annotation'`),
  with the chapter (`chapter`, `match`, `total_chapters`) when asked for.
  Also the position as a fraction of the book (`fraction`), the furthest
  point read (`furthest_fraction`), and for PDFs the page and page count
  (`page`, `page_count`; `page_count_estimated` when the count is worked
  out from the page and the fraction). When the book can't be read (not
  downloaded, DRM-protected, a PDF) the position is still returned, with
  the reason in `unavailable`. At most three queries;
  `resolve_chapter=False` touches no file. Returns None when nothing
  records a position.
- `PyAppleBooks.get_annotation_locations(annotations)`: the spine file and
  chapter of many annotations at once, as `{annotation.id:
  ResolvedLocation}`; each book is looked up once and read once. An
  annotation that can't be placed says why in `unavailable` (`no_location`,
  `orphaned`, or its book's reason such as `not_downloaded`, `drm`,
  `not_epub`).
- `PyAppleBooks.get_annotation_context(annotation_id, chars_before=300,
  chars_after=300)`: a highlight with the text around it, as an
  `AnnotationContext` (`before`, `highlight`, `after`, whether the text was
  cut at either end, the chapter and the spine file). `str()` of it is
  exactly the text `get_annotation_surrounding_text()` returns whenever
  that finds the highlight, unless the highlight occurs several times and
  the context took another occurrence than the first (`disambiguated`). It
  also finds highlights that method doesn't: across soft hyphens and
  zero-width spaces, and with different case, accents, quotes or dashes
  (`text_match`: `exact`, `whitespace`, `invisible`, `folded`). Of several
  occurrences it takes the one inside the annotation's surrounding passage
  when that passage occurs once, and reports how many there are
  (`occurrences`). Instead of an empty string it raises a
  `ContextUnavailableError` whose `reason` says what is missing
  (`no_location`, `no_highlight_text`, `orphaned`, `empty_chapter`,
  `highlight_not_found`), or the error the book raises
  (`BookNotDownloadedError`, `DRMProtectedError`, `NotEpubError`...). It
  never returns the chapter opening in place of a highlight it can't find.
  Accepts an `Annotation` as well as an id.
- `BookContent.resolve(location)`: where a location is in the book, as a
  `ResolvedLocation`: its spine file (`spine_index`, `item_id`) and the
  table-of-contents entry it belongs to (`chapter`), with `match` saying
  how that entry was chosen: `file` (the file holds one entry), `anchor`
  (the file holds several; the last one starting at or before the
  location), `preceding` (the file holds none, or the location is before
  all of them: the last entry earlier in the book), `front_matter` (no
  entry starts before it) or `section_unknown` (the file holds several
  entries but where they start couldn't be read; no chapter is guessed).
  `location` is a `Location`, a CFI string or a spine index. Returns None
  when the location names no file of this book; a malformed CFI never
  raises.
- `py_apple_books.positions`: `ChapterMatch`, `ResolvedLocation`,
  `UnavailableReason`, `PositionSource`, `ReadingPosition`, `TextMatch`,
  `AnnotationContext`, `TextPosition`, `BoundarySource`,
  `BoundaryPrecision`, `BoundaryWarning`, `ReadBoundary` and
  `ResolvedBoundary`. Each is an immutable, hashable and picklable
  dataclass, or an enum whose members are strings (`str()` gives the
  value). The module reads nothing when imported.
  `UnavailableReason.of(exc)` maps an error raised by the library to its
  reason (`NotInLibraryError` to `not_owned`, `BookNotDownloadedError` to
  `not_downloaded`, `DRMProtectedError` to `drm`, `NotEpubError` to
  `not_epub`, `ChapterNotFoundError` to `chapter_not_found`,
  `ContextUnavailableError` to its `reason`, any other library error to
  `unreadable`; None for database errors and anything else). It never
  raises. `TextPosition(spine_index, offset=0)` is a point in a book's
  text; `str()` writes it as `"N:M"` and `TextPosition.parse("N:M")` reads
  it back.

**Spoiler-safe reading**

- `PyAppleBooks.get_read_boundary(book_id, *, basis='position')`: how far a
  book has been read, as a `ReadBoundary`, from the library database alone
  (no book file is read; at most three statements). The boundary is placed
  by the first source the library has: Apple Books' reading-position
  bookmark (the earliest one if a book has several, so the boundary is
  never past any of them), else the newest highlight, note or bookmark with
  a location in the book's text, else the reading progress, else nothing
  (`source` is `reading_position`, `recent_highlight`, `progress` or
  `none`). `basis='furthest'` uses the furthest point read when it is past
  the reading progress (`source` `furthest`). On an older or newer Books
  database a missing column skips the sources that need it and adds the
  `schema_missing_columns` warning; without an annotation store the
  bookmark and highlight are skipped (`annotations_unavailable`). It never
  raises `UnsupportedSchemaError`. `ReadBoundary.includes(location)` tells
  from locations alone whether a location (a highlight's, for example) is
  known to be read.
- `BookContent.resolve_boundary(boundary, *, precision='item')`: places a
  `ReadBoundary` in the book's text as a `ResolvedBoundary`, at the start
  of the spine item the boundary falls in, never later than the point it
  stands for. A bookmark that can't be placed (in a non-linear item, on a
  table-of-contents page, not found, or whose file id and spine step
  disagree) falls back to the highlight, then the progress, then the start
  of the book, never past the bookmark's own item, with a warning code.
  `precision='exact'` raises `InvalidChoiceError` (only `'item'` is
  accepted in this release).
- `BookContent.search(query, *, start=None, until=None, limit=20,
  chars_before=80, chars_after=80, count_total=False,
  count_withheld=False, include_nonlinear=False, include_toc_pages=False)`:
  searches the book's text in reading order, folded like every search of
  the library, and returns a `TextSearchResult` page of `TextHit`s (start
  and end positions, the item's manifest id and a one-line snippet). With
  `until` (a `TextPosition` or a `ResolvedBoundary`), text from that point
  on is cut off before matching: no match runs across it and no snippet
  shows anything past it. Pass `next_start` back as `start` for the next
  page; `limit=None` gives `MAX_SEARCH_HITS` (1,000). `count_total` counts
  every match in scope; `count_withheld` counts the matches past the
  boundary without returning them. A query longer than 1,000 characters
  after folding, or 64,000 before, raises `InvalidArgumentError` (the
  message never repeats the query).
- `BookContent.position_at_percent(percent)`: the start of the linear spine
  item that holds that share of the book's linear text, as a
  `TextPosition`.

**Book content**

- `BookContent.list_spine_items()`: the book's spine (its reading order) as
  `SpineItem` entries: `index` (the same number as `Location.spine_index`),
  `item_id`, `href`, `media_type`, `linear`, `is_toc_page`, `readable` and
  `toc_orders` (the `Chapter.order` of the table-of-contents entries in
  that file).
- `BookContent.get_spine_item_text(item, *, normalize_unicode=False)`: the
  plain text of one whole spine file, by manifest id (`str`, such as
  `Location.chapter_id`) or spine index (`int`). The same text
  `get_chapter()` gives for a file that holds one chapter, never cut at a
  table-of-contents entry. Images, stylesheets, fonts and other binary
  files raise `ChapterNotFoundError`.
- `BookContent.iter_spine_text(*, start=None, until=None,
  include_nonlinear=False, include_toc_pages=False)`: the book's text one
  spine item at a time, as `SpineText` slices, from a `TextPosition` up to
  a `TextPosition` or a `ResolvedBoundary` (exclusive). Each item is read
  when the iteration reaches it.
- `BookContent.get_chapter(chapter_id, *, span='file',
  normalize_unicode=False)`. `span='file'` (the default) returns the same
  text as 1.10. Two opt-in modes follow the book's reading order across
  files: `span='section'` runs to where the next table-of-contents entry of
  any depth begins, so a chapter split over several files comes back
  whole; `span='chapter'` runs to the next entry of the same depth or
  shallower, so a part includes its chapters and a chapter its sections.
  In both modes `chapter_id` is looked up as a chapter's order (`"5"`)
  first, then as a chapter id, then as a manifest id. A bad `span` raises
  `InvalidChoiceError` before anything is read. `normalize_unicode=True`
  removes soft hyphens, zero-width spaces and byte-order marks and
  composes accents.
- `Chapter.spine_index`: the spine position of the chapter's file (None
  when the file isn't in the spine). It is not part of equality, hashing or
  `repr`.
- `PyAppleBooks.get_book_content()` also accepts a `Book`. A book read from
  the same library is used as is, with no query (unless its `path` or
  `state` wasn't read); a book from another library is looked up by id.
- `BookContent(path, *, book_id=None)` and a read-only `book_id` property;
  `get_book_content()` sets it. `BookContent` can be shared between threads
  (the book is read once) and can be pickled or copied.
- `py_apple_books.content.clear_content_cache()`: drops everything the
  library has cached from book files (call it after changing a book's files
  in place). `py_apple_books.content` also defines `SpineItem`,
  `SpineText`, `TextHit`, `TextSearchResult`, `MAX_SEARCH_HITS` and
  `MAX_SNIPPET_CONTEXT`.

**Search**

- `PyAppleBooks.search_annotations(query, *, limit=20, offset=None,
  book_id=None, require_all=False, include_deleted=False)`: ranked
  annotation search, returning `AnnotationHit` objects, best first:
  annotations containing every word of the query (full-text ranking, a word
  in the highlight counting most, then the note, then the surrounding text;
  English stemming, so "habits" finds "habit"), then those containing the
  whole query as written, then (unless `require_all`) those containing some
  of the words; only when none of these match, every word inside a longer
  word, then (unless `require_all`) every word of 3 or more characters. For
  a query of 3 or more characters once folded, the results with
  `limit=None` include every annotation `search_annotation_by_text` finds.
  Common English words are left out of multi-word queries, and double
  quotes search for a phrase. Queries and text are folded as every search
  folds them; a query in a script written without spaces between words
  (Chinese, Japanese, Korean, Thai, Lao, Khmer, Myanmar) or of punctuation
  only is matched as contained text. The index is built in memory on the
  first search and rebuilt when the annotations change; nothing is written
  to disk.
- `PyAppleBooks.search_books(query, *, limit=None, order_by=None,
  offset=None, include_store_series=False)`: the books whose title or
  author contains every word of the query (`"history smith"`), folded like
  `get_book_by_title`, in one SQL statement. It finds every book
  apple-books-mcp 0.9's `search_books` tool finds for the same query.
- `py_apple_books.search`: `AnnotationHit` (`annotation`, `score`,
  `matched_all`, `method`), `MatchMethod` (`FTS`, `SUBSTRING`),
  `fts5_available()` and `MAX_QUERY_LENGTH` (10,000 characters; a longer
  query raises `InvalidArgumentError`).

**Metadata and series**

- `PyAppleBooks.get_book_metadata(book_id, *, read_files=True)`: a
  `py_apple_books.models.BookMetadata` with language (a normalised BCP 47
  tag such as `en-US`), publisher, publication date (`published`, `'YYYY'`,
  `'YYYY-MM'` or `'YYYY-MM-DD'`, and `year`), ISBN (checksum-verified; an
  ISBN-13 when the book has one), subjects, description (plain text), the
  cover image's path inside the book, and a series named in the book file
  (`series_title`, `series_sequence`). What the library records comes
  first; the book's own package document (OPF) fills in the rest, and
  `book_file_fields` says which fields came from it. Only
  `META-INF/container.xml` and the package document are read, from an
  unzipped EPUB that is on this Mac; `file_state`
  (`MetadataFileState`: `read`, `not_requested`, `no_file`, `not_epub`,
  `not_downloaded`, `unreadable`) reports why nothing was read, and file
  problems never raise. Every text value is untrusted text from the library
  or the book file.
- `PyAppleBooks.get_series(book_id)` and `list_series(*,
  started_only=False, limit=None, offset=None)` (provisional): the Apple
  Books Store series the library records, as `Series` (title, `series_id`,
  the series `container` row, `is_ordered`, `volumes`, `volume_for()`,
  `next_after()`, `current`, `up_next`) of `SeriesVolume`s (`book`, `ids`,
  `sequence`, `label`, `in_library`, `reading_status`). Library database
  only. `SeriesVolume.in_library` follows `list_books()`'s rule exactly.
  Provisional: these may change in 1.12.
- `PyAppleBooks.get_books_by_subject(subject, limit=None, order_by=None, *,
  offset=None, include_store_series=False, read_files=True)`: like
  `get_books_by_genre`, and also matching any subject in the book's own
  package document, so books whose genre doesn't mention a topic but whose
  subjects do are found too. Every book `get_books_by_genre` returns is
  returned. `read_files=False` is `get_books_by_genre` itself. Only
  unzipped EPUBs on this Mac are read; books stored only in iCloud, books
  without a file and PDFs are skipped without touching the disk. One
  deadline (the library's `query_timeout` and any `query_deadline()`)
  covers the whole call.

**Engagement** (`py_apple_books.engagement`)

- `get_underlines(limit=None, order_by='-creation_date', *, offset=None,
  include_deleted=False)`: your underlines, newest first.
- `sample_highlights(limit=5, *, offset=None, on=None, seed=None,
  book_id=None, after=None, before=None, exclude_ids=(), exclude_uuids=(),
  exclude_short=True, include_orphans=False)`: a varied, repeatable sample
  of your highlights for a day. The same day and seed give the same picks
  on every platform; a new day gives new ones. Highlights with a note count
  twice, and without `book_id` the picks go round-robin across books. Words
  and short phrases are left out by default. Nothing is stored: pass what
  you have already shown in `exclude_ids` or `exclude_uuids`. The algorithm
  is named by `SAMPLE_ALGORITHM` (`'pab-sample-v1'`).
- `get_highlights_on_this_day(on=None, limit=None,
  order_by='-creation_date', *, offset=None, include_deleted=False,
  include_orphans=True)`: highlights and notes made on this calendar day in
  earlier years, newest first.
- `get_vocabulary(limit=None, order_by='-last_highlighted', *, offset=None,
  book_id=None, after=None, before=None, underline_only=False)`: the words
  and short phrases you highlighted, grouped across books, as
  `VocabularyEntry` objects (`term`, its highlights, `count`, `notes`,
  `first_highlighted`, `last_highlighted`, `asset_ids` and a `context`
  sentence). Returns a list.
- `get_highlight_activity(*, after=None, before=None, book_id=None,
  granularity='month')`: how much you highlighted in a window, as a
  `HighlightActivity` with `ActivityPeriod`s by day, ISO week, month or
  year. `get_highlight_streaks(*, on=None)`: runs of consecutive days with
  a highlight (`HighlightStreaks`). Both measure highlighting, not reading
  time, which Books doesn't record in a form the library can read.
- `get_reading_goals(*, prefs_path=None)`: Books' reading goals as it last
  saved them, as a `ReadingGoals` (the yearly books goal, the daily reading
  goal, Books' own current streak, its list of books finished towards the
  goal, and when the file was written). Read from Books' preferences file
  next to the library, read-only and in-process; only these settings are
  kept. Returns None when the file is missing or can't be read safely.
- `get_finished_books(..., *, finished_after=None, finished_before=None)`:
  keep the books whose finish date is in a window. Books records the date a
  book was marked as finished, so books marked in bulk share one.
- `get_annotations_by_date_range` accepts dates as well as datetimes: a
  date covers that whole local day (it raised `AttributeError` before).
- `LibraryStats.orphan_assets`: `(asset id, count)` for every asset id that
  annotations name and no book has, most annotations first. Its counts add
  up to `orphan_annotations`.

**Removed books** (`py_apple_books.book_info`)

- `PyAppleBooks.get_cached_book_info(asset_ids)`: `{asset id:
  CachedBookInfo}` with the title, author, language, publisher and year
  Apple Books cached for those books, for highlights and notes whose book
  is no longer in the library (`annotation.book` is None). Apple Books
  keeps these caches next to the library, one per Books version, and keeps
  the rows of removed books; every version's cache is searched, newest
  first. Best effort and read-only: macOS may purge the caches, a cache
  problem never raises, and the call returns what it read within a few
  seconds. Results are remembered until `close()` and re-checked at most
  every `BOOK_INFO_RECHECK` seconds (30).

**Models**

- `Book` fields `language`, `year`, `release_date`, `series_id`,
  `series_container_id`, `series_sequence`, `series_label`,
  `series_is_ordered` (the series fields are provisional) and
  `high_water_progress` (the furthest point Books recorded, in percent;
  information only). `Book.is_pdf` (from the database alone) and
  `py_apple_books.models.book.CONTENT_TYPE_PDF`.
- `Annotation` fields `position_fraction`, `furthest_fraction` and
  `location_data` (bookmarks and the reading-position row only), and the
  properties `page_location` (a `PageLocation(ordinal, page_offset)` with
  `page`) and `is_short_selection`.
- `Location.end_sort_key`: where a range CFI ends, in `sort_key` order.
- `py_apple_books.models` exports `BookMetadata`, `MetadataFileState`,
  `Series`, `SeriesVolume` and `PageLocation`.

**Text helpers** (`py_apple_books.text`, pure functions, no I/O)

- `fold_for_match` is now documented public API. It stays deterministic,
  idempotent and never raising; a minor release may only make it fold
  more.
- `finditer_folded(text, query)` and `find_folded(text, query, start=0,
  end=None)`: find a query in a longer text with the library's fold and get
  the matches as offsets into the original text.
- `normalize_unicode(s)`: removes soft hyphens, zero-width spaces and BOMs,
  then composes accents (NFC).
- `snap_break(text, pos, *, lookback=300, floor=0)`: where to end a page of
  text near `pos` without cutting a line, a word or a character.
- `selection_core(text)` and `is_short_selection(text)`: a highlight
  trimmed to its word or phrase, and whether it is a word or a short phrase
  rather than a passage.

**Exceptions**

- `NotEpubError` (an `AppleBooksError`): raised by the chapter methods of
  `BookContent` for a PDF or any other non-EPUB file, with the same
  messages as before.
- `ContextUnavailableError(message, reason=None, annotation_id=None)` (an
  `AppleBooksError`), with reason constants `NO_LOCATION`,
  `NO_HIGHLIGHT_TEXT`, `ORPHANED`, `EMPTY_CHAPTER` and
  `HIGHLIGHT_NOT_FOUND`.
- `UnsafeEpubEntryError.entry`: the full entry name the book used (the
  message may shorten it).

**Writes**

- `write_safety.BACKUP_LOCK_TIMEOUT` (15 s, read at call time): how long a
  backup or restore waits for another one using the same backup folder.

**Testing** (`py_apple_books.testing`, provisional API)

- `write_epub_bundle(dest, files, toc=(), ...)` writes an unzipped EPUB 3
  bundle in the shapes real books come in, byte for byte the same on every
  run: the package document in any folder, files in sub-folders, a nav
  document and an NCX (either or neither), nested entries with fragments,
  non-linear, repeated or malformed spine entries, other media types,
  guide references and landmarks. `write_epub` is unchanged.
- `FixtureLibrary.add_book(finished_date=, last_engaged=)`,
  `add_annotation(user_data=, position_fraction=, furthest_fraction=)`,
  `page_location_blob(page_offset, ordinal=0)`, `add_series(title,
  volumes, *, ordered=True, store_id=None)`, `prefs_path` and
  `write_prefs(...)` (with `YEAR_ZERO`), and `book_info_dir` and
  `add_book_info_cache(rows, ...)`. Rows built with the default arguments
  are exactly the rows 1.10's helpers wrote.
- `python -m py_apple_books.testing.dump_schema` also writes
  `AEBookInfo.sql` when Books keeps per-book info caches next to the
  library: the `CREATE TABLE` and `CREATE INDEX` statements of the newest
  cache's `ZAEBOOKINFO` table, never a row, checked before it's saved.
  `meta.json` records the cache's file name and column count, and
  `--compare` reports its column differences. The cache is opened
  read-only according to its journal mode; files only in iCloud, symlinks
  and FIFOs are never opened. The committed macOS 26.7 / Books 8.5 schema
  fixture now includes `AEBookInfo.sql`.

### Changed

- **Superseded, kept:** `get_annotation_surrounding_text()`,
  `get_current_reading_chapter()` and `get_current_reading_location()` are
  superseded by `get_annotation_context()` and `get_reading_position()`.
  They are unchanged and not deprecated (no warning).
- **Faster chapter text.** The HTML-to-text step behind `get_chapter`,
  chapter content and annotation context is one linear pass over each
  chapter file. It no longer modifies the parsed document, and its time no
  longer grows with the square of the number of paragraphs in a file: a
  synthetic chapter file with 10,000 paragraphs takes a fraction of a
  second instead of several seconds. The returned text is unchanged,
  character for character; the test suite compares it with the 1.10 code
  on many thousands of generated documents.
- `content.is_downloaded()` and `BookContent.is_downloaded` report a file
  or folder that is itself only in iCloud (or has an iCloud stub next to
  it) as not downloaded without running `du`.
- The new content methods read only the book's `container.xml`, package
  document and navigation files, then each file whose text is asked for,
  each checked first not to be stored only in iCloud, and never through
  `du` or a walk of the book folder. They refuse DRM-protected books
  (`DRMProtectedError`) and books stored partly in iCloud
  (`BookNotDownloadedError`).
- **Schema reports check the schema text they copy before running it.**
  `dump_schema` refuses a store whose schema entries are anything but
  single `CREATE` statements without comments, and its self-check runs the
  generated SQL so that it can only build the schema and its bookkeeping
  rows. A store these checks refuse, such as one with an index on a
  function call (which 1.10 dumped), ends the dump with an error that asks
  you to report it on the issue tracker.
- Internal: `PyAppleBooks` inherits private mixin classes, one per feature
  area, so the methods added in 1.11 live in modules of their own. Every
  existing method keeps its signature, defaults, docstring and results.
- **Packaging.** The Python 3.14 classifier. The runtime dependencies are
  unchanged. The wheel adds the new modules (`positions.py`, `search.py`,
  `engagement.py`, `book_info.py`, `models/book_metadata.py`,
  `models/series.py`, private modules and `_api/`).
- **CI.** apple-books-mcp 0.8.2, 0.9.0 and the latest release are driven
  over stdio against the built wheel and a synthetic library, on Python
  3.12 and 3.14. The outputs of 0.8.2 and 0.9.0 must match recorded ones
  (0.9.0's were recorded with the released 1.10.0), and the check fails if
  an argument of a read tool is never exercised. The released 0.8.2 and
  0.9.0 also run their own test suites against the built wheel (Python
  3.13 and 3.14). Free-threaded 3.13t and 3.14t legs run for information
  when uv provides a build. The wheel and sdist checks compare against
  1.10.0.
- **Tests.** The suite checks that importing the package, or any of its
  modules, reads no file other than the package's own files and the Python
  modules it imports, writes nothing, connects to no database, loads no
  system library and starts no process. Caches derived from book files are
  reset between tests. The opt-in live-library test reads only books stored
  as a folder that is wholly on disk, and turns off downloads of iCloud-only
  files for its own process.
- **Maintainer scripts.** The real-library regression scripts
  (`scripts/mcp_regress`) cover apple-books-mcp 0.9.0 and compare against
  the released 1.10.0, where no change at all is expected on a fully
  downloaded library. They turn off downloads of iCloud-only files for their
  own process, and stop if macOS doesn't allow it.

### Fixed

- **(MCP)** Collection writes no longer fail on a collection title or a book
  asset id that isn't valid UTF-8 (they raised a `sqlite3` error that quoted
  the text), and leave such text byte for byte as it was. A collection whose
  id isn't valid UTF-8 is refused like any collection that isn't
  user-created (`SystemCollectionError`). Any other invalid text a write
  would have to read stops it with `WriteError` ("nothing was changed"),
  without quoting the text. When the store is given as a file
  (`library_db=`, `APPLE_BOOKS_LIBRARY_DB`, or a `collection_writer`
  function's `db_path=`), that includes the store's Core Data metadata,
  which 1.10 treated as unavailable.
- `restore_library` could delete the backup it had just restored, when that
  backup was older than the newest `BACKUP_KEEP` and its path was written
  differently from the folder listing (another letter case, or through
  `/System/Volumes/Data`). The backup being restored is now recognized
  however its path is written.
- `BookContent.is_drm_protected` no longer looks inside a book folder that
  is only in iCloud; it reports such a book as protected (it can't be read
  either way) and never raises.
- Building a `Book` or `Annotation` from another one's fields keeps its
  dates (it raised `TypeError` before).
- The HTML-to-text step took time that grew with the square of the number
  of paragraphs in a chapter file (see "Changed").

### Compatibility notes

- `Book` and `Annotation` have new fields, appended after the existing ones
  with default None: positional construction keeps working, but `repr()`,
  `==` and `dataclasses.fields()` include them. `Chapter` has the new last
  field `spine_index` (outside `==`, hashing and `repr`, but in
  `dataclasses.fields()`, `asdict()` and `astuple()`); a `Chapter` pickled
  by 1.10 loads with `spine_index` None. `LibraryStats` has the new last
  field `orphan_assets` with a default, so its `repr()` and `==` include
  it; positional construction with the seven earlier arguments still works.
- `store_info().missing_columns` lists the new fields on Books versions
  whose database lacks their columns (older versions lack `series_sequence`
  and `series_is_ordered`, for example). They read as None there; filtering
  or sorting on them raises `UnsupportedSchemaError`, as for every optional
  column. apple-books-mcp 0.9's `--doctor` shows the list in its NOTE line.
- `NotEpubError` and `ContextUnavailableError` are new subclasses of
  `AppleBooksError`; code that catches `AppleBooksError` keeps working.
- `PyAppleBooks.__bases__` and `__mro__` list private classes from
  `py_apple_books._api`. Calling and subclassing `PyAppleBooks` work as
  before.
- `py_apple_books.models` exports five more names. `Series` and
  `SeriesVolume` are frozen but not hashable (they hold `Book` models);
  `BookMetadata` is frozen and hashable.
- Books stores `position_fraction` and `furthest_fraction` as text, so a
  filter or `order_by` on them in a query compares text (`'0.10'` sorts
  before `'0.9'`); compare the float values in Python instead. The two
  fractions are read only when the row's `type` is read too.
- New methods take dates by one rule: `after`/`before` (and
  `finished_after`/`finished_before`) are inclusive; a datetime is an
  instant (naive means local time), a date covers that whole local day.
  `on` names a local calendar day. Any other type raises
  `InvalidArgumentError`.
- New methods refuse a `limit` below 1 (or a bool) with
  `InvalidArgumentError`; None means no limit. The existing methods keep
  their deprecated "`limit <= 0` means all" behaviour.
- `get_read_boundary` and `get_reading_position` can name different
  bookmarks for a book with several live reading-position rows: the
  boundary takes the earliest (spoiler-safe), the position the newest. A
  `TextPosition` or `ResolvedBoundary` is valid for one book file and one
  library version.
- A location in a file holding several table-of-contents entries is placed
  by comparing its CFI with the entries' anchors. Where that can't be done,
  `section_unknown` is given, never the file's first entry. A newest
  highlight on a non-linear item or a table-of-contents page makes
  `resolve_boundary` fall back to the progress without a warning code of
  its own.
- Books' own list of finished books (`ReadingGoals.finished_assets`) and
  the library's (`get_finished_books`) are separate records and can differ.
- `is_short_selection`, `sample_highlights(exclude_short=True)` and the
  text helpers follow the running Python's Unicode database, so after a
  Python upgrade a character added in a recent Unicode version can be
  classified differently.
- Caches derived from book files are in memory only and bounded (the
  chapter and spine index at about 32 MiB and 4,096 books; the metadata
  cache is bounded too); the ranked-search index takes about 0.7 KB per
  annotation, one per library, freed by `close()`.
  `content.clear_content_cache()` empties the file caches.
- Writers running py-apple-books 1.10 or earlier don't take the backup
  folder lock, so they aren't coordinated with 1.11 writers (they may still
  take a backup each, as before).

### Security

- **No read downloads a file that is only in iCloud** (see "Behavior
  changes"); the library never asks macOS to bring a book back.
- New backups and pre-restore snapshots are created readable and writable
  by the owner only (mode 0600), and new backup folders, including
  `~/.py_apple_books` itself, with mode 0700. Existing files and folders
  keep their permissions. A backup is written to a name created
  exclusively: an existing file, an existing backup or a symbolic link
  under that name is never reused or written through.
- Book package documents are treated as untrusted input: a DOCTYPE with an
  internal subset is refused, external entities are never fetched, and
  parsing is bounded and stops once the needed parts are read. Symlinks
  inside the book folder are refused for metadata reads.
- Books' preferences file is read only if it is a regular local file of at
  most 8 MiB; a path into or through an iCloud Drive or other cloud folder,
  a file that is not downloaded, a link or a special file is refused.
- Error messages about book files name base names only, never paths, and
  shorten long names.
- `get_cached_book_info` also tells whether Books ever opened a book with a
  given id, including books never highlighted. Pass ids taken from the
  user's own annotations; don't expose it as a lookup by arbitrary id.

### Upgrading from 1.10

Most code needs no change. Check these:

- Code that compares `Book` or `Annotation` objects with `==`, or lists
  `dataclasses.fields()`, sees the new fields.
- Books stored partly or wholly in iCloud now raise
  `BookNotDownloadedError` instead of being downloaded on read. Ask the
  user to open them in Apple Books.
- Call `py_apple_books.content.clear_content_cache()` after rewriting a
  book's files in place (tests that edit EPUB fixtures, for example).
- A burst of concurrent writes shares one backup, and a write may raise
  `LibraryBusyError` after waiting 15 s for another write's backup.

### If 1.11 breaks your setup

- Pin the previous release: `pip install 'py-apple-books<1.11'`. For
  apple-books-mcp run through uvx, add the pin to its arguments:
  `uvx --with 'py-apple-books<1.11' apple-books-mcp` (in a Claude Desktop
  config: `"args": ["--with", "py-apple-books<1.11", "apple-books-mcp"]`).
- Please [open an issue](https://github.com/vgnshiyer/py-apple-books/issues).
  A fix ships as 1.11.x. uvx keeps the environment it resolved, so after a
  fix, drop the pin and refresh it once with `uvx apple-books-mcp@latest`
  (or `uv cache clean`).

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
