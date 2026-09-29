"""
The base models every structured value in the harness is built from.

The harness uses Pydantic models, not dataclasses, for its declarations and
results: fields are validated when a declaration is built (a binding that is not
callable fails at construction, not deep inside a proof), unknown keyword
arguments are refused (a misspelled binding is an error, not a silently ignored
extra), and copies are explicit (``model.model_copy(update={...})``).

* :class:`HarnessModel` — immutable declarations and results: adapters, contracts,
  dispositions, observations. The default. ``model_copy(update=...)`` builds
  the copy as a new value, so a changed declaration passes the same design
  checks as the original and a misspelled field is refused.
* :class:`MutableHarnessModel` — state a proof accumulates while it runs.

Both allow arbitrary types, because harness declarations hold callables, protocol
implementations and framework objects that Pydantic cannot validate structurally.
"""

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict


class HarnessModel(BaseModel):
    """An immutable, validated harness value that refuses unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """
        A copy, validated like a new value when ``update`` changes it.

        Pydantic's own copy skips validation and ``model_post_init``, which is
        where declarations check their design, and accepts any key, so a
        changed contract could bypass its checks and a misspelled field would
        be silently ignored.
        """
        copied = super().model_copy(deep=deep)
        if not update:
            return copied
        return self.model_validate({**{name: getattr(copied, name) for name in type(self).model_fields}, **update})


class DueWorkContractDesignError(Exception):
    """
    The contract's declaration is incomplete or contradictory.

    Raised at construction — import/collection time — so a missing disposition
    or an unbindable claim fails the whole module loudly before any behavioral
    proof runs, rather than surfacing as a confusing runtime assertion. Defined
    here, beside the models whose ``model_post_init`` raises it; an ordinary
    ``Exception``, so Pydantic lets it reach the caller as itself.
    """


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


#: The default of a positional constructor argument that may also be passed by name.
#: Typed ``Any`` so the parameter keeps its real annotation.
MISSING: Any = _Missing()


def with_positional(data: dict[str, Any], **positional: Any) -> dict[str, Any]:
    """
    Keyword data for a model whose leading fields may be passed positionally.

    ``DueWorkSource(fn)`` reads better than ``DueWorkSource(callable=fn)``, but
    Pydantic validates (and ``model_copy(update=...)`` rebuilds) through
    ``__init__(**fields)``, so the positional parameter must accept its field
    by name too. Each such ``__init__`` defaults its positional parameters to
    :data:`MISSING` and merges them here.
    """
    for name, value in positional.items():
        if value is MISSING:
            continue
        if name in data:
            raise TypeError(f"got multiple values for argument {name!r}")
        data[name] = value
    return data


class MutableHarnessModel(BaseModel):
    """Validated state a proof updates while it runs; refuses unknown fields."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
