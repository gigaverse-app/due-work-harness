"""The coverage check's result: every site, every disposition, and every problem found."""

from typing import Literal

from pydantic import Field

from due_work_harness.models import HarnessModel, MutableHarnessModel

#: The qualified-name suffix of a site outside any function.
MODULE_LEVEL = "<module>"


class Site(HarnessModel):
    """One handoff reference: its kind and where it is."""

    kind: str
    path: str
    line: int


class Disposition(HarnessModel):
    """How one production function's handoffs are accounted for."""

    how: Literal["covered", "exempt", "baseline", "bridge"]
    sites: int
    where: str


class CoverageReport(MutableHarnessModel):
    """The scan's result: sites by function, their dispositions, and every problem found."""

    kinds: list[str] = Field(default_factory=list)
    sites: dict[str, list[Site]] = Field(default_factory=dict)
    dispositions: dict[str, Disposition] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)

    @property
    def unaccounted(self) -> list[str]:
        return sorted(set(self.sites) - set(self.dispositions))

    def raise_for_problems(self) -> None:
        if self.problems:
            raise AssertionError("due-work coverage:\n  " + "\n  ".join(self.problems))
