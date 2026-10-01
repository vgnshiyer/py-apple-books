"""The suite's own safety nets (tests/conftest.py): where the fixture-HOME
guard looks, and the per-test content-cache reset."""

import pathlib
import types

from tests import conftest


def test_guard_covers_the_container_files_next_to_the_stores():
    locations = dict(conftest._library_locations())
    root = conftest.FIXTURE_HOME.resolve()
    for rel in (conftest.PREFS_PLIST, conftest.BOOK_INFO_CACHES):
        derived = [path for label, path in locations.items() if label.startswith(rel)]
        assert derived, rel
        for path in derived:
            assert pathlib.Path(path).resolve().is_relative_to(root)
    data = root / "Library" / "Containers" / "com.apple.iBooksX" / "Data"
    assert data / conftest.PREFS_PLIST in {pathlib.Path(p).resolve() for p in locations.values()}


def test_guard_checks_default_location_helpers(monkeypatch):
    fake = types.ModuleType("py_apple_books._prefs")
    fake.default_prefs_path = lambda: "/elsewhere/com.apple.iBooksX.plist"
    fake.default_needs_arg_path = lambda required: "/never/called"
    fake.default_not_a_location = lambda: "/ignored"
    monkeypatch.setitem(__import__("sys").modules, "py_apple_books._prefs", fake)
    monkeypatch.setattr(conftest.importlib.util, "find_spec",
                        lambda name: object() if name == "py_apple_books._prefs" else None)
    locations = dict(conftest._library_locations())
    assert locations["py_apple_books._prefs.default_prefs_path()"] == "/elsewhere/com.apple.iBooksX.plist"
    assert not any("needs_arg" in label or "not_a_location" in label for label in locations)


def test_content_cache_clearer_is_found_when_the_library_has_one(monkeypatch):
    from py_apple_books import content

    calls = []
    monkeypatch.setattr(content, "clear_content_cache", lambda: calls.append(1), raising=False)
    clear = conftest._content_cache_clearer()
    assert clear is not None
    clear()
    assert calls == [1]
    monkeypatch.delattr(content, "clear_content_cache")
    assert conftest._content_cache_clearer() is None
