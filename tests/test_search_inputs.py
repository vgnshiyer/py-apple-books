"""Search and id inputs that broke or bent 1.9.1's SQL (F08, G4.1).

Every search facade must accept any text without an exception and
return exactly the rows a Python oracle picks: ``fold_for_match(needle)
in fold_for_match(field)`` over the seeded rows, within the method's
scope. Quotes, LIKE wildcards and SQL payloads are plain text, and the
id getters report them as not found.
"""

import pytest

from py_apple_books.exceptions import CollectionNotFoundError
from py_apple_books.text import fold_for_match

UNION = "zzz%' UNION SELECT 1,2,'rows='||(SELECT count(*) FROM anno_db.ZAEANNOTATION),4 --"
RECURSIVE_CTE = ("0' OR (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                 "SELECT count(*) FROM c) OR '")
NEEDLES = ["'", "don't", "O'Brien", "%", "_", "\\", "’", "1' OR '1'='1", "x' OR 1=1 --",
           UNION, RECURSIVE_CTE]


@pytest.fixture
def seeded(library):
    books = [
        library.add_book("Don't Panic", genre="Kid's Books"),
        library.add_book("O'Brien’s Guide", genre="Fiction"),
        library.add_book("100% Proof", genre="50%_off"),
        library.add_book("snake_case Handbook", genre="Back\\slash"),
        library.add_book("Don’t Look Back", genre="Kid’s Books"),
        library.add_book("x' OR 1=1 -- the book", genre=None),
        library.add_book("Plain Title", genre="Fiction"),
    ]
    first, second = books[0], books[1]
    library.add_collection("Kid's Shelf")
    library.add_collection("100% Done")
    library.add_collection("Plain Shelf")
    deleted = library.add_collection("Don't Show Me", deleted=True)
    library.add_annotation(first, "Don't panic, it's 100% fine.", note="O'Brien said so")
    library.add_annotation(first, "don’t touch snake_case names", color="green")
    library.add_annotation(second, "a back\\slash and 50% off", note="it’s a note")
    library.add_annotation(second, "plain words", raw={"ZANNOTATIONREPRESENTATIVETEXT": "1' OR '1'='1 inside"})
    library.add_annotation("ORPHANASSET", "orphan: don't forget", note="x' OR 1=1 -- noted")
    library.add_annotation(first, None, kind="note", note="just a note, with '_'",
                           raw={"ZANNOTATIONSELECTEDTEXT": None, "ZANNOTATIONREPRESENTATIVETEXT": None})
    # The reading-position bookmark is out of scope even if it has text.
    library.add_annotation(first, None, kind="reading_position",
                           raw={"ZANNOTATIONSELECTEDTEXT": "Don't stop", "ZANNOTATIONNOTE": "100%"})
    return {"deleted": deleted["id"]}


def corpus(library, store, sql):
    return {row[0]: row[1:] for row in library.execute(store, sql)}


def oracle(rows: dict, needle: str) -> set:
    folded = fold_for_match(needle)
    return {pk for pk, fields in rows.items()
            if any(f is not None and folded in fold_for_match(f) for f in fields)}


def ids(rows) -> set:
    return {row.id for row in rows}


@pytest.fixture
def cases(api, library, seeded):
    """(search method, rows in its scope as {id: (field, ...)})."""
    books = "SELECT Z_PK, {} FROM ZBKLIBRARYASSET"
    annos = "SELECT Z_PK, {} FROM ZAEANNOTATION WHERE ZANNOTATIONTYPE != 3"
    return {
        "get_book_by_title": (api.get_book_by_title, corpus(library, "library", books.format("ZTITLE"))),
        "get_books_by_genre": (api.get_books_by_genre, corpus(library, "library", books.format("ZGENRE"))),
        "get_collection_by_title": (api.get_collection_by_title, corpus(
            library, "library", "SELECT Z_PK, ZTITLE FROM ZBKCOLLECTION WHERE ZDELETEDFLAG = 0")),
        "search_annotation_by_highlighted_text": (api.search_annotation_by_highlighted_text, corpus(
            library, "annotations", annos.format("ZANNOTATIONSELECTEDTEXT"))),
        "search_annotation_by_note": (api.search_annotation_by_note, corpus(
            library, "annotations", annos.format("ZANNOTATIONNOTE"))),
        "search_annotation_by_text": (api.search_annotation_by_text, corpus(
            library, "annotations", annos.format(
                "ZANNOTATIONSELECTEDTEXT, ZANNOTATIONREPRESENTATIVETEXT, ZANNOTATIONNOTE"))),
    }


@pytest.mark.parametrize("needle", NEEDLES)
@pytest.mark.parametrize("method", [
    "get_book_by_title", "get_books_by_genre", "get_collection_by_title",
    "search_annotation_by_highlighted_text", "search_annotation_by_note", "search_annotation_by_text",
])
def test_search_matches_the_fold_oracle(cases, method, needle):
    search, rows = cases[method]
    assert ids(search(needle)) == oracle(rows, needle)


def test_the_oracle_is_not_trivial(cases):
    """Apostrophes match both quote styles; % and _ match only themselves."""
    by_text = cases["search_annotation_by_text"]
    titles = cases["get_book_by_title"]
    assert len(oracle(by_text[1], "don't")) == 3
    assert len(oracle(titles[1], "don't")) == 2
    assert len(oracle(titles[1], "%")) == 1
    assert len(oracle(titles[1], "_")) == 1
    assert len(oracle(cases["get_collection_by_title"][1], "'")) == 1


@pytest.mark.parametrize("payload", NEEDLES + ["0' OR ZDELETEDFLAG=1 OR '", "1 OR 1=1"])
def test_id_getters_report_payloads_as_not_found(api, seeded, payload):
    with pytest.raises(IndexError):
        api.get_book_by_id(payload)
    with pytest.raises(IndexError):
        api.get_annotation_by_id(payload)
    with pytest.raises(CollectionNotFoundError):
        api.get_collection_by_id(payload)


@pytest.mark.parametrize("make_id", [
    lambda pk: pk, lambda pk: str(pk), lambda pk: f"{pk}' OR '1'='1", lambda pk: f"{pk} OR 1=1",
])
def test_deleted_collection_is_never_returned(api, library, seeded, make_id):
    with pytest.raises(CollectionNotFoundError):
        api.get_collection_by_id(make_id(seeded["deleted"]))
    assert seeded["deleted"] not in ids(api.list_collections())
    assert seeded["deleted"] not in ids(api.get_collection_by_title("Show"))


def test_payloads_never_reach_the_sql_text(api, seeded, sql_trace):
    for needle in NEEDLES:
        api.search_annotation_by_text(needle)
        list(api.get_book_by_title(needle))
        with pytest.raises(IndexError):
            api.get_book_by_id(needle)
    assert sql_trace
    for sql, params in sql_trace:
        assert "UNION" not in sql and "RECURSIVE" not in sql and "OR 1=1" not in sql and "'1'" not in sql


def test_text_search_is_one_query(api, seeded, sql_trace):
    """Scope, the three-column OR, order and limit are all in one statement,
    and it runs once."""
    got = api.search_annotation_by_text("don't", limit=2)
    assert len(got) == 2
    searches = [(sql, params) for sql, params in sql_trace if "abk_fold" in sql]
    assert len(searches) == 1
    sql, params = searches[0]
    assert ("WHERE ZANNOTATIONTYPE != ? AND (instr(abk_fold(ZANNOTATIONSELECTEDTEXT), ?) > 0 "
            "OR instr(abk_fold(ZANNOTATIONREPRESENTATIVETEXT), ?) > 0 "
            "OR instr(abk_fold(ZANNOTATIONNOTE), ?) > 0) "
            "ORDER BY ZANNOTATIONCREATIONDATE DESC, Z_PK ASC LIMIT ?") in sql
    assert tuple(params) == (3, "don't", "don't", "don't", 2)
