"""Check the wheel and sdist in dist/ before they are published.

Usage: python scripts/check_dist.py [--baseline-wheel PATH]

Stdlib only, so it runs on any supported interpreter with nothing
installed. Checks:

- dist/ holds exactly one wheel and one sdist, both at the static
  ``__version__`` in py_apple_books/__init__.py.
- The wheel ships every module and package-data file from the source
  tree (including py.typed and models/mappings.ini), and no tests,
  __pycache__, bytecode or .DS_Store.
- METADATA declares ``Requires-Python: >=3.10`` and depends on exactly
  ebooklib and beautifulsoup4.
- The sdist carries the test suite and no junk.

``--baseline-wheel`` takes the previous release's wheel (``pip download
py-apple-books==1.9.1 --no-deps``) and compares package files, ignoring
``*.dist-info/``: every baseline file must still be shipped, and every
added file must match ``ADDED_ALLOWED``.
"""

import argparse
import email.parser
import fnmatch
import pathlib
import re
import sys
import tarfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = 'py_apple_books'

# Mirrors [tool.setuptools.package-data] in pyproject.toml. Checked
# against it where tomllib exists (3.11+); 3.10 trusts this copy.
PACKAGE_DATA = [
    'py.typed',
    'models/*.ini',
    'testing/schemas/*/*.sql',
    'testing/schemas/*/*.json',
]

REQUIRED_WHEEL_FILES = [
    'py_apple_books/models/mappings.ini',
    'py_apple_books/py.typed',
]
REQUIRES_PYTHON = '>=3.10'
REQUIRES_DIST = {'ebooklib', 'beautifulsoup4'}

# Package files this release may add on top of the baseline wheel.
# fnmatch's '*' also matches '/', so 'testing/*' covers subdirectories.
ADDED_ALLOWED = [
    'py_apple_books/text.py',
    'py_apple_books/db/metadata.py',
    'py_apple_books/testing/*',
]


def static_version():
    # Same regex the old setup.py used: parsed, not imported, so this
    # works without the runtime dependencies.
    text = (ROOT / PACKAGE / '__init__.py').read_text()
    match = re.search(r"^__version__ = ['\"]([^'\"]+)['\"]", text, re.M)
    if match is None:
        sys.exit(f'check_dist: no __version__ in {PACKAGE}/__init__.py')
    return match.group(1)


def canonical_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def is_junk(path):
    parts = path.split('/')
    return (
        '__pycache__' in parts
        or parts[-1] == '.DS_Store'
        or parts[-1].endswith(('.pyc', '.pyo'))
    )


def package_files(names):
    """Wheel members outside ``*.dist-info/``, without directory entries."""
    return {
        n for n in names
        if not n.endswith('/') and not n.split('/', 1)[0].endswith('.dist-info')
    }


def expected_source_files():
    """Package files the wheel must ship, found in the source tree."""
    pkg = ROOT / PACKAGE
    found = set()
    for path in pkg.rglob('*.py'):
        rel = path.relative_to(ROOT).as_posix()
        if not is_junk(rel):
            found.add(rel)
    for pattern in PACKAGE_DATA:
        for path in pkg.glob(pattern):
            if path.is_file() and path.suffix != '.py':
                found.add(path.relative_to(ROOT).as_posix())
    return found


def check_package_data_in_sync(errors):
    try:
        import tomllib
    except ImportError:  # 3.10
        return
    with open(ROOT / 'pyproject.toml', 'rb') as f:
        declared = tomllib.load(f)['tool']['setuptools']['package-data'][PACKAGE]
    if declared != PACKAGE_DATA:
        errors.append(
            f'PACKAGE_DATA {PACKAGE_DATA} is out of sync with pyproject.toml '
            f'{declared}'
        )


def find_dists(dist_dir, errors):
    wheels = sorted(dist_dir.glob('*.whl'))
    sdists = sorted(dist_dir.glob('*.tar.gz'))
    if len(wheels) != 1 or len(sdists) != 1:
        errors.append(
            f'{dist_dir} must hold exactly 1 wheel and 1 sdist, found '
            f'{[p.name for p in wheels + sdists]}'
        )
        return None, None
    return wheels[0], sdists[0]


def check_wheel(wheel, version, errors):
    # {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl
    file_version = wheel.name[:-len('.whl')].split('-')[1]
    if file_version != version:
        errors.append(f'{wheel.name}: version {file_version} != __version__ {version}')

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        metadata_files = [
            n for n in names
            if n.count('/') == 1 and n.endswith('.dist-info/METADATA')
        ]
        if len(metadata_files) != 1:
            errors.append(f'{wheel.name}: expected one METADATA, found {metadata_files}')
            return names
        metadata = zf.read(metadata_files[0]).decode('utf-8')

    shipped = package_files(names)
    for required in REQUIRED_WHEEL_FILES:
        if required not in shipped:
            errors.append(f'{wheel.name}: missing {required}')
    for missing in sorted(expected_source_files() - shipped):
        errors.append(f'{wheel.name}: missing source file {missing}')
    for name in names:
        if name.split('/', 1)[0] == 'tests' or is_junk(name):
            errors.append(f'{wheel.name}: must not contain {name}')

    headers = email.parser.HeaderParser().parsestr(metadata)
    if headers['Version'] != version:
        errors.append(f'METADATA Version {headers["Version"]} != __version__ {version}')
    if headers['Requires-Python'] != REQUIRES_PYTHON:
        errors.append(
            f'METADATA Requires-Python {headers["Requires-Python"]!r} != {REQUIRES_PYTHON!r}'
        )
    requires = headers.get_all('Requires-Dist') or []
    dist_names = {
        canonical_name(re.match(r'\s*([A-Za-z0-9._-]*)', req).group(1))
        for req in requires
    }
    if dist_names != REQUIRES_DIST:
        errors.append(
            f'METADATA Requires-Dist names {sorted(dist_names)} != '
            f'{sorted(REQUIRES_DIST)} (entries: {requires})'
        )
    return names


def check_sdist(sdist, version, errors):
    base = sdist.name[:-len('.tar.gz')]
    file_version = base.rpartition('-')[2]
    if file_version != version:
        errors.append(f'{sdist.name}: version {file_version} != __version__ {version}')

    with tarfile.open(sdist) as tf:
        names = tf.getnames()
    top = f'{base}/'
    if f'{top}tests/conftest.py' not in names:
        errors.append(f'{sdist.name}: missing tests/conftest.py')
    for name in names:
        if is_junk(name):
            errors.append(f'{sdist.name}: must not contain {name}')


def compare_with_baseline(names, baseline, errors):
    with zipfile.ZipFile(baseline) as zf:
        old = package_files(zf.namelist())
    new = package_files(names)
    missing = sorted(old - new)
    added = sorted(new - old)

    print(f'baseline {baseline.name}: {len(old)} package files, candidate: {len(new)}')
    print(f'missing from candidate ({len(missing)}):')
    for name in missing:
        print(f'  {name}')
        errors.append(f'baseline file missing from the wheel: {name}')
    print(f'added in candidate ({len(added)}):')
    for name in added:
        allowed = any(fnmatch.fnmatch(name, pattern) for pattern in ADDED_ALLOWED)
        print(f'  {name}' + ('' if allowed else '  <- not in ADDED_ALLOWED'))
        if not allowed:
            errors.append(f'unexpected file added to the wheel: {name}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument(
        '--baseline-wheel', type=pathlib.Path,
        help="previous release's wheel; its package files must all still ship",
    )
    args = parser.parse_args(argv)

    errors = []
    version = static_version()
    check_package_data_in_sync(errors)
    wheel, sdist = find_dists(ROOT / 'dist', errors)
    if wheel is not None:
        print(f'checking {wheel.name} and {sdist.name} against __version__ {version}')
        names = check_wheel(wheel, version, errors)
        check_sdist(sdist, version, errors)
        if args.baseline_wheel is not None:
            compare_with_baseline(names, args.baseline_wheel, errors)

    if errors:
        for error in errors:
            print(f'FAIL: {error}', file=sys.stderr)
        print(f'check_dist: {len(errors)} problem(s)', file=sys.stderr)
        return 1
    print('check_dist: OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
