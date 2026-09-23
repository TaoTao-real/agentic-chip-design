from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class CandidateArtifact:
    campaign_id: str
    target: str
    arm: str
    seed: int
    index: int
    parent_id: str | None
    source: str
    diff: str
    diagnosis: list[dict[str, str]]
    selected_hypothesis: int
    visible_feedback: str
    request_sha256: str
    response_sha256: str
    usage: dict[str, Any] | None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    model_elapsed_seconds: float = 0.0
    provider_model: str | None = None
    baseline_diff: str = ""
    parent_source_sha256: str | None = None
    visible_knowledge_sha256: str | None = None
    visible_knowledge_episode_ids: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return (
            f"{self.target}-{self.arm}-seed{self.seed}"
            f"-candidate-{self.index:02d}"
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {
            "id": self.id,
            "source_sha256": sha256_text(self.source),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CandidateArtifact":
        fields = {item.name for item in dataclasses.fields(cls)}
        return cls(**{key: val for key, val in value.items() if key in fields})


@dataclass
class StageResult:
    stage: str
    success: bool
    status: str
    failure_class: str | None = None
    retryable: bool = False
    message: str = ""
    stdout_path: str | None = None
    stderr_path: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    active_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StageResult":
        return cls(**value)


@dataclass
class EvaluationArtifact:
    candidate_id: str
    status: str = "candidate_invalid"
    stage: str = "materialize"
    failure_class: str | None = None
    retryable: bool = False
    raw_error: str = ""
    build_ok: bool = False
    lint_ok: bool = False
    interface_ok: bool = False
    correctness_ok: bool = False
    candidate_valid: bool = False
    promotable: bool = False
    final_valid: bool = False
    valid_improvement: bool = False
    post_synth: dict[str, Any] | None = None
    post_route: dict[str, Any] | None = None
    differential: dict[str, Any] | None = None
    regression: dict[str, Any] | None = None
    candidate_source_path: str | None = None
    candidate_diff_path: str | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)
    active_seconds: float = 0.0

    def append_stage(self, stage: StageResult) -> None:
        self.stages.append(stage.to_dict())
        self.stage = stage.stage
        self.active_seconds += stage.active_seconds
        if not stage.success:
            self.status = stage.status
            self.failure_class = stage.failure_class
            self.retryable = stage.retryable
            self.raw_error = stage.message

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvaluationArtifact":
        return cls(**value)


def dump_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = value.to_dict() if hasattr(value, "to_dict") else value
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(target)


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
