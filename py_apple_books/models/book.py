from dataclasses import dataclass
import pathlib

@dataclass
class Book:
    id_: str
    asset_id: str
    title: str
    author: str
    description: str
    genre: str
    path: pathlib.Path