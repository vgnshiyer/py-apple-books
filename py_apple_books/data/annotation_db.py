import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils, query_utils
from py_apple_books.utils import get_mappings
from functools import lru_cache

def find_all():
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_all(fields_str, query_utils.ANNOTATION_TABLE_NAME)

def find_by_id(annotation_id: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "Z_PK", annotation_id)

def find_by_book_id(book_id: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONASSETID", book_id)

def find_deleted():
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONDELETED", 1)

def find_not_deleted():
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONDELETED", 0)

def find_underlines():
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONISUNDERLINE", 1)

def find_by_chapter(chapter: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZFUTUREPROOFING5", chapter)

def find_by_location(location: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    return db_utils.find_by_field(fields_str, query_utils.ANNOTATION_TABLE_NAME, "ZANNOTATIONLOCATION", location)

def find_by_representative_text(representative_text: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONREPRESENTATIVETEXT LIKE \'{representative_text}\'
    """
    return db_utils.run_query(query)

def find_by_note_text(note_text: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONNOTE LIKE \'{note_text}\'
    """
    return db_utils.run_query(query)

def find_by_selected_text(selected_text: str):
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONSELECTEDTEXT LIKE \'{selected_text}\'
    """
    return db_utils.run_query(query)

def find_highlights():
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONISUNDERLINE = 0 AND ZANNOTATIONSTYLE != 0
    """
    return db_utils.run_query(query)

def find_notes():
    fields_str = query_utils.get_fields_str('Annotation', query_utils.ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {query_utils.ANNOTATION_TABLE_NAME}
        WHERE ZANNOTATIONNOTE != ''
    """
    return db_utils.run_query(query)