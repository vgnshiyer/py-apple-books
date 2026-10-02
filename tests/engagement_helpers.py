"""Helpers for the engagement tests (stream 2.5). Synthetic text only.

``realistic_texts(lib)`` rewrites the highlight texts of a populated
``FixtureLibrary`` to an arbitrary synthetic mix of lengths: mostly
sentences (roughly exponential, mean 150 characters), a long tail of
passages (2 %, 400-1,200 characters) and some single words or short
phrases (10 %), so timing tests exercise the classifier and the narrow
queries on varied text rather than on one fixed length.
"""

import random
import sqlite3
from typing import List

from tests import _bootstrap

WORDS = ("the of and a to in is was that for it with as his on be at by had are this from but not "
         "light river window garden letter morning silence harbour lantern meadow").split()
SHORT = ("ephemeral", "Ubiquitous,", "in medias res", "laconic", "“sonder.”", "liminal", "petrichor")

# Whether the strict timing budgets run (they are soft by default: CI
# machines vary).
SLOW = _bootstrap.SLOW


def passage(rng: random.Random, length: int) -> str:
    """A synthetic sentence of about ``length`` characters."""
    out: List[str] = []
    size = 0
    while size < length:
        word = rng.choice(WORDS)
        out.append(word)
        size += len(word) + 1
    return " ".join(out) + "."


def realistic_texts(lib, *, seed: int = 7, short_share: float = 0.10) -> int:
    """Rewrite every type-2 row's selected and representative text of
    ``lib`` (in one transaction); returns the number of short ones."""
    rng = random.Random(seed)
    con = sqlite3.connect(lib.annotation_path)
    try:
        pks = [r[0] for r in con.execute("SELECT Z_PK FROM ZAEANNOTATION WHERE ZANNOTATIONTYPE = 2")]
        updates = []
        shorts = 0
        for pk in pks:
            draw = rng.random()
            if draw < short_share:
                text = rng.choice(SHORT)
                shorts += 1
            elif draw < short_share + 0.02:
                text = passage(rng, rng.randint(400, 1200))
            else:
                text = passage(rng, max(5, int(rng.expovariate(1 / 150))))
            updates.append((text, text, pk))
        con.executemany("UPDATE ZAEANNOTATION SET ZANNOTATIONSELECTEDTEXT = ?, "
                        "ZANNOTATIONREPRESENTATIVETEXT = ? WHERE Z_PK = ?", updates)
        con.commit()
        return shorts
    finally:
        con.close()
