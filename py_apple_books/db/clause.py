class Clause:
    def __str__(self):
        pass

    def _escape_like(self, value: str) -> str:
        return f"'{value.replace('%', '\\%').replace('_', '\\_')}'"


class Join(Clause):
    def __init__(self, table: str, on: str, type: str = 'INNER'):
        self.table = table
        self.on = on
        self.type = type

    def __str__(self):
        return f"{self.type} JOIN {self.table} ON {self.on}"


class Where(Clause):
    def __init__(self, field: str, value: str, operator: str = '='):
        self.field = field
        self.value = self._escape_like(value)
        self.operator = operator

    def __str__(self):
        return f"{self.field} {self.operator} {self.value}"
