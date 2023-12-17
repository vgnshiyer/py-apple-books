import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils, query_utils
from functools import lru_cache

def find_all(annotations: bool = False):
    if not annotations:
        fields_str = db_utils.get_fields_str('Book', query_utils.BOOK_TABLE_NAME)
        return db_utils.find_all(fields_str, BOOK_TABLE_NAME)
    return db_utils.run_query(query_utils.get_book_annotation_query())

def find_by_id(book_id: str, annotations: bool = False):
    if not annotations:
        fields_str = query_utils.get_fields_str('Book', query_utils.BOOK_TABLE_NAME)
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "Z_PK", book_id)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.Z_PK = {book_id}"
    return db_utils.run_query(query)

def find_by_asset_id(asset_id: str, annotations: bool = False):
    if not annotations:
        fields_str = query_utils.get_fields_str('Book', query_utils.BOOK_TABLE_NAME)
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZASSETID", asset_id)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZASSETID = \'{asset_id}\'"
    return db_utils.run_query(query)

def find_by_title(title: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZTITLE", title)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZTITLE = '{title}'"
    print(query)
    return db_utils.run_query(query)

def find_by_author(author: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZAUTHOR", author)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZAUTHOR = '{author}'"
    return db_utils.run_query(query)

def find_by_genre(genre: str, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZGENRE", genre)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZGENRE = '{genre}'"
    return db_utils.run_query(query)

def find_finished_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISFINISHED", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISFINISHED = 1"
    return db_utils.run_query(query)

def find_unfinished_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISFINISHED", 0)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISFINISHED = 0"
    return db_utils.run_query(query)

def find_explicit_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISEXPLICIT", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISEXPLICIT = 1"
    return db_utils.run_query(query)

def find_locked_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISLOCKED", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISLOCKED = 1"
    return db_utils.run_query(query)

def find_ephemeral_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISEPHEMERAL", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISEPHEMERAL = 1"
    return db_utils.run_query(query)

def find_hidden_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISHIDDEN", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISHIDDEN = 1"
    return db_utils.run_query(query)

def find_sample_books(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISSAMPLE", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISSAMPLE = 1"
    return db_utils.run_query(query)

def find_store_audiobooks(annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISSTOREAUDIOBOOK", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISSTOREAUDIOBOOK = 1"
    return db_utils.run_query(query)

def find_by_rating(rating: int, annotations: bool = False):
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZRATING", rating)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZRATING = {rating}"
    return db_utils.run_query(query)

def find_by_collection_id(collection_id: str):
    query = query_utils.book_collection_member_query()
    query += f"WHERE {query_utils.COLLECTIONMEMBER_TABLE_NAME}.ZCOLLECTION = {collection_id}"
    return db_utils.run_query(query)