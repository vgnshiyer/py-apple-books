"""Read a book's metadata from its package document (OPF) (private).

Only two files of an unzipped EPUB bundle are read,
``META-INF/container.xml`` and the package document it names, both
through :func:`py_apple_books._icloud.read_local` (every folder and the
file checked not to be an iCloud placeholder before anything is looked
up inside it or read, symlinks refused, reads bounded, all under
:func:`~py_apple_books._icloud.no_materialize`). Nothing else in the
bundle is opened; the cover is only named.

The package document is untrusted input. It is streamed through
``pyexpat`` with:

- a DOCTYPE with an internal subset refused, and so every entity
  declaration (no entity expansion at all); parameter entities never
  parsed; external entities and DTDs never fetched;
- element names matched by local name, so OPF 1.x, 2 and 3 and odd
  namespaces all work;
- caps: 64 KiB for ``container.xml``, 4 MiB for the package document,
  50,000 elements, depth 64, 64 KiB of raw description text, and small
  per-field caps;
- parsing stopped at ``</metadata>`` (subjects only) or ``</manifest>``.

Nothing here raises or logs: every outcome is a state
(:data:`READ`, :data:`UNREADABLE`, :data:`NOT_DOWNLOADED`).

Two bounded caches, registered with ``_icloud.register_file_cache`` (so
``content.clear_content_cache()`` empties them): the metadata of up to
512 books (16 MiB) and a compact subject index of up to 20,000 books
(8 MiB). Both are keyed by the bundle's absolute path and checked
against the identity (device, inode, mtime, size) of ``container.xml``
and the package document, read through the same gated walk, on every
use. Only results the files' content decides are stored: never one from
an I/O error, a missing file or an iCloud placeholder.
"""

import datetime as _dt
import html
import math
import os
import posixpath
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit
from xml.parsers import expat

from py_apple_books import _icloud
from py_apple_books.text import fold_for_match

CONTAINER = "META-INF/container.xml"
CONTAINER_MAX_BYTES = 64 * 1024
OPF_MAX_BYTES = 4 * 1024 * 1024
MAX_ELEMENTS = 50_000
MAX_DEPTH = 64
RAW_DESCRIPTION_MAX = 64 * 1024
DESCRIPTION_MAX = 16_000
TEXT_MAX = 300
SUBJECT_MAX = 120
SUBJECTS_MAX = 30
_FIELD_RAW_MAX = 4096
_LIST_MAX = 256
_PACKAGE_MEDIA_TYPE = "application/oebps-package+xml"

# Result states (the MetadataFileState values they become).
READ = "read"
UNREADABLE = "unreadable"
NOT_DOWNLOADED = "not_downloaded"

MODES = ("full", "subjects")


@dataclass(frozen=True)
class OpfFields:
    """What the package document says, cleaned (see the normalisers)."""

    language: Optional[str] = None
    publisher: Optional[str] = None
    published: Optional[str] = None
    year: Optional[int] = None
    isbn: Optional[str] = None
    subjects: Tuple[str, ...] = ()
    description: Optional[str] = None
    cover_href: Optional[str] = None
    series_title: Optional[str] = None
    series_sequence: Optional[float] = None


@dataclass(frozen=True)
class OpfResult:
    """``state`` is :data:`READ` (``fields`` set), :data:`UNREADABLE` or
    :data:`NOT_DOWNLOADED`. ``cacheable``: decided by the files' content
    (not by an I/O error or a placeholder)."""

    state: str
    fields: Optional[OpfFields] = None
    cacheable: bool = field(default=False, compare=False)


_NOT_DOWNLOADED = OpfResult(NOT_DOWNLOADED)
_IO_FAILED = OpfResult(UNREADABLE)


# ---------------------------------------------------------------------------
# Normalisers (also used by the facade for the library's own values)
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￾￿]")
_SPACES = re.compile(r"\s+")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")
_DATE = re.compile(r"\s*([0-9]{4})(?:-([0-9]{1,2})(?:-([0-9]{1,2}))?)?(?:[Tt ][^\n]*)?\s*")
_URL_LIKE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://\S*|www\.\S+"
    r"|[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|edu|gov|info|biz|io|co|us|uk|de|fr|ca|au)(?:[/:?#]\S*)?)")
_TAG_LIKE = re.compile(r"<\s*/?\s*[A-Za-z!]")
_ISBN_SEPARATORS = re.compile(r"[\s\-‐-―−]")
_ISBN_SCHEMES = frozenset({"isbn", "isbn-13", "isbn-10", "isbn13", "isbn10"})
# ONIX code list 5 (product identifier type): 15 ISBN-13, 02 ISBN-10.
_ONIX_ISBN = frozenset({"15", "02"})
_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
                         "blockquote", "tr", "table", "section", "article", "dd", "dt", "hr", "pre"})
_SKIP_TAGS = frozenset({"head", "title", "template"})  # content dropped
_RAW_TEXT_TAGS = ("script", "style")  # raw text up to the end tag, dropped
_RAW_TEXT_END = {name: re.compile(rf"</{name}(?=[\s/>]|$)", re.I) for name in _RAW_TEXT_TAGS}
# How far past a '<' a tag's '>' is looked for (never past the next '<').
_TAG_SCAN = 1024
# A start or end tag's name, right after its '<' (None for '<!' and '<?').
_TAG_NAME = re.compile(r"(/?)([A-Za-z][^\s/<>]*)")
# A numeric character reference, as html.unescape finds them.
_NUMERIC_REF = re.compile(r"&#(?:([xX])([0-9a-fA-F]+)|([0-9]+))(;?)")


def clean_line(value, limit: int = TEXT_MAX) -> Optional[str]:
    """``value`` as one line: control characters dropped, whitespace
    runs collapsed, at most ``limit`` characters; None if nothing is
    left or it isn't a str."""
    if not isinstance(value, str):
        return None
    text = _SPACES.sub(" ", _CONTROL.sub("", value)).strip()
    return text[:limit].rstrip() or None


def normalize_language(value) -> Optional[str]:
    """A BCP 47 tag as ``'en'``, ``'en-US'``, ``'zh-Hant-TW'``: ``_``
    becomes ``-``, the language subtag lower-case, a region upper-case,
    a script title-case. None for anything else (``'English'``), and for
    ``'und'`` and ``'zxx'`` (no language)."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace("_", "-")
    if len(text) > 64 or not _LANGUAGE.fullmatch(text):
        return None
    parts = text.split("-")
    primary = parts[0].lower()
    if primary in ("und", "zxx"):
        return None
    out = [primary]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            out.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            out.append(part.title())
        else:
            out.append(part.lower())
    return "-".join(out)


def latest_plausible_year() -> int:
    return _dt.date.today().year + 5


def plausible_year(year) -> bool:
    """A publication year in 1450..(this year + 5)."""
    return isinstance(year, int) and not isinstance(year, bool) and 1450 <= year <= latest_plausible_year()


def parse_date(value) -> Optional[Tuple[str, int]]:
    """``(published, year)`` from a date text: ``'YYYY'``, ``'YYYY-MM'``
    or ``'YYYY-MM-DD'`` (anything after a ``T`` or a space ignored); a
    month or day out of range shortens it. None for another form or an
    implausible year."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _DATE.fullmatch(value)
    if not match:
        return None
    year = int(match.group(1))
    if not plausible_year(year):
        return None
    month, day = match.group(2), match.group(3)
    if month is None or not 1 <= int(month) <= 12:
        return f"{year:04d}", year
    month_n = int(month)
    if day is not None:
        try:
            _dt.date(year, month_n, int(day))
        except ValueError:
            day = None
    if day is None:
        return f"{year:04d}-{month_n:02d}", year
    return f"{year:04d}-{month_n:02d}-{int(day):02d}", year


def _isbn13_ok(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit() or not digits.isascii():
        return False
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(digits[:12]))
    return (10 - total % 10) % 10 == int(digits[12])


def _isbn10_ok(digits: str) -> bool:
    if len(digits) != 10 or not digits.isascii() or not digits[:9].isdigit():
        return False
    last = digits[9]
    if not (last.isdigit() or last == "X"):
        return False
    values = [int(c) for c in digits[:9]] + [10 if last == "X" else int(last)]
    return sum((10 - i) * v for i, v in enumerate(values)) % 11 == 0


def pick_isbn(identifiers: Iterable[Tuple[str, Optional[str], Optional[str]]]) -> Optional[str]:
    """The first checksum-valid ISBN-13 among ``(text, scheme,
    identifier_type)`` entries, else the first valid ISBN-10.

    An entry counts as an ISBN when it says so (``urn:isbn:`` or
    ``isbn:``, ``opf:scheme="ISBN"``, or an EPUB 3 ``identifier-type``
    refinement of ONIX code 15 or 02); a valid ISBN-13 starting 978 or
    979 also counts when nothing says what it is. An entry that names
    another scheme (a UUID, an ASIN, a calibre id) never does.
    """
    first13 = first10 = None
    for text, scheme, id_type in identifiers:
        if not isinstance(text, str):
            continue
        raw = text.strip()
        low = raw.lower()
        explicit: Optional[bool] = None
        if low.startswith(("urn:isbn:", "isbn:")):
            explicit = True
            raw = raw.rsplit(":", 1)[1]
        elif id_type is not None:
            explicit = id_type.strip() in _ONIX_ISBN or "isbn" in id_type.lower()
        elif scheme is not None and scheme.strip():
            explicit = scheme.strip().lower() in _ISBN_SCHEMES
        elif ":" in raw:
            explicit = False  # urn:uuid:, calibre:..., another scheme
        if explicit is False:
            continue
        digits = _ISBN_SEPARATORS.sub("", raw).upper()
        if _isbn13_ok(digits) and (explicit or digits.startswith(("978", "979"))):
            first13 = first13 or digits
        elif explicit and _isbn10_ok(digits):
            first10 = first10 or digits
    return first13 or first10


def clean_subjects(values: Iterable[Any], *, drop_urls: bool = True) -> Tuple[str, ...]:
    """Subjects: each one line of at most 120 characters, URL-like
    entries dropped (unless ``drop_urls`` is False: the library's own
    genre is kept as Books records it), duplicates (compared folded, as
    searches compare text) dropped, at most 30."""
    out: List[str] = []
    seen = set()
    for value in values:
        text = clean_line(value, SUBJECT_MAX)
        if text is None or (drop_urls and _URL_LIKE.fullmatch(text)):
            continue
        key = (fold_for_match(text) or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= SUBJECTS_MAX:
            break
    return tuple(out)


def _short_ref(match) -> str:
    """A numeric reference without leading zeros, or U+FFFD (what
    ``html.unescape`` makes of it) when more than 7 digits are left: no
    character is that large."""
    is_hex, hex_digits, digits, semicolon = match.groups()
    digits = (hex_digits if is_hex else digits).lstrip("0") or "0"
    return "\ufffd" if len(digits) > 7 else f"&#{is_hex or ''}{digits}{semicolon}"


def _unescape(data: str) -> str:
    """``html.unescape`` that can't fail: numeric references are made
    short first, so ``int()`` never sees more digits than
    ``sys.get_int_max_str_digits()`` allows (a ValueError otherwise)."""
    if "&" not in data:
        return data
    try:
        return html.unescape(_NUMERIC_REF.sub(_short_ref, data))
    except (ValueError, OverflowError):  # pragma: no cover (defensive)
        return data


def _strip_html(text: str) -> str:
    """HTML to text: tags dropped (``script`` and ``style`` with their
    content, ``head``, ``title`` and ``template`` content too), comments,
    declarations and processing instructions dropped, character
    references converted, block tags made line breaks; a ``<`` that
    starts no tag is text.

    A tokenizer of its own rather than ``html.parser``, which takes time
    quadratic in the length of crafted malformed markup on Pythons
    without the CVE-2025-6069 fix (3.10.17, for one: minutes for 64 KiB).
    Here every step either moves past text or looks
    at most :data:`_TAG_SCAN` characters ahead, stopping at the next
    ``<``, so the time is linear in the length of ``text`` and the result
    is the same on every Python.
    """
    parts: List[str] = []
    pending: List[str] = []  # data since the last tag, unescaped together
    skip = 0
    pos, end = 0, len(text)

    def flush() -> None:
        if pending:
            if not skip:
                parts.append(_SPACES.sub(" ", _unescape("".join(pending))))
            pending.clear()

    while pos < end:
        lt = text.find("<", pos)
        if lt < 0:
            pending.append(text[pos:])
            break
        if lt > pos:
            pending.append(text[pos:lt])
        if text.startswith("<!--", lt):
            flush()
            close = text.find("-->", lt + 2)  # "<!-->" is an empty comment
            if close < 0:
                break  # an unterminated comment runs to the end
            pos = close + 3
            continue
        after = text[lt + 1:lt + 2]
        if not (after.isascii() and after.isalpha()) and after not in ("/", "!", "?"):
            pending.append("<")
            pos = lt + 1
            continue
        limit = min(end, lt + 1 + _TAG_SCAN)
        next_lt = text.find("<", lt + 1, limit)
        gt = text.find(">", lt + 1, next_lt if next_lt >= 0 else limit)
        match = _TAG_NAME.match(text, lt + 1, gt) if gt >= 0 else None
        if gt < 0 or (after == "/" and match is None):
            pending.append("<")  # no tag after all: the '<' is text
            pos = lt + 1
            continue
        flush()
        pos = gt + 1
        if match is None:
            continue  # <!DOCTYPE ...>, <![CDATA[...]>, <?...?>: dropped
        closing, name = match.group(1) == "/", match.group(2).lower()
        self_closing = not closing and text[gt - 1] == "/"
        if name in _BLOCK_TAGS:
            parts.append("\n")
        elif name in _RAW_TEXT_TAGS and not closing and not self_closing:
            # Script and style hold raw text up to their end tag.
            raw_end = _RAW_TEXT_END[name].search(text, pos)
            close = text.find(">", raw_end.end()) if raw_end is not None else -1
            pos = end if close < 0 else close + 1
        elif name in _SKIP_TAGS and not self_closing:
            skip = max(0, skip - 1) if closing else skip + 1
    flush()
    return "".join(parts)


def clean_description(value) -> Optional[str]:
    """A description as plain text: HTML (also HTML escaped once more,
    as some package documents carry it) stripped, control characters
    dropped, whitespace collapsed (block tags become line breaks), at
    most 16,000 characters, cut at a word with '…'. The raw text is cut
    to 64 KiB first."""
    if not isinstance(value, str):
        return None
    text = value[:RAW_DESCRIPTION_MAX]
    for _ in range(2):
        if not (_TAG_LIKE.search(text) or "&" in text):
            break
        text = _strip_html(text)
    text = _CONTROL.sub("", text.replace("\t", " "))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX:
        cut = text[:DESCRIPTION_MAX - 1]
        space = max(cut.rfind(" "), cut.rfind("\n"))
        if space > DESCRIPTION_MAX // 2:
            cut = cut[:space]
        text = cut.rstrip() + "…"
    return text


def finite_float(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        number = float(value.strip())
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def bundle_href(opf_dir: str, href) -> Optional[str]:
    """A manifest ``href`` as a path inside the bundle, or None if it is
    absolute, a URL with a scheme, or leaves the bundle. Lexical only."""
    if not isinstance(href, str) or not href.strip() or len(href) > 4096:
        return None
    try:
        parts = urlsplit(href.strip())
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    path = unquote(parts.path)
    if not path or "\0" in path or path.startswith("/") or "\\" in path:
        return None
    joined = posixpath.normpath(posixpath.join(opf_dir, path) if opf_dir else path)
    if joined in (".", "..") or joined.startswith(("../", "/")):
        return None
    return joined


# ---------------------------------------------------------------------------
# Streaming parsers
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """Everything wanted has been read."""


class _Refused(Exception):
    """The document is refused (DOCTYPE with an internal subset, an
    entity declaration, a cap reached, not a package document)."""


def _local(name: str) -> str:
    return name.rpartition(" ")[2].lower()


def _attrs(attrs: Dict[str, str]) -> Dict[str, str]:
    return {_local(k): v for k, v in attrs.items()}


def _refined(refines: dict, element_id: Optional[str], prop: str) -> Optional[str]:
    """The text of the first EPUB 3 ``<meta refines="#id" property=prop>``."""
    if not element_id or not element_id.strip():
        return None
    hit = refines.get(element_id.strip(), {}).get(prop)
    return hit[0] if hit else None


def _refuse_doctype(name, system_id, public_id, has_internal_subset):
    if has_internal_subset:
        raise _Refused()


def _refuse(*args):
    raise _Refused()


def _new_parser() -> "expat.XMLParserType":
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.buffer_text = True
    return parser


class _ContainerParser:
    """The package document ``container.xml`` names: the first
    ``rootfile`` whose media type is the OPF's, else the first one."""

    def __init__(self):
        self.parser = _new_parser()
        self.parser.StartElementHandler = self.start
        self.elements = 0
        self.matching: Optional[str] = None
        self.first: Optional[str] = None

    def start(self, name, attrs):
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            raise _Refused()
        if _local(name) != "rootfile":
            return
        attrs = _attrs(attrs)
        path = attrs.get("full-path")
        if not path:
            return
        if self.first is None:
            self.first = path
        if (attrs.get("media-type") or "").strip().lower() == _PACKAGE_MEDIA_TYPE:
            self.matching = path
            raise _Stop()

    def feed(self, data: bytes) -> None:
        self.parser.Parse(data, False)

    def close(self) -> None:
        self.parser.Parse(b"", True)

    @property
    def rootfile(self) -> Optional[str]:
        return self.matching or self.first


_DC_FIELDS = frozenset({"language", "publisher", "date", "identifier", "subject", "description"})


class _Capture:
    """The text of one metadata element being read."""

    __slots__ = ("name", "attrs", "depth", "parts", "room")

    def __init__(self, name: str, attrs: Dict[str, str], depth: int, cap: int):
        self.name, self.attrs, self.depth = name, attrs, depth
        self.parts: List[str] = []
        self.room = cap

    def add(self, data: str) -> None:
        if self.room > 0:
            piece = data[:self.room]
            self.parts.append(piece)
            self.room -= len(piece)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _OpfParser:
    """Collects the raw metadata values of a package document."""

    def __init__(self, mode: str):
        self.full = mode == "full"
        self.parser = _new_parser()
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.chars
        self.elements = 0
        self.depth = 0
        self.root_seen = False
        self.metadata_depth = 0
        self.metadata_done = False
        self.manifest_depth = 0
        self.capture: Optional[_Capture] = None
        self.values: Dict[str, List[Tuple[str, Dict[str, str]]]] = {name: [] for name in _DC_FIELDS}
        self.named_meta: List[Tuple[str, str]] = []  # OPF 2 <meta name content>
        self.property_meta: List[Tuple[str, str, Dict[str, str]]] = []  # OPF 3 (property, text, attrs)
        self.items: List[Dict[str, str]] = []  # cover candidates
        self.cover_ids: frozenset = frozenset()  # OPF 2 <meta name="cover">

    # -- expat handlers ---------------------------------------------------

    def start(self, name, attrs):
        self.elements += 1
        self.depth += 1
        if self.elements > MAX_ELEMENTS:
            # The manifest is only read for the cover: keep what is known.
            if self.full and self.metadata_done:
                raise _Stop()
            raise _Refused()
        if self.depth > MAX_DEPTH:
            raise _Refused()
        local = _local(name)
        if not self.root_seen:
            self.root_seen = True
            if local != "package":
                raise _Refused()
            return
        if self.capture is not None:
            return
        if self.metadata_depth:
            if local in _DC_FIELDS:
                if self.full or local == "subject":
                    cap = RAW_DESCRIPTION_MAX if local == "description" else _FIELD_RAW_MAX
                    self.capture = _Capture(local, _attrs(attrs), self.depth, cap)
            elif local == "meta" and self.full:
                attrs = _attrs(attrs)
                if "name" in attrs:
                    if len(self.named_meta) < _LIST_MAX:
                        self.named_meta.append((attrs.get("name") or "", attrs.get("content") or ""))
                elif "property" in attrs:
                    self.capture = _Capture("meta", attrs, self.depth, _FIELD_RAW_MAX)
        elif local == "metadata" and not self.metadata_done:
            self.metadata_depth = self.depth
        elif local == "manifest" and self.full:
            self.manifest_depth = self.depth
        elif self.manifest_depth and local == "item" and len(self.items) < _LIST_MAX:
            attrs = _attrs(attrs)
            if self._cover_candidate(attrs):
                self.items.append(attrs)

    def end(self, name):
        capture = self.capture
        if capture is not None and capture.depth == self.depth:
            self.capture = None
            self._finish(capture)
        if self.metadata_depth == self.depth:
            self.metadata_depth = 0
            self.metadata_done = True
            self.cover_ids = frozenset(
                content.strip() for meta_name, content in self.named_meta
                if meta_name.strip().lower() == "cover" and content.strip())
            if not self.full:
                raise _Stop()
        elif self.manifest_depth == self.depth:
            self.manifest_depth = 0
            if self.metadata_done:
                raise _Stop()
        self.depth -= 1

    def chars(self, data):
        if self.capture is not None:
            self.capture.add(data)

    # -- helpers ----------------------------------------------------------

    def _finish(self, capture: _Capture) -> None:
        name, attrs, text = capture.name, capture.attrs, capture.text
        if name == "meta":
            if len(self.property_meta) < _LIST_MAX:
                self.property_meta.append(((attrs.get("property") or "").strip(), text, attrs))
        elif len(self.values[name]) < _LIST_MAX:
            self.values[name].append((text, attrs))

    def _cover_candidate(self, attrs: Dict[str, str]) -> bool:
        properties = (attrs.get("properties") or "").split()
        return "cover-image" in properties or (attrs.get("id") or "").strip() in self.cover_ids

    def feed(self, data: bytes) -> None:
        self.parser.Parse(data, False)

    def close(self) -> None:
        self.parser.Parse(b"", True)

    # -- the result -------------------------------------------------------

    def fields(self, opf_dir: str) -> OpfFields:
        subjects = clean_subjects(text for text, _ in self.values["subject"])
        if not self.full:
            return OpfFields(subjects=subjects)
        refines: Dict[str, Dict[str, Tuple[str, Dict[str, str]]]] = {}
        for prop, text, attrs in self.property_meta:
            target = (attrs.get("refines") or "").strip()
            if target.startswith("#") and prop:
                refines.setdefault(target[1:], {}).setdefault(prop.lower(), (text.strip(), attrs))

        language = None
        for text, _ in self.values["language"]:
            language = normalize_language(text)
            if language:
                break
        publisher = next((p for p in (clean_line(t) for t, _ in self.values["publisher"]) if p), None)
        published, year = self._date()
        isbn = pick_isbn(
            (text, attrs.get("scheme"), _refined(refines, attrs.get("id"), "identifier-type"))
            for text, attrs in self.values["identifier"])
        description = next((d for d in (clean_description(t) for t, _ in self.values["description"]) if d),
                           None)
        series_title, series_sequence = self._series(refines)
        return OpfFields(language=language, publisher=publisher, published=published, year=year,
                         isbn=isbn, subjects=subjects, description=description,
                         cover_href=self._cover(opf_dir), series_title=series_title,
                         series_sequence=series_sequence)

    def _date(self) -> Tuple[Optional[str], Optional[int]]:
        # Publication first, then an undated event, then the original
        # publication, then creation; modification (and anything else)
        # never.
        ranked = {"publication": 0, "": 1, "original-publication": 2, "creation": 3}
        best: Optional[Tuple[int, int, Tuple[str, int]]] = None
        for position, (text, attrs) in enumerate(self.values["date"]):
            rank = ranked.get((attrs.get("event") or "").strip().lower())
            if rank is None:
                continue
            parsed = parse_date(text)
            if parsed is not None and (best is None or (rank, position) < best[:2]):
                best = (rank, position, parsed)
        return best[2] if best else (None, None)

    def _series(self, refines) -> Tuple[Optional[str], Optional[float]]:
        named = {}
        for meta_name, content in self.named_meta:
            named.setdefault(meta_name.strip().lower(), content)
        title = clean_line(named.get("calibre:series"))
        if title:
            return title, finite_float(named.get("calibre:series_index"))
        for prop, text, attrs in self.property_meta:
            if prop.lower() != "belongs-to-collection" or (attrs.get("refines") or "").strip():
                continue
            kind = (_refined(refines, attrs.get("id"), "collection-type") or "").strip().lower()
            if kind not in ("", "series"):
                continue
            title = clean_line(text)
            if title:
                return title, finite_float(_refined(refines, attrs.get("id"), "group-position"))
        return None, None

    def _cover(self, opf_dir: str) -> Optional[str]:
        ids = self.cover_ids
        chosen = next((i for i in self.items if "cover-image" in (i.get("properties") or "").split()), None)
        if chosen is None:
            for item in self.items:
                media = (item.get("media-type") or "").strip().lower()
                if (item.get("id") or "").strip() in ids and (not media or media.startswith("image/")):
                    chosen = item
                    break
        return bundle_href(opf_dir, chosen.get("href")) if chosen else None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _identity(st) -> tuple:
    return (getattr(st, "st_dev", None), getattr(st, "st_ino", None),
            getattr(st, "st_mtime_ns", None), getattr(st, "st_size", None))


def _feed(bundle, rel: str, max_bytes: int, target) -> None:
    """Stream ``rel`` into ``target`` (a parser); returns when the file
    is read or ``target`` has all it wants. Raises what ``read_local``
    and the parser raise."""
    try:
        _icloud.read_local(bundle, rel, max_bytes=max_bytes, sink=target.feed)
        target.close()
    except _Stop:
        pass


def _failure(exc: BaseException) -> OpfResult:
    """The result for a refused or failed read."""
    if isinstance(exc, _icloud._NotLocal) or _icloud.is_materialize_error(exc):
        return _NOT_DOWNLOADED
    if isinstance(exc, (_icloud._TooLarge, _Refused, expat.ExpatError)):
        return OpfResult(UNREADABLE, cacheable=True)
    # _Missing, _IOFailed, _Unsafe (a symlink, a special file: not the
    # content), anything unexpected.
    return _IO_FAILED


@dataclass(frozen=True)
class _Read:
    """One read of a bundle: the result and the identities it is valid
    for (``opf_rel``/``opf_id`` None when ``container.xml`` alone
    decided it)."""

    result: OpfResult
    container_id: Optional[tuple] = None
    opf_rel: Optional[str] = None
    opf_id: Optional[tuple] = None


def _safe_rel(rel: Optional[str]) -> Optional[str]:
    """``rel`` (a ``container.xml`` path) if it stays inside the bundle."""
    try:
        return "/".join(_icloud._parts(rel)) if rel is not None else None
    except _icloud._Unsafe:
        return None


def _read(bundle, mode: str) -> _Read:
    """Read ``bundle``'s package document. Never raises."""
    container_id = opf_rel = opf_id = None
    try:
        container_id = _identity(_icloud.stat_local(bundle, CONTAINER))
        finder = _ContainerParser()
        _feed(bundle, CONTAINER, CONTAINER_MAX_BYTES, finder)
        opf_rel = _safe_rel(finder.rootfile)
        if opf_rel is None:
            # No package document named, or a path that leaves the
            # bundle: container.xml decided it.
            return _Read(OpfResult(UNREADABLE, cacheable=True), container_id)
        opf_id = _identity(_icloud.stat_local(bundle, opf_rel))
        parser = _OpfParser(mode)
        _feed(bundle, opf_rel, OPF_MAX_BYTES, parser)
        fields = parser.fields(posixpath.dirname(opf_rel))
        return _Read(OpfResult(READ, fields, cacheable=True), container_id, opf_rel, opf_id)
    except Exception as e:  # noqa: BLE001 (never raises: every failure is a state)
        result = _failure(e)
        if not result.cacheable or container_id is None or (opf_rel is not None and opf_id is None):
            return _Read(OpfResult(result.state))
        # Decided by the content of container.xml (opf_rel None) or of
        # the package document.
        return _Read(result, container_id, opf_rel, opf_id)


def _still_valid(bundle, entry: _Read) -> bool:
    """Whether the files ``entry`` was read from are unchanged (through
    the same gated walk; a placeholder or any error means no)."""
    try:
        if _identity(_icloud.stat_local(bundle, CONTAINER)) != entry.container_id:
            return False
        if entry.opf_rel is not None:
            return _identity(_icloud.stat_local(bundle, entry.opf_rel)) == entry.opf_id
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


class _BoundedCache:
    """An LRU bounded by entry count and by an estimated weight in
    bytes. One lock, held only around dict operations; stored values
    are immutable."""

    def __init__(self, max_entries: int, max_weight: int):
        self.max_entries = max_entries
        self.max_weight = max_weight
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Any, Tuple[Any, int]]" = OrderedDict()
        self._weight = 0

    def get(self, key):
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            self._entries.move_to_end(key)
            return hit[0]

    def put(self, key, value, weight: int) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._weight -= old[1]
            if weight > self.max_weight:
                return
            self._entries[key] = (value, weight)
            self._weight += weight
            while self._entries and (len(self._entries) > self.max_entries or self._weight > self.max_weight):
                _, (_, dropped) = self._entries.popitem(last=False)
                self._weight -= dropped

    def discard(self, key) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._weight -= old[1]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._weight = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def weight(self) -> int:
        with self._lock:
            return self._weight


_metadata_cache = _BoundedCache(512, 16 * 1024 * 1024)
_subject_index = _BoundedCache(20_000, 8 * 1024 * 1024)


def _weight(fields: Optional[OpfFields]) -> int:
    if fields is None:
        return 256
    total = 256
    for value in (fields.language, fields.publisher, fields.published, fields.isbn, fields.description,
                  fields.cover_href, fields.series_title, *fields.subjects):
        if value:
            total += 64 + 4 * len(value)
    return total


def _clear_caches() -> None:
    _metadata_cache.clear()
    _subject_index.clear()


_icloud.register_file_cache(_clear_caches)


def _key(bundle) -> Any:
    """The cache key: the bundle's absolute path, or None when it has
    none (a relative path while the working folder is gone: ``getcwd``
    fails; not a path at all)."""
    try:
        return os.path.abspath(os.fspath(bundle))
    except (OSError, TypeError, ValueError):
        return None


def read_metadata(bundle) -> OpfResult:
    """The package document's metadata (``'full'``), from the cache when
    the files are unchanged. The caller has already checked that the
    library doesn't record the book as stored only in iCloud and that
    the bundle itself is local. Never raises."""
    key = _key(bundle)
    if key is None:
        return _IO_FAILED  # nothing to look up a relative path from
    entry = _metadata_cache.get(key)
    if entry is not None and _still_valid(bundle, entry):
        return entry.result
    entry = _read(bundle, "full")
    if entry.result.cacheable:
        _metadata_cache.put(key, entry, _weight(entry.result.fields))
    else:
        _metadata_cache.discard(key)
    return entry.result


@dataclass(frozen=True)
class _Subjects:
    """A subject-index entry: the folded subjects (None: unreadable)."""

    folded: Optional[Tuple[str, ...]]
    container_id: Optional[tuple]
    opf_rel: Optional[str]
    opf_id: Optional[tuple]


def _fold_all(subjects: Iterable[str]) -> Tuple[str, ...]:
    return tuple(fold_for_match(s) or "" for s in subjects)


def read_subjects(bundle) -> Optional[Tuple[str, ...]]:
    """The package document's subjects, folded with ``fold_for_match``
    (``()`` if it has none); None when it can't be read (not local,
    missing, unsafe, malformed). From the subject index or the metadata
    cache when the files are unchanged. Never raises."""
    key = _key(bundle)
    if key is None:
        return None  # nothing to look up a relative path from
    indexed = _subject_index.get(key)
    if indexed is not None and _still_valid(bundle, indexed):
        return indexed.folded
    cached = _metadata_cache.get(key)
    if cached is not None and _still_valid(bundle, cached):
        entry = cached
    else:
        entry = _read(bundle, "subjects")
    result = entry.result
    folded = _fold_all(result.fields.subjects) if result.state == READ and result.fields else None
    if result.cacheable:
        item = _Subjects(folded, entry.container_id, entry.opf_rel, entry.opf_id)
        _subject_index.put(key, item, 128 + sum(64 + 4 * len(s) for s in folded or ()))
    else:
        _subject_index.discard(key)
    return folded


def subjects_match(needle: Optional[str], folded: Optional[Iterable[str]]) -> bool:
    """Whether a ``search``-style needle (already folded) is in any of
    the folded subjects."""
    if needle is None or not folded:
        return False
    return any(needle in subject for subject in folded)

