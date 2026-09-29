"""Seeded fuzzing of the read path's SQL (F08, G4.1, R21, R22).

Random needles built from quotes, LIKE and SQL metacharacters, control
characters, combining marks, ligatures, NBSP, emoji, right-to-left
text, SQL payloads and lone surrogates are wrapped in the sentinel
``zqx…xqz``. Every search facade must return exactly the rows a Python
oracle picks (``fold_for_match`` containment within the method's
scope), no executed SQL may contain the sentinel, and odd ids, limits,
offsets and field names may only raise the documented errors.

300 iterations per test by default; set APPLE_BOOKS_FUZZ_ITERATIONS to
change it.
"""

import os
import random
import warnings

import pytest

from py_apple_books.exceptions import CollectionNotFoundError, InvalidArgumentError, UnknownFieldError
from py_apple_books.models import Annotation, Book
from py_apple_books.text import fold_for_match

ITERATIONS = int(os.environ.get("APPLE_BOOKS_FUZZ_ITERATIONS", "300"))
SEED = 20260928
OPEN, CLOSE = "zqx", "xqz"
INT64_MAX = 2**63 - 1

POOLS = {
    "quotes": ["'", '"', chr(0x2019), chr(0x2018), chr(0x201C), chr(0x201D)],
    "sql": ["%", "_", "\\", ";", "--", "/*", "*/", "%%", "\\%", "'--"],
    "controls": [chr(c) for c in [*range(1, 32), 127]],
    "unicode": [chr(0x301), "e" + chr(0x301), chr(0xFB01), chr(0xFB02), chr(0xA0), chr(0x1F600),
                chr(0x5E9) + chr(0x5DC) + chr(0x5D5) + chr(0x5DD), chr(0x645) + chr(0x631) + chr(0x62D),
                chr(0x200F), chr(0xDF), chr(0x130), "G" + chr(0xF6) + "del", chr(0x2026), chr(0x2014)],
    "payloads": [" UNION SELECT 1,2,3 --", " OR 1=1", "' OR '1'='1", "') OR ('1'='1",
                 " WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"],
    "ascii": list("abcxyz019 ") + ["don't", "it's", "the", "  "],
}


def fragment(rng: random.Random, surrogates: bool = True) -> str:
    parts = []
    for _ in range(rng.randint(1, 4)):
        if surrogates and rng.random() < 0.1:
            parts.append(chr(rng.randint(0xD800, 0xDFFF)))
        else:
            parts.append(rng.choice(POOLS[rng.choice(list(POOLS))]))
    return "".join(parts)


def wrap(fragment_: str) -> str:
    return OPEN + fragment_ + CLOSE


def needle(rng: random.Random, seeded: list) -> str:
    """A sentinel-wrapped needle: half the time one of the seeded
    fragments (possibly upper-cased or with the other apostrophe), so
    searches also hit."""
    if seeded and rng.random() < 0.5:
        frag = rng.choice(seeded)
        if rng.random() < 0.3:
            frag = frag.upper()
        if rng.random() < 0.3:
            frag = frag.replace("'", chr(0x2019))
        return wrap(frag)
    return wrap(fragment(rng))


def huge_int(rng: random.Random) -> int:
    value = rng.choice([INT64_MAX, 2**63, 10**20, -2**63 - 1, rng.getrandbits(rng.randint(64, 80))])
    return -value if rng.random() < 0.2 else value


@pytest.fixture
def corpus(library):
    """Books, collections (one deleted) and annotations whose text holds
    sentinel-wrapped fragments (no lone surrogates: SQLite can't store
    them); returns the rows in each search's scope."""
    rng = random.Random(SEED)
    frags = [fragment(rng, surrogates=False) for _ in range(24)]
    text = lambda: f"lead {wrap(rng.choice(frags))} tail" if rng.random() < 0.8 else rng.choice(frags)

    books = [library.add_book(text(), genre=text() if rng.random() < 0.8 else None) for _ in range(8)]
    for i in range(5):
        library.add_collection(text(), deleted=i == 0)
    for i in range(20):
        owner = rng.choice(books) if i % 5 else "ORPHANASSET"
        kind = "reading_position" if i % 7 == 0 else "highlight"
        library.add_annotation(owner, None, kind=kind, raw={
            "ZANNOTATIONSELECTEDTEXT": text() if rng.random() < 0.9 else None,
            "ZANNOTATIONREPRESENTATIVETEXT": text() if rng.random() < 0.7 else None,
            "ZANNOTATIONNOTE": text() if rng.random() < 0.4 else None,
        })

    def rows(store, sql):
        return {row[0]: row[1:] for row in library.execute(store, sql)}

    annotations = "SELECT Z_PK, {} FROM ZAEANNOTATION WHERE ZANNOTATIONTYPE != 3"
    return {
        "frags": frags,
        "books": len(books),
        "annotations": len(rows("annotations", annotations.format("1"))),
        "scopes": {
            "get_book_by_title": rows("library", "SELECT Z_PK, ZTITLE FROM ZBKLIBRARYASSET"),
            "get_books_by_genre": rows("library", "SELECT Z_PK, ZGENRE FROM ZBKLIBRARYASSET"),
            "get_collection_by_title": rows(
                "library", "SELECT Z_PK, ZTITLE FROM ZBKCOLLECTION WHERE ZDELETEDFLAG = 0"),
            "search_annotation_by_highlighted_text": rows(
                "annotations", annotations.format("ZANNOTATIONSELECTEDTEXT")),
            "search_annotation_by_note": rows("annotations", annotations.format("ZANNOTATIONNOTE")),
            "search_annotation_by_text": rows("annotations", annotations.format(
                "ZANNOTATIONSELECTEDTEXT, ZANNOTATIONREPRESENTATIVETEXT, ZANNOTATIONNOTE")),
        },
    }


def oracle(rows: dict, needle_: str) -> set:
    folded = fold_for_match(needle_)
    return {pk for pk, fields in rows.items()
            if any(f is not None and folded in fold_for_match(f) for f in fields)}


def test_search_facades_match_the_oracle(api, corpus, sql_trace):
    rng = random.Random(SEED + 1)
    hits = 0
    for _ in range(ITERATIONS):
        text = needle(rng, corpus["frags"])
        for method, rows in corpus["scopes"].items():
            expected = oracle(rows, text)
            got = {row.id for row in getattr(api, method)(text)}
            assert got == expected, (method, ascii(text))
            hits += bool(expected)
        leaked = [sql for sql, _ in sql_trace if OPEN in sql]
        assert not leaked, ascii(text)
        sql_trace.clear()
    assert hits, "no needle matched anything: the oracle is trivial"


def test_id_getters_only_raise_index_error(api, corpus):
    rng = random.Random(SEED + 2)
    for _ in range(ITERATIONS):
        for value in (huge_int(rng), needle(rng, corpus["frags"]), str(huge_int(rng))):
            with pytest.raises(IndexError):
                api.get_book_by_id(value)
            with pytest.raises(IndexError):
                api.get_annotation_by_id(value)
            with pytest.raises(CollectionNotFoundError):  # an IndexError
                api.get_collection_by_id(value)


def effective(limit, offset, total: int) -> int:
    start = min(offset or 0, INT64_MAX)
    count = total if limit is None or limit <= 0 or limit > INT64_MAX else limit
    return max(0, min(count, total - start))


def test_limit_and_offset(api, corpus):
    rng = random.Random(SEED + 3)
    draw = lambda: rng.choice([None, 0, 1, 3, -1, -2, -7, huge_int(rng)])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # limit <= 0
        for _ in range(ITERATIONS):
            limit, offset = draw(), draw()
            calls = [
                (lambda: list(api.list_books(limit=limit, offset=offset)), corpus["books"]),
                (lambda: list(api.list_annotations(limit=limit, offset=offset)), corpus["annotations"]),
                (lambda: api.search_annotation_by_text(OPEN, limit=limit, offset=offset), None),
            ]
            for call, total in calls:
                if offset is not None and offset < 0:
                    with pytest.raises(InvalidArgumentError):
                        call()
                    continue
                got = call()
                if total is not None:
                    assert len(got) == effective(limit, offset, total), (limit, offset)


def test_unknown_field_names(api, corpus):
    rng = random.Random(SEED + 4)
    for _ in range(ITERATIONS):
        name = needle(rng, [])
        name = rng.choice(["", "-", "title,", "-title, "]) + name + rng.choice(["", "__in", "__not_gt", ",-id"])
        for call in (
            lambda: api.list_books(order_by=name),
            lambda: api.search_annotation_by_note("x", order_by=[name]),
            lambda: Book.manager.filter(**{name: 1}),
            lambda: Annotation.manager.filter(type__ne=3, **{name: "x"}),
        ):
            with pytest.raises(UnknownFieldError):
                call()
