import sqlite3
from pathlib import Path
from urllib.parse import quote
from py_apple_books.db.exceptions import DBError, DBConnectionError, DBQueryError


def find_sqlite_file(directory: Path) -> Path:
    """Return the first ``.sqlite`` file in a directory.

    Apple Books names its stores ``BKLibrary-<digits>.sqlite`` /
    ``AEAnnotation_*.sqlite``; there is exactly one per directory in
    practice. Sorted so the choice is deterministic if that ever
    changes.
    """
    try:
        return sorted(Path(directory).glob("*.sqlite"))[0]
    except IndexError:
        raise DBConnectionError(
            f"No sqlite files found in {directory}. Please open Apple Books at least once."
        )


def _read_only_uri(db_file: Path) -> str:
    """SQLite URI that opens a database read-only.

    The read path must never be able to mutate the user's library —
    Apple Books owns these files. ``quote`` keeps the URI valid if the
    path ever contains characters special to URIs.
    """
    return f"file:{quote(str(db_file))}?mode=ro"


class DBClient:
    def _get_sqlite_file(self, path: Path) -> Path:
        return find_sqlite_file(path)

    def _get_cursor(self, paths: list[tuple[str, Path]]):
        _, first_path = paths[0]
        try:
            # uri=True also enables URI interpretation for the ATTACH
            # statements below, so the attached databases inherit
            # read-only mode.
            conn = sqlite3.connect(_read_only_uri(self._get_sqlite_file(first_path)), uri=True)
            cursor = conn.cursor()
            for db_name, path in paths[1:]:
                cursor.execute(f"ATTACH DATABASE '{_read_only_uri(self._get_sqlite_file(path))}' AS {db_name}")
            self.conn = conn
            return cursor
        except DBConnectionError:
            raise
        except sqlite3.Error as e:
            raise DBConnectionError(f"Error connecting to database: {e}")
        except Exception as e:
            raise DBError(f"Unexpected error while connecting to database: {e}")

    def execute(self, *args, **kwargs):
        raise NotImplementedError

    def close(self):
        self.cursor.close()
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()


class AppleBooksDBClient(DBClient):
    """
    Read-only wrapper for the Apple Books SQLite databases.

    Write access goes through :mod:`py_apple_books.collection_writer`,
    which opens its own short-lived read-write connection with guard
    rails (backup, Books-running check, schema validation, single
    transaction).
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
