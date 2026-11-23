"""Failure diagnostics explain blockers without weakening failure semantics."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from uav_runtime.atomic import allocation
from uav_runtime.atomic.common import AtomicError
from uav_runtime.planning.scheduler import SchedulerError


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/flight-control/scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location("_diagnostic_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = load_script("submit_queue_batch")
with patch.dict(sys.modules, {"submit_queue_batch": validator}):
    assembler = load_script("assemble_plan")


def allocation_input(budget=34.99):
    return {"candidates": [{"uav_id": "early", "range_budget": 1000, "time_budget": budget},
                           {"uav_id": "late", "range_budget": 1000, "time_budget": 100}],
            "jobs": [{"job_id": "J1"}],
            "costs": {"reachable": {"early": ["J1"], "late": ["J1"]},
                      "transfer_range": {uid: [[None, 10], [10, None]] for uid in ("early", "late")},
                      "transfer_time": {"early": [[None, 10], [10, None]],
                                        "late": [[None, 25], [25, None]]},
                      "work_range": [20], "work_time": [20]},
            "constraints": {"required_drones": 2}}


def plan_input():
    assignments = []
    for uid in ("first", "middle", "last"):
        assignments.append({"uav_id": uid, "flight_alt": 1.2, "jobs": ["J1"],
                            "battery_level": 90, "flight_remain": 10000,
                            "range_budget": 8000, "time_budget": 1000,
                            "estimated_range_m": 100, "estimated_battery_after": 89,
                            "origin": {"longitude": 120, "latitude": 24},
                            "steps": [{"type": "transit", "waypoints": [
                                {"longitude": 120, "latitude": 24.001, "altitude": 1.2}]}]})
    return {"plan": {"status": "OK", "task_key": "T-diagnosis", "assignments": assignments,
                     "mock": True, "map_used": False, "whole_mission_budget_complete": False,
                     "budget_scope": "horizontal", "unused": [], "jobs": ["J1"]}}


class AllocationDiagnosticsTests(unittest.TestCase):
    def test_real_solver_reports_waiting_bound_and_keeps_original_constraints(self):
        request = allocation_input()
        original = deepcopy(request)
        with self.assertRaises(AtomicError) as error:
            allocation.compute(request)
        self.assertEqual(error.exception.code, "NO_FEASIBLE_ALLOCATION")
        diagnosis = error.exception.details["diagnosis"]
        self.assertEqual(diagnosis["binding_constraint"], "SHARED_JOB_TIME_BUDGET")
        violation, = diagnosis["violations"]
        self.assertEqual(violation["uav_id"], "early")
        self.assertAlmostEqual(violation["waiting_lower_bound_s"], 5.001)
        self.assertAlmostEqual(violation["required_time_lower_bound_s"], 35.001)
        self.assertGreater(violation["excess_s"], 0)
        self.assertEqual(request, original)

    def test_real_solver_succeeds_when_waiting_budget_is_sufficient(self):
        result = allocation.compute(allocation_input(35.002))
        self.assertEqual(set(result["owners"]["J1"]), {"early", "late"})

    def test_optional_participant_does_not_make_waiting_bound_mandatory(self):
        request = allocation_input()
        request["constraints"]["required_drones"] = 1
        result = allocation.compute(request)
        self.assertEqual(result["owners"]["J1"], ["late"])

    def test_other_failures_do_not_claim_a_waiting_diagnosis(self):
        for change in ("single", "fixed", "optional", "timeout"):
            request = allocation_input()
            code = "PLANNING_INFEASIBLE"
            if change == "single":
                request["constraints"]["single_owner_jobs"] = ["J1"]
            elif change == "fixed":
                request["constraints"]["fixed_assignments"] = {"J1": "early"}
            elif change == "optional":
                request["constraints"].pop("required_drones")
            else:
                code = "SOLVER_TIMEOUT"
            with self.subTest(change=change), patch.object(
                    allocation, "solve", side_effect=SchedulerError(code, "test failure")):
                with self.assertRaises(AtomicError) as error:
                    allocation.compute(request)
                if change == "timeout":
                    self.assertEqual(error.exception.code, code)
                    self.assertNotIn("diagnosis", error.exception.details)
                else:
                    self.assertEqual(error.exception.details["diagnosis"]["binding_constraint"], "UNDETERMINED")

    def test_multi_job_routes_do_not_use_the_single_job_bound(self):
        scenario = {"required_drones": 2, "eligible_uavs": {"1": [1, 2], "2": [1, 2]},
                    "single_owner_jobs": [], "fixed_assignments": {}}
        self.assertEqual(allocation._infeasibility_diagnosis(scenario, ["a", "b"], ["x", "y"]), {})


class AssemblyDiagnosticsTests(unittest.TestCase):
    def test_real_pipeline_stops_and_creates_no_artifact_on_partial_failure(self):
        request = plan_input()
        request["plan"]["assignments"][0]["origin"]["latitude"] = 23
        pipeline = {"steps": [
            {"id": "assemble", "script": "skills/flight-control/scripts/assemble_plan.py", "input": request},
            {"id": "later", "script": "skills/spatial-compute/scripts/resolve_jobs.py", "input": {}}]}
        with tempfile.TemporaryDirectory(prefix="uav-failed-assembly-") as tmp:
            root = Path(tmp)
            for name in ("uav_runtime", "skills"):
                shutil.copytree(ROOT / name, root / name,
                                ignore=shutil.ignore_patterns(".venv", "__pycache__"))
            process = subprocess.run([sys.executable, "-B", str(root / "uav_runtime/run.py"),
                                      "--pipeline", "--input", json.dumps(pipeline)],
                                     cwd=root, capture_output=True, text=True)
            self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["failed_step"], "assemble")
            self.assertEqual(len(result["participants"]), 3)
            self.assertFalse(result["artifact_created"])
            self.assertNotIn("later", result["pipeline_steps"])
            self.assertFalse(list((root / "artifacts").rglob("*.json")))

    def test_failure_at_any_position_reports_all_aircraft_without_actions(self):
        for index in range(3):
            request = plan_input()
            request["plan"]["assignments"][index]["origin"]["latitude"] = 23
            with self.subTest(index=index), patch.object(validator, "DEFAULT_MAX_DISTANCE_M", 1000):
                with self.assertRaises(AtomicError) as error:
                    assembler.compute(request)
                self.assertEqual(error.exception.code, "INVALID_ARGUMENT")
                details = error.exception.details
                self.assertFalse(details["validated"])
                self.assertFalse(details["submitted"])
                self.assertFalse(details["validated_for_execution"])
                self.assertEqual(details["max_distance_m"], 1000)
                self.assertEqual(details["uav_id"], request["plan"]["assignments"][index]["uav_id"])
                self.assertEqual(len(details["participants"]), 3)
                self.assertNotIn("artifact_file", details)
                for i, row in enumerate(details["participants"]):
                    self.assertEqual(row["validated"], i != index)
                    self.assertNotIn("actions", row)

    def test_multiple_failures_are_all_reported(self):
        request = plan_input()
        for row in request["plan"]["assignments"]:
            row["origin"]["latitude"] = 23
        with self.assertRaises(AtomicError) as error:
            assembler.compute(request)
        self.assertEqual([p["validated"] for p in error.exception.details["participants"]], [False] * 3)

    def test_success_preserves_action_display_modes(self):
        for mode in ("true", "false", "compact", "full", "0", "off", "no"):
            with self.subTest(mode=mode), patch.object(assembler, "SHOW_ACTIONS", True), \
                    patch.object(assembler, "SHOW_ACTIONS_MODE", mode):
                result = assembler.compute(plan_input())
                self.assertTrue(result["validated"])
                self.assertEqual(len(result["participants"]), 3)
                self.assertIs(result["_summary"]["show_actions"], True)
                self.assertEqual(result["_summary"]["actions_format"], "full" if mode == "full" else "compact")
                for row in result["_summary"]["participants"]:
                    if mode == "full":
                        self.assertIsInstance(row["actions"][2]["params"]["waypoints"], list)
                        self.assertIn("local_batch", row)
                    else:
                        self.assertEqual(row["actions"][2]["params"]["waypoints"]["count"], 1)

                with patch.object(assembler, "SHOW_ACTIONS", False):
                    hidden = assembler.compute(plan_input())
                self.assertIs(hidden["_summary"]["show_actions"], False)
                for row in hidden["_summary"]["participants"]:
                    self.assertNotIn("actions", row)
                    self.assertNotIn("local_batch", row)

    def test_script_default_switch_controls_real_cli_without_changing_actions(self):
        with tempfile.TemporaryDirectory(prefix="uav-action-display-") as tmp:
            root = Path(tmp)
            for name in ("uav_runtime", "skills"):
                shutil.copytree(ROOT / name, root / name,
                                ignore=shutil.ignore_patterns(".venv", "__pycache__"))
            relative = "skills/flight-control/scripts/assemble_plan.py"
            script = root / relative
            original = script.read_text()
            self.assertTrue(any(f"SHOW_ACTIONS = {v}  #" in original for v in (True, False)))
            env = os.environ.copy()
            env.pop("UAV_SHOW_ACTIONS", None)
            batches = []
            for enabled in (True, False):
                source = original
                for value in (True, False):
                    source = source.replace(f"SHOW_ACTIONS = {value}  #", f"SHOW_ACTIONS = {enabled}  #", 1)
                script.write_text(source)
                process = subprocess.run([sys.executable, "-B", str(root / "uav_runtime/run.py"),
                                          relative, "--input", json.dumps(plan_input())],
                                         cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                result = json.loads(process.stdout)
                self.assertIs(result["summary"]["show_actions"], enabled)
                self.assertTrue(result["validated"])
                self.assertTrue(result["artifact_created"])
                for row in result["summary"]["participants"]:
                    self.assertEqual("actions" in row, enabled)
                artifact = json.loads(Path(result["artifact_file"]).read_text())["data"]
                batches.append([(row["raw_batch"], row["dry_run"]) for row in artifact["participants"]])
            self.assertEqual(batches[0], batches[1])


if __name__ == "__main__":
    unittest.main()
