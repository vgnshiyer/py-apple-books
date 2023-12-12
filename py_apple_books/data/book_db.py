import sqlite3
from pathlib import Path
from functools import lru_cache

BOOK_DB_PATH = (
    Path.home()
    / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary"
)

@lru_cache(maxsize=1)
def get_db_cursor():
    sqlite_file = list(BOOK_DB_PATH.glob("*.sqlite"))[0]
    conn = sqlite3.connect(sqlite_file)
    return conn.cursor()

def get_all_books(fields: list = None):
    if fields == None:
        raise ValueError("fields not specified")

    cursor = get_db_cursor()
    fields_str = ", ".join(fields)
    query = f"""
        SELECT {fields_str}
        FROM ZBKLIBRARYASSET
    """
    cursor.execute(query)
    return cursor.fetchall()

def get_book_by_id(book_id: str, fields: list = None):
    if fields == None:
        raise ValueError("fields not specified")

    cursor = get_db_cursor()
    fields_str = ", ".join(fields)
    query = f"""
        SELECT {fields_str}
        FROM ZBKLIBRARYASSET
        WHERE Z_PK = ?
    """
    cursor.execute(query, (book_id,))
    return cursor.fetchone()

def get_books_in_collection(collection_id: str, fields: list = None):
    if fields == None:
        raise ValueError("fields not specified")

    cursor = get_db_cursor()
    fields_str = ", ".join([f"ZBKLIBRARYASSET.{field}" for field in fields])
    query = f"""
        SELECT {fields_str}
        FROM ZBKLIBRARYASSET
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZASSET = ZBKLIBRARYASSET.ZASSETID
        WHERE ZBKCOLLECTIONMEMBER.ZCOLLECTION = ?
    """
    cursor.execute(query, (collection_id,))
    return cursor.fetchall()
    
