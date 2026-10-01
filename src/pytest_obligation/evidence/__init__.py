"""
Observation evidence: which product checks a proof actually executed, recorded as data.

Generated and handwritten tests make their product assertions through
``observation.assert_observation``, which records each equality check by name
and source location. Values never leave the process, since they may hold
tokens. The collection plugin can report the records, so a reviewer can compare
what two tests really check before deleting one as redundant. A record says a
check ran; it is not coverage, and not proof that two tests are equivalent.

* ``observation`` — the check record and ``assert_observation``.
* ``observation_report`` — renders a run's recorded checks for comparison.
"""
