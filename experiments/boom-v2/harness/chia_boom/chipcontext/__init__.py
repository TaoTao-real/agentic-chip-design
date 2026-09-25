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
from .extraction import ExtractionService
from .query import (
    ChipContextQueryService,
    EvidenceHandle,
    EvidenceResolver,
    QueryError,
    QueryScope,
    RegisteredStore,
    TrustedStoreRegistry,
)
from .query_cli import QueryAttemptFailure, QueryAttemptResult, run_query_attempt
from .store import EvidenceIntegrityError, EvidenceMeter, EvidenceStore

__all__ = [
    "ArtifactRef",
    "CandidateRef",
    "CheckRecord",
    "ChipContextService",
    "ChipContextQueryService",
    "EvidenceHandle",
    "EvidenceIntegrityError",
    "EvidenceMeter",
    "EvidenceResolver",
    "EvidenceStore",
    "ExtractionService",
    "Measurement",
    "PreparationResult",
    "QueryError",
    "QueryAttemptFailure",
    "QueryAttemptResult",
    "QueryScope",
    "RegisteredStore",
    "SchemaError",
    "WorkingState",
    "TrustedStoreRegistry",
    "run_query_attempt",
]
