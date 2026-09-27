"""
Comparable observation evidence, shared by generated and handwritten tests.

Only assertion names and locations leave the process. Values may contain tokens.
This records equality checks, not coverage or semantic equivalence between tests.

The records are plain dataclasses with one explicit JSON boundary
(:func:`check_from_json` / :func:`observed_test_from_json` read it, :meth:`ObservationCheck.to_json`
and :meth:`ObservedTest.to_json` write it), so the core needs no validation
library. Reading rejects unknown keys and wrongly typed values, as the wire
contract between pytest workers, report files and the CLI requires.
"""

import inspect
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal, get_args

type ObservationOutcome = Literal["passed", "failed", "skipped", "xfail", "xpass"]
type ObservationPhase = Literal["setup", "call", "teardown"]
type ObservationKind = Literal["generated", "application"]

_OUTCOMES: frozenset[str] = frozenset(get_args(ObservationOutcome.__value__))
_PHASES: frozenset[str] = frozenset(get_args(ObservationPhase.__value__))
_KINDS: frozenset[str] = frozenset(get_args(ObservationKind.__value__))


def _require_keys(
    kind: str, data: object, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    assert isinstance(data, dict), f"{kind} must be a JSON object, got {type(data).__name__}"
    extra = set(data) - required - optional
    assert not extra, f"{kind} has unexpected keys {sorted(extra)}"
    missing = required - set(data)
    assert not missing, f"{kind} is missing keys {sorted(missing)}"
    return data


def _require_type(kind: str, name: str, value: object, expected: type) -> Any:
    # bool is an int subclass; an int field must not accept one, nor a bool field an int.
    ok = isinstance(value, expected) and (expected is bool or not isinstance(value, bool))
    assert ok, f"{kind}.{name} must be {expected.__name__}, got {type(value).__name__}"
    return value


@dataclass(frozen=True)
class ObservationCheck:
    """Canonical assertion metadata validated on the worker/file boundary; no domain values."""

    because: str
    fields: list[str]
    matched: bool
    path: str
    line: int

    def to_json(self, root: Path | None = None) -> dict[str, object]:
        """Serialize, making ``path`` relative to ``root`` when it lies beneath it."""
        return {
            "because": self.because,
            "fields": list(self.fields),
            "matched": self.matched,
            "path": _source_path(self.path, root),
            "line": self.line,
        }


def _source_path(path: str, root: Path | None) -> str:
    # Worker transport and report files normalize at the same serialization boundary.
    if root is None:
        return path
    assert isinstance(root, Path), "observation serialization context must be the pytest root"
    source = Path(path)
    return source.relative_to(root).as_posix() if source.is_relative_to(root) else source.as_posix()


def check_from_json(data: object) -> ObservationCheck:
    kind = "ObservationCheck"
    raw = _require_keys(kind, data, frozenset({"because", "fields", "matched", "path", "line"}))
    names = _require_type(kind, "fields", raw["fields"], list)
    for name in names:
        _require_type(kind, "fields[]", name, str)
    return ObservationCheck(
        because=_require_type(kind, "because", raw["because"], str),
        fields=list(names),
        matched=_require_type(kind, "matched", raw["matched"], bool),
        path=_require_type(kind, "path", raw["path"], str),
        line=_require_type(kind, "line", raw["line"], int),
    )


@dataclass
class ObservedTest:
    """One validated execution record; serves pytest workers, JSON files and the CLI."""

    kind: ObservationKind
    outcomes: dict[ObservationPhase, ObservationOutcome] = field(default_factory=dict)
    checks: list[ObservationCheck] = field(default_factory=list)

    @property
    def status(self) -> str:
        for status in ("failed", "xfail", "skipped", "xpass"):
            if status in self.outcomes.values():
                return status
        return "passed" if self.outcomes == dict.fromkeys(("setup", "call", "teardown"), "passed") else "incomplete"

    def to_json(self, root: Path | None = None) -> dict[str, object]:
        return {
            "kind": self.kind,
            "outcomes": dict(self.outcomes),
            "checks": [check.to_json(root) for check in self.checks],
        }


def observed_test_from_json(data: object) -> ObservedTest:
    kind = "ObservedTest"
    raw = _require_keys(kind, data, frozenset({"kind"}), frozenset({"outcomes", "checks"}))
    test_kind = raw["kind"]
    assert test_kind in _KINDS, f"{kind}.kind must be one of {sorted(_KINDS)}, got {test_kind!r}"
    outcomes = _require_type(kind, "outcomes", raw.get("outcomes", {}), dict)
    for phase, outcome in outcomes.items():
        assert phase in _PHASES, f"{kind}.outcomes key must be one of {sorted(_PHASES)}, got {phase!r}"
        assert outcome in _OUTCOMES, f"{kind}.outcomes value must be one of {sorted(_OUTCOMES)}, got {outcome!r}"
    checks = _require_type(kind, "checks", raw.get("checks", []), list)
    return ObservedTest(kind=test_kind, outcomes=dict(outcomes), checks=[check_from_json(check) for check in checks])


# Repeated node IDs (xdist --dist=each, reruns) own separate executions, not one overwritten outcome.
type ObservationReport = dict[str, list[ObservedTest]]


_recording: ContextVar[list[ObservationCheck] | None] = ContextVar("durable_observations", default=None)


@contextmanager
def record_observations() -> Iterator[list[ObservationCheck]]:
    checks: list[ObservationCheck] = []
    token = _recording.set(checks)
    try:
        yield checks
    finally:
        _recording.reset(token)


def _is_pydantic_model(value: object) -> bool:
    # Pydantic is optional: if it was never imported, no value can be one of its models.
    pydantic = sys.modules.get("pydantic")
    return pydantic is not None and isinstance(value, pydantic.BaseModel)


def assert_observation[T](actual: T, expected: T, *, because: str) -> None:
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
        elif _is_pydantic_model(expected):
            # Field(exclude=True) affects serialization, not equality. Never dump observation values.
            names = list(type(expected).model_fields)
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
