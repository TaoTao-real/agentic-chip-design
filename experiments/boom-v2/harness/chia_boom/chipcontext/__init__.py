"""Deterministic evidence preparation for the BOOM harness.

This package is deliberately independent of CHIA, Ray, and model SDKs.  The
runtime bridge is a later work package; CC-01 only prepares and queries sealed
legacy evidence.
"""

from .schema import (
    ArtifactRef,
    CandidateRef,
    CheckRecord,
    Measurement,
    SchemaError,
    WorkingState,
)
from .service import ChipContextService, PreparationResult
from .store import EvidenceStore

__all__ = [
    "ArtifactRef",
    "CandidateRef",
    "CheckRecord",
    "ChipContextService",
    "EvidenceStore",
    "Measurement",
    "PreparationResult",
    "SchemaError",
    "WorkingState",
]
