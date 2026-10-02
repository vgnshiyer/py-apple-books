"""Names, titles and OS errors in error messages (private).

Error messages reach MCP clients verbatim, so they must stay short and
must not carry absolute paths. A book can name an entry or have a title
thousands of characters long; these helpers shorten what goes into a
message, keeping its start and end. Every name or title of at most 80
characters comes out exactly as 1.10 formatted it.
"""

import os
from typing import Optional

NAME_LIMIT = 80
NAME_HEAD = 60
NAME_TAIL = 20
# A quoted name may be longer than the name (escapes, quotes).
QUOTED_LIMIT = 85
DETAIL_LIMIT = 200
DETAIL_HEAD = 150
DETAIL_TAIL = 50
ELLIPSIS = "…"


def shorten(text: str, limit: int = NAME_LIMIT, head: int = NAME_HEAD, tail: int = NAME_TAIL) -> str:
    """``text`` if it has at most ``limit`` characters, else its first
    ``head`` and last ``tail`` characters joined by an ellipsis."""
    if len(text) <= limit:
        return text
    return text[:head] + ELLIPSIS + (text[-tail:] if tail else "")


def quote_name(name) -> str:
    """``repr(name)`` for a message (what 1.10 wrote as ``f'{name!r}'``),
    with a name over 80 characters shortened first and the quoted form
    capped at 85 characters."""
    if isinstance(name, str):
        quoted = repr(name)
        if len(name) <= NAME_LIMIT and len(quoted) <= QUOTED_LIMIT:
            return quoted
        quoted = repr(shorten(name))
    else:
        quoted = repr(name)
    return shorten(quoted, QUOTED_LIMIT, QUOTED_LIMIT - NAME_TAIL - 1, NAME_TAIL)


def quote_title(title) -> str:
    """A title in single quotes (what 1.10 wrote as ``f"'{title}'"``,
    so None gives ``'None'``), shortened past 80 characters."""
    return f"'{shorten(str(title))}'"


def _home_to_tilde(text: str) -> str:
    try:
        home = os.path.expanduser("~")
    except Exception:  # noqa: BLE001 (no home: nothing to hide)
        return text
    home = home.rstrip(os.sep)
    if not home:
        return text
    return text.replace(home, "~")


def detail(exc: BaseException) -> str:
    """The text of ``exc`` for the end of a message.

    An ``OSError`` gives ``[Errno N] strerror``, plus ``: <name>`` with
    only the base name of the file it names (quoted as
    :func:`quote_name` does), never a path. Any other exception gives
    ``str(exc)``. The home folder is written ``~``, and the result is
    capped at 200 characters (the first 150 and the last 50).
    """
    if isinstance(exc, OSError) and exc.errno is not None:
        text = f"[Errno {exc.errno}] {exc.strerror}"
        name = _base_name(exc.filename)
        if name is not None:
            text += f": {quote_name(name)}"
    else:
        text = str(exc)
    return shorten(_home_to_tilde(text), DETAIL_LIMIT, DETAIL_HEAD, DETAIL_TAIL)


def _base_name(filename) -> Optional[str]:
    if filename is None or isinstance(filename, int):
        return None
    try:
        text = os.fsdecode(filename)
    except (TypeError, ValueError):
        return None
    return os.path.basename(text.rstrip("/")) or text
