from dataclasses import dataclass
from py_apple_books.models.annotation import Annotation

@dataclass
class Highlight(Annotation):
    # Interactions
    color: str