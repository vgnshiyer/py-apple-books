import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils
from py_apple_books.utils import get_mappings
from functools import lru_cache

BOOK_DB_PATH = (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary")

TABLE_NAME = "ZBKLIBRARYASSET"

@lru_cache(maxsize=1)
def get_fields_str():
    class_name = "Book"
    mappings = get_mappings(class_name)
    return ", ".join(f"{TABLE_NAME}.{mappings[field]} AS {field}" for field in mappings)

def find_all():
    return db_utils.find_all(BOOK_DB_PATH, get_fields_str(), TABLE_NAME)

def find_by_id(book_id: str):
    return db_utils.find_by_field(BOOK_DB_PATH, get_fields_str(), TABLE_NAME, "Z_PK", book_id)

def find_by_asset_id(asset_id: str):
    return db_utils.find_by_field(BOOK_DB_PATH, get_fields_str(), TABLE_NAME, "ZASSETID", asset_id)

def find_by_title(title: str):
    return db_utils.find_by_field(BOOK_DB_PATH, get_fields_str(), TABLE_NAME, "ZTITLE", title)

def find_by_author(author: str):
    return db_utils.find_by_field(BOOK_DB_PATH, get_fields_str(), TABLE_NAME, "ZAUTHOR", author)

def find_by_genre(genre: str):
    return db_utils.find_by_field(BOOK_DB_PATH, get_fields_str(), TABLE_NAME, "ZGENRE", genre)

def find_by_collection_id(collection_id: str):
    cursor = db_utils.get_db_cursor(BOOK_DB_PATH)
    fields_str = get_fields_str()
    query = f"""
        SELECT {fields_str}
        FROM {TABLE_NAME}
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZASSET = {TABLE_NAME}.Z_PK
        WHERE ZBKCOLLECTIONMEMBER.ZCOLLECTION = ?
    """
    cursor.execute(query, (collection_id,))
    return cursor.fetchall()