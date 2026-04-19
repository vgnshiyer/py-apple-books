"""Shared pytest fixtures.

Includes a helper to build unzipped EPUB bundle directories in a tmp
path. Apple Books stores EPUBs unzipped on disk, so fixture EPUBs are
written as directories rather than ``.zip`` files — ``BookContent``
only supports that layout.
"""

from __future__ import annotations

import pathlib
import zipfile
from dataclasses import dataclass
from typing import List, Optional

import pytest
from ebooklib import epub


@dataclass
class _BuiltEpub:
    """An unzipped EPUB bundle plus extra metadata for tests to assert against."""

    path: pathlib.Path
    chapter_hrefs: List[str]


def _unzip_epub_to_dir(zipped: pathlib.Path, dir_path: pathlib.Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zipped) as z:
        z.extractall(dir_path)


def _write_book_as_unzipped_epub(
    book: epub.EpubBook, dir_path: pathlib.Path, tmp: pathlib.Path
) -> pathlib.Path:
    """Write ``book`` via ebooklib to a zip, then extract into ``dir_path``."""
    zipped = tmp / "tmp.epub"
    epub.write_epub(str(zipped), book)
    _unzip_epub_to_dir(zipped, dir_path)
    return dir_path


@pytest.fixture
def epub_factory(tmp_path):
    """Factory fixture that builds a minimal unzipped EPUB on demand.

    Accepts customization via keyword arguments so individual tests can
    shape their own fixtures (nested ToC, shared-file siblings, etc.).
    """

    counter = {"n": 0}

    def _build(
        chapter_titles: Optional[List[str]] = None,
        title: str = "Test Book",
        author: str = "Test Author",
    ) -> _BuiltEpub:
        counter["n"] += 1
        book = epub.EpubBook()
        book.set_identifier(f"test-{counter['n']}")
        book.set_title(title)
        book.set_language("en")
        book.add_author(author)

        chapter_titles = chapter_titles or ["Chapter 1", "Chapter 2", "Chapter 3"]
        chapters = []
        hrefs = []
        for i, ct in enumerate(chapter_titles, start=1):
            href = f"chap{i}.xhtml"
            c = epub.EpubHtml(title=ct, file_name=href, lang="en")
            c.content = (
                f"<html><body>"
                f"<h1>{ct}</h1>"
                f"<p>Body paragraph for {ct}. It contains meaningful text.</p>"
                f"<p>A second paragraph with more words for testing.</p>"
                f"</body></html>"
            )
            book.add_item(c)
            chapters.append(c)
            hrefs.append(href)

        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = tuple(
            epub.Link(c.file_name, c.title, f"link{i}")
            for i, c in enumerate(chapters, start=1)
        )
        book.spine = ["nav"] + chapters

        dir_path = tmp_path / f"book-{counter['n']}.epub"
        _write_book_as_unzipped_epub(book, dir_path, tmp_path)
        return _BuiltEpub(path=dir_path, chapter_hrefs=hrefs)

    return _build


@pytest.fixture
def simple_epub(epub_factory):
    """A default 3-chapter EPUB bundle."""
    return epub_factory()
