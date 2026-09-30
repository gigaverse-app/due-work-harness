"""
Comparable observation evidence, shared by generated and handwritten tests.

Only assertion names and locations leave the process. Values may contain tokens.
This records equality checks, not coverage or semantic equivalence between tests.

The records are strict Pydantic models with one JSON boundary, the report adapter
in :mod:`due_work_harness.evidence.observation_report`. Reading rejects unknown
keys and wrongly typed values, as the wire contract between pytest workers,
report files and the CLI requires.
"""

import inspect
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, FieldSerializationInfo, field_serializer

from due_work_harness.models import HarnessModel, MutableHarnessModel

ObservationOutcome: TypeAlias = Literal["passed", "failed", "skipped", "xfail", "xpass"]
ObservationPhase: TypeAlias = Literal["setup", "call", "teardown"]
ObservationKind: TypeAlias = Literal["generated", "application"]


class ObservationCheck(HarnessModel):
    """Canonical assertion metadata validated on the worker/file boundary; no domain values."""

    # Strict: a wrongly typed value on the wire (a bool for ``line``) is refused, not coerced.
    model_config = ConfigDict(strict=True)

    because: str
    fields: list[str]
    matched: bool
    path: str
    line: int

    @field_serializer("path")
    def _serialize_path(self, path: str, info: FieldSerializationInfo) -> str:
        """Make ``path`` relative to the serialization context's root when it lies beneath it."""
        return _source_path(path, info.context)


def _source_path(path: str, root: object) -> str:
    # Worker transport and report files normalize at the same serialization boundary.
    if root is None:
        return path
    assert isinstance(root, Path), "observation serialization context must be the pytest root"
    source = Path(path)
    return source.relative_to(root).as_posix() if source.is_relative_to(root) else source.as_posix()


class ObservedTest(MutableHarnessModel):
    """One validated execution record; serves pytest workers, JSON files and the CLI."""

    model_config = ConfigDict(strict=True)

    kind: ObservationKind
    outcomes: dict[ObservationPhase, ObservationOutcome] = Field(default_factory=dict)
    checks: list[ObservationCheck] = Field(default_factory=list)

    @property
    def status(self) -> str:
        for status in ("failed", "xfail", "skipped", "xpass"):
            if status in self.outcomes.values():
                return status
        return "passed" if self.outcomes == dict.fromkeys(("setup", "call", "teardown"), "passed") else "incomplete"


# Repeated node IDs (xdist --dist=each, reruns) own separate executions, not one overwritten outcome.
ObservationReport: TypeAlias = dict[str, list[ObservedTest]]


_recording: ContextVar[list[ObservationCheck] | None] = ContextVar("durable_observations", default=None)


@contextmanager
def record_observations() -> Iterator[list[ObservationCheck]]:
    checks: list[ObservationCheck] = []
    token = _recording.set(checks)
    try:
        yield checks
    finally:
        _recording.reset(token)


def _model_field_names(model: BaseModel) -> list[str]:
    return list(type(model).model_fields)


T = TypeVar("T")


def assert_observation(actual: T, expected: T, *, because: str) -> None:
    """
    Assert the whole observation; record field names without reading values for reporting.

    Existing mappings, dataclasses and Pydantic models already name their observations. Scalar
    and tuple observations stay supported but cannot offer field-level navigation.
    Call in the test thread; recording does not propagate to new worker threads.
    """
    matched = actual == expected
    if (checks := _recording.get()) is not None:
        if isinstance(expected, Mapping) and all(isinstance(key, str) for key in expected):
            names = sorted(expected)
        elif isinstance(expected, BaseModel):
            # Field(exclude=True) affects serialization, not equality. Never dump observation values.
            names = _model_field_names(expected)
        elif is_dataclass(expected) and not isinstance(expected, type):
            names = [item.name for item in fields(expected) if item.compare]
        else:
            names = ["<value>"]
        frame = inspect.currentframe()
        assert frame is not None and frame.f_back is not None
        caller = frame.f_back
        checks.append(
            ObservationCheck(
                because=because,
                fields=names,
                matched=matched,
                path=str(Path(caller.f_code.co_filename).resolve()),
                line=caller.f_lineno,
            )
        )
        del caller, frame
    assert matched, because
