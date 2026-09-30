"""
Generated competing-event contracts, with optional exploration over the same runner.

Bind production commands and independent observations with the declarations below.
Attach them to DueWorkContract.in_flight / evidence_confluence .
Only explicit exploration imports Hypothesis; fixed histories and replay do not.
"""

from .bindings import (
    EvidenceArrival,
    EvidenceConfluence,
    EvidenceExpectation,
    EvidenceRetry,
    EvidenceSession,
    InFlightConvergence,
    InFlightSession,
    Intent,
)
from .engine.provider import AcceptedProviderRequest, ProviderControl
from .engine.runner import replay_history
from .model import Bounds, HistoryTrace, InterleavingFailure, KnownFailure
from .ports import PendingRequest, Transport

__all__ = [
    "AcceptedProviderRequest",
    "Bounds",
    "EvidenceArrival",
    "EvidenceConfluence",
    "EvidenceExpectation",
    "EvidenceRetry",
    "EvidenceSession",
    "HistoryTrace",
    "InFlightConvergence",
    "InFlightSession",
    "Intent",
    "InterleavingFailure",
    "KnownFailure",
    "PendingRequest",
    "ProviderControl",
    "Transport",
    "replay_history",
]
