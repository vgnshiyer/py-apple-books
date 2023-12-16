import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils
from functools import lru_cache

BOOK_TABLE_NAME = "ZBKLIBRARYASSET"
ANNOTATION_TABLE_NAME = "anno_db.ZAEANNOTATION"

fields_str = db_utils.get_fields_str('Book', BOOK_TABLE_NAME)

@lru_cache(maxsize=1)
def get_book_annotation_query():
    book_fields_str = db_utils.get_fields_str('Book', BOOK_TABLE_NAME)
    annotation_fields_str = db_utils.get_fields_str('Annotation', ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {book_fields_str}, {annotation_fields_str}
        FROM {BOOK_TABLE_NAME}
        LEFT JOIN {ANNOTATION_TABLE_NAME}
        ON {BOOK_TABLE_NAME}.ZASSETID = {ANNOTATION_TABLE_NAME}.ZANNOTATIONASSETID
    """
    return query

def find_all(annotations=False):
    if not annotations:
        return db_utils.find_all(fields_str, BOOK_TABLE_NAME)
    return db_utils.run_query(get_book_annotation_query())

def find_by_id(book_id: str, annotations=False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "Z_PK", book_id)
    query = get_book_annotation_query()
    query += f"""
        WHERE {BOOK_TABLE_NAME}.Z_PK = {book_id}
    """
    return db_utils.run_query(query)

def find_by_asset_id(asset_id: str):
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZASSETID", asset_id)

def find_by_title(title: str):
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZTITLE", title)

def find_by_author(author: str):
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZAUTHOR", author)

def find_by_genre(genre: str):
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZGENRE", genre)

def find_finished_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISFINISHED", 1)

def find_unfinished_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISFINISHED", 0)

def find_explicit_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISEXPLICIT", 1)

def find_locked_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISLOCKED", 1)

def find_ephemeral_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISEPHEMERAL", 1)

def find_hidden_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISHIDDEN", 1)

def find_sample_books():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISSAMPLE", 1)

def find_store_audiobooks():
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISSTOREAUDIOBOOK", 1)

def find_by_rating(rating: int):
    return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZRATING", rating)

def find_by_collection_id(collection_id: str):
    cursor = db_utils.get_db_cursor()
    fields_str = fields_str
    query = f"""
        SELECT {fields_str}
        FROM {BOOK_TABLE_NAME}
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZASSET = {BOOK_TABLE_NAME}.Z_PK
        WHERE ZBKCOLLECTIONMEMBER.ZCOLLECTION = ?
    """
    cursor.execute(query, (collection_id,))
    return cursor.fetchall()