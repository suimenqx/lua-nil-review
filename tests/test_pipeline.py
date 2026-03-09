from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PipelineTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name)
        self.state_dir = self.repo / "artifacts" / "string-find-nil"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_file(self, relative_path: str, content: str) -> Path:
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_config(self, payload: dict) -> Path:
        path = self.repo / ".lua-nil-review.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def run_script(self, script: str, *extra_args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / script),
            "--root",
            str(self.repo),
            "--state-dir",
            str(self.state_dir),
            *extra_args,
        ]
        return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=check)

    def analyze(self, *, use_config: bool = False) -> dict:
        args = ["--resume"]
        if use_config:
            args.extend(["--config", str(self.repo / ".lua-nil-review.json")])
        result = self.run_script("analyze_string_find_nil.py", *args)
        return json.loads(result.stdout)

    def prepare(self) -> dict:
        result = self.run_script("prepare_review_shards.py", "--resume")
        return json.loads(result.stdout)

    def claim(self) -> dict:
        result = self.run_script("review_shard.py", "--claim-next")
        return json.loads(result.stdout)

    def complete(self, template_relative_path: str, *, decision: str = "confirm") -> dict:
        template_path = self.state_dir / template_relative_path
        payload = json.loads(template_path.read_text(encoding="utf-8"))
        for item in payload["finding_reviews"]:
            item["decision"] = decision
            item["rationale"] = f"{decision} in test"
            item["severity"] = "medium"
        payload["reviewer"] = "unittest"
        payload["summary"] = "completed in test"
        review_input = self.repo / "review-input.json"
        review_input.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_script("review_shard.py", "--complete", str(review_input))
        return json.loads(result.stdout)

    def merge(self, *, use_config: bool = False) -> dict:
        args: list[str] = []
        if use_config:
            args.extend(["--config", str(self.repo / ".lua-nil-review.json")])
        result = self.run_script("merge_review_results.py", *args)
        return json.loads(result.stdout)

    def load_manifest(self) -> dict:
        return json.loads((self.state_dir / "manifest.json").read_text(encoding="utf-8"))

    def load_files_index(self) -> list[dict]:
        entries = []
        with (self.state_dir / "files.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def load_all_findings(self) -> list[dict]:
        findings = []
        for path in sorted((self.state_dir / "analysis").glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            findings.extend(payload.get("findings", []))
        return findings

    def test_detection_and_suppression(self) -> None:
        self.write_file(
            "positive.lua",
            "local function demo()\n  local y = nil\n  string.find(y, 'a')\nend\n",
        )
        self.write_file(
            "guarded.lua",
            "local function guarded(x)\n  if x then\n    string.find(x, 'b')\n  end\nend\n",
        )
        self.write_file(
            "maybe.lua",
            "local function maybe()\n  local y = fetch_name()\n  string.find(y, 'c')\nend\n",
        )
        self.write_file(
            "suppressed.lua",
            "local function suppressed()\n  local z = nil\n  string.find(z, 'd')\nend\n",
        )
        self.write_config(
            {
                "suppressions": [
                    {"file": "suppressed.lua", "line": 3, "rule_id": "lua.string-find-first-arg-nil"}
                ]
            }
        )

        self.analyze(use_config=True)
        findings = self.load_all_findings()
        self.assertEqual(3, len(findings))
        suppressed = [finding for finding in findings if finding["suppressed"]]
        unsuppressed = [finding for finding in findings if not finding["suppressed"]]
        self.assertEqual(1, len(suppressed))
        self.assertEqual({"nil", "maybe_nil"}, {finding["nil_state"] for finding in unsuppressed})

        prepared = self.prepare()
        self.assertEqual(2, prepared["suppressed_findings"])
        self.assertEqual(1, prepared["trace_summary"]["auto_filtered_low_confidence"])
        shard_files = list((self.state_dir / "findings").glob("*.jsonl"))
        self.assertEqual(1, len(shard_files))
        shard_findings = [json.loads(line) for line in shard_files[0].read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(1, len(shard_findings))

    def test_incremental_reuses_unchanged_files(self) -> None:
        self.write_file("a.lua", "local function a()\n  local y = nil\n  string.find(y, 'a')\nend\n")
        self.write_file("b.lua", "local function b()\n  local y = fetch()\n  string.find(y, 'b')\nend\n")

        self.analyze()
        first_index = self.load_files_index()
        self.assertEqual({"analyzed"}, {entry["analysis_status"] for entry in first_index})

        self.write_file("a.lua", "local function a()\n  local y = nil\n  local z = y\n  string.find(z, 'a')\nend\n")
        self.analyze()
        second_index = {entry["file"]: entry for entry in self.load_files_index()}
        self.assertEqual("analyzed", second_index["a.lua"]["analysis_status"])
        self.assertEqual("reused", second_index["b.lua"]["analysis_status"])

    def test_stale_in_review_shard_is_reclaimed(self) -> None:
        self.write_file("sample.lua", "local function demo()\n  local y = nil\n  string.find(y, 'a')\nend\n")
        self.analyze()
        self.prepare()
        claim = self.claim()
        shard_id = claim["shard_id"]
        manifest = self.load_manifest()
        manifest["shards"][shard_id]["heartbeat_at"] = "2000-01-01T00:00:00Z"
        manifest["shards"][shard_id]["status"] = "in_review"
        (self.state_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        reclaimed = self.claim()
        self.assertEqual("claimed", reclaimed["status"])
        self.assertEqual(shard_id, reclaimed["shard_id"])

    def test_manifest_rebuild_after_corruption(self) -> None:
        self.write_file("sample.lua", "local function demo()\n  local y = nil\n  string.find(y, 'a')\nend\n")
        self.analyze()
        self.prepare()
        (self.state_dir / "manifest.json").write_text("{this is not json", encoding="utf-8")

        claim = self.claim()
        self.assertEqual("claimed", claim["status"])
        manifest = self.load_manifest()
        self.assertIn("shards", manifest)
        self.assertGreaterEqual(manifest["shards_total"], 1)

    def test_large_file_shards_use_short_snippets_and_merge(self) -> None:
        lines = [f"-- filler {index}" for index in range(1, 3201)]
        lines[3098] = "local function giant()"
        lines[3099] = "  local y = nil"
        lines[3100] = "  string.find(y, 'needle')"
        lines[3101] = "end"
        self.write_file("giant.lua", "\n".join(lines) + "\n")

        self.analyze()
        self.prepare()
        claim = self.claim()
        self.assertEqual("claimed", claim["status"])
        finding = claim["findings"][0]
        self.assertGreaterEqual(finding["line"], 3101)
        for snippet in finding["snippets"]:
            snippet_lines = snippet["content"].splitlines()
            self.assertLessEqual(len(snippet_lines), 31)

        self.complete(claim["review_template_path"])
        summary = self.merge()
        self.assertEqual(1, summary["totals"]["confirmed_findings"])
        report = (self.state_dir / "final" / "report.md").read_text(encoding="utf-8")
        self.assertIn("giant.lua", report)

    def test_adapter_docs_are_generated_from_shared_source(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "generate_adapter_docs.py"), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)
        self.assertIn("adapter docs are up to date", result.stdout)

    def test_wrapper_claim_and_complete_flow(self) -> None:
        self.write_file("sample.lua", "local function demo()\n  local y = nil\n  string.find(y, 'a')\nend\n")
        claim = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_review_cycle.py"),
                "claim",
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        claim_payload = json.loads(claim.stdout)
        self.assertEqual("claimed", claim_payload["claim"]["status"])
        template_path = self.state_dir / claim_payload["claim"]["review_template_path"]

        review = json.loads(template_path.read_text(encoding="utf-8"))
        for item in review["finding_reviews"]:
            item["decision"] = "confirm"
            item["rationale"] = "wrapper flow"
            item["severity"] = "medium"
        review["reviewer"] = "wrapper-test"
        review["summary"] = "ok"
        review_path = self.repo / "wrapper-review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")

        complete = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_review_cycle.py"),
                "complete",
                "--review-json",
                str(review_path),
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        complete_payload = json.loads(complete.stdout)
        self.assertEqual("reviewed", complete_payload["complete"]["status"])
        self.assertEqual(1, complete_payload["merge"]["totals"]["confirmed_findings"])


if __name__ == "__main__":
    unittest.main()
