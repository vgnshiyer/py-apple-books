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

    def _format_order_by(self, order_by: str) -> str:
        if order_by:
            field = order_by[1:] if order_by.startswith('-') else order_by
            direction = ' DESC' if order_by.startswith('-') else ''
            order_by = self.model_class._get_mappings(self.model_name)[field] + direction
        return order_by

    def all(self, only: List[str] = None, limit: int = None, order_by: str = None) -> ModelIterable:
        fields = self._get_fields(only)
        order_by = self._format_order_by(order_by)
        query = Query.select(self.table_name, fields=fields, limit=limit, order_by=order_by)
        return ModelIterable(callable=self._create_callable(query), model_class=self.model_class)

    def filter(self, only: List[str] = None, use_or: bool = False, limit: int = None,
               order_by: str = None, **filters) -> ModelIterable:
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
            elif field.endswith('__lte'):
                where_clauses.append(Where(self._get_db_field(field.split('__')[0]), value, operator='<='))
            elif field.endswith('__isnull'):
                db_field = self._get_db_field(field.split('__')[0])
                if value:
                    where_clauses.append(Where(db_field, 'NULL', operator='IS'))
                else:
                    where_clauses.append(Where(db_field, 'NULL', operator='IS NOT'))
            else:
                where_clauses.append(Where(self._get_db_field(field), value, operator='='))

        order_by = self._format_order_by(order_by)
        query = Query.select(self.table_name, fields=fields, where=where_clauses, use_or=use_or,
                             limit=limit, order_by=order_by)
        return ModelIterable(callable=self._create_callable(query), model_class=self.model_class)

    def handle_relations(self, model_object):
        """
        Handle the relations for a model object.
        """
        for relation in self.model_class.relations:
            # handle one-to-many relations
            if relation['type'] == 'OneToMany':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                setattr(model_object, relation['name'],
                        related_model.manager.filter(**{foreign_key: val}))

            # handle many-to-one relations
            elif relation['type'] == 'ManyToOne':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                try:
                    setattr(model_object, relation['name'],
                            related_model.manager.filter(**{foreign_key: val})[0])
                except IndexError:
                    setattr(model_object, relation['name'], None)

            # handle one-to-one relations
            elif relation['type'] == 'OneToOne':
                related_model = relation['related_model']
                foreign_key = relation['foreign_key']
                val = getattr(model_object, foreign_key)
                try:
                    setattr(model_object, relation['name'],
                            related_model.manager.filter(**{foreign_key: val})[0])
                except IndexError:
                    setattr(model_object, relation['name'], None)

            # handle many-to-many relations
            elif relation['type'] == 'ManyToMany':
                related_ids = self.get_related_ids(model_object, relation)
                related_model = relation['related_model']
                setattr(model_object, relation['name'],
                        related_model.manager.filter(**{f"{relation['to_key']}__in": related_ids}))

    def get_related_ids(self, model_object, relation: dict) -> list[str]:
        """
        Get related IDs for many-to-many relationships.
        """
        # This method is a minor contrivance to avoid multi-level join
        join_table = self.model_class._get_mappings('Tables')[relation['join_table']]
        from_key = self.model_class._get_mappings(relation['join_table'])[relation['from_key']]
        to_key = self.model_class._get_mappings(relation['join_table'])[relation['to_key']]
        val = getattr(model_object, relation['from_key'])

        query = Query.select(join_table, fields=[to_key], where=[Where(from_key, val, operator='=')])
        related_ids = self.compiler.execute(query)
        return [row[0] for row in related_ids]

    # TODO
    def add(self, data: dict) -> str:
        pass

    def update(self, id: int, data: dict) -> str:
        pass

    def delete(self, id: int) -> str:
        pass
