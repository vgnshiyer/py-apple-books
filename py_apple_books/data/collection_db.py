import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils
from functools import lru_cache

COLLECTION_TABLE_NAME = "ZBKCOLLECTION"
BOOK_TABLE_NAME = "ZBKLIBRARYASSET"
COLLECTIONMEMBER_TABLE_NAME = "ZBKCOLLECTIONMEMBER"

fields_str = db_utils.get_fields_str('Collection', COLLECTION_TABLE_NAME)

@lru_cache(maxsize=1)
def get_book_collection_query():
    collection_fields_str = db_utils.get_fields_str('Collection', COLLECTION_TABLE_NAME)
    book_fields_str = db_utils.get_fields_str('Book', BOOK_TABLE_NAME)
    query = f"""
        SELECT {collection_fields_str}, {book_fields_str}
        FROM {COLLECTION_TABLE_NAME}
        JOIN {COLLECTIONMEMBER_TABLE_NAME}
        ON {COLLECTION_TABLE_NAME}.Z_PK = {COLLECTIONMEMBER_TABLE_NAME}.ZCOLLECTION
        JOIN {BOOK_TABLE_NAME}
        ON {COLLECTIONMEMBER_TABLE_NAME}.ZASSETID = {BOOK_TABLE_NAME}.ZASSETID
    """
    return query

def find_all(books: bool = False):
    if not books:
        return db_utils.find_all(fields_str, COLLECTION_TABLE_NAME)
    return db_utils.run_query(get_book_collection_query())

def find_by_id(collection_id: str, books: bool = False):
    if not books:
        return db_utils.find_by_field(fields_str, COLLECTION_TABLE_NAME, "Z_PK", collection_id)
    query = get_book_collection_query() + f"WHERE {COLLECTION_TABLE_NAME}.Z_PK = {collection_id}"
    return db_utils.run_query(query)

def find_by_name(collection_name: str, books: bool = False):
    if not books:
        return db_utils.find_by_field(fields_str, COLLECTION_TABLE_NAME, "ZTITLE", collection_name)
    query = get_book_collection_query() + f"WHERE {COLLECTION_TABLE_NAME}.ZTITLE = {collection_name}"
    return db_utils.run_query(query)

def find_by_book_id(book_id: str):
    cursor = db_utils.get_db_cursor(COLLECTION_DB_PATH)
    fields_str = db_utils.get_fields_str('Collection', COLLECTION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {COLLECTION_TABLE_NAME}
        INNER JOIN {COLLECTIONMEMBER_TABLE_NAME}
        ON {COLLECTIONMEMBER_TABLE_NAME}.ZCOLLECTION = ZBKCOLLECTION.Z_PK
        WHERE {COLLECTIONMEMBER_TABLE_NAME}.ZASSET = ?
    """
    cursor.execute(query, (book_id,))
    return cursor.fetchall()