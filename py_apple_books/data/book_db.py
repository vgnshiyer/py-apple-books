import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils
from functools import lru_cache

BOOK_DB_PATH = (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary")

TABLE_NAME = "ZBKLIBRARYASSET"

def find_all():
    return db_utils.find_all(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME)

def find_by_id(book_id: str):
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "Z_PK", book_id)

def find_by_asset_id(asset_id: str):
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZASSETID", asset_id)

def find_by_title(title: str):
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZTITLE", title)

def find_by_author(author: str):
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZAUTHOR", author)

def find_by_genre(genre: str):
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZGENRE", genre)

def find_finished_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISFINISHED", 1)

def find_unfinished_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISFINISHED", 0)

def find_explicit_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISEXPLICIT", 1)

def find_locked_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISLOCKED", 1)

def find_ephemeral_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISEPHEMERAL", 1)

def find_hidden_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISHIDDEN", 1)

def find_sample_books():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISSAMPLE", 1)

def find_store_audiobooks():
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZISSTOREAUDIOBOOK", 1)

def find_by_rating(rating: int):
    return db_utils.find_by_field(BOOK_DB_PATH, db_utils.get_fields_str('Book', TABLE_NAME), TABLE_NAME, "ZRATING", rating)

def find_by_collection_id(collection_id: str):
    cursor = db_utils.get_db_cursor(BOOK_DB_PATH)
    fields_str = db_utils.get_fields_str('Book', TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {TABLE_NAME}
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZASSET = {TABLE_NAME}.Z_PK
        WHERE ZBKCOLLECTIONMEMBER.ZCOLLECTION = ?
    """
    cursor.execute(query, (collection_id,))
    return cursor.fetchall()