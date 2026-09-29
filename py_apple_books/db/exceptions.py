# Defined in py_apple_books.exceptions since 1.10 (so DBError can derive
# from AppleBooksError without an import cycle) and re-exported here.
# Same class objects: ``except DBError`` works whichever module it was
# imported from.
from py_apple_books.exceptions import DBConnectionError, DBError, DBQueryError

__all__ = ["DBError", "DBConnectionError", "DBQueryError"]
