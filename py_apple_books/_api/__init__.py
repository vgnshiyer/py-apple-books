"""Private building blocks of :class:`py_apple_books.PyAppleBooks`.

The facade inherits one mixin class per theme (``positions``,
``reading``, ``search``, ``metadata``, ``engagement``, ``book_info``,
``pdf``), so each theme's methods live in a module of their own. Every
method that existed in 1.10 stays in ``py_apple_books/api.py``; only new
methods go into a mixin. ``_common`` holds the helpers they share.

Rules for mixin code:

- A mixin defines no ``__init__``, no instance state and no class
  attributes other than its methods (``dir(PyAppleBooks)`` shows them).
- ``api.py`` binds each mixin's public methods with ``_bind_library``,
  exactly like the facade's own: they run with the instance's library
  as :func:`~py_apple_books.db.current_library`. Read it from there;
  ``self.__library`` would be name-mangled to the mixin's own name.
- A mixin module doesn't import ``py_apple_books.api`` at import time
  (``api.py`` imports the mixins; import it inside a function if
  needed), and does no I/O at import.

Nothing here is public API.
"""
