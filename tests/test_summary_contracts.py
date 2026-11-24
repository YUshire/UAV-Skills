"""Summary evidence must remain useful without reopening full route artifacts."""
from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from uav_runtime.area_route import query_area
from uav_runtime.graph_planner import _compact_scenarios
from uav_runtime.pipeline import compute
from uav_runtime.seven_inputs import _candidate_briefs


def candidate(uid, longitude=0.001, budget=1000, radius=500):
    return {
        "uav_id": uid,
        "origin": {"longitude": longitude, "latitude": 0.0},
        "range_budget": budget,
        "time_budget": 300,
        "max_flight_radius": radius,
        "max_endurance": 20,
        "battery_level": 80,
        "payload_status": "高清相机",
    }


class CandidateSummaryTests(unittest.TestCase):
    def test_unreachable_candidates_cannot_outrank_reachable_ones(self):
        fleet = [candidate("too_far", longitude=0.003, radius=200),
                 candidate("short_budget", longitude=0.0001, budget=10),
                 candidate("reachable", longitude=0.001)]
        items, count = _candidate_briefs(fleet, [0.0, 0.0])
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["uav_id"], "reachable")
        self.assertTrue(items[0]["reachable_lb"])
        self.assertFalse(next(x for x in items if x["uav_id"] == "too_far")["radius_ok"])
        self.assertFalse(next(x for x in items if x["uav_id"] == "short_budget")["range_ok"])

    def test_shortlist_limit_does_not_become_total_reachable_count(self):
        fleet = [candidate(f"UAV-{i:03d}") for i in range(20)]
        items, count = _candidate_briefs(fleet, [0.0, 0.0])
        self.assertEqual(len(items), 15)
        self.assertEqual(count, 20)

    def test_unknown_destination_does_not_claim_reachability(self):
        items, count = _candidate_briefs(
            [candidate("small", budget=500), candidate("large", budget=1000)], None)
        self.assertIsNone(count)
        self.assertEqual([x["uav_id"] for x in items], ["large", "small"])
        self.assertTrue(all("reachable_lb" not in x for x in items))


class PipelineSummaryTests(unittest.TestCase):
    def test_failure_preserves_last_success_evidence_and_stops(self):
        summary = {"task_key": "T1", "assignments": [{"uav_id": "UAV-002"}]}
        outputs = [
            {"status": "OK", "artifact_created": True,
             "artifact_file": "/workspace/routes.json", "summary": summary},
            {"status": "COORDINATE_RANGE_EXCEEDED", "artifact_created": False,
             "max_distance_m": 20000},
        ]
        effects = [subprocess.CompletedProcess([], index, json.dumps(value), "")
                   for index, value in enumerate(outputs)]
        with patch("uav_runtime.pipeline.subprocess.run", side_effect=effects) as run:
            result = compute({"steps": [
                {"id": "routes", "script": "skills/trajectory-generation/scripts/planning_routes.py", "input": {}},
                {"id": "actions", "script": "skills/flight-control/scripts/assemble_plan.py", "input": {}},
                {"id": "never", "script": "skills/spatial-compute/scripts/resolve_jobs.py", "input": {}},
            ]})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["failed_step"], "actions")
        self.assertEqual(result["last_ok_step"], "routes")
        self.assertEqual(result["last_ok_summary"], summary)
        self.assertEqual(result["status"], "COORDINATE_RANGE_EXCEEDED")
        self.assertEqual(result["max_distance_m"], 20000)
        self.assertFalse(result["artifact_created"])


def scenario(index, pending=None):
    return {"scenario_id": f"B{index}", "events": {"S1": str(index)},
            "planning_status": "CONDITIONAL",
            "geometry_plan": {"task_key": f"T{index}"},
            "pending": pending or [{"code": "RUNTIME_GEOMETRY", "message": "待补路径"}]}


class GraphSummaryTests(unittest.TestCase):
    def test_shared_evidence_is_referenced_without_losing_distinct_alerts(self):
        variants = [scenario(i) for i in range(1, 5)]
        variants[-1]["pending"] = [{"code": "TIME_RESERVATION", "message": "超出预留时间"}]
        original = deepcopy(variants)
        summaries = _compact_scenarios(variants)
        self.assertEqual([x["task_key"] for x in summaries], ["T1", "T2", "T3", "T4"])
        by_id = {x["scenario_id"]: x for x in summaries}
        for index, summary in enumerate(summaries):
            evidence = summary.get("pending")
            if evidence is None:
                evidence = by_id[summary["same_pending_as"]]["pending"]
            self.assertEqual(evidence, variants[index]["pending"])
        self.assertEqual(summaries[-1]["pending"][0]["code"], "TIME_RESERVATION")
        self.assertEqual(variants, original)

    def test_small_graph_keeps_direct_evidence_in_each_branch(self):
        summaries = _compact_scenarios([scenario(i) for i in range(1, 4)])
        self.assertTrue(all("pending" in x and "same_pending_as" not in x for x in summaries))


class AreaSummaryTests(unittest.TestCase):
    def test_large_route_exposes_length_before_waypoints_without_certifying_coverage(self):
        result = query_area(SimpleNamespace(
            center_lon=120.0, center_lat=24.0, center_alt=0,
            flight_alt=1.2, shape="rectangle", length=400, width=300,
            lane=2, mode="coverage", start_lon=None, start_lat=None))
        encoded = json.dumps(result)
        self.assertLess(encoded.index('"path_length"'), encoded.index('"waypoints"'))
        self.assertLess(encoded.index('"waypoint_count"'), encoded.index('"waypoints"'))
        self.assertEqual(result["waypoint_count"], len(result["waypoints"]))
        self.assertGreater(result["path_length"], 0)
        self.assertFalse(result["coverage_certified"])
        self.assertFalse(result["map_used"])


if __name__ == "__main__":
    unittest.main()
