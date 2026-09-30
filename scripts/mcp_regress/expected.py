"""Rule-based expected changes for compare.py.

Each ITEMS entry is an audit item whose intended fix may change MCP output,
the stage from which it applies (see STAGES) and the harness keys
(``tool(json-args)``) it may change:

  tools       every call of these tools
  calls       fnmatch patterns on the full key
  book_tools  {set_name: [tools]}: calls of these tools whose ``book_id``
              is in book_sets(home)[set_name]

Rules may allow more than actually changes. Allowed means "not a
regression if it changes", never "must change". Every addition needs an
item id. Per-book rules are resolved from the snapshot by book_sets(), so
this file holds no ids, titles or counts from any library.
"""
import fnmatch
import json
import pathlib
import sqlite3
import urllib.parse

# 'final' adds no items of its own: it is 'semantics' as the release gate runs it.
STAGES = ["baseline", "query", "semantics", "final"]

# Stream names accepted by --through. models-data and writes map to
# baseline because each must be output-neutral by itself.
ALIASES = {
    "foundations": "baseline",
    "models-data": "baseline",
    "writes": "baseline",
    "connections": "query",
    "orm": "semantics",
    "facade": "semantics",
}

ITEMS = {
    # Apostrophes no longer break the search SQL. Harness keys sort their
    # arguments, so "limit" precedes "text".
    "F08": {
        "stage": "query",
        "calls": ['search_annotations(*"text": "don\'t"*'],
    },
    # Folded, parameterised text matching.
    "G4.1": {
        "stage": "query",
        "tools": ["search_annotations", "search_notes", "search_books_by_title",
                  "get_books_by_genre", "search_collections_by_title", "revisit_book"],
    },
    # Newest-first default order for colour and text searches.
    "F28": {
        "stage": "query",
        "tools": ["get_highlights_by_color", "search_notes", "search_annotations"],
    },
    # Owned scope: series containers and unowned Store-series rows hidden.
    "F07": {
        "stage": "semantics",
        "tools": ["list_all_books", "search_books_by_title", "get_books_by_genre",
                  "get_books_in_progress", "get_unstarted_books", "get_recently_read_books",
                  "get_library_stats", "library_snapshot", "weekly_digest", "revisit_book",
                  "currently_reading_resource"],
        "book_tools": {"f07_rows": ["list_book_chapters", "get_chapter_content",
                                    "get_current_reading_position"]},
    },
    # Disjoint reading-status partition.
    "F04": {
        "stage": "semantics",
        "tools": ["get_books_in_progress", "get_unstarted_books", "get_finished_books",
                  "get_library_stats", "library_snapshot", "currently_reading_resource",
                  "weekly_digest"],
    },
    # Live annotations only (no deleted or non-positive-type rows).
    "F25": {
        "stage": "semantics",
        "tools": ["list_all_annotations", "recent_annotations", "get_highlights_by_color",
                  "search_notes", "search_annotations", "get_annotations_by_date_range",
                  "get_library_stats", "library_snapshot", "weekly_digest",
                  "currently_reading_resource", "revisit_book"],
        "book_tools": {"f25_books": ["describe_book", "list_annotations",
                                     "get_current_reading_position"]},
    },
    # 'Last read' also counts the engaged date.
    "F62": {
        "stage": "semantics",
        "tools": ["get_recently_read_books", "get_books_in_progress", "get_finished_books",
                  "currently_reading_resource", "weekly_digest", "library_snapshot"],
        "book_tools": {"f62_books": ["describe_book", "get_current_reading_position"]},
    },
    # An unknown colour ('underline' is not one) gets a typed error
    # naming the valid colours.
    "F27": {
        "stage": "semantics",
        "calls": ['get_highlights_by_color({"color": "underline"*'],
    },
}

DOCUMENTS = ("Library", "Containers", "com.apple.iBooksX", "Data", "Documents")
CANONICAL = {
    "BKLibrary": "BKLibrary-1-091020131601.sqlite",
    "AEAnnotation": "AEAnnotation_v10312011_1727_local.sqlite",
}
APPLE_EPOCH = 978307200  # Core Data timestamps count from 2001-01-01 UTC

SET_QUERIES = {
    # Books whose live-annotation set changes under F25: a non-position
    # annotation (type 3 is the reading position) that is deleted or has
    # a non-positive type.
    "f25_books": """
        SELECT Z_PK FROM ZBKLIBRARYASSET WHERE ZASSETID IN (
            SELECT ZANNOTATIONASSETID FROM anno.ZAEANNOTATION
            WHERE ZANNOTATIONTYPE != 3
              AND (ZANNOTATIONTYPE <= 0 OR ZANNOTATIONDELETED = 1))""",
    # Books whose 'Last Read' date moves under F62: engaged after the last
    # open, on a later calendar day (the output shows local dates only,
    # so run compare.py under the same TZ as the harness).
    "f62_books": f"""
        SELECT Z_PK FROM ZBKLIBRARYASSET
        WHERE ZLASTENGAGEDDATE IS NOT NULL
          AND (ZLASTOPENDATE IS NULL
               OR (ZLASTENGAGEDDATE > ZLASTOPENDATE
                   AND date(ZLASTENGAGEDDATE + {APPLE_EPOCH}, 'unixepoch', 'localtime')
                       != date(ZLASTOPENDATE + {APPLE_EPOCH}, 'unixepoch', 'localtime')))""",
    # Rows F07 hides: series containers (content type 5) and Store-series
    # items with no sign of ownership.
    "f07_rows": """
        SELECT Z_PK FROM ZBKLIBRARYASSET
        WHERE ZCONTENTTYPE = 5
           OR (ZDATASOURCEIDENTIFIER = 'com.apple.ibooks.BKLibraryDataSourceSeries'
               AND ZCANREDOWNLOAD IS NOT 1)""",
}


def resolve_stage(name):
    """Return the STAGES entry for a stage or alias; ValueError if unknown."""
    stage = ALIASES.get(name, name)
    if stage not in STAGES:
        raise ValueError(f"unknown stage {name!r}; use one of {STAGES + sorted(ALIASES)}")
    return stage


def _store(home, sub):
    folder = pathlib.Path(home, *DOCUMENTS, sub)
    canonical = folder / CANONICAL[sub]
    if canonical.is_file():
        return canonical
    found = sorted(folder.glob("*.sqlite"))
    if not found:
        raise FileNotFoundError(f"no {sub} store under {folder}")
    return found[0]


def _ro_uri(path):
    return f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"


def book_sets(home):
    """Return ``{set_name: {book Z_PK}}`` for the per-book rules, read-only."""
    conn = sqlite3.connect(_ro_uri(_store(home, "BKLibrary")), uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS anno", (_ro_uri(_store(home, "AEAnnotation")),))
        return {name: {row[0] for row in conn.execute(sql)} for name, sql in SET_QUERIES.items()}
    finally:
        conn.close()


def _split(key):
    tool, _, rest = key.partition("(")
    try:
        args = json.loads(rest[:-1]) if rest.endswith(")") else {}
    except ValueError:
        args = {}
    return tool, args if isinstance(args, dict) else {}


def _book_id(args):
    bid = args.get("book_id")
    if isinstance(bid, bool):
        return None
    if isinstance(bid, int):
        return bid
    if isinstance(bid, str) and bid.strip().isdigit():
        return int(bid)
    return None


def _matches(rule, key, sets):
    tool, args = _split(key)
    if tool in rule.get("tools", ()):
        return True
    if any(fnmatch.fnmatchcase(key, pattern) for pattern in rule.get("calls", ())):
        return True
    for set_name, tools in rule.get("book_tools", {}).items():
        if tool in tools and _book_id(args) in sets.get(set_name, ()):
            return True
    return False


def allowed_by_item(keys, stage, sets):
    """Return ``{item: set(keys)}`` for the items active at ``stage``."""
    limit = STAGES.index(resolve_stage(stage))
    return {item: {k for k in keys if _matches(rule, k, sets)}
            for item, rule in ITEMS.items()
            if STAGES.index(rule["stage"]) <= limit}


def allowed_keys(keys, stage, sets):
    """Return the keys some item active at ``stage`` allows to change."""
    return set().union(*allowed_by_item(keys, stage, sets).values())
