"""scripts/mcp_regress on synthetic runs: 1.10.0 baselines allow no
change, the MCP versions must match, 0.9.0 gets its extra calls (in
harness.py and odd_args.py).
Skipped where scripts/ isn't shipped (the sdist, a copied test tree)."""

import importlib
import json
import pathlib
import sys
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mcp_regress"
pytestmark = pytest.mark.skipif(not (HERE / "compare.py").is_file(),
                                reason="scripts/mcp_regress not present")


MODULES = ("harness", "compare", "expected", "odd_args")


@pytest.fixture
def tools(monkeypatch):
    # Restores all of sys.path afterwards, odd_args.py's own insert included.
    monkeypatch.syspath_prepend(str(HERE))
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    mods = types.SimpleNamespace(**{n: importlib.import_module(n) for n in MODULES})
    yield mods
    for name in (*MODULES, "mcp_regress_expected"):
        sys.modules.pop(name, None)


def write_runs(tmp_path, outs, **info):
    paths = []
    for name, out in zip(("a", "b", "c"), outs):
        run = {"lib": f"/{name}", "out": out, "timings": {k: 0.0 for k in out}}
        run.update(info.get(name, info.get("all", {})))
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(run))
        paths.append(str(path))
    return paths


BASE = {'search_annotations({"limit": 200, "text": "the"})': "one",
        'list_all_books({})': "books"}
CHANGED = dict(BASE, **{'search_annotations({"limit": 200, "text": "the"})': "two"})
V110 = {"mcp_version": "0.9.0", "lib_release": "1.10.0", "lib_version": "1.10.0"}


def test_no_items_are_active_against_110(tools):
    assert tools.expected.active_items("final", "1.10.0") == []
    assert len(tools.expected.active_items("final", "1.9.1")) == len(tools.expected.ITEMS)
    assert tools.expected.active_items("query", "1.9.1") == ["F08", "G4.1", "F28"]
    assert all(rule["release"] == "1.10.0" for rule in tools.expected.ITEMS.values())


def test_identical_runs_pass_against_110(tools, tmp_path):
    paths = write_runs(tmp_path, [BASE, BASE, BASE],
                       a=V110, b=V110, c={"mcp_version": "0.9.0", "lib_release": None})
    assert tools.compare.main(paths) == 0  # no --home needed: no per-book rule is active


def test_a_change_is_unexpected_against_110_but_allowed_against_191(tools, tmp_path, capsys):
    paths = write_runs(tmp_path, [BASE, BASE, CHANGED],
                       a=V110, b=V110, c={"mcp_version": "0.9.0", "lib_release": None})
    assert tools.compare.main(paths) == 1
    assert "UNEXPECTED: 1" in capsys.readouterr().out
    # The same change against 1.9.1 baselines is the 1.10 search rework.
    legacy = write_runs(tmp_path, [BASE, BASE, CHANGED])  # runs without the new keys
    assert tools.compare.main(legacy + ["--through", "query"]) == 0
    assert tools.compare.main(legacy) == 2  # final needs --home for the per-book rules


def test_runs_must_share_the_mcp_version_and_a_known_baseline(tools, tmp_path):
    mixed = write_runs(tmp_path, [BASE, BASE, BASE],
                       a=V110, b=V110, c={"mcp_version": "0.8.2", "lib_release": None})
    assert tools.compare.main(mixed) == 2
    dev = dict(V110, lib_release=None)
    unknown = write_runs(tmp_path, [BASE, BASE, BASE], a=dev, b=dev, c=dev)
    assert tools.compare.main(unknown) == 2
    assert tools.compare.main(unknown + ["--baseline-release", "1.10.0"]) == 0


# The arguments apple-books-mcp 0.9.0 added to its read tools (from the
# released 0.8.2 and 0.9.0 sources), and search_books, new in 0.9.0.
ADDED_IN_090 = {
    "search_books": {"query", "limit", "offset"},
    "search_books_by_title": {"limit", "offset"},
    "list_all_collections": {"offset"},
    "list_all_books": {"offset"},
    "get_books_by_genre": {"offset"},
    "get_books_in_progress": {"offset"},
    "get_finished_books": {"offset"},
    "get_unstarted_books": {"offset"},
    "get_recently_read_books": {"offset"},
    "list_all_annotations": {"offset"},
    "list_annotations": {"offset"},
    "recent_annotations": {"offset"},
    "get_highlights_by_color": {"offset", "order_by"},
    "search_notes": {"offset", "order_by"},
    "search_annotations": {"offset", "order_by"},
    "get_annotations_by_date_range": {"offset", "order_by"},
}
ARGS = dict(book_ids=[1, 2], annotated=[1], collections=[3], context_ids=[4],
            describe_ids=[4], chapter_calls=[[1, "c1"], [1, "c2"], [2, "x"]],
            revisit_title="t", colors=["yellow", "green"])


def test_090_gets_its_new_calls(tools):
    old = list(tools.harness.calls(ARGS, "0.8.2"))
    new = list(tools.harness.calls(ARGS, "0.9.0"))
    assert new[:len(old)] == old
    added = new[len(old):]
    assert ("search_books", {"query": "the"}) in added
    assert [kw for tool, kw in added if tool == "get_chapter_content"] == [{"book_id": 1}, {"book_id": 2}]
    assert all(tool != "search_books" for tool, _ in old)
    assert ("get_annotation_context", {"annotation_id": 4, "chars_before": 50, "chars_after": 50}) in added
    assert len(set(map(repr, new))) == len(new)  # no call twice: keys must be unique


def test_every_argument_090_added_is_passed(tools):
    """Each argument 0.9.0 added, on every tool that has it, is passed by
    at least one call, and order_by asks for the non-default order."""
    added = list(tools.harness.calls(ARGS, "0.9.0"))[len(list(tools.harness.calls(ARGS, "0.8.2"))):]
    passed = {(tool, name) for tool, kw in added for name in kw}
    missing = sorted((tool, name) for tool, names in ADDED_IN_090.items() for name in names
                     if (tool, name) not in passed)
    assert not missing
    assert {kw["order_by"] for _, kw in added if "order_by" in kw} == {"oldest"}
    colors = [kw["color"] for tool, kw in added if tool == "get_highlights_by_color" and "order_by" in kw]
    assert colors == ARGS["colors"]


def test_odd_args_adds_090_calls_only_for_090(tools):
    odd = tools.odd_args
    assert odd.calls_for(None) == odd.calls_for("0.8.2") == odd.CALLS
    new = odd.calls_for("0.9.0")
    assert new[:len(odd.CALLS)] == odd.CALLS and new[len(odd.CALLS):] == odd.CALLS_090
    keys = [odd.key_of(name, kwargs) for name, kwargs in new]
    assert len(set(keys)) == len(keys)
    assert all(key.isascii() for key in keys)  # a lone surrogate or NUL is printed escaped
    passed = {(tool, name, repr(value)) for tool, kwargs in odd.CALLS_090 for name, value in kwargs.items()}
    for tool in ("search_annotations", "list_all_books", "get_highlights_by_color"):
        assert {(tool, "offset", repr(odd.HUGE)), (tool, "offset", "-1")} <= passed
    assert ("search_annotations", "order_by", "'sideways'") in passed
    assert {("search_books", "query", repr(odd.SURROGATE)), ("search_books", "query", repr(chr(0)))} <= passed


def test_short_contexts_are_spread_and_capped(tools):
    args = dict(book_ids=[], annotated=[], collections=[], context_ids=list(range(100)),
                describe_ids=[], chapter_calls=[], revisit_title="t", colors=[])
    short = [kw["annotation_id"] for tool, kw in tools.harness.calls(args, "0.9.0")
             if tool == "get_annotation_context" and "chars_before" in kw]
    assert short == list(range(0, 100, 10))


def test_unscoped_falls_back_only_for_a_missing_keyword(tools, capsys):
    def old_release(limit=None):
        return ["default view"]

    def new_release(limit=None, *, include_deleted=False):
        return ["every row" if include_deleted else "default view"]

    calls = []

    def broken(limit=None, *, include_deleted=False):
        # Fails only in the scoped view, so a fallback to method() that
        # caught the TypeError would return the default view instead.
        calls.append(include_deleted)
        if include_deleted:
            raise TypeError("a bug inside the method")
        return ["default view"]

    assert tools.harness._unscoped(old_release, include_deleted=True) == ["default view"]
    assert "old_release has no include_deleted" in capsys.readouterr().err
    assert tools.harness._unscoped(new_release, include_deleted=True) == ["every row"]
    with pytest.raises(TypeError, match="a bug inside"):
        tools.harness._unscoped(broken, include_deleted=True)
    assert calls == [True]
    assert "note:" not in capsys.readouterr().err


@pytest.mark.parametrize("platform, on, allow, exits", [
    ("darwin", False, False, True),
    ("darwin", False, True, False),
    ("darwin", True, False, False),
    ("linux", False, False, False),
])
def test_runs_stop_when_downloads_cannot_be_turned_off(tools, monkeypatch, platform, on, allow, exits):
    monkeypatch.setattr(tools.harness, "disable_materialization", lambda: on)
    monkeypatch.setattr(tools.harness.sys, "platform", platform)
    if exits:
        with pytest.raises(SystemExit) as stop:
            tools.harness.require_download_policy(allow)
        assert stop.value.code == 2
    else:
        assert tools.harness.require_download_policy(allow) is on
    assert tools.harness.parse_args(["run", "--allow-downloads", "a", "b"]).allow_downloads
    assert not tools.harness.parse_args(["discover", "a"]).allow_downloads


def test_release_of_compares_sources(tools, tmp_path, monkeypatch):
    pkg = tmp_path / "py_apple_books"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '9.9.9'\n")
    lib = types.SimpleNamespace(__file__=str(pkg / "__init__.py"), __version__="9.9.9")
    monkeypatch.setitem(tools.harness.RELEASES, "9.9.9", tools.harness.source_digest(lib))
    assert tools.harness.release_of(lib) == "9.9.9"
    assert tools.harness.release_problem(lib) is None
    (pkg / "extra.py").write_text("")
    assert tools.harness.release_of(lib) is None
    assert "differ" in tools.harness.release_problem(lib)
    assert set(tools.harness.RELEASES) >= {"1.9.1", "1.10.0"}
