import sqlite3
from pathlib import Path
from py_apple_books.db.exceptions import DBError, DBConnectionError, DBQueryError


class DBClient:
    def _get_sqlite_file(self, path: Path) -> Path:
        return list(path.glob("*.sqlite"))[0]

    def _get_cursor(self, paths: list[tuple[str, Path]]):
        _, first_path = paths[0]
        try:
            conn = sqlite3.connect(self._get_sqlite_file(first_path))
            cursor = conn.cursor()
            for db_name, path in paths[1:]:
                cursor.execute(f"ATTACH DATABASE '{self._get_sqlite_file(path)}' AS {db_name}")
            return cursor
        except sqlite3.Error as e:
            raise DBConnectionError(f"Error connecting to database: {e}")
        except IndexError:
            raise DBConnectionError("No sqlite files found. Please open iBooks at least once.")
        except Exception as e:
            raise DBError(f"Unexpected error while connecting to database: {e}")

    def execute(self, *args, **kwargs):
        raise NotImplementedError

    def close(self):
        self.cursor.close()


class AppleBooksDBClient(DBClient):
    """
    Wrapper for the Apple Books SQLite database
    """
    book_lib_db = ("lib_db", Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary")
    anno_db = ("anno_db", Path.home() / "Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation")

    def __init__(self):
        self.cursor = self._get_cursor(paths=[self.book_lib_db, self.anno_db])

    def execute(self, query: str) -> list:
        try:
            self.cursor.execute(query)
            return self.cursor.fetchall()
        except sqlite3.Error as e:
            raise DBQueryError(f"Error executing query: {e}")
        except Exception as e:
            raise DBError(f"Unexpected error while executing query: {e}")
