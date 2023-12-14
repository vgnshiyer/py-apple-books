import sqlite3
from pathlib import Path
from py_apple_books.data import db_utils
from py_apple_books.utils import get_mappings
from functools import lru_cache

ANNOTATION_DB_PATH = (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation")

TABLE_NAME = "ZAEANNOTATION"

def find_all():
    return db_utils.find_all(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME)

def find_by_id(annotation_id: str):
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "Z_PK", annotation_id)

def find_by_book_id(book_id: str):
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONASSETID", book_id)

def find_deleted():
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONDELETED", 1)

def find_not_deleted():
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONDELETED", 0)

def find_by_style(style: int):
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONSTYLE", style)

def find_underlines():
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONISUNDERLINE", 1)

def find_by_chapter(chapter: str):
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZFUTUREPROOFING5", chapter)

def find_by_location(location: str):
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONLOCATION", location)

def find_by_representative_text(representative_text: str):
    return db_utils.find_by_field(ANNOTATION_DB_PATH, db_utils.get_fields_str("Annotation", TABLE_NAME), TABLE_NAME, "ZANNOTATIONREPRESENTATIVETEXT", representative_text)