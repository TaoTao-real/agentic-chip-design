from __future__ import annotations

import contextlib
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from .optimization_trace import CostSpan, signed_record


TIMELINE_SCHEMA = "chia-boom.validation-timeline.v1"


@dataclass
class Timeline:
    run_id: str
    spans: list[CostSpan] = field(default_factory=list)

    @contextlib.contextmanager
    def measure(
        self,
        stage: str,
        *,
        span_id: str,
        parent_span_id: str | None = None,
        candidate_id: str | None = None,
        queue_time_ns: int | None = None,
        leaf: bool = True,
        provenance: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        started = time.monotonic_ns()
        try:
            yield
        finally:
            wall = time.monotonic_ns() - started
            self.spans.append(CostSpan(
                span_id=span_id,
                stage=stage,
                wall_time_ns=wall,
                active_time_ns=wall,
                queue_time_ns=queue_time_ns,
                parent_span_id=parent_span_id,
                candidate_id=candidate_id,
                leaf=leaf,
                provenance=provenance or {},
            ))

    def add_unknown(
        self,
        stage: str,
        *,
        span_id: str,
        reason: str,
        parent_span_id: str | None = None,
        candidate_id: str | None = None,
        leaf: bool = True,
    ) -> None:
        self.spans.append(CostSpan(
            span_id=span_id,
            stage=stage,
            wall_time_ns=None,
            active_time_ns=None,
            queue_time_ns=None,
            parent_span_id=parent_span_id,
            candidate_id=candidate_id,
            leaf=leaf,
            unavailable_reason=reason,
        ))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": TIMELINE_SCHEMA,
            "run_id": self.run_id,
            "spans": [span.to_dict() for span in self.spans],
            "summary": summarize_spans(self.spans),
        }
        return signed_record(payload)


def _sum_known(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def summarize_spans(spans: list[CostSpan]) -> dict[str, Any]:
    """Sum leaf active spans only; parent envelopes never double count work."""

    leaves = [span for span in spans if span.leaf]
    validation = [
        span for span in leaves
        if span.stage in {
            "materialize", "chisel_elaboration", "interface_check", "differential",
            "vivado_post_synth", "trace_build", "feedback_prepare", "feedback_ready",
            "clean_rebuild", "differential_seed", "place_route", "megaboom_regression",
            "final_record",
            "infrastructure_retry",
        }
    ]
    vivado = [
        span for span in leaves
        if span.stage in {"vivado_post_synth", "place_route"}
    ]
    model = [
        span for span in leaves
        if span.stage in {"model_api", "tool_inspection", "edit", "controller_wait", "scheduler_queue"}
    ]
    return {
        "leaf_span_count": len(leaves),
        "candidate_validation_active_ns": _sum_known(
            [span.active_time_ns for span in validation]
        ),
        "vivado_active_ns": _sum_known([span.active_time_ns for span in vivado]),
        "model_search_active_ns": _sum_known([span.active_time_ns for span in model]),
        "unknown_active_span_count": sum(
            span.active_time_ns is None for span in leaves
        ),
    }


def timeline_from_evaluation(
    run_id: str,
    candidate_id: str,
    evaluation: dict[str, Any],
) -> Timeline:
    timeline = Timeline(run_id=run_id)
    total_seconds = evaluation.get("active_seconds")
    total_ns = (
        int(float(total_seconds) * 1_000_000_000)
        if isinstance(total_seconds, (int, float)) else None
    )
    timeline.spans.append(CostSpan(
        span_id=f"{candidate_id}-candidate-validation-total",
        stage="candidate_validation_total",
        wall_time_ns=total_ns,
        active_time_ns=total_ns,
        queue_time_ns=None,
        candidate_id=candidate_id,
        leaf=False,
        unavailable_reason=(None if total_ns is not None else "not_measured"),
        queue_unavailable_reason="ray_queue_not_separately_measured",
    ))
    timeline.add_unknown(
        "scheduler_queue",
        span_id=f"{candidate_id}-scheduler-queue",
        reason="ray_queue_not_separately_measured",
        candidate_id=candidate_id,
    )
    stage_map = {
        "materialize": "materialize",
        "elaboration": "chisel_elaboration",
        "interface": "interface_check",
        "correctness": "differential",
        "differential": "differential",
        "post_synth": "vivado_post_synth",
        "synthesis": "vivado_post_synth",
        "post_route": "place_route",
        "route": "place_route",
        "regression": "megaboom_regression",
    }
    for index, row in enumerate(evaluation.get("stages", []), start=1):
        if not isinstance(row, dict):
            continue
        raw_stage = str(row.get("stage", "unknown"))
        stage = stage_map.get(raw_stage, raw_stage)
        active = row.get("active_seconds")
        active_ns = (
            int(float(active) * 1_000_000_000)
            if isinstance(active, (int, float))
            else None
        )
        if raw_stage == "elaboration":
            timeline.add_unknown(
                "materialize",
                span_id=f"{candidate_id}-stage-{index:02d}-materialize",
                reason="legacy_elaboration_stage_did_not_separate_materialization",
                candidate_id=candidate_id,
            )
        if raw_stage == "correctness":
            timing = (evaluation.get("differential") or {}).get("stage_timing_ns")
            if isinstance(timing, dict):
                interface_ns = timing.get("interface_check")
                other_values = [
                    timing.get("testbench_prepare"), timing.get("verilator_build"),
                    timing.get("differential_run"),
                ]
                if isinstance(interface_ns, int):
                    timeline.spans.append(CostSpan(
                        span_id=f"{candidate_id}-stage-{index:02d}-interface",
                        stage="interface_check", wall_time_ns=interface_ns,
                        active_time_ns=interface_ns, queue_time_ns=None,
                        candidate_id=candidate_id,
                        provenance={"evaluation_stage_index": index, "raw_stage": raw_stage},
                    ))
                if all(value is None or isinstance(value, int) for value in other_values):
                    known = [value for value in other_values if isinstance(value, int)]
                    differential_ns = sum(known) if known else None
                    timeline.spans.append(CostSpan(
                        span_id=f"{candidate_id}-stage-{index:02d}-differential",
                        stage="differential", wall_time_ns=differential_ns,
                        active_time_ns=differential_ns, queue_time_ns=None,
                        candidate_id=candidate_id,
                        unavailable_reason=(None if differential_ns is not None else "not_measured"),
                        provenance={"evaluation_stage_index": index, "raw_stage": raw_stage},
                    ))
                    continue
        timeline.spans.append(CostSpan(
            span_id=f"{candidate_id}-stage-{index:02d}",
            stage=stage,
            wall_time_ns=active_ns,
            active_time_ns=active_ns,
            queue_time_ns=None,
            candidate_id=candidate_id,
            unavailable_reason=None if active_ns is not None else "not_measured",
            provenance={"evaluation_stage_index": index, "raw_stage": raw_stage},
        ))
    return timeline


def merge_timelines(run_id: str, *timelines: Timeline) -> Timeline:
    merged = Timeline(run_id=run_id)
    seen: set[str] = set()
    for timeline in timelines:
        for span in timeline.spans:
            if span.span_id in seen:
                raise ValueError(f"duplicate validation span: {span.span_id}")
            seen.add(span.span_id)
            merged.spans.append(span)
    return merged


def finalization_timeline(
    run_id: str,
    candidate_id: str,
    evaluation: dict[str, Any],
) -> Timeline:
    timeline = Timeline(run_id=run_id)
    total_seconds = evaluation.get("active_seconds")
    total_ns = (
        int(float(total_seconds) * 1_000_000_000)
        if isinstance(total_seconds, (int, float)) else None
    )
    timeline.spans.append(CostSpan(
        span_id=f"{candidate_id}-finalization-total",
        stage="finalization_total",
        wall_time_ns=total_ns,
        active_time_ns=total_ns,
        queue_time_ns=None,
        candidate_id=candidate_id,
        leaf=False,
        unavailable_reason=(None if total_ns is not None else "not_measured"),
        queue_unavailable_reason="ray_queue_not_separately_measured",
    ))
    differential_seed = iter((42, 43, 44))
    for index, row in enumerate(evaluation.get("stages", []), start=1):
        if not isinstance(row, dict):
            continue
        raw_stage = str(row.get("stage", "unknown"))
        active = row.get("active_seconds")
        active_ns = (
            int(float(active) * 1_000_000_000)
            if isinstance(active, (int, float)) else None
        )
        if raw_stage == "elaboration":
            stage, provenance = "clean_rebuild", {"raw_stage": raw_stage}
        elif raw_stage == "correctness":
            seed = next(differential_seed, None)
            stage, provenance = "differential_seed", {
                "raw_stage": raw_stage, "seed": seed,
            }
        elif raw_stage == "route":
            stage, provenance = "place_route", {"raw_stage": raw_stage}
        elif raw_stage == "regression":
            stage, provenance = "megaboom_regression", {"raw_stage": raw_stage}
        else:
            stage, provenance = raw_stage, {"raw_stage": raw_stage}
        timeline.spans.append(CostSpan(
            span_id=f"{candidate_id}-final-{index:02d}",
            stage=stage,
            wall_time_ns=active_ns,
            active_time_ns=active_ns,
            queue_time_ns=None,
            candidate_id=candidate_id,
            unavailable_reason=None if active_ns is not None else "not_measured",
            queue_unavailable_reason="ray_queue_not_separately_measured",
            provenance=provenance,
        ))
    timeline.spans.append(CostSpan(
        span_id=f"{candidate_id}-final-record",
        stage="final_record",
        wall_time_ns=None,
        active_time_ns=None,
        queue_time_ns=None,
        candidate_id=candidate_id,
        unavailable_reason="legacy_finalizer_did_not_time_record_publication",
        queue_unavailable_reason="not_applicable",
    ))
    return timeline
