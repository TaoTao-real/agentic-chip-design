from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MEMORY_MODES = ("none", "generic", "target")
GENERIC_CLASS = "cross-target-process-memory"
TARGET_CLASS = "target-specific-solution"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9_+-]{2,}", value.lower())
        if token not in {"the", "and", "for", "with", "from"}
    }


@dataclass(frozen=True)
class KnowledgeResult:
    payload: dict[str, Any]
    audit: dict[str, Any]


class KnowledgeStore:
    """Small, deterministic, access-controlled Design Episode store.

    The index is intentionally shallow.  Episode bodies enter the model context
    only after an explicit ``retrieve`` call, so we can measure both retrieval
    decisions and returned context volume.
    """

    def __init__(self, mode: str, root: Path | None = None):
        if mode not in MEMORY_MODES:
            raise ValueError(f"unknown memory mode: {mode}")
        self.mode = mode
        bundled_root = Path(__file__).resolve().parent / "design_episodes"
        self.root = root or bundled_root
        roots = [bundled_root]
        if root is not None and root.resolve() != bundled_root.resolve():
            roots.append(root)
        # The public package contains process memory only.  A target-memory
        # experiment must mount a separate private directory explicitly; this
        # keeps known target answers out of blind runs and out of this repo.
        self._episodes: dict[str, dict[str, Any]] = {}
        self._paths: dict[str, Path] = {}
        paths = (
            [path for item in roots for path in sorted(item.glob("*.json"))]
            if mode != "none" else []
        )
        for path in paths:
            episode = json.loads(path.read_text())
            if (
                episode.get("usage_class") == "analysis_only"
                or episode.get("eligible_for_agent_context") is False
                or episode.get("eligible_for_knowledge_store") is False
            ):
                raise ValueError(
                    f"analysis-only evidence cannot enter KnowledgeStore: {path.name}"
                )
            knowledge_class = episode.get("knowledge_class")
            if knowledge_class == GENERIC_CLASS:
                pass
            elif knowledge_class == TARGET_CLASS and mode == "target":
                pass
            else:
                continue
            episode_id = str(episode["episode_id"])
            if episode_id in self._episodes:
                raise ValueError(f"duplicate episode id: {episode_id}")
            self._episodes[episode_id] = episode
            self._paths[episode_id] = path

    @property
    def enabled(self) -> bool:
        return bool(self._episodes)

    def manifest(self) -> dict[str, Any]:
        return {
            "memory_mode": self.mode,
            "episodes": [
                {
                    "episode_id": episode_id,
                    "knowledge_class": self._episodes[episode_id].get("knowledge_class"),
                    "source_path": self._paths[episode_id].name,
                    "source_sha256": hashlib.sha256(self._paths[episode_id].read_bytes()).hexdigest(),
                }
                for episode_id in sorted(self._episodes)
            ],
        }

    def _keys(self, episode: dict[str, Any]) -> list[str]:
        values = list(episode.get("retrieval_keys", []))
        values.extend(episode.get("applicability", {}).get("positive_retrieval_keys", []))
        if not values:
            values.extend(item.get("observation", "") for item in episode.get("reusable_observations", []))
        return [str(value) for value in values]

    def search(self, query: str, max_results: int = 4) -> KnowledgeResult:
        query_tokens = _tokens(query)
        rows: list[tuple[int, str, dict[str, Any]]] = []
        for episode_id, episode in self._episodes.items():
            keys = self._keys(episode)
            haystack = " ".join([episode_id, episode.get("knowledge_class", ""), *keys])
            score = len(query_tokens & _tokens(haystack))
            if not query_tokens:
                score = 0
            rows.append((score, episode_id, episode))
        rows.sort(key=lambda row: (-row[0], row[1]))
        matches = []
        for score, episode_id, episode in rows[: max(1, min(max_results, 8))]:
            matches.append({
                "episode_id": episode_id,
                "knowledge_class": episode.get("knowledge_class"),
                "score": score,
                "retrieval_keys": self._keys(episode),
                "access_policy": episode.get("access_policy"),
                "available_sections": [
                    key for key in episode
                    if key not in {"schema_version", "episode_id", "knowledge_class", "access_policy"}
                ],
            })
        payload = {"query": query, "memory_mode": self.mode, "matches": matches}
        return KnowledgeResult(payload=payload, audit={
            "operation": "search", "query": query, "matches": [row["episode_id"] for row in matches],
            "response_chars": len(_canonical(payload)), "response_sha256": _sha(payload),
        })

    def retrieve(self, episode_id: str, sections: list[str]) -> KnowledgeResult:
        if episode_id not in self._episodes:
            raise ValueError("episode is unavailable in this memory mode")
        episode = self._episodes[episode_id]
        if not sections:
            raise ValueError("at least one section must be requested")
        forbidden = {"schema_version", "episode_id", "knowledge_class", "access_policy"}
        unique_sections = list(dict.fromkeys(str(item) for item in sections))
        if len(unique_sections) > 6:
            raise ValueError("retrieve at most six sections per call")
        missing = [key for key in unique_sections if key not in episode or key in forbidden]
        if missing:
            raise ValueError(f"unavailable sections: {missing}")
        selected = {key: episode[key] for key in unique_sections}
        payload = {
            "episode_id": episode_id,
            "knowledge_class": episode.get("knowledge_class"),
            "access_policy": episode.get("access_policy"),
            "sections": selected,
            "source_sha256": hashlib.sha256(self._paths[episode_id].read_bytes()).hexdigest(),
        }
        encoded = _canonical(payload)
        if len(encoded) > 16_000:
            raise ValueError("requested knowledge exceeds the 16000-character retrieval limit")
        return KnowledgeResult(payload=payload, audit={
            "operation": "retrieve", "episode_id": episode_id,
            "knowledge_class": episode.get("knowledge_class"),
            "sections": unique_sections, "response_chars": len(encoded),
            "response_sha256": _sha(payload),
        })


MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Search the access-controlled Design Episode index. Returns compact metadata only; "
                "use retrieve_knowledge to request relevant sections."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query", "max_results"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_knowledge",
            "description": (
                "Retrieve selected sections of one indexed Design Episode. Treat retrieved knowledge "
                "as a hypothesis and validate it with the normal correctness and EDA tools."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "episode_id": {"type": "string"},
                    "sections": {
                        "type": "array", "minItems": 1, "maxItems": 6,
                        "items": {"type": "string"},
                    },
                },
                "required": ["episode_id", "sections"],
                "additionalProperties": False,
            },
        },
    },
]
