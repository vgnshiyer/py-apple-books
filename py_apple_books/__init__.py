import logging

from .api import PyAppleBooks, LibraryStats

__all__ = ['PyAppleBooks', 'LibraryStats']

__version__ = '1.11.0'

# Library convention: emit records, let the application configure
# handlers. Without this, warnings would reach stderr through logging's
# last-resort handler in apps that never configure logging.
logging.getLogger('py_apple_books').addHandler(logging.NullHandler())
