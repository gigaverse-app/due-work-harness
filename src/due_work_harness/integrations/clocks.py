"""
The frozen clock hosts share: ``time-machine`` holding the application's clock at a moment.

Proofs that age work past a recovery delay (profile A's lifecycle states, RQ's
lease expiry) need the application to read a later time than the test's. Any
host whose application reads Python's clock can supply this one.
"""

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any


def time_machine_clock(moment: datetime) -> AbstractContextManager[Any]:
    """The application's clock held at ``moment``, not ticking, until the block ends."""
    import time_machine

    return time_machine.travel(moment, tick=False)


__all__ = ["time_machine_clock"]
