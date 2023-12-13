import sqlite3
from pathlib import Path
from functools import lru_cache

COLLECTION_DB_PATH = (
    Path.home()
    / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary"
)

COLLECTION_FIELDS = [
    'Z_PK',
    'ZTITLE',
]

@lru_cache(maxsize=1)
def get_db_cursor():
    sqlite_file = list(COLLECTION_DB_PATH.glob("*.sqlite"))[0]
    conn = sqlite3.connect(sqlite_file)
    return conn.cursor()

def get_all_collections():
    cursor = get_db_cursor()
    fields_str = ", ".join(COLLECTION_FIELDS)
    query = f"""
        SELECT {fields_str}
        FROM ZBKCOLLECTION
    """
    cursor.execute(query)
    return cursor.fetchall()

def get_collection_by_id(collection_id: str):
    cursor = get_db_cursor()
    fields_str = ", ".join(COLLECTION_FIELDS)
    query = f"""
        SELECT {fields_str}
        FROM ZBKCOLLECTION
        WHERE Z_PK = ?
    """
    cursor.execute(query, (collection_id,))
    return cursor.fetchone()

def get_collections_by_book_id(book_id: str):
    cursor = get_db_cursor()
    fields_str = ", ".join(COLLECTION_FIELDS)
    query = f"""
        SELECT {fields_str}
        FROM ZBKCOLLECTION
        INNER JOIN ZBKCOLLECTIONMEMBER
        ON ZBKCOLLECTIONMEMBER.ZCOLLECTION = ZBKCOLLECTION.Z_PK
        WHERE ZBKCOLLECTIONMEMBER.ZASSET = ?
    """
    cursor.execute(query, (book_id,))
    return cursor.fetchall()