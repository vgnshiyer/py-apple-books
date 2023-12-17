from functools import lru_cache
from py_apple_books.utils import get_mappings

COLLECTION_TABLE_NAME = "ZBKCOLLECTION"
BOOK_TABLE_NAME = "ZBKLIBRARYASSET"
ANNOTATION_TABLE_NAME = "anno_db.ZAEANNOTATION"
COLLECTIONMEMBER_TABLE_NAME = "ZBKCOLLECTIONMEMBER"

@lru_cache(maxsize=5)
def get_fields_str(class_name: str, table_name: str) -> str:
    mappings = get_mappings(class_name)
    return ", ".join(f"{table_name}.{mappings[field]} AS {field}" for field in mappings)

@lru_cache(maxsize=1)
def get_book_annotation_query() -> str:
    book_fields_str = get_fields_str('Book', BOOK_TABLE_NAME)
    annotation_fields_str = get_fields_str('Annotation', ANNOTATION_TABLE_NAME)
    query = f"""
        SELECT {book_fields_str}, {annotation_fields_str}
        FROM {BOOK_TABLE_NAME}
        LEFT JOIN {ANNOTATION_TABLE_NAME}
        ON {BOOK_TABLE_NAME}.ZASSETID = {ANNOTATION_TABLE_NAME}.ZANNOTATIONASSETID
    """
    return query

@lru_cache(maxsize=1)
def book_collection_member_query() -> str:
    fields_str = get_fields_str('Book', BOOK_TABLE_NAME)
    query = f"""
        SELECT {fields_str}
        FROM {BOOK_TABLE_NAME}
        INNER JOIN {COLLECTIONMEMBER_TABLE_NAME}
        ON {COLLECTIONMEMBER_TABLE_NAME}.ZASSET = {BOOK_TABLE_NAME}.Z_PK
    """
    return query

@lru_cache(maxsize=1)
def get_book_collection_query() -> str:
    collection_fields_str = get_fields_str('Collection', COLLECTION_TABLE_NAME)
    book_fields_str = get_fields_str('Book', BOOK_TABLE_NAME)
    query = f"""
        SELECT {collection_fields_str}, {book_fields_str}
        FROM {COLLECTION_TABLE_NAME}
        JOIN {COLLECTIONMEMBER_TABLE_NAME}
        ON {COLLECTION_TABLE_NAME}.Z_PK = {COLLECTIONMEMBER_TABLE_NAME}.ZCOLLECTION
        JOIN {BOOK_TABLE_NAME}
        ON {COLLECTIONMEMBER_TABLE_NAME}.ZASSETID = {BOOK_TABLE_NAME}.ZASSETID
    """
    return query

@lru_cache(maxsize=1)
def get_collection_collection_member_query() -> str:
    collection_fields_str = get_fields_str('Collection', COLLECTION_TABLE_NAME)
    query = f"""
        SELECT {collection_fields_str}
        FROM {COLLECTION_TABLE_NAME}
        JOIN {COLLECTIONMEMBER_TABLE_NAME}
        ON {COLLECTION_TABLE_NAME}.Z_PK = {COLLECTIONMEMBER_TABLE_NAME}.ZCOLLECTION
    """
    return query
