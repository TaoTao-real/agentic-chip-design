from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from chia_boom.interactive import (
    INSPECTION_HARD_LIMIT_TURNS,
    INSPECTION_WARNING_TURNS,
    _advance_inspection_budget,
    _available_tool_specs,
    _interactive_system,
    _interactive_system_with_feedback,
    _interactive_tool_specs,
    _messages_for_model,
    _meets_auto_stop,
)
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

    def test_analysis_only_records_are_rejected_even_if_disguised_as_episode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "forbidden.json").write_text(json.dumps({
                "schema_version": "design-episode-v2",
                "episode_id": "forbidden-audit",
                "knowledge_class": "cross-target-process-memory",
                "usage_class": "analysis_only",
                "eligible_for_agent_context": False,
                "eligible_for_knowledge_store": False,
                "access_policy": {},
                "retrieval_keys": ["must not load"],
            }))
            with self.assertRaisesRegex(ValueError, "analysis-only"):
                KnowledgeStore("generic", root)

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

    def test_e0_e1_share_raw_tools_and_only_e1_adds_structured_queries(self) -> None:
        e0 = {
            item["function"]["name"] for item in _interactive_tool_specs(False, "E0")
        }
        e1 = {
            item["function"]["name"] for item in _interactive_tool_specs(False, "E1")
        }
        self.assertIn("read_candidate_artifact", e0)
        self.assertIn("read_candidate_artifact", e1)
        self.assertEqual(
            e1 - e0,
            {
                "query_candidate_status",
                "list_candidate_artifacts",
                "query_candidate_failure",
                "compare_candidate_metrics",
                "query_candidate_timing_paths",
            },
        )
        self.assertEqual(e0 - e1, set())
        self.assertEqual(
            _interactive_system_with_feedback("none", "E0"),
            _interactive_system("none"),
        )
        self.assertIn(
            "deterministic structured facts",
            _interactive_system_with_feedback("none", "E1"),
        )

    def test_model_prompt_archives_old_tool_bytes_without_changing_audit(self) -> None:
        messages: list[dict] = [{"role": "user", "content": "start"}]
        for index in range(6):
            call_id = f"call-{index}"
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": call_id,
                        "function": {"name": "read_generated_rtl", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "raw RTL " + str(index) + ("x" * 10_000),
                },
            ])
        original = json.loads(json.dumps(messages))
        compacted = _messages_for_model(messages, keep_recent_tool_results=2)
        self.assertEqual(messages, original)
        archived = [
            json.loads(row["content"])
            for row in compacted if row.get("role") == "tool"
            and "archived_tool_result" in row["content"]
        ]
        self.assertEqual(len(archived), 4)
        self.assertTrue(all(row["tool"] == "read_generated_rtl" for row in archived))
        self.assertEqual(
            [row["content"] for row in compacted if row.get("role") == "tool"][-2:],
            [row["content"] for row in messages if row.get("role") == "tool"][-2:],
        )
        self.assertLess(
            len(json.dumps(compacted)),
            len(json.dumps(messages)) // 2,
        )

    def test_inspection_budget_warns_blocks_and_resets_without_hardware_hint(self) -> None:
        current = 0
        notices = {}
        for _ in range(INSPECTION_HARD_LIMIT_TURNS):
            current, notice = _advance_inspection_budget(
                current, observed=True, progressed=False
            )
            if notice:
                notices[current] = notice
        self.assertEqual(
            set(notices), {INSPECTION_WARNING_TURNS, INSPECTION_HARD_LIMIT_TURNS}
        )
        self.assertNotIn("prefix", " ".join(notices.values()).lower())
        self.assertNotIn("priority", " ".join(notices.values()).lower())
        reset, notice = _advance_inspection_budget(
            current, observed=False, progressed=True
        )
        self.assertEqual(reset, 0)
        self.assertIsNone(notice)
        specs = _interactive_tool_specs(False, "E1")
        limited = _available_tool_specs(specs, INSPECTION_HARD_LIMIT_TURNS)
        names = {row["function"]["name"] for row in limited}
        self.assertIn("apply_exact_edits", names)
        self.assertIn("evaluate_candidate", names)
        self.assertNotIn("read_generated_rtl", names)
        self.assertNotIn("query_candidate_status", names)


if __name__ == "__main__":
    unittest.main()
