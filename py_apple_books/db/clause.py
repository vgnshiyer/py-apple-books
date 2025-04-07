class Clause:
    def __str__(self):
        pass

    def _escape_like(self, value: str) -> str:
        return f"'{value.replace('%', '\\%').replace('_', '\\_')}'"

    def _escape_value(self, value) -> str:
        if isinstance(value, str):
            return f"'{value}'"
        return str(value)


class Join(Clause):
    def __init__(self, table: str, on: str, type: str = ''):
        self.table = table
        self.on = on
        self.type = type

    def __str__(self):
        return f"{self.type} JOIN {self.table} ON {self.on}"


class Where(Clause):
    def __init__(self, field: str, value, operator: str = '='):
        self.field = field
        self.value = value
        self.operator = operator
        self.is_list = isinstance(value, (list, tuple))

    def __str__(self):
        if self.operator == 'IN':
            # Handle lists for IN operator
            if isinstance(self.value, str):
                # If it's already a comma-separated string, use it directly
                formatted_value = f"({self.value})"
            else:
                # Format each item separately and join them
                formatted_items = [self._escape_value(item) for item in self.value]
                formatted_value = f"({', '.join(formatted_items)})"
            return f"{self.field} {self.operator} {formatted_value}"
        else:
            # Handle regular operators
            return f"{self.field} {self.operator} {self._escape_value(self.value)}"
