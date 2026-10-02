"""tests/mcp_compat/run.py without starting a server: which probes a
pinned apple-books-mcp version gets, how uvx is asked for the library
(with an optional extra), and the option checks."""

import argparse
import importlib.util
import pathlib
import sys
import types
import zipfile

import pytest

RUN_PY = pathlib.Path(__file__).resolve().parent / "mcp_compat" / "run.py"
pytestmark = pytest.mark.skipif(not RUN_PY.is_file(), reason="tests/mcp_compat/run.py not present")


@pytest.fixture
def run():
    spec = importlib.util.spec_from_file_location("mcp_compat_run", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # NamedTuple and dataclass lookups need it
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("since, version, applies", [
    (None, "0.8.2", True),
    (None, "latest", True),
    ("0.9.0", "0.8.2", False),
    ("0.9.0", "0.9.0", True),
    ("0.9.0", "0.10.0", True),  # compared as numbers, not as text
    ("0.9.0", "1.0", True),
    ("0.9.0", "latest", True),
    ("0.10.0", "0.9.0", False),
])
def test_probes_apply_from_their_version(run, since, version, applies):
    assert run.applies(run.Probe({}, since=since), version) is applies


def _uvx_args(tmp_path, extras=None):
    lib = tmp_path / "py_apple_books-1.11.0-py3-none-any.whl"
    return types.SimpleNamespace(mcp_version="0.9.0", uvx_python="3.12", lib=lib, lib_extra=extras)


def test_uvx_command_with_and_without_an_extra(run, tmp_path, monkeypatch):
    monkeypatch.setattr(run.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(run, "_uv_dir", lambda kind: None)
    plain = run.uvx_server(_uvx_args(tmp_path), {})
    lib = tmp_path / "py_apple_books-1.11.0-py3-none-any.whl"
    assert plain == ["uvx", "--python", "3.12", "--from", "apple-books-mcp==0.9.0", "--with", str(lib),
                     "--refresh-package", "py-apple-books", "apple-books-mcp"]
    extra = run.uvx_server(_uvx_args(tmp_path, ["pdf"]), {})
    # A PEP 508 direct reference, so uv installs the extra's dependencies.
    assert extra[extra.index("--with") + 1] == f"py-apple-books[pdf] @ {lib.as_uri()}"
    assert extra[:extra.index("--with")] == plain[:plain.index("--with")]
    both = run.uvx_server(_uvx_args(tmp_path, ["pdf", "cli"]), {})
    assert both[both.index("--with") + 1].startswith("py-apple-books[pdf,cli] @ file://")


def _wheel(tmp_path, *extras):
    path = tmp_path / "py_apple_books-9.9.9-py3-none-any.whl"
    metadata = "Metadata-Version: 2.4\nName: py-apple-books\nVersion: 9.9.9\n"
    metadata += "".join(f"Provides-Extra: {e}\n" for e in extras)
    metadata += "\nProvides-Extra: not-a-header (this is the description)\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("py_apple_books/__init__.py", "")
        zf.writestr("py_apple_books-9.9.9.dist-info/METADATA", metadata)
    return path


def test_declared_extras_of_a_wheel_and_a_tree(run, tmp_path):
    assert run.declared_extras(_wheel(tmp_path)) == set()
    assert run.declared_extras(_wheel(tmp_path, "pdf", "Fancy_Name")) == {"pdf", "fancy-name"}
    tree = tmp_path / "tree"
    tree.mkdir()
    assert run.declared_extras(tree) is None  # no pyproject.toml
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "py-apple-books"\n[project.optional-dependencies]\n'
        'pdf = ["pyobjc-framework-Quartz>=11.1; sys_platform == \'darwin\'"]\n')
    expected = {"pdf"} if sys.version_info >= (3, 11) else None  # tomllib from 3.11
    assert run.declared_extras(tree) == expected


def test_option_checks_exit_2(run, tmp_path, capsys):
    wheel = _wheel(tmp_path)
    for argv in (["--mcp-version", "0.9.0", "--python", sys.executable, "--lib-extra", "pdf"],
                 ["--mcp-version", "0.9.0", "--lib-extra", "not an extra"],
                 ["--mcp-version", "0.9.0", "--lib", str(wheel), "--lib-extra", "pdf"],
                 ["--mcp-version", "latest", "--update"]):
        with pytest.raises(SystemExit) as stop:
            run.main(argv)
        assert stop.value.code == 2, argv
    assert "declares no extras" in capsys.readouterr().err


def test_extra_name_type(run):
    assert run.extra_name("pdf") == "pdf"
    for bad in ("", "-pdf", "pdf ", "a,b"):
        with pytest.raises(argparse.ArgumentTypeError):
            run.extra_name(bad)
