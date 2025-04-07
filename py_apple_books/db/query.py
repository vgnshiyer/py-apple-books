from py_apple_books.db.clause import Where
from typing import List, Optional, Any, Union, Dict
from py_apple_books.db.client import DBClient


class QueryCompiler:
    """SQL query compiler"""

    def __init__(self, client: DBClient):
        self.client = client

    def execute(self, query: str) -> list[Any]:
        return self.client.execute(query)


class Query:
    """SQL query builder"""

    # TODO: add join query support (single and multi-level)

    @staticmethod
    def select(table_name: str,
               fields: Union[List[str], str] = '*',
               where: Optional[List[Where]] = None,
               order_by: Optional[str] = None,
               limit: Optional[int] = None,
               use_or: bool = False) -> str:
        """
        Build and execute a SELECT query

        Args:
            table_name: The name of the table
            fields: The fields to select
            where: The WHERE clause
            order_by: The ORDER BY clause
            limit: The LIMIT clause
        """
        if isinstance(fields, list):
            fields_str = ', '.join(fields)
        else:
            fields_str = fields

        query = f"SELECT {fields_str} FROM {table_name}"

        if where:
            where_clauses = [str(clause) for clause in where]
            if use_or:
                query += f" WHERE {' OR '.join(where_clauses)}"
            else:
                query += f" WHERE {' AND '.join(where_clauses)}"

        if order_by:
            query += f" ORDER BY {order_by}"

        if limit:
            query += f" LIMIT {limit}"

        return query

    @staticmethod
    def insert(table_name: str,
               data: Dict[str, Any]) -> str:
        """
        Build and execute an INSERT query
        """
        fields = ', '.join(data.keys())
        values = ', '.join([f"'{value}'" for value in data.values()])
        query = f"INSERT INTO {table_name} ({fields}) VALUES ({values})"
        return query

    @staticmethod
    def update(table_name: str,
               data: Dict[str, Any],
               where: List[Where]) -> str:
        """
        Build and execute an UPDATE query
        """
        set_clause = ', '.join([f"{field} = '{value}'" for field, value in data.items()])
        where_clause = ' AND '.join([str(clause) for clause in where])
        query = f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}"
        return query

    @staticmethod
    def delete(table_name: str,
               where: List[Where]) -> str:
        """
        Build and execute a DELETE query
        """
        where_clause = ' AND '.join([str(clause) for clause in where])
        query = f"DELETE FROM {table_name} WHERE {where_clause}"
        return query
