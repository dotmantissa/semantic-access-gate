"""
Load the shipped contract module outside the GenVM so its pure functions and its
validator logic can be driven directly.

The `genlayer` SDK is temporarily swapped for a stub, the contract module is
imported under the name `sag`, and the real module entry is then restored. Because
`from genlayer import *` binds its names at import time, `sag` keeps referring to
the stub afterwards while the rest of the test session is unaffected. This is what
lets tests/unit and tests/direct coexist in one pytest run.
"""

import importlib.util
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
_CONTRACT = _ROOT / "contracts" / "semantic_access_gate.py"

sys.path.insert(0, str(_HERE))
import genlayer_stub  # noqa: E402


def _load_contract_module():
    saved = sys.modules.get("genlayer")
    sys.modules["genlayer"] = genlayer_stub
    try:
        spec = importlib.util.spec_from_file_location("sag", _CONTRACT)
        module = importlib.util.module_from_spec(spec)
        sys.modules["sag"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("genlayer", None)
        else:
            sys.modules["genlayer"] = saved


sag = _load_contract_module()


@pytest.fixture
def contract_module():
    return sag


@pytest.fixture
def stub():
    return genlayer_stub


@pytest.fixture(autouse=True)
def reset_handlers():
    """Every test starts with no network handlers, so an unstubbed call fails loudly."""
    genlayer_stub._Web.get_handler = None
    genlayer_stub._Web.render_handler = None
    genlayer_stub._Nondet.prompt_handler = None
    yield
    genlayer_stub._Web.get_handler = None
    genlayer_stub._Web.render_handler = None
    genlayer_stub._Nondet.prompt_handler = None
