import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils
from functools import lru_cache

COLLECTION_DB_PATH = (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary")

COLLECTION_TABLE_NAME = "ZBKCOLLECTION"
BOOK_TABLE_NAME = "ZBKLIBRARYASSET"

def find_all():
    return db_utils.find_all(COLLECTION_DB_PATH, db_utils.get_fields_str('Collection', TABLE_NAME), TABLE_NAME)

def find_all_with_books():
    collection_fields_str = db_utils.get_fields_str('Collection', COLLECTION_TABLE_NAME)
    book_fields_str = db_utils.get_fields_str('Book', BOOK_TABLE_NAME)
    query = f"""
        SELECT {collection_fields_str}, {book_fields_str}
        FROM {COLLECTION_TABLE_NAME}
    """

def find_by_id(collection_id: str):
    return db_utils.find_by_field(COLLECTION_DB_PATH, db_utils.get_fields_str('Collection', TABLE_NAME), TABLE_NAME, "Z_PK", collection_id)

def find_by_name(collection_name: str):
    return db_utils.find_by_field(COLLECTION_DB_PATH, db_utils.get_fields_str('Collection', TABLE_NAME), TABLE_NAME, "ZTITLE", collection_name)

def find_by_book_id(book_id: str):
    cursor = db_utils.get_db_cursor(COLLECTION_DB_PATH)
    fields_str = db_utils.get_fields_str('Collection', TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {TABLE_NAME}
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZCOLLECTION = ZBKCOLLECTION.Z_PK
        WHERE ZBKCOLLECTIONMEMBER.ZASSET = ?
    """
    cursor.execute(query, (book_id,))
    return cursor.fetchall()