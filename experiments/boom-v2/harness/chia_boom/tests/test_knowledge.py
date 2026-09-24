from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from chia_boom.interactive import _interactive_system, _meets_auto_stop
from chia_boom.knowledge import KnowledgeStore


class KnowledgeTests(unittest.TestCase):
    def test_none_mode_has_no_catalog(self) -> None:
        store = KnowledgeStore("none")
        self.assertFalse(store.enabled)
        self.assertEqual(store.manifest()["episodes"], [])

    def test_generic_mode_cannot_retrieve_target_solution(self) -> None:
        store = KnowledgeStore("generic")
        result = store.search("differential failure and repair", 4)
        ids = [row["episode_id"] for row in result.payload["matches"]]
        self.assertIn("interactive-hardware-loop-recovery-generic-v1", ids)
        self.assertIn("memory-ablation-control-generic-v1", ids)
        self.assertTrue(all("target" not in item for item in ids))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            store.retrieve("private-target-episode", ["search_history"])

    def test_target_mode_requires_explicit_section_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "private.json").write_text(json.dumps({
                "schema_version": "design-episode-v2",
                "episode_id": "private-target-episode",
                "knowledge_class": "target-specific-solution",
                "access_policy": {},
                "retrieval_keys": ["target timing"],
                "evidence_to_hypothesis": ["private evidence"],
                "applicability": {"positive_retrieval_keys": ["target timing"]},
            }))
            store = KnowledgeStore("target", root)
            search = store.search("target timing", 4)
            self.assertIn("private-target-episode", [
                row["episode_id"] for row in search.payload["matches"]
            ])
            retrieved = store.retrieve(
                "private-target-episode",
                ["evidence_to_hypothesis", "applicability"],
            )
            self.assertEqual(
                set(retrieved.payload["sections"]),
                {"evidence_to_hypothesis", "applicability"},
            )
            self.assertEqual(
                retrieved.audit["knowledge_class"], "target-specific-solution"
            )

    def test_prompts_distinguish_blind_and_memory_assisted_runs(self) -> None:
        self.assertIn("No human\ndiagnosis", _interactive_system("none"))
        self.assertIn("No target-specific diagnosis", _interactive_system("generic"))
        self.assertIn("Structured historical design memory", _interactive_system("target"))
        self.assertIn("Search the", _interactive_system("generic"))
        self.assertIn("Search its index once", _interactive_system("target"))

    def test_auto_stop_requires_measured_threshold(self) -> None:
        self.assertFalse(_meets_auto_stop(31.92, {"critical_delay_ns": 22.0}, None))
        self.assertTrue(_meets_auto_stop(31.92, {"critical_delay_ns": 22.0}, 20.0))
        self.assertFalse(_meets_auto_stop(31.92, {"critical_delay_ns": 30.0}, 20.0))


if __name__ == "__main__":
    unittest.main()
