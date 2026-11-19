"""Behavioral checks for complex planning; uses only offline records and geometry."""
import unittest

from uav_runtime.atomic.common import AtomicError
from uav_runtime.graph_planner import compute
from uav_runtime.task_graph import validate, scenarios


def area(nid="S1", uid="UAV-016", action="patrol", **job):
    return {"id": nid, "action": action, "uav_id": uid,
            "job": dict(center_lon=121.442057, center_lat=25.239462,
                        shape="rectangle", length=12, width=8, **job)}


def graph(*nodes, edges=(), **kwargs):
    return dict(nodes=list(nodes), edges=list(edges), **kwargs)


class GraphContractTests(unittest.TestCase):
    def test_cycle_rejected(self):
        with self.assertRaises(AtomicError):
            validate(graph(area("A"), area("B"), edges=[{"from":"A","to":"B"}, {"from":"B","to":"A"}]))

    def test_mutually_exclusive_results(self):
        g = validate(graph(area("A", action="search"), area("B"), area("C"),
                           edges=[{"from":"A","to":"B","on":"found"},
                                  {"from":"A","to":"C","on":"not_found"}]))
        variants = scenarios(g)
        self.assertEqual(len(variants), 3)
        self.assertFalse(any({"B", "C"} <= set(v["active"]) for v in variants))
        self.assertEqual(next(v for v in variants if v["events"]["A"] == "timeout")["active"], ["A"])

    def test_target_reference_requires_found_gate(self):
        b = {"id":"B","action":"search","uav_id":"UAV-254",
             "job":{"center_from":{"from_step":"A","field":"target_position"},"shape":"circle","radius":5}}
        with self.assertRaises(AtomicError):
            scenarios(validate(graph(area("A", action="search"), b, edges=[{"from":"A","to":"B"}])))

    def test_impossible_join_rejected(self):
        with self.assertRaises(AtomicError):
            scenarios(validate(graph(area("A", action="search"), area("B"),
                               edges=[{"from":"A","to":"B","on":"found"},
                                      {"from":"A","to":"B","on":"not_found"}])))

    def test_observed_branch_prunes_counterfactuals(self):
        g = validate(graph(area("A", action="search"), area("B", action="search"),
                           edges=[{"from":"A","to":"B","on":"found"}]))
        variants = scenarios(g, {"B":"found"})
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["events"], {"A":"found", "B":"found"})

    def test_unbounded_repeat_rejected(self):
        with self.assertRaises(AtomicError):
            validate(graph(area(), repeat="until_found"))

    def test_invalid_dynamic_region_not_silently_deferred(self):
        b = {"id":"B","action":"search","job":{"center_from":{"from_step":"A","field":"target_position"},"shape":"circle"}}
        with self.assertRaises(AtomicError):
            validate(graph(area("A", action="search"), b, edges=[{"from":"A","to":"B","on":"found"}]))

    def test_unknown_parameter_not_silently_dropped(self):
        b = {"id":"B","action":"track","params":{"custom_speed":100}}
        with self.assertRaises(AtomicError):
            validate(graph(b))


class NumericPlanningTests(unittest.TestCase):
    def test_50_and_100_metre_patrols_assemble(self):
        for height in (50, 100):
            with self.subTest(height=height):
                result = compute(graph(area(), flight_alt=height))
                self.assertEqual(result["planning_status"], "PLANNED", result["reply"])
                scenario = result["scenarios"][0]
                self.assertTrue(scenario["geometry_actions"]["validated"])
                self.assertEqual(scenario["geometry_plan"]["assignments"][0]["flight_alt"], height)
                self.assertFalse(result["submitted"])
                self.assertFalse(result["whole_mission_budget_complete"])

    def test_aircraft_altitude_limit_still_applies(self):
        with self.assertRaises(AtomicError) as error:
            compute(graph(area(), flight_alt=2501))
        self.assertEqual(error.exception.code, "CANDIDATE_EXCLUDED")

    def test_cross_aircraft_dependency_is_scheduled(self):
        b = area("B", "UAV-254")
        # Both aircraft are registered near this site.
        result = compute(graph(area("A"), b, edges=[{"from":"A","to":"B"}]))
        scenario = result["scenarios"][0]
        self.assertEqual(result["planning_status"], "PLANNED", result["reply"])
        a, b = scenario["timeline"]
        self.assertGreaterEqual(b["start_s"], a["finish_s"])

    def test_independent_aircraft_can_start_in_parallel(self):
        result = compute(graph(area("A"), area("B", "UAV-254")))
        scenario = result["scenarios"][0]
        self.assertEqual(result["planning_status"], "PLANNED", result["reply"])
        self.assertEqual([n["start_s"] for n in scenario["timeline"]], [0, 0])

    def test_same_aircraft_resource_serialization(self):
        result = compute(graph(area("A"), area("B")))
        scenario = result["scenarios"][0]
        a, b = scenario["timeline"]
        self.assertGreaterEqual(b["start_s"], a["finish_s"])
        self.assertIn({"from":"A","to":"B","uav_id":"UAV-016"}, scenario["resource_edges"])

    def test_unknown_target_preserved_not_substituted_with_region_center(self):
        target = {"from_step":"A","field":"target_position"}
        b = {"id":"B","action":"search","uav_id":"UAV-254", "job":{"center_from":target,"shape":"circle","radius":5}}
        result = compute(graph(area("A", action="search", search_timeout=30), b,
                               edges=[{"from":"A","to":"B","on":"found"}]))
        found = next(v for v in result["scenarios"] if v["events"]["A"] == "found")
        self.assertEqual(result["planning_status"], "CONDITIONAL", result["reply"])
        self.assertEqual([j["job_id"] for j in found["geometry_plan"]["jobs"]], ["A"])
        self.assertTrue(any(p.get("node_id") == "B" for p in found["pending"]))
        self.assertEqual(result["graph"]["nodes"][1]["job"]["center_from"], target)

    def test_bound_target_can_be_planned(self):
        b = {"id":"B","action":"search","uav_id":"UAV-254",
             "job":{"center_from":{"from_step":"A","field":"target_position"},"shape":"circle","radius":5,"search_timeout":30}}
        result = compute(graph(area("A", action="search", search_timeout=30), b,
                               edges=[{"from":"A","to":"B","on":"found"}], observed_events={"A":"found"},
                               runtime_bindings={"A":{"target_position":{"longitude":121.4421,"latitude":25.2395}}}))
        self.assertEqual(len(result["scenarios"]), 1)
        self.assertEqual([j["job_id"] for j in result["scenarios"][0]["geometry_plan"]["jobs"]], ["A", "B"])

    def test_video_and_custom_tracking_preserved(self):
        video = {"id":"V","action":"record_start","uav_id":"UAV-016","params":{"until_step":"T"}}
        tracking = {"id":"T","action":"track","uav_id":"UAV-016","params":{"duration_s":360}}
        result = compute(graph(area("A", action="search", search_timeout=30), video, tracking,
                               edges=[{"from":"A","to":"V","on":"found"},{"from":"V","to":"T"}]))
        found = next(v for v in result["scenarios"] if v["events"]["A"] == "found")
        self.assertIn("开启录像", result["reply"])
        self.assertIn("持续360秒", result["reply"])
        self.assertEqual(found["timeline"][-1]["duration_s"], 360)
        self.assertFalse(result["execution_contract"]["flatten_to_legacy_queue"])

    def test_deadline_detects_climb_and_return_not_just_horizontal_cost(self):
        result = compute(graph(area(), flight_alt=100, max_duration_s=10))
        scenario = result["scenarios"][0]
        self.assertLess(scenario["geometry_plan"]["assignments"][0]["estimated_time_s"], 10)
        self.assertEqual(result["planning_status"], "INFEASIBLE")
        self.assertTrue(any(v["code"] == "MISSION_DEADLINE" for v in scenario["violations"]))

    def test_long_tracking_counts_against_time_budget(self):
        tracking = {"id":"T","action":"track","uav_id":"UAV-016","params":{"duration_s":10000}}
        result = compute(graph(area("A"), tracking, edges=[{"from":"A","to":"T"}]))
        self.assertEqual(result["planning_status"], "INFEASIBLE", result["reply"])
        self.assertTrue(any(v["code"] == "TIME_BUDGET" for v in result["scenarios"][0]["violations"]))

    def test_search_timeout_reservation_is_not_proof_of_impossibility(self):
        result = compute(graph(area(action="search", search_timeout=100000)))
        self.assertEqual(result["planning_status"], "CONDITIONAL", result["reply"])
        self.assertTrue(any(p["code"] == "TIME_RESERVATION" for p in result["scenarios"][0]["pending"]))

    def test_dynamic_job_count_is_not_applied_to_only_the_known_prefix(self):
        b = {"id":"B","action":"search","uav_id":"UAV-254",
             "job":{"center_from":{"from_step":"A","field":"target_position"},"shape":"circle","radius":5}}
        result = compute(graph(area("A", action="search", search_timeout=30), b,
                               edges=[{"from":"A","to":"B","on":"found"}], constraints={"required_drones":2},
                               observed_events={"A":"found"}))
        found = result["scenarios"][0]
        self.assertEqual(result["planning_status"], "CONDITIONAL", result["reply"])
        self.assertTrue(any(p["code"] == "DEFERRED_FLEET_COUNT" for p in found["pending"]))

    def test_route_does_not_skip_a_disconnected_tracking_step(self):
        tracking = {"id":"T","action":"track","uav_id":"UAV-016","params":{"duration_s":30}}
        result = compute(graph(tracking, area("P")))
        self.assertEqual(result["planning_status"], "CONDITIONAL", result["reply"])
        self.assertIsNone(result["scenarios"][0]["geometry_plan"])

    def test_common_prefix_altitude_does_not_change_between_results(self):
        b = {"id":"B","action":"search","uav_id":"UAV-016",
             "job":{"center_from":{"from_step":"A","field":"target_position"},"shape":"circle","radius":5}}
        result = compute(graph(area("A", uid="UAV-254", action="search", search_timeout=30), b,
                               edges=[{"from":"A","to":"B","on":"found"}], flight_alt=100))
        heights = [next(a["flight_alt"] for a in v["geometry_plan"]["assignments"] if a["uav_id"] == "UAV-254")
                   for v in result["scenarios"]]
        self.assertEqual(heights, [100.3, 100.3, 100.3])

    def test_common_prefix_auto_executor_does_not_depend_on_unknown_result(self):
        a = area("A", action="search", search_timeout=30)
        a.pop("uav_id")
        result = compute(graph(a, area("B", "UAV-254"), uav_ids=["UAV-016", "UAV-254"],
                               edges=[{"from":"A","to":"B","on":"found"}]))
        owners = [tuple(v["actors"]["A"]) for v in result["scenarios"]]
        self.assertEqual(len(set(owners)), 1)


if __name__ == "__main__":
    unittest.main()
