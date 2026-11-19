"""Candidate screening uses the action converter's existing distance boundary."""
from copy import deepcopy
import importlib.util
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from uav_runtime.atomic.common import AtomicError
from uav_runtime.seven_inputs import prepare_candidates, resolve_jobs
from uav_runtime.seven_geometry import costs


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_candidate_test_validator", ROOT / "skills/flight-control/scripts/submit_queue_batch.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def candidate(uid, distance_m):
    return {"uav_id": uid, "origin": {"longitude": 120.0,
            "latitude": 24.0 - math.degrees(distance_m / validator.EARTH_RADIUS_M)},
            "battery_level": 90.0, "range_budget": 100000.0, "time_budget": 10000.0,
            "flight_remain": 110000.0, "max_altitude": 500.0, "max_speed": 20.0,
            "cruise_speed": 10.0}


class CandidateCoordinateTests(unittest.TestCase):
    def prepare(self, rows, center=True, named=False):
        selected = {"candidates": deepcopy(rows), "excluded": [], "reserve_battery_percent": 20}
        request = {"uav_ids": [r["uav_id"] for r in rows]} if named else {}
        if center:
            request.update(center_lon=120.0, center_lat=24.0)
        facts = {"fleet": [dict(row, has_status=True, has_resource=True) for row in rows],
                 "records": [{"max_range": 1000000.0}]}
        with patch("uav_runtime.atomic.query.compute", return_value=facts), \
             patch("uav_runtime.atomic.selection.compute", return_value=selected):
            return prepare_candidates(request)

    def test_converter_boundary_is_applied_before_costs(self):
        limit = validator.DEFAULT_MAX_DISTANCE_M
        for distance in (limit - 0.01, limit + 0.01, limit + 180):
            with self.subTest(distance=distance):
                row = candidate("test-uav", distance)
                if distance < limit:
                    result = self.prepare([row])
                    self.assertEqual(len(result["candidates"]), 1)
                    self.assertEqual(result["_summary"]["coordinate_filter"]["excluded_count"], 0)
                else:
                    with self.assertRaises(AtomicError) as error:
                        self.prepare([row])
                    self.assertEqual(error.exception.code, "COORDINATE_RANGE_EXCEEDED")
                    self.assertEqual(error.exception.details["coordinate_filter"]["max_distance_m"], limit)

    def test_mixed_pool_keeps_in_range_candidate_and_exclusion_evidence(self):
        near = candidate("near", 100.0)
        far = candidate("far", validator.DEFAULT_MAX_DISTANCE_M + 180)
        result = self.prepare([near, far])
        self.assertEqual([r["uav_id"] for r in result["candidates"]], ["near"])
        excluded = result["excluded"][0]
        self.assertEqual(excluded["uav_id"], "far")
        self.assertEqual(excluded["constraint"], "COORDINATE_RANGE")
        self.assertEqual(excluded["range_budget"], far["range_budget"])
        self.assertIn("exceeds --max-distance-m", excluded["message"])

    def test_unknown_center_does_not_invent_a_coordinate_constraint(self):
        row = candidate("test-uav", validator.DEFAULT_MAX_DISTANCE_M + 180)
        result = self.prepare([row], center=False)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["_summary"]["coordinate_filter"])

    def test_named_aircraft_is_not_silently_removed_from_the_request(self):
        rows = [candidate("near", 100.0), candidate("far", validator.DEFAULT_MAX_DISTANCE_M + 180)]
        with self.assertRaises(AtomicError) as error:
            self.prepare(rows, named=True)
        self.assertEqual(error.exception.code, "CANDIDATE_EXCLUDED")
        self.assertEqual(error.exception.details["excluded"][0]["uav_id"], "far")
        self.assertEqual(error.exception.details["available_count"], 1)

    def test_in_range_coordinate_does_not_bypass_range_budget(self):
        row = candidate("test-uav", 100.0)
        row["range_budget"] = 1.0
        prepared = self.prepare([row])
        jobs = resolve_jobs({"jobs": [{"center_lon": 120.0, "center_lat": 24.0,
                             "shape": "rectangle", "length": 12, "width": 8}]})
        with self.assertRaises(AtomicError) as error:
            costs({"candidates": prepared["candidates"], "jobs": jobs["jobs"]})
        self.assertEqual(error.exception.code, "NO_FEASIBLE_ALLOCATION")


if __name__ == "__main__":
    unittest.main()
