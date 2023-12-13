import unittest
from unittest.mock import patch, MagicMock
from py_apple_books.api import BooksApi
from py_apple_books.models.book import Book
from py_apple_books.models.collection import Collection
from py_apple_books.exceptions import CollectionNotFoundError, BookNotFoundError

class TestBooksApi(unittest.TestCase):
    
    def setUp(self):
        self.booksApi = BooksApi()

    @patch('py_apple_books.data.collection_db.find_all')
    def test_list_collections(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'collection1'),
            (2, 'collection2'),
            (3, 'collection3'),
        ]
        collections = self.booksApi.list_collections()
        self.assertEqual(len(collections), 3)
        self.assertIsInstance(collections[0], Collection)
        self.assertIsInstance(collections[1], Collection)
        self.assertIsInstance(collections[2], Collection)

    @patch('py_apple_books.data.collection_db.find_all')
    def test_list_collections_empty(self, mock_find_all):
        mock_find_all.return_value = []
        collections = self.booksApi.list_collections()
        self.assertEqual(len(collections), 0)

    @patch('py_apple_books.data.collection_db.find_by_id')
    def test_get_collection_by_id(self, mock_find_by_id):
        mock_find_by_id.return_value = (1, 'collection1')
        collection = self.booksApi.get_collection_by_id('1')
        self.assertIsInstance(collection, Collection)
        self.assertEqual(collection.id_, 1)
        self.assertEqual(collection.title, 'collection1')

    @patch('py_apple_books.data.collection_db.find_by_id')
    def test_get_collection_by_id_not_found(self, mock_find_by_id):
        mock_find_by_id.return_value = None
        with self.assertRaises(CollectionNotFoundError):
            self.booksApi.get_collection_by_id('1')

    @patch('py_apple_books.data.collection_db.find_by_name')
    def test_get_collection_by_name(self, mock_find_by_name):
        mock_find_by_name.return_value = (1, 'collection1')
        collection = self.booksApi.get_collection_by_name('collection1')
        self.assertIsInstance(collection, Collection)
        self.assertEqual(collection.id_, 1)
        self.assertEqual(collection.title, 'collection1')

    @patch('py_apple_books.data.collection_db.find_by_name')
    def test_get_collection_by_name_not_found(self, mock_find_by_name):
        mock_find_by_name.return_value = None
        with self.assertRaises(CollectionNotFoundError):
            self.booksApi.get_collection_by_name('collection1')

    @patch('py_apple_books.data.book_db.find_all')
    def test_list_books(self, mock_find_all):
        mock_find_all.return_value = [
            (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1'),
            (2, 'asset2', 'title2', 'author2', 'desc2', 'genre2', 'file_path2'),
            (3, 'asset3', 'title3', 'author3', 'desc3', 'genre3', 'file_path3'),
        ]
        books = self.booksApi.list_books()
        self.assertEqual(len(books), 3)
        self.assertIsInstance(books[0], Book)
        self.assertIsInstance(books[1], Book)
        self.assertIsInstance(books[2], Book)

    @patch('py_apple_books.data.book_db.find_all')
    def test_list_books_empty(self, mock_find_all):
        mock_find_all.return_value = []
        books = self.booksApi.list_books()
        self.assertEqual(len(books), 0)

    @patch('py_apple_books.data.book_db.find_by_id')
    def test_get_book_by_id(self, mock_find_by_id):
        mock_find_by_id.return_value = (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1')
        book = self.booksApi.get_book_by_id('1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.id_, 1)
        self.assertEqual(book.asset_id, 'asset1')
        self.assertEqual(book.title, 'title1')
        self.assertEqual(book.author, 'author1')
        self.assertEqual(book.description, 'desc1')
        self.assertEqual(book.genre, 'genre1')
        self.assertEqual(book.path, 'file_path1')

    @patch('py_apple_books.data.book_db.find_by_id')
    def test_get_book_by_id_not_found(self, mock_find_by_id):
        mock_find_by_id.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_id('1')

    @patch('py_apple_books.data.book_db.find_by_asset_id')
    def test_get_book_by_asset_id(self, mock_find_by_asset_id):
        mock_find_by_asset_id.return_value = (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1')
        book = self.booksApi.get_book_by_asset_id('asset1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.id_, 1)
        self.assertEqual(book.asset_id, 'asset1')
        self.assertEqual(book.title, 'title1')
        self.assertEqual(book.author, 'author1')
        self.assertEqual(book.description, 'desc1')
        self.assertEqual(book.genre, 'genre1')
        self.assertEqual(book.path, 'file_path1')

    @patch('py_apple_books.data.book_db.find_by_asset_id')
    def test_get_book_by_asset_id_not_found(self, mock_find_by_asset_id):
        mock_find_by_asset_id.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_asset_id('asset1')

    @patch('py_apple_books.data.book_db.find_by_title')
    def test_get_book_by_title(self, mock_find_by_title):
        mock_find_by_title.return_value = (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1')
        book = self.booksApi.get_book_by_title('title1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.id_, 1)
        self.assertEqual(book.asset_id, 'asset1')
        self.assertEqual(book.title, 'title1')
        self.assertEqual(book.author, 'author1')
        self.assertEqual(book.description, 'desc1')
        self.assertEqual(book.genre, 'genre1')
        self.assertEqual(book.path, 'file_path1')

    @patch('py_apple_books.data.book_db.find_by_title')
    def test_get_book_by_title_not_found(self, mock_find_by_title):
        mock_find_by_title.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_title('title1')

    @patch('py_apple_books.data.book_db.find_by_author')
    def test_get_book_by_author(self, mock_find_by_author):
        mock_find_by_author.return_value = (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1')
        book = self.booksApi.get_book_by_author('author1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.id_, 1)
        self.assertEqual(book.asset_id, 'asset1')
        self.assertEqual(book.title, 'title1')
        self.assertEqual(book.author, 'author1')
        self.assertEqual(book.description, 'desc1')
        self.assertEqual(book.genre, 'genre1')
        self.assertEqual(book.path, 'file_path1')

    @patch('py_apple_books.data.book_db.find_by_author')
    def test_get_book_by_author_not_found(self, mock_find_by_author):
        mock_find_by_author.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_author('author1')

    @patch('py_apple_books.data.book_db.find_by_genre')
    def test_get_book_by_genre(self, mock_find_by_genre):
        mock_find_by_genre.return_value = (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1')
        book = self.booksApi.get_book_by_genre('genre1')
        self.assertIsInstance(book, Book)
        self.assertEqual(book.id_, 1)
        self.assertEqual(book.asset_id, 'asset1')
        self.assertEqual(book.title, 'title1')
        self.assertEqual(book.author, 'author1')
        self.assertEqual(book.description, 'desc1')
        self.assertEqual(book.genre, 'genre1')
        self.assertEqual(book.path, 'file_path1')

    @patch('py_apple_books.data.book_db.find_by_genre')
    def test_get_book_by_genre_not_found(self, mock_find_by_genre):
        mock_find_by_genre.return_value = None
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_book_by_genre('genre1')

    @patch('py_apple_books.data.book_db.find_by_collection_id')
    def test_get_books_by_collection_id(self, mock_find_by_collection_id):
        mock_find_by_collection_id.return_value = [
            (1, 'asset1', 'title1', 'author1', 'desc1', 'genre1', 'file_path1'),
            (2, 'asset2', 'title2', 'author2', 'desc2', 'genre2', 'file_path2'),
            (3, 'asset3', 'title3', 'author3', 'desc3', 'genre3', 'file_path3'),
        ]
        books = self.booksApi.get_books_by_collection_id('1')
        self.assertEqual(len(books), 3)
        self.assertIsInstance(books[0], Book)
        self.assertIsInstance(books[1], Book)
        self.assertIsInstance(books[2], Book)

    @patch('py_apple_books.data.book_db.find_by_collection_id')
    def test_get_books_by_collection_id_empty(self, mock_find_by_collection_id):
        mock_find_by_collection_id.return_value = []
        with self.assertRaises(BookNotFoundError):
            self.booksApi.get_books_by_collection_id('1')

if __name__ == '__main__':
    unittest.main()