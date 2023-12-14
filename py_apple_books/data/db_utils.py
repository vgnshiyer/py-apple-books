from functools import lru_cache
from pathlib import Path
import sqlite3

@lru_cache(maxsize=1)
def get_db_cursor(db_path: Path):
    sqlite_file = list(db_path.glob("*.sqlite"))[0]
    conn = sqlite3.connect(sqlite_file)
    return conn.cursor()

def find_all(db_path: Path, fields_str: str, table: str):
    cursor = get_db_cursor(db_path)
    query = f"""
        SELECT {fields_str}
        FROM {table}
    """
    cursor.execute(query)
    return cursor.fetchall()

def find_by_field(db_path: Path, fields_str: str, table: str, field: str, value: str):
    cursor = get_db_cursor(db_path)
    query = f"""
        SELECT {fields_str}
        FROM {table}
        WHERE {field} = ?
    """
    cursor.execute(query, (value,))
    return cursor.fetchone()