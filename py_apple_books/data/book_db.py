import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils, query_utils
from functools import lru_cache

def find_all(annotations: bool = False) -> list:
    """
    Fetches all books in the database

    Returns:
        list: list of all books in the database
    """
    if not annotations:
        fields_str = query_utils.get_fields_str('Book', query_utils.BOOK_TABLE_NAME)
        return db_utils.find_all(fields_str, query_utils.BOOK_TABLE_NAME)
    return db_utils.run_query(query_utils.get_book_annotation_query())

def find_by_id(book_id: str, annotations: bool = False) -> list:
    """
    Fetches a book by its id

    Args:
        book_id (str): id of the book

    Returns:
        list: list of books with the specified id
    """
    if not annotations:
        fields_str = query_utils.get_fields_str('Book', query_utils.BOOK_TABLE_NAME)
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "Z_PK", book_id)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.Z_PK = {book_id}"
    return db_utils.run_query(query)

def find_by_asset_id(asset_id: str, annotations: bool = False) -> list:
    """
    Fetches a book by its asset id

    Args:
        asset_id (str): asset id of the book

    Returns:
        list: list of books with the specified asset id
    """
    if not annotations:
        fields_str = query_utils.get_fields_str('Book', query_utils.BOOK_TABLE_NAME)
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZASSETID", asset_id)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZASSETID = \'{asset_id}\'"
    return db_utils.run_query(query)

def find_by_title(title: str, annotations: bool = False) -> list:
    """
    Fetches a book by its title

    Args:
        title (str): title of the book

    Returns:
        list: list of books with the specified title
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZTITLE", title)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZTITLE = '{title}'"
    print(query)
    return db_utils.run_query(query)

def find_by_author(author: str, annotations: bool = False) -> list:
    """
    Fetches a book by its author

    Args:
        author (str): author of the book

    Returns:
        list: list of books with the specified author
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZAUTHOR", author)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZAUTHOR = '{author}'"
    return db_utils.run_query(query)

def find_by_genre(genre: str, annotations: bool = False) -> list:
    """
    Fetches a book by its genre
    
    Args:
        genre (str): genre of the book

    Returns:
        list: list of books with the specified genre
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZGENRE", genre)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZGENRE = '{genre}'"
    return db_utils.run_query(query)

def find_finished_books(annotations: bool = False) -> list:
    """
    Fetches all finished books

    Returns:    
        list: list of all finished books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISFINISHED", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISFINISHED = 1"
    return db_utils.run_query(query)

def find_unfinished_books(annotations: bool = False) -> list:
    """
    Fetches all unfinished books

    Returns:
        list: list of all unfinished books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISFINISHED", 0)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISFINISHED = 0"
    return db_utils.run_query(query)

def find_explicit_books(annotations: bool = False) -> list:
    """
    Fetches all explicit books
    
    Returns:
        list: list of all explicit books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISEXPLICIT", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISEXPLICIT = 1"
    return db_utils.run_query(query)

def find_locked_books(annotations: bool = False) -> list:
    """
    Fetches all locked books

    Returns:
        list: list of all locked books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISLOCKED", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISLOCKED = 1"
    return db_utils.run_query(query)

def find_ephemeral_books(annotations: bool = False) -> list:
    """
    Fetches all ephemeral books

    Returns:
        list: list of all ephemeral books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISEPHEMERAL", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISEPHEMERAL = 1"
    return db_utils.run_query(query)

def find_hidden_books(annotations: bool = False) -> list:
    """
    Fetches all hidden books

    Returns:
        list: list of all hidden books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISHIDDEN", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISHIDDEN = 1"
    return db_utils.run_query(query)

def find_sample_books(annotations: bool = False) -> list:
    """
    Fetches all sample books
    
    Returns:
        list: list of all sample books
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISSAMPLE", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISSAMPLE = 1"
    return db_utils.run_query(query)

def find_store_audiobooks(annotations: bool = False) -> list:
    """
    Fetches all store audiobooks
    
    Returns:
        list: list of all store audiobooks
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZISSTOREAUDIOBOOK", 1)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZISSTOREAUDIOBOOK = 1"
    return db_utils.run_query(query)

def find_by_rating(rating: int, annotations: bool = False) -> list:
    """
    Fetches all books with the specified rating
    
    Args:   
        rating (int): rating of the book

    Returns:
        list: list of all books with the specified rating
    """
    if not annotations:
        return db_utils.find_by_field(fields_str, query_utils.BOOK_TABLE_NAME, "ZRATING", rating)
    query = query_utils.get_book_annotation_query()
    query += f"WHERE {query_utils.BOOK_TABLE_NAME}.ZRATING = {rating}"
    return db_utils.run_query(query)

def find_by_collection_id(collection_id: str) -> list:
    """
    Fetches all books in a collection

    Args:
        collection_id (str): id of the collection

    Returns:
        list: list of all books in the collection
    """
    query = query_utils.book_collection_member_query()
    query += f"WHERE {query_utils.COLLECTIONMEMBER_TABLE_NAME}.ZCOLLECTION = {collection_id}"
    return db_utils.run_query(query)