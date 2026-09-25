from __future__ import annotations

from typing import Any, Iterable

from .extractors import SourceDocument, extract_differential, extract_vivado
from .schema import ArtifactRef, SchemaError
from .store import EvidenceStore


EXTRACTION_SCHEMA = "chipcontext.extraction.v1"


def _same_evidence_scope(refs: Iterable[ArtifactRef]) -> tuple[str, str]:
    values = list(refs)
    if not values:
        raise SchemaError("an extraction needs at least one input artifact")
    owners = {value.owner_ref for value in values}
    attempts = {value.attempt_id for value in values}
    if len(owners) != 1 or len(attempts) != 1:
        raise SchemaError("extraction inputs must belong to one candidate attempt")
    return next(iter(owners)), next(iter(attempts))


class ExtractionService:
    """Build immutable fact sidecars from registered raw tool artifacts."""

    def __init__(
        self,
        store: EvidenceStore,
        *,
        allowed_access: set[str] | None = None,
    ) -> None:
        self.store = store
        self.allowed_access = {"public"} if allowed_access is None else allowed_access

    def _document(self, ref_id: str) -> tuple[ArtifactRef, SourceDocument]:
        ref, data = self.store.verified_artifact_bytes(
            ref_id, allowed_access=self.allowed_access
        )
        return ref, SourceDocument(ref.ref_id, ref.sha256, data)

    def _publish(
        self,
        *,
        extractor: dict[str, Any],
        refs: list[ArtifactRef],
        parsed: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        owner_ref, attempt_id = _same_evidence_scope(refs)
        payload = {
            "owner_ref": owner_ref,
            "attempt_id": attempt_id,
            "extractor": extractor,
            "inputs": [
                {
                    "artifact_ref": ref.ref_id,
                    "artifact_sha256": ref.sha256,
                    "kind": ref.kind,
                }
                for ref in refs
            ],
            "config": config,
            "facts": parsed["facts"],
            "coverage": parsed["coverage"],
            "missing": parsed["missing"],
            "conflicts": parsed["conflicts"],
        }
        return self.store.write_record("extractions", EXTRACTION_SCHEMA, payload)

    def vivado(
        self,
        *,
        timing_summary_ref: str,
        utilization_ref: str,
        timing_paths_ref: str | None,
        period_ns: float,
        stage: str,
    ) -> dict[str, Any]:
        timing_ref, timing = self._document(timing_summary_ref)
        utilization_artifact, utilization = self._document(utilization_ref)
        refs = [timing_ref, utilization_artifact]
        paths = None
        if timing_paths_ref is not None:
            paths_artifact, paths = self._document(timing_paths_ref)
            refs.append(paths_artifact)
        parsed = extract_vivado(
            timing_summary=timing,
            utilization=utilization,
            timing_paths=paths,
            period_ns=period_ns,
            stage=stage,
        )
        return self._publish(
            extractor={
                "name": "vivado",
                "revision": parsed["extractor_revision"],
            },
            refs=refs,
            parsed=parsed,
            config={"clock_period_ns": period_ns, "stage": stage},
        )

    def differential(
        self,
        *,
        result_ref: str,
        stdout_ref: str | None,
    ) -> dict[str, Any]:
        result_artifact, result = self._document(result_ref)
        refs = [result_artifact]
        stdout = None
        if stdout_ref is not None:
            stdout_artifact, stdout = self._document(stdout_ref)
            refs.append(stdout_artifact)
        parsed = extract_differential(result=result, stdout=stdout)
        return self._publish(
            extractor={
                "name": "verilator_differential",
                "revision": parsed["extractor_revision"],
            },
            refs=refs,
            parsed=parsed,
            config={},
        )

    def worst_timing_path(self, extraction_ref: str) -> dict[str, Any]:
        """Return the first collected path with its exact drilldown location."""
        record = self.store.read_record("extractions", extraction_ref)
        for item in record.get("inputs", []):
            ref_id = item.get("artifact_ref") if isinstance(item, dict) else None
            if not isinstance(ref_id, str):
                raise SchemaError("extraction record has an invalid input reference")
            ref, _ = self.store.verified_artifact_bytes(
                ref_id, allowed_access=self.allowed_access
            )
            if ref.sha256 != item.get("artifact_sha256"):
                raise RuntimeError("extraction input hash no longer matches its record")
        if record.get("extractor", {}).get("name") != "vivado":
            raise SchemaError("timing-path lookup requires a Vivado extraction")
        paths = record.get("facts", {}).get("timing_paths")
        coverage = record.get("coverage", {}).get("timing_paths")
        if not isinstance(paths, list) or not paths:
            availability = (
                "parse_failed"
                if isinstance(coverage, dict)
                and coverage.get("parse_status") == "parse_failed"
                else "not_collected"
            )
            return {
                "availability": availability,
                "fact": None,
                "coverage": coverage,
                "missing": record.get("missing", []),
                "source_ref": extraction_ref,
            }
        first = paths[0]
        if first.get("rank") != 1:
            return {
                "availability": "inconclusive",
                "fact": None,
                "coverage": coverage,
                "missing": record.get("missing", []),
                "source_ref": extraction_ref,
            }
        return {
            "availability": first.get("availability", "available"),
            "fact": first,
            "coverage": coverage,
            "missing": record.get("missing", []),
            "source_ref": extraction_ref,
        }
