"""Drift cases: every public ``PyAppleBooks`` method, as the MCP calls
it, for the schema-drift tests (``tests/test_schema_drift.py``).

The drift tests seed a library (``test_schema_drift.seed``), drift a copy
of it (columns renamed or dropped) and require :func:`everything` to
return the same results on both. Each stream adds its new methods in a
module of its own, ``tests/drift_cases/<stream>.py``, so no stream edits
another's cases. A module defines:

``CASES``
    ``{label: case}``; ``case(api, rows)`` calls the method and returns
    a comparable result (traverse relations, project models to tuples).
    The label is the method's name, optionally followed by a qualifier
    in parentheses (``'list_books(all)'``). Labels are unique across
    modules.
``EXEMPT`` (optional)
    ``{method: reason}`` for a public method with no drift case.
``seed(lib, rows)`` (optional)
    Adds rows the module's cases need to the seeded ``FixtureLibrary``
    and returns a dict of new keys for ``rows`` (or None). Runs on both
    the full and the drifted library, before any drift.

``test_schema_drift.test_every_public_method_has_a_drift_case`` fails
when a public method of ``PyAppleBooks`` has neither a case nor an
exemption.
"""

import importlib
import pkgutil
from types import ModuleType
from typing import Callable, Dict, List, Tuple

Case = Callable[[object, dict], object]


def modules() -> List[ModuleType]:
    """Every case module in this package, by name."""
    names = sorted(info.name for info in pkgutil.iter_modules(__path__) if not info.name.startswith("_"))
    return [importlib.import_module(f"{__name__}.{name}") for name in names]


def method_of(label: str) -> str:
    """The method a case label names (``'list_books(all)'`` -> ``'list_books'``)."""
    return label.split("(", 1)[0].strip()


def cases() -> Dict[str, Case]:
    """``{label: case}`` over every module; a label two modules define
    raises ``ValueError``."""
    found: Dict[str, Case] = {}
    for module in modules():
        for label, case in getattr(module, "CASES", {}).items():
            if label in found:
                raise ValueError(f"drift case {label!r} is defined twice ({module.__name__})")
            found[label] = case
    return found


def exempt() -> Dict[str, Tuple[str, str]]:
    """``{method: (module, reason)}`` over every module."""
    found: Dict[str, Tuple[str, str]] = {}
    for module in modules():
        for method, reason in getattr(module, "EXEMPT", {}).items():
            if method in found:
                raise ValueError(f"{method!r} is exempted twice ({module.__name__})")
            found[method] = (module.__name__, reason)
    return found


def covered() -> Dict[str, List[str]]:
    """``{method: [labels]}``: the methods the cases call."""
    methods: Dict[str, List[str]] = {}
    for label in cases():
        methods.setdefault(method_of(label), []).append(label)
    return methods


def seed(lib, rows: dict) -> dict:
    """Run every module's ``seed(lib, rows)`` and return ``rows`` with
    the keys they add (a key added twice raises ``ValueError``)."""
    rows = dict(rows)
    for module in modules():
        hook = getattr(module, "seed", None)
        if hook is None:
            continue
        added = hook(lib, dict(rows)) or {}
        clash = set(added) & set(rows)
        if clash:
            raise ValueError(f"{module.__name__}.seed() redefines rows {sorted(clash)}")
        rows.update(added)
    return rows


def everything(api, rows: dict) -> dict:
    """``{label: result}`` for every case."""
    return {label: case(api, rows) for label, case in cases().items()}


def outcome(call: Callable[[], object]):
    """``call()``'s result, or ``('raised', class name, message)`` if it
    raises an exception: for cases whose seeded call is expected to
    fail the same way on every schema."""
    try:
        return call()
    except Exception as e:  # noqa: BLE001 (compared, not swallowed)
        return ("raised", type(e).__name__, str(e))
