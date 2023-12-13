class CollectionNotFoundError(Exception):
    def __init__(self, collection_id=None, collection_name=None):
        if collection_id:
            super().__init__(f"Collection with id {collection_id} not found")
        elif collection_name:
            super().__init__(f"Collection with name {collection_name} not found")
        else:
            super().__init__("Collection not found")

class BookNotFoundError(Exception):
    def __init__(self, book_id=None, asset_id=None, title=None, author=None, genre=None, collection_id=None):
        if book_id:
            super().__init__(f"Book with id {book_id} not found")
        elif asset_id:
            super().__init__(f"Book with asset id {asset_id} not found")
        elif title:
            super().__init__(f"Book with title {title} not found")
        elif author:
            super().__init__(f"Book with author {author} not found")
        elif genre:
            super().__init__(f"Book with genre {genre} not found")
        elif collection_id:
            super().__init__(f"Book with collection id {collection_id} not found")
        else:
            super().__init__("Book not found")