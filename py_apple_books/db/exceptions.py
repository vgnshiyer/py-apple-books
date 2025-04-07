class DBError(Exception):
    pass


class DBConnectionError(DBError):
    pass


class DBQueryError(DBError):
    pass
