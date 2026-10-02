"""scripts/check_dist.py's metadata rules (the script itself runs in CI's
dist job against the built wheel). Skipped where scripts/ isn't shipped,
as in the sdist or a copied test tree."""

import fnmatch
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_dist.py"
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/check_dist.py not present")

BASE = ["ebooklib>=0.20", "beautifulsoup4>=4.12"]
PDF = 'pyobjc-framework-Quartz<13,>=11.1; sys_platform == "darwin" and extra == "pdf"'


@pytest.fixture(scope="module")
def check_dist():
    spec = importlib.util.spec_from_file_location("check_dist", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def problems(check_dist, requires, provides=()):
    errors = []
    check_dist.check_requirements(list(requires), list(provides), errors)
    return errors


def test_the_110_requirements_pass(check_dist):
    assert problems(check_dist, BASE) == []


def test_no_extra_is_allowed_in_this_release(check_dist):
    # The optional PDF support moved to a later release: the wheel
    # declares no extra.
    assert check_dist.EXTRAS == {}
    assert problems(check_dist, BASE + [PDF], ["pdf"])
    assert problems(check_dist, BASE, ["pdf"])


@pytest.fixture
def with_pdf_extra(check_dist, monkeypatch):
    # The extras rules, as a release with the PDF support would use them.
    monkeypatch.setattr(check_dist, "EXTRAS", {"pdf": "pyobjc-framework-quartz"})
    return check_dist


def test_one_pdf_extra_on_quartz_passes(with_pdf_extra):
    check_dist = with_pdf_extra
    assert problems(check_dist, BASE + [PDF], ["pdf"]) == []
    single_quotes = "pyobjc-framework-quartz>=11.1; extra == 'pdf'"
    assert problems(check_dist, BASE + [single_quotes], ["pdf"]) == []
    # "or" inside a quoted value is not an operator.
    quoted_or = 'pyobjc-framework-Quartz>=11.1; platform_version != "or" and extra == "pdf"'
    assert problems(check_dist, BASE + [quoted_or], ["pdf"]) == []


@pytest.mark.parametrize("requires, provides", [
    (BASE + [PDF], []),  # extra not declared
    (BASE, ["pdf"]),  # declared, nothing required
    (BASE + [PDF, PDF], ["pdf"]),  # twice
    (BASE + ['pyobjc-core>=11; extra == "pdf"'], ["pdf"]),  # another distribution
    (BASE + ['rich; extra == "cli"'], ["cli"]),  # another extra
    (BASE + ['tomli; python_version < "3.11"'], []),  # a conditional dependency
    # The extra plus an "or": Quartz would be required without the extra.
    (BASE + ['pyobjc-framework-Quartz>=11.1; extra == "pdf" or python_version >= "3"'], ["pdf"]),
    (BASE + ['pyobjc-framework-Quartz>=11.1; sys_platform == "darwin" or extra == "pdf"'], ["pdf"]),
    (BASE + ["pyobjc-framework-Quartz>=11.1; (extra == 'pdf' or os_name == 'posix')"], ["pdf"]),
    (BASE[:1], []),  # one missing
    (BASE + ["lxml"], []),  # one added
])
def test_anything_else_fails(with_pdf_extra, requires, provides):
    assert problems(with_pdf_extra, requires, provides)


def test_planned_new_files_are_allowed(check_dist):
    def allowed(name):
        return any(fnmatch.fnmatch(name, p) for p in check_dist.ADDED_ALLOWED)

    for name in ("_icloud.py", "_messages.py", "_epub_index.py", "_content_resolve.py",
                 "_content_reading.py", "_spans.py", "_opf.py", "_prefs.py",
                 "positions.py", "search.py", "engagement.py",
                 "book_info.py", "_api/__init__.py", "_api/_common.py", "_api/search.py",
                 "_api/pdf.py", "models/book_metadata.py", "models/series.py",
                 "testing/schemas/macos-26.7-25G229_books-8.5-6570/AEBookInfo.sql"):
        assert allowed(f"py_apple_books/{name}"), name
    # Tier B files that moved to a later release, and anything unplanned.
    for name in ("_cfi.py", "_pdf_worker.py", "_pdf_runner.py", "pdf.py",
                 "other.py", "models/other.py"):
        assert not allowed(f"py_apple_books/{name}"), name
    assert not allowed("tests/conftest.py")

