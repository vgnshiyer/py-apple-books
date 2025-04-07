from py_apple_books.db import QueryCompiler
from py_apple_books.db import AppleBooksDBClient
from py_apple_books.db import Query
from py_apple_books.db.clause import Where
from typing import Callable, Any, List
from functools import partial


class ModelIterable:
    def __init__(self, callable: Callable[[], list[Any]], model_class):
        self.run_query = callable
        self.model_class = model_class

    def __iter__(self):
        results = self.run_query()
        for result in results:
            yield self.model_class.from_db(result)

    def __len__(self):
        results = self.run_query()
        return len(results)

    def __getitem__(self, index: int) -> Any:
        results = self.run_query()
        return self.model_class.from_db(results[index])


class ModelManager:
    def __init__(self, model_class):
        self.model_class = model_class
        self.compiler = QueryCompiler(AppleBooksDBClient())

        self.model_name = self.model_class.__name__
        self.table_name = None
        if self.model_name != 'Model':
            self.table_name = self.model_class._get_mappings('Tables')[self.model_name.lower()]

    def _create_callable(self, query: str) -> Callable[[], list[Any]]:
        return partial(self.compiler.execute, query)

    def _get_db_field(self, field: str) -> str:
        return self.model_class._get_mappings(self.model_name)[field]

    def _get_fields(self, only: List[str] = None) -> List[str]:
        fields = list(self.model_class._get_mappings(self.model_name).values())
        if only:
            fields = [field for field in fields if field in only]
        return fields

    def all(self, only: List[str] = None) -> ModelIterable:
        fields = self._get_fields(only)
        query = Query.select(self.table_name, fields=fields)
        return ModelIterable(callable=self._create_callable(query), model_class=self.model_class)

    def filter(self, only: List[str] = None, use_or: bool = False, **filters) -> ModelIterable:
        fields = self._get_fields(only)

        where_clauses = []
        for field, value in filters.items():
            if field.endswith('__contains'):
                where_clauses.append(Where(self._get_db_field(field.split('__')[0]), f'%{value}%', operator='LIKE'))
            elif field.endswith('__in'):
                where_clauses.append(Where(self._get_db_field(field.split('__')[0]), value, operator='IN'))
            elif field.endswith('__gt'):
                where_clauses.append(Where(self._get_db_field(field.split('__')[0]), value, operator='>'))
            elif field.endswith('__gte'):
                where_clauses.append(Where(self._get_db_field(field.split('__')[0]), value, operator='>='))
            elif field.endswith('__lt'):
                where_clauses.append(Where(self._get_db_field(field.split('__')[0]), value, operator='<'))
            else:
                where_clauses.append(Where(self._get_db_field(field), value, operator='='))

        query = Query.select(self.table_name, fields=fields, where=where_clauses, use_or=use_or)
        return ModelIterable(callable=self._create_callable(query), model_class=self.model_class)

    # TODO
    def add(self, data: dict) -> str:
        pass

    def update(self, id: int, data: dict) -> str:
        pass

    def delete(self, id: int) -> str:
        pass
