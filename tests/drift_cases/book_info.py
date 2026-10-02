"""Drift cases for stream 2.6 (book info): ``get_cached_book_info`` for
the seeded orphan highlight's asset id. The caches sit beside the stores,
so the call returns the same on every store schema, and it needs no
column of either store."""

# The asset id of the seeded "orphan" annotation (test_schema_drift.seed).
ORPHAN_ASSET = "GONE-ASSET"


def seed(lib, rows):
    lib.add_book_info_cache([
        {"asset_id": ORPHAN_ASSET, "title": "Removed Book", "author": "Former Author", "year": "2001"},
    ])
    return None


CASES = {
    "get_cached_book_info": lambda api, rows: api.get_cached_book_info(
        [ORPHAN_ASSET, "UNKNOWN-ASSET", None]),
}
