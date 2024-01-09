import unittest
from unittest.mock import patch, MagicMock
from py_apple_books.api import PyAppleBooks
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.highlight import Highlight
from py_apple_books.models.underline import Underline
from py_apple_books.exceptions import CollectionNotFoundError, BookNotFoundError, AnnotationNotFoundError
import time, datetime

class TestBooksApi(unittest.TestCase):
    
    def setUp(self):
        self.booksApi = PyAppleBooks()

    @patch('py_apple_books.data.collection_db.find_all')
    def test_list_collections(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'collection1', 0, 0, 'details1'),
            (2, 'collection2', 0, 0, 'details2')
        ]
        collections = self.booksApi.list_collections()
        self.assertEqual(len(collections), 2)
        self.assertIsInstance(collections[0], Collection)
        self.assertIsInstance(collections[1], Collection)

    @patch('py_apple_books.data.collection_db.find_all')
    def test_list_collections_empty(self, mock_find_all):
        mock_find_all.return_value = []
        collections = self.booksApi.list_collections()
        self.assertEqual(len(collections), 0)

    @patch('py_apple_books.data.collection_db.find_by_id')
    def test_get_collection_by_id(self, mock_find_by_id):
        mock_find_by_id.return_value = [(1, 'collection1', 1, 1, 'details1')]
        collection = self.booksApi.get_collection_by_id('1')
        self.assertIsInstance(collection, Collection)
        self.assertEqual(collection.id, 1)
        self.assertEqual(collection.title, 'collection1')

    @patch('py_apple_books.data.collection_db.find_by_id')
    def test_get_collection_by_id_not_found(self, mock_find_by_id):
        mock_find_by_id.return_value = None
        with self.assertRaises(CollectionNotFoundError):
            self.booksApi.get_collection_by_id('1')

    @patch('py_apple_books.data.collection_db.find_by_name')
    def test_get_collection_by_name(self, mock_find_by_name):
        mock_find_by_name.return_value = [(1, 'collection1', 1, 1, 'details1')]
        collection = self.booksApi.get_collection_by_name('collection1')
        self.assertIsInstance(collection, Collection)
        self.assertEqual(collection.id, 1)
        self.assertEqual(collection.title, 'collection1')

    @patch('py_apple_books.data.collection_db.find_by_name')
    def test_get_collection_by_name_not_found(self, mock_find_by_name):
        mock_find_by_name.return_value = None
        with self.assertRaises(CollectionNotFoundError):
            self.booksApi.get_collection_by_name('collection1')

    @patch('py_apple_books.data.book_db.find_all')
    def test_list_books(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0),
            (2, 'asset2', 'title2', 'author2', 'desc2', 'genre2', 'content_type2', '1', 'file_path2', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0),
        ]
        books = self.booksApi.list_books()
        self.assertEqual(len(books), 2)
        self.assertIsInstance(books[0], Book)
        self.assertIsInstance(books[1], Book)

    @patch('py_apple_books.data.book_db.find_all')
    def test_list_books_empty(self, mock_find_all):
        mock_find_all.return_value = []
        books = self.booksApi.list_books()
        self.assertEqual(len(books), 0)

    @patch('py_apple_books.data.book_db.find_by_id')
    def test_get_book_by_id(self, mock_find_by_id):
        mock_find_by_id.return_value = [(1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0)]
        book = self.booksApi.get_book_by_id('1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.id, 1)

    @patch('py_apple_books.data.book_db.find_by_id')
    def test_get_book_by_id_not_found(self, mock_find_by_id):
        mock_find_by_id.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_id('1')

    @patch('py_apple_books.data.book_db.find_by_asset_id')
    def test_get_book_by_asset_id(self, mock_find_by_asset_id):
        mock_find_by_asset_id.return_value = [(1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0)]
        book = self.booksApi.get_book_by_asset_id('asset1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.asset_id, 'asset1')

    @patch('py_apple_books.data.book_db.find_by_asset_id')
    def test_get_book_by_asset_id_not_found(self, mock_find_by_asset_id):
        mock_find_by_asset_id.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_asset_id('asset1')

    @patch('py_apple_books.data.book_db.find_by_title')
    def test_get_book_by_title(self, mock_find_by_title):
        mock_find_by_title.return_value = [(1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0)]
        book = self.booksApi.get_book_by_title('title1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.title, 'title1')

    @patch('py_apple_books.data.book_db.find_by_title')
    def test_get_book_by_title_not_found(self, mock_find_by_title):
        mock_find_by_title.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_title('title1')

    @patch('py_apple_books.data.book_db.find_by_author')
    def test_get_book_by_author(self, mock_find_by_author):
        mock_find_by_author.return_value = [(1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0)]
        book = self.booksApi.get_books_by_author('author1')
        self.assertIsInstance(book[0], Book)
        self.assertEqual(book[0].author, 'author1')

    @patch('py_apple_books.data.book_db.find_by_author')
    def test_get_book_by_author_not_found(self, mock_find_by_author):
        mock_find_by_author.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_books_by_author('author1')

    @patch('py_apple_books.data.book_db.find_by_genre')
    def test_get_book_by_genre(self, mock_find_by_genre):
        mock_find_by_genre.return_value = [(1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0)]
        book = self.booksApi.get_books_by_genre('genre1')
        self.assertIsInstance(book[0], Book)
        self.assertEqual(book[0].genre, 'genre1')

    @patch('py_apple_books.data.book_db.find_by_genre')
    def test_get_book_by_genre_not_found(self, mock_find_by_genre):
        mock_find_by_genre.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_books_by_genre('genre1')

    @patch('py_apple_books.data.book_db.find_by_collection_id')
    def test_get_books_by_collection_id(self, mock_find_by_collection_id):
        mock_find_by_collection_id.return_value = [
            (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'content_type1', '1', 'file_path1', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0),
            (2, 'asset2', 'title2', 'author2', 'desc2', 'genre2', 'content_type2', '1', 'file_path2', False, 0.0, 0.0, time.time(), time.time(), time.time(), time.time(), False, False, False, False, False, False, 0, 0),
        ]
        books = self.booksApi.get_books_by_collection_id('1')
        self.assertEqual(len(books), 2)
        self.assertIsInstance(books[0], Book)
        self.assertIsInstance(books[1], Book)

    @patch('py_apple_books.data.book_db.find_by_collection_id')
    def test_get_books_by_collection_id_empty(self, mock_find_by_collection_id):
        mock_find_by_collection_id.return_value = []
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_books_by_collection_id('1')

    @patch('py_apple_books.data.annotation_db.find_all')
    def test_list_annotations(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.list_annotations()
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)

    @patch('py_apple_books.data.annotation_db.find_all')
    def test_list_annotations_empty(self, mock_find_all):
        mock_find_all.return_value = []
        annotations = self.booksApi.list_annotations()
        self.assertEqual(len(annotations), 0)

    @patch('py_apple_books.data.annotation_db.find_by_id')
    def test_get_annotation_by_id(self, mock_find_by_id):
        mock_find_by_id.return_value = [(1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1')]
        annotation = self.booksApi.get_annotation_by_id('1')
        self.assertIsInstance(annotation, Annotation)
        self.assertEqual(annotation.id, 1)

    @patch('py_apple_books.data.annotation_db.find_by_id')
    def test_get_annotation_by_id_not_found(self, mock_find_by_id):
        mock_find_by_id.return_value = None
        with self.assertRaises(AnnotationNotFoundError):
            self.booksApi.get_annotation_by_id('1')

    @patch('py_apple_books.data.annotation_db.find_by_book_id')
    def test_get_annotations_by_asset_id(self, mock_find_by_book_id):
        mock_find_by_book_id.return_value = [
            (1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.get_annotations_by_book_id('asset1')
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)

    @patch('py_apple_books.data.annotation_db.find_by_book_id')
    def test_get_annotations_by_asset_id_empty(self, mock_find_by_book_id):
        mock_find_by_book_id.return_value = []
        with self.assertRaises(AnnotationNotFoundError):
            self.booksApi.get_annotations_by_book_id('asset1')

    @patch('py_apple_books.data.annotation_db.find_by_representative_text')
    def test_get_annotations_by_representative_text(self, mock_find_by_representative_text):
        mock_find_by_representative_text.return_value = [
            (1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.get_annotations_by_representative_text('rep_text1')
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)

    @patch('py_apple_books.data.annotation_db.find_by_representative_text')
    def test_get_annotations_by_representative_text_empty(self, mock_find_by_representative_text):
        mock_find_by_representative_text.return_value = []
        annotations = self.booksApi.get_annotations_by_representative_text('rep_text1')
        self.assertEqual(len(annotations), 0)

    @patch('py_apple_books.data.annotation_db.find_by_selected_text')
    def test_get_annotations_by_selected_text(self, mock_find_by_selected_text):
        mock_find_by_selected_text.return_value = [
            (1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.get_annotations_by_selected_text('sel_text1')
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)

    @patch('py_apple_books.data.annotation_db.find_by_selected_text')
    def test_get_annotations_by_selected_text_empty(self, mock_find_by_selected_text):
        mock_find_by_selected_text.return_value = []
        annotations = self.booksApi.get_annotations_by_selected_text('sel_text1')
        self.assertEqual(len(annotations), 0)

    @patch('py_apple_books.data.annotation_db.find_by_note_text')
    def test_get_annotations_by_note(self, mock_find_by_note):
        mock_find_by_note.return_value = [
            (1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.get_annotations_by_note('note1')
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)

    @patch('py_apple_books.data.annotation_db.find_by_note_text')
    def test_get_annotations_by_note_empty(self, mock_find_by_note):
        mock_find_by_note.return_value = []
        annotations = self.booksApi.get_annotations_by_note('note1')
        self.assertEqual(len(annotations), 0)

    @patch('py_apple_books.data.annotation_db.find_by_location')
    def test_get_annotations_by_location(self, mock_find_by_location):
        mock_find_by_location.return_value = [(1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),]
        annotation = self.booksApi.get_annotation_by_location('locat1')
        self.assertIsInstance(annotation, Annotation)
        self.assertEqual(annotation.location, 'locat1')

    @patch('py_apple_books.data.annotation_db.find_by_location')
    def test_get_annotations_by_location_not_found(self, mock_find_by_location):
        mock_find_by_location.return_value = None
        with self.assertRaises(AnnotationNotFoundError):
            self.booksApi.get_annotation_by_location('locat1')

    @patch('py_apple_books.data.annotation_db.find_by_chapter')
    def test_get_annotations_by_chapter(self, mock_find_by_chapter):
        mock_find_by_chapter.return_value = [
            (1, 'asset1', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 1, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.get_annotations_by_chapter('chapter1')
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)

    @patch('py_apple_books.data.annotation_db.find_by_chapter')
    def test_get_annotations_by_chapter_empty(self, mock_find_by_chapter):
        mock_find_by_chapter.return_value = []
        annotations = self.booksApi.get_annotations_by_chapter('chapter1')
        self.assertEqual(len(annotations), 0)

    @patch('py_apple_books.data.annotation_db.find_highlights')
    def test_list_highlights(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'asset1', 0, 0, 2, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 0, 3, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        highlights = self.booksApi.list_highlights()
        self.assertEqual(len(highlights), 2)
        self.assertIsInstance(highlights[0], Highlight)
        self.assertIsInstance(highlights[1], Highlight)

    @patch('py_apple_books.data.annotation_db.find_highlights')
    def test_list_highlights_empty(self, mock_find_all):
        mock_find_all.return_value = []
        highlights = self.booksApi.list_highlights()
        self.assertEqual(len(highlights), 0)

    @patch('py_apple_books.data.annotation_db.find_underlines')
    def test_list_underlines(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'asset1', 0, 1, 0, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 0, 1, 0, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        underlines = self.booksApi.list_underlines()
        self.assertEqual(len(underlines), 2)
        self.assertIsInstance(underlines[0], Underline)
        self.assertIsInstance(underlines[1], Underline)

    @patch('py_apple_books.data.annotation_db.find_underlines')
    def test_list_underlines_empty(self, mock_find_all):
        mock_find_all.return_value = []
        underlines = self.booksApi.list_underlines()
        self.assertEqual(len(underlines), 0)

    @patch('py_apple_books.data.annotation_db.find_notes')
    def test_list_notes(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'asset1', 1, 0, 0, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 1, 0, 0, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        notes = self.booksApi.get_all_notes()
        self.assertEqual(len(notes), 2)
        self.assertIsInstance(notes[0], Annotation)
        self.assertIsInstance(notes[1], Annotation)
        self.assertEqual(notes[0].note, 'note1')
        self.assertEqual(notes[1].note, 'note2')

    @patch('py_apple_books.data.annotation_db.find_notes')
    def test_list_notes_empty(self, mock_find_all):
        mock_find_all.return_value = []
        notes = self.booksApi.get_all_notes()
        self.assertEqual(len(notes), 0)

    @patch('py_apple_books.data.annotation_db.find_by_style')
    def test_get_annotations_by_color(self, mock_find_by_style):
        mock_find_by_style.return_value = [
            (1, 'asset1', 1, 0, 0, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text1', 'rep_text1', 'note1', 'locat1', 'chapter1'),
            (2, 'asset2', 1, 0, 0, datetime.datetime.now().timestamp(), datetime.datetime.now().timestamp(), 'sel_text2', 'rep_text2', 'note2', 'locat2', 'chapter2')
        ]
        annotations = self.booksApi.get_annotation_by_color('yellow')
        self.assertEqual(len(annotations), 2)
        self.assertIsInstance(annotations[0], Annotation)
        self.assertIsInstance(annotations[1], Annotation)


if __name__ == '__main__':
    unittest.main()