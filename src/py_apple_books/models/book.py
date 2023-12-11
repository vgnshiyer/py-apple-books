from dataclasses import dataclass
from src.py_apple_books.models.collection import Collection

@dataclass
class Book:
    id_: str # Z_PK
    asset_id: str # ZASSETID
    title: str # ZTITLE
    author: str # ZAUTHOR
    description: str # ZBOOKDESCRIPTION
    genre: str # ZGENRE
    path: pathlib.Path # ZPATH
    collections: list[Collection]