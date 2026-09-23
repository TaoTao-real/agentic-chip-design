from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
import statistics
from dataclasses import dataclass
from typing import Any

from .artifacts import CandidateArtifact, EvaluationArtifact


ARMS = ("A", "B", "C", "D")
FORMAL_SEEDS = (42, 43, 44)

SYSTEM_PROMPT = """You optimize one real synthesizable BOOM Chisel module.
Return one conservative edit set that reduces the measured critical
path without changing architectural interfaces, queue sizes, widths, or
cycle-visible behavior. Never modify tests, constraints, reports, build
scripts, or scoring. Discover the limiting structure yourself from the
supplied source and, when present, unannotated raw tool reports. Return JSON
with exactly diagnosis, selected_hypothesis, and edits. diagnosis must be a
non-empty list of objects containing exactly evidence, source_region,
hypothesis, predicted_effect, and risk. selected_hypothesis is the zero-based
diagnosis entry implemented by edits. edits must be a non-empty list of objects
containing exactly old and new. Each old string must occur exactly once in the
supplied source; new is its complete replacement and must differ from old.
Together the edits must concretely implement the selected hypothesis while
preserving all invariants. Do not return the complete source or Markdown
fences. When prior candidate feedback is present, directly address the most
recent failure and produce source different from every prior candidate; a
repeated edit or source is invalid."""


@dataclass(frozen=True)
class RunKey:
    target: str
    arm: str
    seed: int

    @property
    def id(self) -> str:
        return f"{self.target}-{self.arm}-seed{self.seed}"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_config(config: dict[str, Any], *, formal: bool = False) -> None:
    required = {"version", "model", "search", "physical", "remote", "targets", "arms"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"missing config keys: {sorted(missing)}")
    if tuple(config["arms"]) != ARMS:
        raise ValueError("arms must be exactly A/B/C/D")
    if config["model"]["api_base"].rstrip("/") != "https://api.deepseek.com/v1":
        raise ValueError("formal campaign requires the official DeepSeek endpoint")
    if config["model"]["id"] != "deepseek-v4-pro":
        raise ValueError("formal campaign requires deepseek-v4-pro")
    if config["model"].get("thinking") != "disabled":
        raise ValueError("thinking must be disabled")
    if config["search"]["candidate_limit"] != 5:
        raise ValueError("candidate_limit must remain five")
    if formal and tuple(config["search"]["replicates"]) != FORMAL_SEEDS:
        raise ValueError("formal seeds must remain 42/43/44")
    experiment_kind = config.get("experiment_kind", "two-target-ablation")
    expected_targets = 1 if experiment_kind == "issueq-blind-reproduction" else 2
    if len(config["targets"]) != expected_targets:
        raise ValueError(
            f"{experiment_kind} requires exactly {expected_targets} target(s)"
        )
    unresolved = []
    for section in ("remote", "environment"):
        for key, value in config.get(section, {}).items():
            if isinstance(value, str) and "$" in value:
                unresolved.append(f"{section}.{key}")
    if unresolved:
        raise ValueError(
            "unresolved environment variables in config: " + ", ".join(unresolved)
        )
    for target in config["targets"].values():
        validate_unbiased_target(target)


def validate_unbiased_target(target: dict[str, Any]) -> None:
    allowed = {"id", "mutable_file", "invariants", "rtl_top", "rtl_files"}
    extra = set(target) - allowed
    if extra:
        raise ValueError(f"target contains non-protocol fields: {sorted(extra)}")
    text = "\n".join(target.get("invariants", [])).lower()
    forbidden = (
        "prefix", "selectfirstn", "priority", "balanced tree", "bottleneck",
        "critical-path family", "allocation mask", "branch recovery path",
    )
    leaked = [word for word in forbidden if word in text]
    if leaked:
        raise ValueError(f"target invariants leak design hints: {leaked}")


def validate_generic_episode_bundle(value: Any) -> None:
    """Reject target history from the formal arm-D process-memory bundle."""
    if isinstance(value, list):
        episodes = value
    elif isinstance(value, dict) and isinstance(value.get("episodes"), list):
        episodes = value["episodes"]
    elif isinstance(value, dict):
        episodes = [value]
    else:
        raise ValueError("design episode bundle must be an object or list")
    for index, episode in enumerate(episodes):
        if not isinstance(episode, dict):
            raise ValueError(f"design episode {index} is not an object")
        if episode.get("knowledge_class") != "cross-target-process-memory":
            raise ValueError(
                f"design episode {index} is not cross-target process memory"
            )
        policy = episode.get("access_policy", {})
        if policy.get("contains_target_specific_solution") is not False:
            raise ValueError(
                f"design episode {index} lacks an explicit no-target-solution policy"
            )


def build_schedule(config: dict[str, Any], *, preflight: bool = False) -> list[RunKey]:
    if preflight:
        return [
            RunKey(target, arm, 41)
            for target in sorted(config["targets"])
            for arm in ("B", "C")
        ]
    validate_config(config, formal=True)
    rng = random.Random(config["search"]["schedule_seed"])
    blocks: list[list[RunKey]] = []
    for target in sorted(config["targets"]):
        for seed in FORMAL_SEEDS:
            block = [RunKey(target, arm, seed) for arm in ARMS]
            rng.shuffle(block)
            blocks.append(block)
    order: list[RunKey] = []
    while blocks:
        for block in blocks:
            if block:
                order.append(block.pop())
        blocks = [block for block in blocks if block]
    return order


def build_user_prompt(
    *, arm: str, target: dict[str, Any], source: str, candidate_index: int,
    timing: str = "", feedback: str = "", episodes: str = "",
    iteration_digest: str = "",
) -> str:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    sections = [
        f"Target ID: {target['id']}",
        f"Mutable file: {target['mutable_file']}",
        f"Candidate index: {candidate_index}",
        "INVARIANTS\n" + "\n".join(f"- {x}" for x in target["invariants"]),
        "SOURCE\n" + source + "\nEND SOURCE",
    ]
    if arm in ("B", "C", "D"):
        sections.append("FROZEN BASELINE TIMING\n" + timing + "\nEND TIMING")
    if arm in ("C", "D"):
        sections.append(
            "PRIOR CANDIDATE FEEDBACK\n"
            + (feedback or "No prior candidate.")
            + "\nEND FEEDBACK"
        )
        if feedback and feedback != "No prior candidate.":
            sections.append(
                "ITERATION REQUIREMENT\n"
                "Use the unedited evidence above to repair the most recent failure. "
                "The resulting complete source must differ from every prior candidate. "
                "Do not repeat an earlier edit set.\n"
                + iteration_digest
                + "\nEND ITERATION REQUIREMENT"
            )
    if arm == "D":
        sections.append("RETRIEVED DESIGN EPISODES\n" + episodes + "\nEND EPISODES")
    sections.append(
        "Propose exactly one non-empty set of exact source edits. Preserve every "
        "invariant. No human bottleneck analysis is available."
    )
    prompt = "\n\n".join(sections)
    assert_information_policy(arm, prompt)
    return prompt


def build_iteration_digest(
    candidates: list[tuple[CandidateArtifact, EvaluationArtifact]],
) -> str:
    if not candidates:
        return ""
    candidate, evaluation = candidates[-1]
    raw = evaluation.raw_error or ""
    markers = (
        "error", "failed", "mismatch", "fatal", "exception", "not found",
        "type mismatch", "combinational cycle", "assertion", "timeout",
    )
    lines = []
    seen = set()
    for line in raw.splitlines():
        normalized = line.strip()
        if normalized and any(marker in normalized.lower() for marker in markers):
            if normalized not in seen:
                seen.add(normalized)
                lines.append(normalized)
        if len(lines) >= 24:
            break
    if not lines and raw:
        lines = [raw[-4000:]]
    return "MACHINE FAILURE INDEX\n" + json.dumps(
        {
            "candidate_id": candidate.id,
            "forbidden_repeat_diff_sha256": hashlib.sha256(
                candidate.diff.encode()
            ).hexdigest(),
            "stage": evaluation.stage,
            "status": evaluation.status,
            "failure_class": evaluation.failure_class,
            "exact_error_excerpt": lines,
        },
        indent=2,
        sort_keys=True,
    )


def build_iteration_digest_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    row = rows[-1]
    candidate = row.get("candidate") or {}
    evaluation = row.get("evaluation") or {}
    raw = evaluation.get("raw_error") or ""
    markers = (
        "error", "failed", "mismatch", "fatal", "exception", "not found",
        "type mismatch", "combinational cycle", "assertion", "timeout",
        "occurs", "repeat",
    )
    lines = []
    seen = set()
    for line in raw.splitlines():
        normalized = line.strip()
        if normalized and any(marker in normalized.lower() for marker in markers):
            if normalized not in seen:
                seen.add(normalized)
                lines.append(normalized)
        if len(lines) >= 24:
            break
    if not lines and raw:
        lines = [raw[-4000:]]
    proposal = row.get("model_proposal") or {}
    diff_or_proposal = candidate.get("diff") or json.dumps(
        proposal, sort_keys=True, separators=(",", ":")
    )
    return "MACHINE FAILURE INDEX\n" + json.dumps(
        {
            "candidate_id": candidate.get("id") or evaluation.get("candidate_id"),
            "forbidden_repeat_diff_or_proposal_sha256": hashlib.sha256(
                diff_or_proposal.encode()
            ).hexdigest(),
            "stage": evaluation.get("stage"),
            "status": evaluation.get("status"),
            "failure_class": evaluation.get("failure_class"),
            "exact_error_excerpt": lines,
            # This is raw model/tool evidence, not a diagnosis.  Keeping it
            # at the end of a long prompt makes the repair requirement
            # auditable and prevents the context position from hiding the
            # failed change behind full EDA logs.
            "exact_failed_diff_or_proposal": diff_or_proposal,
        },
        indent=2,
        sort_keys=True,
    )


def assert_information_policy(arm: str, prompt: str) -> None:
    markers = {
        "timing": "FROZEN BASELINE TIMING",
        "feedback": "PRIOR CANDIDATE FEEDBACK",
        "episodes": "RETRIEVED DESIGN EPISODES",
    }
    expected = {
        "A": set(),
        "B": {"timing"},
        "C": {"timing", "feedback"},
        "D": {"timing", "feedback", "episodes"},
    }[arm]
    for name, marker in markers.items():
        if (marker in prompt) != (name in expected):
            raise AssertionError(f"{arm} information policy violated for {name}")


def validate_proposal(value: dict[str, Any]) -> None:
    if set(value) != {"diagnosis", "selected_hypothesis", "edits"}:
        raise ValueError("unexpected proposal schema")
    diagnosis = value["diagnosis"]
    if not isinstance(diagnosis, list) or not diagnosis:
        raise ValueError("diagnosis must be a non-empty list")
    fields = {"evidence", "source_region", "hypothesis", "predicted_effect", "risk"}
    if any(not isinstance(item, dict) or set(item) != fields for item in diagnosis):
        raise ValueError("unexpected diagnosis entry schema")
    selected = value["selected_hypothesis"]
    if not isinstance(selected, int) or not 0 <= selected < len(diagnosis):
        raise ValueError("selected_hypothesis out of range")
    edits = value["edits"]
    if not isinstance(edits, list) or not edits or len(edits) > 16:
        raise ValueError("edits must contain between one and sixteen replacements")
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
            raise ValueError("each edit must contain exactly old and new")
        old, new = edit["old"], edit["new"]
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ValueError("edit old/new values must be strings and old must be non-empty")
        if old == new or "```" in old or "```" in new:
            raise ValueError("each edit must make an unfenced source change")


def apply_exact_edits(source: str, edits: list[dict[str, str]]) -> str:
    result = source
    for index, edit in enumerate(edits, start=1):
        old, new = edit["old"], edit["new"]
        occurrences = result.count(old)
        if occurrences != 1:
            raise ValueError(
                f"edit {index} old text occurs {occurrences} times; expected exactly once"
            )
        result = result.replace(old, new, 1)
    if result == source:
        raise ValueError("candidate_source_unchanged")
    return result


def duplicate_candidate_id(
    source: str,
    candidates: list[tuple[CandidateArtifact, EvaluationArtifact]],
) -> str | None:
    return next(
        (candidate.id for candidate, _ in candidates if candidate.source == source),
        None,
    )


def make_diff(parent_source: str, candidate_source: str, mutable_file: str) -> str:
    return "".join(
        difflib.unified_diff(
            parent_source.splitlines(True), candidate_source.splitlines(True),
            fromfile="a/" + mutable_file, tofile="b/" + mutable_file,
        )
    )


def evaluation_feedback(
    candidates: list[tuple[CandidateArtifact, EvaluationArtifact]],
) -> str:
    if not candidates:
        return "No prior candidate."
    rows = []
    for candidate, evaluation in candidates:
        rows.append(
            {
                "candidate_id": candidate.id,
                "parent_id": candidate.parent_id,
                "candidate_diff": candidate.diff,
                "agent_diagnosis": candidate.diagnosis,
                "evaluation": evaluation.to_dict(),
            }
        )
    return json.dumps(rows, indent=2, sort_keys=True)


def evaluation_feedback_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No prior candidate."
    feedback = []
    for row in rows:
        candidate = row.get("candidate") or {}
        evaluation = row.get("evaluation") or {}
        proposal = row.get("model_proposal") or {}
        feedback.append(
            {
                "candidate_id": candidate.get("id") or evaluation.get("candidate_id"),
                "parent_id": candidate.get("parent_id"),
                "candidate_diff": candidate.get("diff", ""),
                "agent_diagnosis": candidate.get("diagnosis")
                    or proposal.get("diagnosis", []),
                "proposed_edits": proposal.get("edits", []),
                "evaluation": evaluation,
            }
        )
    return json.dumps(feedback, indent=2, sort_keys=True)


def candidate_valid(evaluation: EvaluationArtifact, baseline: dict[str, Any]) -> bool:
    if not (evaluation.build_ok and evaluation.lint_ok and evaluation.correctness_ok):
        return False
    ppa = evaluation.post_synth or {}
    return (
        isinstance(ppa.get("critical_delay_ns"), (int, float))
        and ppa.get("slice_luts", float("inf"))
        <= baseline["slice_luts"] * baseline.get("maximum_lut_ratio", 1.05)
    )


def ppa_score(evaluation: EvaluationArtifact) -> tuple[float, float]:
    ppa = evaluation.post_synth or {}
    return float(ppa.get("critical_delay_ns", float("inf"))), float(
        ppa.get("slice_luts", float("inf"))
    )


def choose_parent(
    rows: list[tuple[CandidateArtifact, EvaluationArtifact]],
    baseline: dict[str, Any],
) -> tuple[CandidateArtifact, EvaluationArtifact] | None:
    eligible = [row for row in rows if row[1].promotable]
    return min(eligible, key=lambda row: ppa_score(row[1])) if eligible else None


def update_validity(
    evaluation: EvaluationArtifact,
    baseline: dict[str, Any],
    parent: EvaluationArtifact | None = None,
) -> EvaluationArtifact:
    evaluation.candidate_valid = candidate_valid(evaluation, baseline)
    threshold = (
        ppa_score(parent)[0]
        if parent is not None and parent.candidate_valid
        else float(baseline["critical_delay_ns"])
    )
    evaluation.promotable = (
        evaluation.candidate_valid
        and ppa_score(evaluation)[0] < threshold
    )
    if evaluation.final_valid and evaluation.post_route:
        evaluation.valid_improvement = (
            evaluation.post_route["critical_delay_ns"]
            < baseline["post_route_critical_delay_ns"]
        )
    return evaluation


def classify_failure(message: str, *, returncode: int | None = None) -> tuple[str, bool]:
    text = message.lower()
    infra_patterns = (
        "connection timed out", "connection reset", "broken pipe", "no route to host",
        "rayactorerror", "node died", "worker crashed", "disk quota", "no space left",
    )
    if any(pattern in text for pattern in infra_patterns):
        return "infrastructure", True
    if "candidate_source_unchanged" in text:
        return "candidate_noop", False
    if "timeout" in text and "vivado" not in text and "elaboration" not in text:
        return "infrastructure_timeout", True
    if "stack overflow" in text or "stackoverflow" in text or "recursion" in text:
        return "candidate_elaboration_recursion", False
    if returncode is not None and returncode < 0:
        return "infrastructure_process_killed", True
    return "candidate_tool_failure", False


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, Any] = {}
    for arm in ARMS:
        subset = [row for row in rows if row["arm"] == arm]
        delays = [row["best_post_route_delay_ns"] for row in subset if row.get("best_post_route_delay_ns") is not None]
        tokens = [row["provider_tokens"] for row in subset if row.get("provider_tokens") is not None]
        times = [row["active_seconds"] for row in subset]
        by_arm[arm] = {
            "runs": len(subset),
            "valid_improved_runs": sum(bool(row.get("valid_improvement")) for row in subset),
            "valid_candidate_rate": (
                sum(row.get("valid_candidates", 0) for row in subset)
                / max(1, sum(row.get("candidate_count", 0) for row in subset))
            ),
            "median_delay_ns": statistics.median(delays) if delays else None,
            "delay_iqr_ns": [percentile(delays, 0.25), percentile(delays, 0.75)] if delays else None,
            "median_provider_tokens": statistics.median(tokens) if tokens else None,
            "median_active_seconds": statistics.median(times) if times else None,
        }
    return by_arm


def audit_prompt_for_answer_leak(
    prompt: str, forbidden_terms: list[str] | tuple[str, ...] = (),
) -> list[str]:
    # Structural words can naturally occur in source, raw EDA output, and raw
    # candidate feedback. Remove those machine-originated regions and audit all
    # remaining human-authored prose, including Design Episodes.
    prose = prompt
    for begin, end in (
        ("SOURCE\n", "\nEND SOURCE"),
        ("FROZEN BASELINE TIMING\n", "\nEND TIMING"),
        ("PRIOR CANDIDATE FEEDBACK\n", "\nEND FEEDBACK"),
        ("MACHINE FAILURE INDEX\n", "\nEND ITERATION REQUIREMENT"),
    ):
        prose = re.sub(re.escape(begin) + r".*?" + re.escape(end), "", prose, flags=re.S)
    prose = prose.lower()
    return [term for term in forbidden_terms if term.lower() in prose]


def target_from_run_id(run_id: str) -> str:
    match = re.match(r"(.+)-[ABCD]-seed\d+$", run_id)
    if not match:
        raise ValueError(f"invalid run id: {run_id}")
    return match.group(1)
