from dataclasses import dataclass
import pathlib

@dataclass
class Book:
    id_: str # Z_PK
    asset_id: str # ZASSETID
    title: str # ZTITLE
    author: str # ZAUTHOR
    description: str # ZBOOKDESCRIPTION
    genre: str # ZGENRE
    path: pathlib.Path # ZPATH