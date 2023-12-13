import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils

BOOK_DB_PATH = (
    Path.home()
    / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary"
)

BOOK_FIELDS = [
    'Z_PK',
    'ZASSETID',
    'ZTITLE',
    'ZAUTHOR',
    'ZBOOKDESCRIPTION',
    'ZGENRE',
    'ZPATH'
]

TABLE_NAME = "ZBKLIBRARYASSET"

def find_all():
    return db_utils.find_all(BOOK_DB_PATH, BOOK_FIELDS, TABLE_NAME)

def find_by_id(book_id: str):
    return db_utils.find_by_field(BOOK_DB_PATH, BOOK_FIELDS, TABLE_NAME, "Z_PK", book_id)

def find_by_asset_id(asset_id: str):
    return db_utils.find_by_field(BOOK_DB_PATH, BOOK_FIELDS, TABLE_NAME, "ZASSETID", asset_id)

def find_by_title(title: str):
    return db_utils.find_by_field(BOOK_DB_PATH, BOOK_FIELDS, TABLE_NAME, "ZTITLE", title)

def find_by_author(author: str):
    return db_utils.find_by_field(BOOK_DB_PATH, BOOK_FIELDS, TABLE_NAME, "ZAUTHOR", author)

def find_by_genre(genre: str):
    return db_utils.find_by_field(BOOK_DB_PATH, BOOK_FIELDS, TABLE_NAME, "ZGENRE", genre)

def find_by_collection_id(collection_id: str):
    cursor = db_utils.get_db_cursor(BOOK_DB_PATH)
    fields_str = ", ".join([f"{TABLE_NAME}.{field}" for field in BOOK_FIELDS])
    query = f"""
        SELECT {fields_str}
        FROM {TABLE_NAME}
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZASSETID = {TABLE_NAME}.ZASSETID
        WHERE ZBKCOLLECTIONMEMBER.ZCOLLECTION = ?
    """
    cursor.execute(query, (collection_id,))
    return cursor.fetchall()