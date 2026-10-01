"""
The root-owned gap probes, exercised in both directions.

These probes exist so an adopter's ``detect``/``prove``/exemption evidence is a
binding rather than a hand-rolled assertion. That only pays off if the probes
themselves are pinned, which is what this module does — every one of them can
otherwise pass for the wrong reason and nobody would see it.
"""

import pytest
from sample_production import cache

from pytest_obligation.gap_probes import LossIsAbsorbedElsewhere
from pytest_obligation.host import Host, hosted

# Every test here binds an absorbing path the probe must recognise as production.
pytestmark = pytest.mark.usefixtures("production_host")


class _Effect:
    """A cache-shaped effect: present, absent, and re-derivable on read."""

    def __init__(self, *, rederives: bool = True) -> None:
        self.present: set[int] = set()
        self.rederives = rederives
        self._ids = iter(range(1, 100))

    def strand(self) -> int:
        """Publish the work without dispatching the effect."""
        return next(self._ids)

    def observe(self, identity: int) -> bool:
        return identity in self.present

    def absorb(self, identity: int) -> None:
        # The production read path the exemption names; a cache that does not
        # rederive on read only reads.
        if self.rederives:
            cache.rederive(self.present, identity)
        else:
            cache.read_only(self.present, identity)


def test_an_exemption_whose_absorbing_path_produces_the_effect_holds() -> None:
    effect = _Effect()
    LossIsAbsorbedElsewhere(strand=effect.strand, observe=effect.observe, absorb=effect.absorb)()


def test_an_exemption_whose_absorbing_path_does_nothing_is_refused() -> None:
    """The reason claims something re-establishes the effect. Make it."""
    effect = _Effect(rederives=False)
    with pytest.raises(AssertionError, match="losing this callback loses the work"):
        LossIsAbsorbedElsewhere(strand=effect.strand, observe=effect.observe, absorb=effect.absorb)()


def test_an_exemption_whose_effect_was_never_absent_is_refused() -> None:
    """
    The positive control.

    If the effect is present before the absorbing path runs, the probe cannot
    tell "the loss is absorbed" from "the dispatch was never suppressed" — and
    the second reading is the one that makes an exemption look sound.
    """
    effect = _Effect()

    def strand_without_suppressing() -> int:
        identity = effect.strand()
        effect.present.add(identity)
        return identity

    with pytest.raises(AssertionError, match="never actually suppressed"):
        LossIsAbsorbedElsewhere(strand=strand_without_suppressing, observe=effect.observe, absorb=effect.absorb)()


def test_a_test_authored_absorbing_path_is_refused() -> None:
    """
    An exemption is a claim about PRODUCTION absorbing the loss.

    A test-side absorber proves only that the test can produce the effect,
    which is never what the exemption's reason says.
    """
    effect = _Effect()

    def absorb_here(identity: int) -> None:
        effect.present.add(identity)

    with pytest.raises(AssertionError, match="absorb is test code that references no production"):
        LossIsAbsorbedElsewhere(strand=effect.strand, observe=effect.observe, absorb=absorb_here)()


def test_production_is_what_the_host_says_it_is() -> None:
    """The same absorber is test code under a host that names no production package."""
    effect = _Effect()
    with hosted(Host()), pytest.raises(AssertionError, match="absorb is test code that references no production"):
        LossIsAbsorbedElsewhere(strand=effect.strand, observe=effect.observe, absorb=effect.absorb)()
