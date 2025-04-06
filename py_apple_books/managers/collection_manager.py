from py_apple_books.data import collection_db
from py_apple_books.models.collection import Collection
from py_apple_books.models.book import Book
from py_apple_books.exceptions import CollectionNotFoundError
from py_apple_books.utils import get_mappings
from py_apple_books.managers.base import ManagerBase
from typing import Optional, List, Dict


class CollectionManager(ManagerBase):
    """Manages collection-related operations."""

    def __init__(self):
        super().__init__()
        self._collection_mappings = get_mappings('Collection')
        self._book_mappings = get_mappings('Book')
        self._collection_map: Dict[str, Collection] = {}

    def _get_or_create_collection(self, collection_params: dict) -> Collection:
        id_ = collection_params['id']
        if id_ not in self._collection_map:
            self._collection_map[id_] = Collection(**collection_params)
        return self._collection_map[id_]

    def _create_collection_object(self, raw_collection_data: list) -> Collection:
        collection_fields_len = len(self._collection_mappings)
        collection_dict = dict(zip(self._collection_mappings.keys(), raw_collection_data[:collection_fields_len]))
        collection = self._get_or_create_collection(collection_dict)
        if raw_collection_data[collection_fields_len:]:
            book_dict = dict(zip(self._book_mappings.keys(), raw_collection_data[collection_fields_len:]))
            if book_dict['id']:
                collection.add_book(Book(**book_dict))
        return collection

    def list_collections(self, include_books: bool = False) -> List[Collection]:
        """
        Returns a list of Collection objects.

        Args:
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            list[Collection]: list of Collection objects
        """
        list_of_collections = set()
        raw_collection_data = collection_db.find_all(include_books)
        if not raw_collection_data:
            return []
        for collection_data in raw_collection_data:
            list_of_collections.add(self._create_collection_object(collection_data))
        return list(list_of_collections)

    def get_collection_by_id(self, collection_id: str, include_books: bool = False) -> Optional[Collection]:
        """
        Fetches a Collection object by id.

        Args:
            collection_id (str): collection id
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            Collection: Collection object
        """
        raw_collection_data = collection_db.find_by_id(collection_id, include_books)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_id=collection_id)
        raw_collection_data = raw_collection_data[0]
        return self._create_collection_object(raw_collection_data)

    def get_collection_by_name(self, collection_name: str, include_books: bool = False) -> Optional[Collection]:
        """
        Fetches a Collection object by name.

        Args:
            collection_name (str): collection name
            include_books (bool, optional): if True, includes books in the collection. Defaults to False.

        Returns:
            Collection: Collection object
        """
        raw_collection_data = collection_db.find_by_name(collection_name, include_books)
        if not raw_collection_data:
            raise CollectionNotFoundError(collection_name=collection_name)
        raw_collection_data = raw_collection_data[0]
        return self._create_collection_object(raw_collection_data)
