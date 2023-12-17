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

class AnnotationNotFoundError(Exception):
    def __init__(self, annotation_id=None, asset_id=None, representative_text=None, selected_text=None, note=None, chapter=None, location=None):
        if annotation_id:
            super().__init__(f"Annotation with id {annotation_id} not found")
        elif asset_id:
            super().__init__(f"Annotation with asset id {asset_id} not found")
        elif representative_text:
            super().__init__(f"Annotation with representative text {representative_text} not found")
        elif selected_text:
            super().__init__(f"Annotation with selected text {selected_text} not found")
        elif note:
            super().__init__(f"Annotation with note {note} not found")
        elif chapter:
            super().__init__(f"Annotation with chapter {chapter} not found")
        elif location:
            super().__init__(f"Annotation with location {location} not found")
        else:
            super().__init__("Annotation not found")