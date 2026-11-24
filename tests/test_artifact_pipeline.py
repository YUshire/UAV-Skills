"""Artifact handoff and fail-stop behavior, including the real offline CLI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from uav_runtime.atomic.common import AtomicError
from uav_runtime.pipeline import compute


ROOT = Path(__file__).resolve().parents[1]
REGIONS = "skills/spatial-compute/scripts/resolve_jobs.py"
COSTS = "skills/trajectory-generation/scripts/planning_routes.py"


class HandoffTests(unittest.TestCase):
    def test_exact_returned_path_is_passed_without_reconstruction(self):
        returned = "/workspace/artifacts/regions-302e328802794bdbb68b811ad0068054.json"
        outputs = [
            {"status": "OK", "artifact_created": True, "artifact_file": returned},
            {"status": "OK", "artifact_created": True, "artifact_file": "/workspace/costs.json"},
        ]
        calls = []

        def run(command, **kwargs):
            calls.append(json.loads(command[-1]))
            return subprocess.CompletedProcess(command, 0, json.dumps(outputs[len(calls) - 1]), "")

        with patch("uav_runtime.pipeline.subprocess.run", side_effect=run):
            result = compute({"steps": [
                {"id": "regions", "script": REGIONS, "input": {}},
                {"id": "costs", "script": COSTS, "input": {
                    "jobs": {"ref_step": "regions", "select": "/data/jobs"}}},
            ]})
        self.assertEqual(calls[1]["jobs"], {"ref": returned, "select": "/data/jobs"})
        self.assertEqual(result["artifact_file"], outputs[-1]["artifact_file"])

    def test_failed_step_preserves_diagnosis_and_stops(self):
        failure = {"status": "NO_FEASIBLE_ALLOCATION", "artifact_created": False,
                   "candidate_screening": {"diagnosis": {"binding_constraint": "FLIGHT_RADIUS"}}}
        with patch("uav_runtime.pipeline.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 2, json.dumps(failure), "")) as run:
            result = compute({"steps": [
                {"id": "costs", "script": COSTS, "input": {}},
                {"id": "later", "script": REGIONS, "input": {}},
            ]})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["candidate_screening"], failure["candidate_screening"])
        self.assertEqual(result["failed_step"], "costs")
        self.assertFalse(result["artifact_created"])

    def test_invalid_manifests_are_rejected_before_running(self):
        invalid = [
            [{"id": "first", "script": REGIONS, "input": {"jobs": {"ref_step": "later"}}}],
            [{"id": "same", "script": REGIONS, "input": {}}] * 2,
            [{"id": "online", "script": "skills/flight-control/scripts/submit_queue_batch.py", "input": {}}],
        ]
        with patch("uav_runtime.pipeline.subprocess.run") as run:
            for steps in invalid:
                with self.subTest(steps=steps), self.assertRaises(AtomicError):
                    compute({"steps": steps})
            run.assert_not_called()

    def test_invalid_tool_output_does_not_start_next_step(self):
        with patch("uav_runtime.pipeline.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, "not JSON", "bad output")) as run:
            with self.assertRaises(AtomicError) as error:
                compute({"steps": [{"id": "regions", "script": REGIONS, "input": {}}]})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(error.exception.details["failed_step"], "regions")


class OfflineCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="uav-pipeline-test-")
        cls.workspace = Path(cls.directory.name)
        for name in ("uav_runtime", "skills", "data"):
            shutil.copytree(ROOT / name, cls.workspace / name,
                            ignore=shutil.ignore_patterns(".venv", "__pycache__"))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_documented_pipeline_and_legacy_entrypoint(self):
        text = (ROOT / "skills/mission-planning/references/artifact-pipeline.md").read_text()
        request = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
        process = subprocess.run(
            [sys.executable, "-B", str(self.workspace / "uav_runtime/run.py"),
             "--pipeline", "--input", json.dumps(request)],
            cwd=self.workspace.parent, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(len(result["pipeline_steps"]), 6)
        self.assertFalse(result["submitted"])
        self.assertTrue(result["validated"])
        participants = result["summary"]["participants"]
        self.assertIsInstance(result["summary"]["show_actions"], bool)
        if result["summary"]["show_actions"]:
            self.assertTrue(participants[0]["actions"])
        else:
            self.assertNotIn("actions", participants[0])
        saved = json.loads(Path(result["artifact_file"]).read_text())
        self.assertTrue(saved["data"]["participants"][0]["raw_batch"])
        outputs = result["pipeline_steps"]
        costs = json.loads(Path(outputs["costs"]["artifact_file"]).read_text())
        self.assertEqual(costs["input"]["jobs"]["ref"], outputs["regions"]["artifact_file"])
        self.assertEqual(costs["input"]["candidates"]["ref"], outputs["fleet"]["artifact_file"])
        # The original single-script CLI still accepts the same input.
        legacy = subprocess.run(
            [sys.executable, "-B", str(self.workspace / "uav_runtime/run.py"), REGIONS,
             "--input", json.dumps(request["steps"][1]["input"])],
            cwd=self.workspace.parent, capture_output=True, text=True)
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        self.assertEqual(json.loads(legacy.stdout)["status"], "OK")

    def test_unreachable_aircraft_stops_at_costs_without_losing_diagnosis(self):
        text = (ROOT / "skills/mission-planning/references/artifact-pipeline.md").read_text()
        request = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
        request["steps"][0]["input"]["uav_ids"] = ["UAV-346"]
        request["steps"][1]["input"]["jobs"] = [{
            "job_id": "J1", "center_lon": 120.808063, "center_lat": 24.223875,
            "shape": "circle", "radius": 10, "task_type": "search"}]
        process = subprocess.run(
            [sys.executable, "-B", str(self.workspace / "uav_runtime/run.py"),
             "--pipeline", "--input", json.dumps(request)],
            cwd=self.workspace.parent, capture_output=True, text=True)
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "NO_FEASIBLE_ALLOCATION")
        self.assertEqual(result["failed_step"], "costs")
        self.assertEqual(set(result["pipeline_steps"]), {"fleet", "regions"})
        self.assertEqual(result["candidate_screening"]["diagnosis"]["binding_constraint"], "FLIGHT_RADIUS")
        self.assertFalse(result["artifact_created"])


if __name__ == "__main__":
    unittest.main()
