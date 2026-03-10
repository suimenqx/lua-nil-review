from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class SymbolTracingTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name)
        self.state_dir = self.repo / "artifacts" / "string-find-nil"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_file(self, relative_path: str, content: str) -> None:
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_config(self, payload: dict) -> None:
        (self.repo / ".lua-nil-review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_wrapper(self, *args: str) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_review_cycle.py"),
                *args,
                "--root",
                str(self.repo),
                "--state-dir",
                str(self.state_dir),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def load_analysis_findings(self) -> list[dict]:
        findings: list[dict] = []
        for path in sorted(self.state_dir.joinpath("analysis").glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            findings.extend(payload.get("findings", []))
        return findings

    def test_jump_and_trace_capture_collision_branches(self) -> None:
        self.write_file(
            "main.lua",
            "local Config = require('config')\n"
            "local function demo(name)\n"
            "  local value = Config.get(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )
        self.write_file(
            "src/ui/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return name or ''\n"
            "end\n"
            "return M\n",
        )
        self.write_file(
            "src/net/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return nil\n"
            "end\n"
            "return M\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(1, refresh["analyze"]["symbol_index"]["collision_groups"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["traced"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["escalated"])

        jump = self.run_wrapper("jump", "--file", "main.lua", "--line", "3", "--expr", "Config.get")
        candidates = jump["jump"]["candidates"]
        self.assertEqual("collision_multi_candidate", jump["jump"]["resolution_kind"])
        self.assertEqual(2, len(candidates))
        self.assertEqual({"always_non_nil", "always_nil"}, {item["return_summary"]["state"] for item in candidates})
        self.assertTrue(jump["jump"]["external_config_dependency"])

        bundle_paths = sorted(self.state_dir.joinpath("trace_bundles").glob("*.json"))
        self.assertEqual(1, len(bundle_paths))
        bundle = json.loads(bundle_paths[0].read_text(encoding="utf-8"))
        self.assertEqual("mixed", bundle["overall"])
        self.assertEqual({"safe", "risky"}, {item["status"] for item in bundle["branch_outcomes"]})
        self.assertTrue(bundle["needs_source_escalation"])
        self.assertTrue(bundle["external_config_dependency"])

    def test_claim_payload_includes_trace_bundle_and_slices(self) -> None:
        self.write_file(
            "main.lua",
            "local Config = require('config')\n"
            "local function demo(name)\n"
            "  local value = Config.get(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )
        self.write_file(
            "src/ui/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return name or ''\n"
            "end\n"
            "return M\n",
        )
        self.write_file(
            "src/net/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return nil\n"
            "end\n"
            "return M\n",
        )

        claim = self.run_wrapper("claim")
        self.assertEqual("claimed", claim["claim"]["status"])
        finding = claim["claim"]["findings"][0]
        self.assertEqual("mixed", finding["trace_bundle"]["overall"])
        self.assertEqual(2, len(finding["trace_slices"]))
        self.assertEqual({"safe", "risky"}, {item["status"] for item in finding["trace_bundle"]["branch_outcomes"]})
        self.assertTrue(finding["trace_bundle"]["external_config_dependency"])

    def test_safe_trace_auto_silences_finding(self) -> None:
        self.write_file(
            "main.lua",
            "local function resolve(name)\n"
            "  return name or ''\n"
            "end\n"
            "local function demo(name)\n"
            "  local value = resolve(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(0, refresh["prepare"]["shards_total"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["auto_silenced"])

        analysis_files = sorted(self.state_dir.joinpath("analysis").glob("*.json"))
        self.assertEqual(1, len(analysis_files))
        analysis_doc = json.loads(analysis_files[0].read_text(encoding="utf-8"))
        self.assertEqual(1, len(analysis_doc["findings"]))
        self.assertTrue(analysis_doc["findings"][0]["trace_auto_silenced"])
        self.assertEqual("safe", analysis_doc["findings"][0]["trace_status"])

    def test_module_resolution_override_selects_single_candidate(self) -> None:
        self.write_config(
            {
                "symbol_tracing": {
                    "module_resolution_overrides": {
                        "config": ["src/ui/config.lua"]
                    }
                }
            }
        )
        self.write_file(
            "main.lua",
            "local Config = require('config')\n"
            "local function demo(name)\n"
            "  local value = Config.get(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )
        self.write_file(
            "src/ui/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return name or ''\n"
            "end\n"
            "return M\n",
        )
        self.write_file(
            "src/net/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return nil\n"
            "end\n"
            "return M\n",
        )

        refresh = self.run_wrapper("refresh", "--config", str(self.repo / ".lua-nil-review.json"))
        self.assertEqual(0, refresh["prepare"]["shards_total"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["auto_silenced"])

        jump = self.run_wrapper(
            "jump",
            "--config",
            str(self.repo / ".lua-nil-review.json"),
            "--file",
            "main.lua",
            "--line",
            "3",
            "--expr",
            "Config.get",
        )
        self.assertTrue(jump["jump"]["used_override"])
        self.assertFalse(jump["jump"]["external_config_dependency"])
        self.assertEqual(1, len(jump["jump"]["candidates"]))
        self.assertEqual("src/ui/config.lua", jump["jump"]["candidates"][0]["file"])

    def test_trace_callsite_and_build_symbol_index_command(self) -> None:
        self.write_file(
            "main.lua",
            "local function resolve(name)\n"
            "  return name or ''\n"
            "end\n"
            "local function demo(name)\n"
            "  local value = resolve(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )

        build = self.run_wrapper("build-symbol-index")
        self.assertEqual(1, build["build_symbol_index"]["files_indexed"])

        trace = self.run_wrapper("trace", "--file", "main.lua", "--line", "5", "--expr", "resolve")
        self.assertEqual("callsite", trace["trace"]["root"]["kind"])
        self.assertEqual("safe", trace["trace"]["overall"])
        self.assertFalse(trace["trace"]["external_config_dependency"])
        expanded = self.run_wrapper("trace", "--file", "main.lua", "--line", "5", "--expr", "resolve", "--expand-node", "node-1")
        self.assertEqual("node-1", expanded["trace"]["expanded_node"])
        self.assertEqual("node-1", expanded["trace"]["expanded_node_detail"]["node_id"])

    def test_risk_tiering_filters_unconfirmed_function_returns(self) -> None:
        self.write_file(
            "main.lua",
            "local data = { user = { name = 'alice' } }\n"
            "local function deterministic()\n"
            "  local value = nil\n"
            "  string.find(value, 'a')\n"
            "end\n"
            "local function missing_key()\n"
            "  local value = data.user.email\n"
            "  string.find(value, 'a')\n"
            "end\n"
            "local function medium(user)\n"
            "  local value = user.email\n"
            "  string.find(value, 'a')\n"
            "end\n"
            "local function low()\n"
            "  local value = fetch_name()\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(0, refresh["prepare"]["trace_summary"]["auto_filtered_low_confidence"])
        self.assertEqual(4, refresh["prepare"]["trace_summary"]["visible_after_trace"])

        findings_by_line = {finding["line"]: finding for finding in self.load_analysis_findings()}
        self.assertEqual(1, findings_by_line[4]["risk_level"])
        self.assertEqual("deterministic_nil", findings_by_line[4]["risk_category"])
        self.assertEqual(1, findings_by_line[8]["risk_level"])
        self.assertEqual("missing_local_table_key", findings_by_line[8]["risk_category"])
        self.assertEqual(2, findings_by_line[12]["risk_level"])
        self.assertEqual("local_unguarded_index", findings_by_line[12]["risk_category"])
        self.assertEqual(3, findings_by_line[16]["risk_level"])
        self.assertEqual("function_return_unverified", findings_by_line[16]["risk_category"])
        self.assertTrue(findings_by_line[16]["human_review_visible"])
        self.assertIsNone(findings_by_line[16]["auto_filtered_reason"])
        self.assertEqual("uncertain", findings_by_line[16]["trace_status"])

    def test_level3_risky_return_survives_trace_gate(self) -> None:
        self.write_file(
            "main.lua",
            "local function resolve()\n"
            "  return nil\n"
            "end\n"
            "local function demo()\n"
            "  local value = resolve()\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(1, refresh["prepare"]["shards_total"])
        finding = self.load_analysis_findings()[0]
        self.assertEqual(3, finding["risk_level"])
        self.assertEqual("risky", finding["trace_status"])
        self.assertTrue(finding["trace_gate_passed"])
        self.assertTrue(finding["human_review_visible"])

    def test_agentic_retrace_promotes_uncertain_chain_before_claim(self) -> None:
        self.write_config(
            {
                "symbol_tracing": {
                    "max_depth": 2,
                    "max_expanded_nodes": 8,
                    "agentic_retrace_depth_bonus": 8,
                    "agentic_retrace_max_expanded_nodes": 96,
                }
            }
        )
        self.write_file(
            "main.lua",
            "local function leaf(name)\n"
            "  return name or ''\n"
            "end\n"
            "local function middle(name)\n"
            "  return leaf(name)\n"
            "end\n"
            "local function top(name)\n"
            "  return middle(name)\n"
            "end\n"
            "local function demo(name)\n"
            "  local value = top(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh", "--config", str(self.repo / ".lua-nil-review.json"))
        self.assertEqual(0, refresh["prepare"]["shards_total"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["agentic_retraced"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["agentic_improved"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["agentic_promoted_safe"])

        finding = self.load_analysis_findings()[0]
        self.assertEqual("safe", finding["trace_status"])
        self.assertTrue(finding["trace_auto_silenced"])
        self.assertTrue(finding["agentic_trace"]["triggered"])
        self.assertTrue(finding["agentic_trace"]["improved"])

        bundle_path = self.state_dir / finding["trace_bundle_path"]
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertTrue(bundle["agentic_strategy"]["triggered"])
        self.assertEqual("safe", bundle["agentic_strategy"]["retry_overall"])
        self.assertIn(bundle["agentic_strategy"]["initial_overall"], {"uncertain", "budget_exhausted"})

    def test_agentic_strategy_records_frontier_jump_expansions_for_uncertain_calls(self) -> None:
        self.write_file(
            "main.lua",
            "local function demo()\n"
            "  local value = fetch_name()\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["agentic_retraced"])
        self.assertGreaterEqual(refresh["prepare"]["trace_summary"]["agentic_frontier_jumps"], 1)
        finding = self.load_analysis_findings()[0]
        bundle_path = self.state_dir / finding["trace_bundle_path"]
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        strategy = bundle["agentic_strategy"]
        self.assertTrue(strategy["triggered"])
        self.assertGreaterEqual(len(strategy["frontier_jumps"]), 1)
        self.assertEqual("fetch_name", strategy["frontier_jumps"][0]["expression"])
        self.assertEqual(0, strategy["frontier_jumps"][0]["candidate_count"])

    def test_parameter_without_callers_stays_visible_until_proven_safe(self) -> None:
        self.write_file(
            "main.lua",
            "local function sink(name)\n"
            "  string.find(name, 'a')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(1, refresh["prepare"]["shards_total"])
        finding = self.load_analysis_findings()[0]
        self.assertEqual(3, finding["risk_level"])
        self.assertEqual("parameter_unverified", finding["risk_category"])
        self.assertEqual("uncertain", finding["trace_status"])
        self.assertFalse(finding["trace_auto_silenced"])
        self.assertTrue(finding["human_review_visible"])

    def test_parameter_trace_auto_silences_only_when_caller_chain_is_safe(self) -> None:
        self.write_file(
            "main.lua",
            "local function sink(name)\n"
            "  string.find(name, 'a')\n"
            "end\n"
            "local function forward(name)\n"
            "  sink(name)\n"
            "end\n"
            "local function demo()\n"
            "  forward('alice')\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(0, refresh["prepare"]["shards_total"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["auto_silenced"])
        finding = self.load_analysis_findings()[0]
        self.assertEqual("safe", finding["trace_status"])
        self.assertTrue(finding["trace_auto_silenced"])
        self.assertFalse(finding["human_review_visible"])

    def test_parameter_trace_keeps_mixed_callers_visible(self) -> None:
        self.write_file(
            "main.lua",
            "local function sink(name)\n"
            "  string.find(name, 'a')\n"
            "end\n"
            "local function safe()\n"
            "  sink('alice')\n"
            "end\n"
            "local function unsafe()\n"
            "  sink()\n"
            "end\n",
        )

        refresh = self.run_wrapper("refresh")
        self.assertEqual(1, refresh["prepare"]["shards_total"])
        finding = self.load_analysis_findings()[0]
        self.assertEqual("mixed", finding["trace_status"])
        self.assertFalse(finding["trace_auto_silenced"])
        self.assertTrue(finding["human_review_visible"])

    def test_priority_prefix_selects_scenario_candidate(self) -> None:
        self.write_config(
            {
                "symbol_tracing": {
                    "module_resolution_priority": ["src/ui", "src/common"]
                }
            }
        )
        self.write_file(
            "main.lua",
            "local Config = require('config')\n"
            "local function demo(name)\n"
            "  local value = Config.get(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )
        self.write_file(
            "src/ui/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return name or ''\n"
            "end\n"
            "return M\n",
        )
        self.write_file(
            "src/net/config.lua",
            "local M = {}\n"
            "function M.get(name)\n"
            "  return nil\n"
            "end\n"
            "return M\n",
        )

        refresh = self.run_wrapper("refresh", "--config", str(self.repo / ".lua-nil-review.json"))
        self.assertEqual(0, refresh["prepare"]["shards_total"])
        self.assertEqual(1, refresh["prepare"]["trace_summary"]["auto_silenced"])

        jump = self.run_wrapper(
            "jump",
            "--config",
            str(self.repo / ".lua-nil-review.json"),
            "--file",
            "main.lua",
            "--line",
            "3",
            "--expr",
            "Config.get",
        )
        self.assertTrue(jump["jump"]["applied_priority"])
        self.assertEqual("src/ui", jump["jump"]["matched_priority_prefix"])
        self.assertEqual("priority_prefix", jump["jump"]["resolution_strategy"])
        self.assertFalse(jump["jump"]["external_config_dependency"])
        self.assertEqual(1, len(jump["jump"]["candidates"]))
        self.assertEqual("src/ui/config.lua", jump["jump"]["candidates"][0]["file"])

    def test_stale_trace_bundle_is_removed_after_finding_disappears(self) -> None:
        self.write_file(
            "main.lua",
            "local function resolve(name)\n"
            "  return nil\n"
            "end\n"
            "local function demo(name)\n"
            "  local value = resolve(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )
        self.run_wrapper("refresh")
        bundles = sorted(self.state_dir.joinpath("trace_bundles").glob("*.json"))
        self.assertEqual(1, len(bundles))

        self.write_file(
            "main.lua",
            "local function resolve(name)\n"
            "  return name or ''\n"
            "end\n"
            "local function demo(name)\n"
            "  local value = resolve(name)\n"
            "  string.find(value, 'a')\n"
            "end\n",
        )
        self.run_wrapper("refresh")
        bundles = sorted(self.state_dir.joinpath("trace_bundles").glob("*.json"))
        self.assertEqual(1, len(bundles))
        payload = json.loads(bundles[0].read_text(encoding="utf-8"))
        self.assertTrue(payload["trace_auto_silenced"])


if __name__ == "__main__":
    unittest.main()
