import pathlib
import sqlite3

BOOK_DB_PATH = (
    pathlib.Path.home()
    / 'Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary/'
)

if __name__ == '__main__':
    sqlite_files = list(BOOK_DB_PATH.glob('*.sqlite'))
    if len(sqlite_files) == 0:
        print('No sqlite files found')
        exit(1)
    else:
        for sqlite_file in sqlite_files:
            print('Found sqlite file: {}'.format(sqlite_file))
            db = sqlite3.connect(str(sqlite_file))
            cursor = db.cursor()

            # Get table names
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            print("Table names:")
            print(cursor.fetchall())

            db.close()