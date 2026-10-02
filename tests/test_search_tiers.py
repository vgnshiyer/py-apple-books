"""The ranked search's tiers (``search._query``) give exactly what they
gave before a repeated substring scan was skipped: same rows, order,
scores, ``matched_all`` and methods, for every scope, ``require_all``
and ``want``, with and without FTS5. The reference below is ``_query``
as it was (every tier's statement run).

Synthetic rows only."""

import random
from typing import List, Optional

import pytest

from py_apple_books import search
from py_apple_books.search import MatchMethod, fold_for_match


def _query_reference(conn, fts: Optional[str], plan, asset_id, include_deleted: bool, require_all: bool,
                     want: Optional[int] = None, tiers: Optional[set] = None) -> List[tuple]:
    """``search._query`` before the skip, statement for statement.
    ``tiers`` gets the tiers (1-4) that listed a row."""
    scope, params = [], []
    if not include_deleted:
        scope.append("live = 1")
    if asset_id is not None:
        scope.append("asset = ?")
        params.append(asset_id)
    extra = "".join(f" AND {s}" for s in scope)
    params.append(-1 if want is None else want)
    hits: List[tuple] = []
    seen = set()

    def enough() -> bool:
        return want is not None and len(hits) >= want

    def add(rows, matched_all: bool, method: MatchMethod, tier: int) -> None:
        for rowid, score in rows:
            if rowid not in seen:
                seen.add(rowid)
                hits.append((rowid, float(score), matched_all, method))
                if tiers is not None:
                    tiers.add(tier)

    def substring(items, connector: str, matched_all: bool, tier: int) -> None:
        conditions, score_terms = [], []
        for _ in items:
            conditions.append("(" + " OR ".join(f"instr({c}, ?) > 0" for c in search._COLUMNS) + ")")
            score_terms.append(" + ".join(f"(instr({c}, ?) > 0) * {w}"
                                          for c, w in zip(search._COLUMNS, search._WEIGHTS)))
        per_item = [item for item in items for _ in search._COLUMNS]
        sql = (f"SELECT rowid, {' + '.join(score_terms)} AS s FROM ann "
               f"WHERE ({f' {connector} '.join(conditions)}){extra} ORDER BY s DESC, rowid DESC LIMIT ?")
        add(conn.execute(sql, per_item + per_item + params).fetchall(), matched_all, MatchMethod.SUBSTRING, tier)

    use_fts = fts is not None and not plan.substring
    weights = ", ".join(str(w) for w in search._WEIGHTS)
    fts_sql = (f"SELECT rowid, -bm25(ann, {weights}) FROM ann WHERE ann MATCH ?{extra} "
               f"ORDER BY bm25(ann, {weights}), rowid DESC LIMIT ?")
    if use_fts:
        add(conn.execute(fts_sql, [" AND ".join(plan.terms)] + params).fetchall(), True, MatchMethod.FTS, 1)
    else:
        substring(plan.items, "AND", True, 1)
    if enough():
        return hits
    if plan.needle is not None:
        substring((plan.needle,), "AND", True, 2)
    if enough():
        return hits
    if not require_all and len(plan.items) > 1:
        if use_fts:
            add(conn.execute(fts_sql, [" OR ".join(plan.terms)] + params).fetchall(), False, MatchMethod.FTS, 3)
        else:
            substring(plan.items, "OR", False, 3)
    if not hits and use_fts:
        long_items = tuple(item for item in plan.items if len(item) >= 3)
        if long_items:
            substring(plan.items, "AND", True, 4)
            if not require_all and len(long_items) < len(plan.items) and not enough():
                substring(long_items, "AND", False, 4)
    return hits


# Books-style text: curly quotes and apostrophes, dashes, ellipses,
# ligatures, accents (precomposed and combining), scripts without spaces.
WORDS = ["habit", "habits", "decision", "making", "indecision", "memory", "memories", "learning",
         "the", "of", "a", "and", "x", "ab", "don’t", "won’t", "it’s", "“quoted”", "‘single’",
         "—", "–", "…", "...", "ﬁnd", "find", "Straße", "strasse", "naïve", "café", "café",
         "co-operate", "e-mail", "50%", "under_score", "Görel", "GÖREL", "名前", "名前です", "학교에",
         "ภาษา", " ", "line\nbreak", "zzqqxv", "qqzzvx", "prefixalpha", "alpha", "omega"]


def _text(rng, low: int = 1, high: int = 9) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(low, high)))


def _database(rng, rows: int, fts: bool, monkeypatch):
    if not fts:
        monkeypatch.setattr(search, "_fts5", False)
    conn, tokenizer = search._new_database()
    data = []
    for pk in range(1, rows + 1):
        sel = _text(rng)
        note = _text(rng) if rng.random() < 0.2 else ""
        rep = f"{_text(rng, 0, 6)} {sel} {_text(rng, 0, 6)}" if rng.random() < 0.7 else ""
        data.append((pk, fold_for_match(sel) or "", fold_for_match(note) or "", fold_for_match(rep) or "",
                     rng.choice(["A", "B", "C"]), 0 if rng.random() < 0.15 else 1))
    conn.executemany("INSERT INTO ann (rowid, sel, note, rep, asset, live) VALUES (?, ?, ?, ?, ?, ?)", data)
    return conn, tokenizer, [row[1] for row in data] + [row[3] for row in data if row[3]]


def _queries(rng, texts) -> List[str]:
    queries = list(WORDS) + ["zzqqxvw", "qzwv", "名前でした", "...!", "—–", '"zzq xxv"', "zzq xxv",
                             "the of", "habit zz", "alpha zz", " ab", "ab ", "  ", "making decision"]
    for _ in range(120):
        kind = rng.random()
        if kind < 0.4:
            text = rng.choice(texts)
            start = rng.randrange(max(1, len(text)))
            queries.append(text[start:start + rng.randint(1, 16)])
        elif kind < 0.6:
            queries.append(_text(rng, 1, 3))
        elif kind < 0.75:
            queries.append(f'"{_text(rng, 2, 3)}" {rng.choice(WORDS)}')
        else:
            queries.append("".join(rng.choice("zqxvwk") for _ in range(rng.randint(3, 9))))  # no hit
    # One item whose needle is not the item: an inner piece of a word is
    # no FTS5 token and no row has the needle, so only tier 4's
    # every-item scan finds it (a skip of that scan loses the hits).
    words = sorted({word for text in texts for word in text.split() if len(word) >= 5})
    for _ in range(30):
        word = rng.choice(words)
        start = rng.randint(1, len(word) - 4)
        piece = word[start:rng.randint(start + 3, len(word) - 1)]
        queries.extend([f"{piece}!", f"the {piece}", f'"{piece}"', f"{piece}?"])
    return queries


@pytest.mark.parametrize("fts", [True, False], ids=["fts5", "no-fts5"])
def test_tiers_equal_the_reference(fts, monkeypatch):
    """Seeded random corpora and queries (words, fragments cut anywhere,
    phrases, typography, CJK, punctuation, no-hit words): the hit lists
    are identical to the reference's in every option, and the repeated
    scan is indeed skipped in many of them. With FTS5, many comparisons
    get their hits from tier 4 only, so a wrong skip of its scan fails."""
    rng = random.Random(1111 if fts else 2222)
    conn, tokenizer, texts = _database(rng, 300, fts, monkeypatch)
    assert (tokenizer is not None) is fts
    calls = compared = repeated = from_tier_4 = 0
    try:
        for query in _queries(rng, texts):
            plan = search._plan(query)
            if plan is None:
                continue
            repeated += plan.items == (plan.needle,)
            for asset_id in (None, "A"):
                for include_deleted in (False, True):
                    for require_all in (False, True):
                        for want in (None, 1, 3, 20):
                            args = (conn, tokenizer, plan, asset_id, include_deleted, require_all, want)
                            tiers = set()
                            got, expected = search._query(*args), _query_reference(*args, tiers=tiers)
                            calls += 1
                            compared += bool(expected)
                            from_tier_4 += 4 in tiers
                            assert got == expected, (ascii(query), asset_id, include_deleted, require_all, want)
    finally:
        conn.close()
    assert calls > 2000 and compared > 1000 and repeated > 30, (calls, compared, repeated)
    if fts:
        assert from_tier_4 > 1000, from_tier_4  # tier 4 runs only with FTS5


def _scans(conn, fn) -> int:
    """The substring scans (statements calling instr) ``fn()`` runs."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return sum("instr(" in s for s in statements)


@pytest.mark.parametrize("query, fts, before, after", [
    ("kkvvjj", True, 2, 1),  # one word, no hit: tier 4 is tier 2 again
    ("zzqqxv", True, 1, 1),  # one word found by tier 1 (FTS5): tier 2 only
    ("kkvvjj jjvvkk", True, 2, 2),  # two words: tier 4 differs (every word, anywhere)
    ('"kkv jjv"', True, 2, 2),  # a phrase: the needle keeps its quotes
    ("efixalph!", True, 2, 2),  # one item, another needle: tier 4 differs, and finds "prefixalpha"
    ("名前でした", True, 2, 1),  # a substring plan of one item: tier 2 is tier 1 again
    ("kkvvjj", False, 2, 1),  # no FTS5: tier 2 is tier 1 again
    ("zzqqxv", False, 2, 1),  # the same, with hits
])
def test_a_repeated_scan_is_skipped(query, fts, before, after, monkeypatch):
    rng = random.Random(3)
    conn, tokenizer, _ = _database(rng, 50, fts, monkeypatch)
    try:
        plan = search._plan(query)
        args = (conn, tokenizer, plan, None, False, False, 20)
        assert _scans(conn, lambda: _query_reference(*args)) == before
        assert _scans(conn, lambda: search._query(*args)) == after
        tiers = set()
        assert search._query(*args) == _query_reference(*args, tiers=tiers)
        if query == "efixalph!":
            assert tiers == {4}
    finally:
        conn.close()
