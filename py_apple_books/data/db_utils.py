from functools import lru_cache
from pathlib import Path
import sqlite3
from py_apple_books.utils import get_mappings

DB_PATHS = [
    (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary"),
    (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation")
]

@lru_cache(maxsize=1)
def get_db_cursor() -> sqlite3.Cursor:
    """
    Gets a cursor for the database

    Returns:
        sqlite3.Cursor: cursor for the database
    """
    sqlite_file = list(DB_PATHS[0].glob("*.sqlite"))[0]
    conn = sqlite3.connect(sqlite_file)
    cursor = conn.cursor()

    other_sqlite_file = list(DB_PATHS[1].glob("*.sqlite"))[0]
    cursor.execute(f"ATTACH DATABASE '{other_sqlite_file}' AS anno_db")
    return cursor


def find_all(fields_str: str, table: str) -> list:
    """
    Fetches all rows in a table

    Args:
        fields_str (str): fields to fetch
        table (str): table to fetch from

    Returns:
        list: list of all rows in the table
    """
    cursor = get_db_cursor()
    query = f"""
        SELECT {fields_str}
        FROM {table}
    """
    cursor.execute(query)
    return cursor.fetchall()

def find_by_field(fields_str: str, table: str, field: str, value: str) -> list:
    """
    Fetches rows in a table by a field

    Args:
        fields_str (str): fields to fetch
        table (str): table to fetch from
        field (str): field to match
        value (str): value to match

    Returns:
        list: list of rows in the table matching the field and value
    """
    cursor = get_db_cursor()
    query = f"""
        SELECT {fields_str}
        FROM {table}
        WHERE {field} = ?
    """
    cursor.execute(query, (value,))
    return cursor.fetchall()

def run_query(query: str) -> list:
    cursor = get_db_cursor()
    cursor.execute(query)
    return cursor.fetchall()