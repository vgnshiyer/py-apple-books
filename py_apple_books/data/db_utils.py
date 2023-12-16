from functools import lru_cache
from pathlib import Path
import sqlite3
from py_apple_books.utils import get_mappings

DB_PATHS = [
    (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary"),
    (Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation")
]

@lru_cache(maxsize=1)
def get_db_cursor():
    sqlite_file = list(DB_PATHS[0].glob("*.sqlite"))[0]
    conn = sqlite3.connect(sqlite_file)
    cursor = conn.cursor()

    other_sqlite_file = list(DB_PATHS[1].glob("*.sqlite"))[0]
    cursor.execute(f"ATTACH DATABASE '{other_sqlite_file}' AS anno_db")
    return cursor

def get_fields_str(class_name: str, table_name: str):
    mappings = get_mappings(class_name)
    return ", ".join(f"{table_name}.{mappings[field]} AS {field}" for field in mappings)


def find_all(fields_str: str, table: str):
    cursor = get_db_cursor()
    query = f"""
        SELECT {fields_str}
        FROM {table}
    """
    cursor.execute(query)
    return cursor.fetchall()

def find_by_field(fields_str: str, table: str, field: str, value: str):
    cursor = get_db_cursor()
    query = f"""
        SELECT {fields_str}
        FROM {table}
        WHERE {field} = ?
    """
    cursor.execute(query, (value,))
    return cursor.fetchall()

def run_query(query: str):
    cursor = get_db_cursor()
    cursor.execute(query)
    return cursor.fetchall()