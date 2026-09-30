"""
Minimal stand-in for the GenVM `genlayer` module.

This exists for one reason: the validator half of the consensus round never runs in
direct mode, so the only way to test it is to import the contract module outside the
VM and drive its functions directly. The stub replaces the SDK surface, never the
contract. Every function under test in tests/unit is the exact function that ships
in contracts/semantic_access_gate.py, byte for byte.

The web and LLM handlers are settable per test so the deterministic logic wrapped
around the network boundary can be exercised across every branch.
"""

import typing


class UserError(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class VMError(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class Return:
    """Mirrors gl.vm.Return: a successful leader result carrying its payload."""

    def __init__(self, calldata: typing.Any) -> None:
        self.calldata = calldata


class Rollback:
    """Mirrors a leader that reverted with a message."""

    def __init__(self, message: str = "") -> None:
        self.message = message


class _Web:
    """Web handlers, swapped per test. Default raises so an unset test fails loudly."""

    get_handler: typing.Any = None
    render_handler: typing.Any = None

    def get(self, url: str, headers: typing.Any = None) -> typing.Any:
        if _Web.get_handler is None:
            raise AssertionError("web.get called with no handler installed: " + url)
        return _Web.get_handler(url)

    def render(self, url: str, mode: str = "html", **kwargs: typing.Any) -> typing.Any:
        if _Web.render_handler is None:
            raise AssertionError("web.render called with no handler installed: " + url)
        return _Web.render_handler(url, mode)


class WebResponse:
    def __init__(self, status: int, body: typing.Any, headers: typing.Any = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class _Nondet:
    web = _Web()
    prompt_handler: typing.Any = None

    def exec_prompt(
        self, prompt: str, response_format: str = "text", images: typing.Any = None
    ) -> typing.Any:
        if _Nondet.prompt_handler is None:
            raise AssertionError("exec_prompt called with no handler installed")
        return _Nondet.prompt_handler(prompt)


class _VM:
    UserError = UserError
    VMError = VMError
    Return = Return

    # Captured leader/validator pair from the most recent run_nondet_unsafe call,
    # so tests can drive the validator function with arbitrary leader results.
    last_leader: typing.Any = None
    last_validator: typing.Any = None

    def run_nondet_unsafe(self, leader_fn: typing.Any, validator_fn: typing.Any):
        _VM.last_leader = leader_fn
        _VM.last_validator = validator_fn
        return leader_fn()

    def spawn_sandbox(self, fn: typing.Any):
        return fn()


def _identity_decorator(fn):
    return fn


class _Write:
    def __call__(self, fn):
        return fn

    payable = staticmethod(_identity_decorator)

    def min_gas(self, **kwargs):
        return _identity_decorator


class _View:
    def __call__(self, fn):
        return fn

    def min_gas(self, **kwargs):
        return _identity_decorator


class _Public:
    view = _View()
    write = _Write()


class _Message:
    sender_address: typing.Any = None
    value: int = 0


class Contract:
    pass


class _ContractHandle:
    def __init__(self, address):
        self.address = address

    def view(self):
        return self

    def emit(self, **kwargs):
        return None

    def emit_transfer(self, **kwargs):
        return None


class _GL:
    vm = _VM()
    nondet = _Nondet()
    public = _Public()
    message = _Message()
    message_raw: dict = {}
    Contract = Contract

    def get_contract_at(self, address):
        return _ContractHandle(address)


gl = _GL()


def allow_storage(cls):
    return cls


class _Generic:
    def __class_getitem__(cls, item):
        return cls


class TreeMap(dict, _Generic):
    def __class_getitem__(cls, item):
        return cls


class DynArray(list, _Generic):
    def __class_getitem__(cls, item):
        return cls


class Array(list, _Generic):
    def __class_getitem__(cls, item):
        return cls


class Address:
    def __init__(self, value: str = "0x" + "0" * 40):
        self._value = str(value)

    @property
    def as_hex(self) -> str:
        return self._value

    def __eq__(self, other):
        return isinstance(other, Address) and other._value.lower() == self._value.lower()

    def __hash__(self):
        return hash(self._value.lower())


Keccak256 = bytes


def _int_type(value=0):
    return int(value)


u8 = u16 = u24 = u32 = u40 = u48 = u56 = u64 = _int_type
u72 = u80 = u88 = u96 = u104 = u112 = u120 = u128 = _int_type
u136 = u144 = u152 = u160 = u168 = u176 = u184 = u192 = _int_type
u200 = u208 = u216 = u224 = u232 = u240 = u248 = u256 = _int_type
i8 = i16 = i24 = i32 = i40 = i48 = i56 = i64 = _int_type
i72 = i80 = i88 = i96 = i104 = i112 = i120 = i128 = _int_type
i136 = i144 = i152 = i160 = i168 = i176 = i184 = i192 = _int_type
i200 = i208 = i216 = i224 = i232 = i240 = i248 = i256 = _int_type
bigint = _int_type

__all__ = [
    "gl",
    "Address",
    "allow_storage",
    "Array",
    "DynArray",
    "Keccak256",
    "TreeMap",
    "bigint",
    "u8", "u16", "u24", "u32", "u40", "u48", "u56", "u64",
    "u72", "u80", "u88", "u96", "u104", "u112", "u120", "u128",
    "u136", "u144", "u152", "u160", "u168", "u176", "u184", "u192",
    "u200", "u208", "u216", "u224", "u232", "u240", "u248", "u256",
    "i8", "i16", "i24", "i32", "i40", "i48", "i56", "i64",
    "i72", "i80", "i88", "i96", "i104", "i112", "i120", "i128",
    "i136", "i144", "i152", "i160", "i168", "i176", "i184", "i192",
    "i200", "i208", "i216", "i224", "i232", "i240", "i248", "i256",
]
