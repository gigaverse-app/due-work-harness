"""
A stand-in production package for the harness's binding self-tests.

The binding tripwires in :mod:`pytest_obligation.binding` ask whether a test
adapter reaches code in one of the host's ``production_packages``, and treat any
path with a ``tests`` directory as test code. Self-tests that need a
"production" callable to point at configure
``Host(production_packages=frozenset({"sample_production"}))`` and bind the
small modules here. Nothing in this package is a reference implementation of a
profile; it exists only to be recognised as production.
"""
