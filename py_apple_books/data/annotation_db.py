import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils, query_utils
from py_apple_books.utils import get_mappings
from functools import lru_cache

def find_all() -> list:
    """
    Fetches all annotations in the database

    Returns:
        list: list of all annotations in the database
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_all(fields_str, query_utils.ANNOTATION_TABLE_NAME)

def find_by_id(annotation_id: str) -> list:
    """
    Fetches an annotation by its id

    Args:
        annotation_id (str): id of the annotation

    Returns:
        list: list of annotations with the specified id
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "Z_PK", annotation_id)

def find_by_book_id(book_id: str) -> list:
    """
    Fetches all annotations for a book

    Args:
        book_id (str): id of the book

    Returns:
        list: list of annotations for the specified book
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONASSETID", book_id)

def find_deleted() -> list:
    """
    Fetches all deleted annotations

    Returns:
        list: list of deleted annotations
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONDELETED", 1)

def find_not_deleted() -> list:
    """
    Fetches all non-deleted annotations

    Returns:
        list: list of non-deleted annotations
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONDELETED", 0)

def find_underlines() -> list:
    """
    Fetches all underlines

    Returns:
        list: list of underlines
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONISUNDERLINE", 1)

def find_by_chapter(chapter: str) -> list:
    """
    Fetches all annotations for a chapter

    Args:
        chapter (str): chapter number

    Returns:
        list: list of annotations for the specified chapter
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZFUTUREPROOFING5", chapter)

def find_by_location(location: str) -> list:
    """
    Fetches annotation for a location

    Args:
        location (str): location of the annotation

    Returns:
        list: list of annotations for the specified location
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONLOCATION", location)

def find_by_representative_text(representative_text: str) -> list:
    """
    Fetches annotations by representative text

    Args:
        representative_text (str): representative text of the annotation

    Returns:
        list: list of annotations with the specified representative text
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONREPRESENTATIVETEXT LIKE \'{representative_text}\'
    """
    return db_utils.run_query(query)

def find_by_note_text(note_text: str) -> list:
    """
    Fetches annotations by note text

    Args:
        note_text (str): note text of the annotation

    Returns:
        list: list of annotations with the specified note text
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONNOTE LIKE \'{note_text}\'
    """
    return db_utils.run_query(query)

def find_by_selected_text(selected_text: str):
    """
    Fetches annotations by selected text

    Args:
        selected_text (str): selected text of the annotation

    Returns:
        list: list of annotations with the specified selected text
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONSELECTEDTEXT LIKE \'{selected_text}\'
    """
    return db_utils.run_query(query)

def find_highlights() -> list:
    """
    Fetches all highlights

    Returns:
        list: list of highlights
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONISUNDERLINE = 0 AND ZANNOTATIONSTYLE != 0
    """
    return db_utils.run_query(query)

def find_notes() -> list:
    """
    Fetches all notes

    Returns:
        list: list of notes
    """
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONNOTE != ''
    """
    return db_utils.run_query(query)