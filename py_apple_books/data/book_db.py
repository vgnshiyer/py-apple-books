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

def find_all(annotations: bool = False):
    if not annotations:
        return db_utils.find_all(fields_str, BOOK_TABLE_NAME)
    return db_utils.run_query(get_book_annotation_query())

def find_by_id(book_id: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "Z_PK", book_id)
    query = get_book_annotation_query()
    query += f"""WHERE {BOOK_TABLE_NAME}.Z_PK = {book_id}"""
    return db_utils.run_query(query)

def find_by_asset_id(asset_id: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZASSETID", asset_id)
    query = get_book_annotation_query()
    query += f"""WHERE {BOOK_TABLE_NAME}.ZASSETID = \'{asset_id}\'"""
    return db_utils.run_query(query)

def find_by_title(title: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZTITLE", title)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZTITLE = '{title}'"
    print(query)
    return db_utils.run_query(query)

def find_by_author(author: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZAUTHOR", author)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZAUTHOR = '{author}'"
    return db_utils.run_query(query)

def find_by_genre(genre: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZGENRE", genre)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZGENRE = '{genre}'"
    return db_utils.run_query(query)

def find_finished_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISFINISHED", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISFINISHED = 1"
    return db_utils.run_query(query)

def find_unfinished_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISFINISHED", 0)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISFINISHED = 0"
    return db_utils.run_query(query)

def find_explicit_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISEXPLICIT", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISEXPLICIT = 1"
    return db_utils.run_query(query)

def find_locked_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISLOCKED", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISLOCKED = 1"
    return db_utils.run_query(query)

def find_ephemeral_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISEPHEMERAL", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISEPHEMERAL = 1"
    return db_utils.run_query(query)

def find_hidden_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISHIDDEN", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISHIDDEN = 1"
    return db_utils.run_query(query)

def find_sample_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISSAMPLE", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISSAMPLE = 1"
    return db_utils.run_query(query)

def find_store_audiobooks(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZISSTOREAUDIOBOOK", 1)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZISSTOREAUDIOBOOK = 1"
    return db_utils.run_query(query)

def find_by_rating(rating: int, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, BOOK_TABLE_NAME, "ZRATING", rating)
    query = get_book_annotation_query()
    query += f"WHERE {BOOK_TABLE_NAME}.ZRATING = {rating}"
    return db_utils.run_query(query)

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