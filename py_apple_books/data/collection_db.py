import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils, query_utils
from functools import lru_cache

def find_all(books: bool = False) -> list:
    """
    Fetches all collections in the database

    Returns:
        list: list of all collections in the database
    """
    if not books:
        fields_str = query_utils.get_fields_str('Collection', query_utils.COLLECTION_TABLE_NAME)
        return db_utils.find_all(fields_str, query_utils.COLLECTION_TABLE_NAME)
    return db_utils.run_query(query_utils.get_book_collection_query())

def find_by_id(collection_id: str, books: bool = False) -> list:
    """
    Fetches a collection by its id

    Args:
        collection_id (str): id of the collection

    Returns:
        list: list of collections with the specified id
    """
    if not books:
        fields_str = query_utils.get_fields_str('Collection', query_utils.COLLECTION_TABLE_NAME)
        return db_utils.find_by_field(fields_str, query_utils.COLLECTION_TABLE_NAME, "Z_PK", collection_id)
    query = query_utils.get_book_collection_query() + f"WHERE {query_utils.COLLECTION_TABLE_NAME}.Z_PK = {collection_id}"
    return db_utils.run_query(query)

def find_by_name(collection_name: str, books: bool = False) -> list:
    """
    Fetches a collection by its name

    Args:
        collection_name (str): name of the collection

    Returns:
        list: list of collections with the specified name
    """
    if not books:
        fields_str = query_utils.get_fields_str('Collection', query_utils.COLLECTION_TABLE_NAME)
        return db_utils.find_by_field(fields_str, query_utils.COLLECTION_TABLE_NAME, "ZTITLE", collection_name)
    query = query_utils.get_book_collection_query() + f"WHERE {query_utils.COLLECTION_TABLE_NAME}.ZTITLE = {collection_name}"
    return db_utils.run_query(query)

def find_by_book_id(book_id: str) -> list:
    """
    Fetches all collections for a book

    Args:
        book_id (str): id of the book

    Returns:
        list: list of collections for the specified book
    """
    query = query_utils.get_book_collection_query() + f"WHERE {query_utils.BOOK_TABLE_NAME}.Z_PK = {book_id}"
    return db_utils.run_query(query)