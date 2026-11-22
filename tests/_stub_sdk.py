"""A tiny stand-in for the GenLayer SDK, for logic tests only.

It lets tests/test_logic.py import contracts/consensus_feed.py and run every
method with plain `python3`, with no GenVM, no gltest and no network.

It is NOT GenVM. It proves the contract's own logic (parsing, median,
outlier rejection, quorum, tolerance, ring history, access control). It
proves nothing about GenVM compatibility; tests/test_consensus_feed.py
covers that through gltest's direct mode.
"""
import importlib.util
import sys
import types
import typing
from pathlib import Path

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "consensus_feed.py"


class u256(int):
    pass


class Address:
    def __init__(self, value):
        if isinstance(value, Address):
            value = value.as_bytes
        if isinstance(value, str):
            value = bytes.fromhex(value[2:] if value.startswith("0x") else value)
        if len(value) != 20:
            raise ValueError("address must be 20 bytes")
        self.as_bytes = bytes(value)

    def __eq__(self, other):
        return isinstance(other, Address) and other.as_bytes == self.as_bytes

    def __hash__(self):
        return hash(self.as_bytes)


class TreeMap(dict):
    def __class_getitem__(cls, item):
        return typing.Annotated[cls, item]


class DynArray(list):
    def __class_getitem__(cls, item):
        return typing.Annotated[cls, item]


class UserError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class Return:
    def __init__(self, calldata):
        self.calldata = calldata


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body


class World:
    """What a test controls: who is calling, and what the web answers."""

    def __init__(self):
        self.sender = Address(b"\x01" * 20)
        self.web = {}            # url substring -> (status, body) | Exception
        self.captured = []       # (leader_result, leader_fn, validator_fn)

    def mock(self, needle, status=200, body=""):
        self.web[needle] = (status, body)

    def fail(self, needle):
        self.web[needle] = OSError("network down")

    def run_validator(self, leader_result=None):
        stored, _leader, validator = self.captured[-1]
        return validator(Return(stored if leader_result is None else leader_result))


def _web_get(world):
    def get(url, **_kwargs):
        for needle, answer in world.web.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                status, body = answer
                return Response(status, body.encode("utf-8"))
        raise OSError("no mock for " + url)
    return get


def _run_nondet_unsafe(world):
    def run(leader_fn, validator_fn):
        result = leader_fn()
        world.captured.append((result, leader_fn, validator_fn))
        return result
    return run


class _Contract:
    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)
        for name, annotation in getattr(cls, "__annotations__", {}).items():
            origin = typing.get_origin(annotation)
            base = typing.get_args(annotation)[0] if origin is typing.Annotated else None
            if base in (TreeMap, DynArray):
                setattr(self, name, base())
        return self


def _public():
    def passthrough(fn):
        return fn
    write = passthrough
    write.payable = passthrough
    return types.SimpleNamespace(view=passthrough, write=write)


def load(world):
    """Import the contract against this stub. Returns its module."""
    class _Message:
        @property
        def sender_address(self):
            return world.sender

    gl = types.SimpleNamespace(
        Contract=_Contract,
        public=_public(),
        message=_Message(),
        vm=types.SimpleNamespace(
            UserError=UserError, Return=Return,
            run_nondet_unsafe=_run_nondet_unsafe(world),
        ),
        nondet=types.SimpleNamespace(web=types.SimpleNamespace(get=_web_get(world))),
    )
    stub = types.ModuleType("genlayer")
    stub.gl = gl
    stub.u256 = u256
    stub.Address = Address
    stub.TreeMap = TreeMap
    stub.DynArray = DynArray
    stub.__all__ = ["gl", "u256", "Address", "TreeMap", "DynArray"]

    previous = sys.modules.get("genlayer")
    sys.modules["genlayer"] = stub
    try:
        spec = importlib.util.spec_from_file_location("consensus_feed_under_test", CONTRACT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            del sys.modules["genlayer"]
        else:
            sys.modules["genlayer"] = previous
    return module
