"""
Derived profile evidence shared by native and work-table suites.

Declarations remain canonical in contract.py. This report projects only coverage
metadata and pytest outcomes; it never executes bindings or serializes domain data.
"""

from typing import Literal

from pydantic import Field, computed_field

from pytest_obligation.models import HarnessModel, MutableHarnessModel

AssessmentState = Literal["claimed", "declined", "not applicable", "known gap", "not assessed"]


class Assessment(HarnessModel):
    """A declaration decision, independent of whether any selected test passed."""

    state: AssessmentState = Field(description="The adopter’s explicit decision; not a test verdict.")
    because: str | None = Field(default=None, description="Reason for an unclaimed guarantee or family.")


class CaseEvidence(MutableHarnessModel):
    """Coverage of one generated case; a pass requires successful setup, call and teardown."""

    family: str = Field(description="Binding or history family that generated this case.")
    collected: bool = Field(default=False, description="Pytest collected this expected case before selection filters.")
    selected: bool = Field(default=False, description="The case survived pytest selection filters.")
    outcomes: dict[str, list[str]] = Field(
        default_factory=dict,
        description="All observed outcomes by pytest phase, including retries and distributed runs.",
    )

    @computed_field
    @property
    def passed(self) -> bool:
        # A prior pass cannot cover an interrupted repeat on another worker or retry.
        phases = ("setup", "call", "teardown")
        return (
            self.collected
            and self.selected
            and len({len(self.outcomes.get(phase, ())) for phase in phases}) == 1
            and all(
                self.outcomes.get(phase) and all(result == "passed" for result in self.outcomes[phase])
                for phase in phases
            )
        )


class ProfileCoverage(MutableHarnessModel):
    """Named guarantee, independently assessed families, and evidence for its expected cases."""

    title: str = Field(description="Canonical profile name from the A–J catalog.")
    assessment: Assessment
    families: dict[str, Assessment] = Field(default_factory=dict, description="Independent family coverage decisions.")
    cases: dict[str, CaseEvidence] = Field(
        default_factory=dict, description="Every expected behavioral proof, including uncollected or unselected cases."
    )

    @computed_field
    @property
    def verified(self) -> bool:
        """No inferred whole-profile success from a filtered run, omitted family or known failure."""
        return (
            self.assessment.state == "claimed"
            and all(family.state in ("claimed", "not applicable") for family in self.families.values())
            and bool(self.cases)
            and all(case.passed for case in self.cases.values())
        )


class SuiteCoverage(MutableHarnessModel):
    """Coverage scoped to a collected suite; absent/unimported suites are never certified."""

    name: str = Field(description="Adopter contract name; the report key identifies its collected suite class.")
    profiles: dict[str, ProfileCoverage]
