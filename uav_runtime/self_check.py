"""Check mock geometry, the real numerical solver and optionally the three databases."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav_runtime.area_route import query_area
from uav_runtime.direct_path import direct_route
from uav_runtime.atomic.allocation import compute
from uav_runtime.atomic.common import AtomicError
from uav_runtime.atomic.query import rows
from uav_runtime.atomic.selection import compute as select_candidates
from uav_runtime.planning_geometry import (region_route, search_coverage_m,
                                           search_lane_m)
from uav_runtime.capabilities import check_task
from uav_runtime.seven_geometry import build, costs


def environment_report():
    external = os.environ.get("MIGRATION_ROOT")
    external_root = Path(external).resolve() if external else None
    missing = []
    if external_root is None or not (external_root / "scripts/mission_client.py").is_file():
        missing.append("MIGRATION_ROOT/scripts/mission_client.py")
    for name in ("ROS_SETUP", "CATKIN_SETUP", "UAV_CONFIG"):
        value = os.environ.get(name)
        if not value or not Path(value).is_file():
            missing.append(name)
    if not os.environ.get("NANOBOT_SESSION_ID"):
        missing.append("NANOBOT_SESSION_ID")
    return {"workspace": str(ROOT), "python": sys.executable, "trajectory_backend": "mock",
            "map_used": False, "online_execution_configured": not missing,
            "online_execution_verified": False, "online_missing": missing,
            "perception_entrypoint_present": bool(external_root and
                (external_root / "scripts/uav_perception.py").is_file()),
            "semantic_map_provider_present": bool(external_root and
                (external_root / "uav_runtime/map_provider.py").is_file())}


def check_entrypoints():
    """Exercise the public launcher outside the workspace without startup variables."""
    environment = os.environ.copy()
    for name in ("MIGRATION_ROOT", "PLAN_AGENT_WORKSPACE", "PYTHONPATH", "UAV_DATA_DIR"):
        environment.pop(name, None)
    checked = []
    with tempfile.TemporaryDirectory(prefix="uav-entrypoint-check-") as directory:
        def invoke(script, *arguments, json_output=False):
            process = subprocess.run([sys.executable, str(ROOT / "uav_runtime/run.py"), script, *arguments],
                                     cwd=directory, env=environment, capture_output=True, text=True, timeout=30)
            if process.returncode:
                raise RuntimeError(script + ": " + (process.stderr or process.stdout).strip())
            checked.append(script)
            return json.loads(process.stdout) if json_output else None

        for script in ("data-query/scripts/prepare_candidates.py", "spatial-compute/scripts/resolve_jobs.py",
                       "mission-orchestration/scripts/allocate.py", "trajectory-generation/scripts/planning_routes.py",
                       "mission-planning/scripts/check_task.py", "target-detection/scripts/validate_targets.py"):
            invoke("skills/" + script, "--help")
        route = invoke("skills/trajectory-generation/scripts/mock_offline_map.py", "query-path",
                       "--start-lon", "0", "--start-lat", "0", "--end-lon", "0.001", "--end-lat", "0",
                       "--seed", "1", json_output=True)
        if (route.get("mock") is not True or route.get("map_used") is not False
                or route.get("submitted") is not False or route.get("validated_for_execution") is not False
                or not 110 < route["one_way_distance"] < 112):
            raise RuntimeError("离线航迹入口的计算或能力标记错误")
        area = invoke("skills/trajectory-generation/scripts/mock_offline_map.py", "query-area",
                      "--center-lon", "0", "--center-lat", "0", "--shape", "rectangle",
                      "--length", "8", "--width", "6", "--mode", "boundary", json_output=True)
        if (area.get("mock") is not True or not area["closed"]
                or abs(area["path_length"] - 28) > 0.01
                or area["waypoints"][0] != area["waypoints"][-1]):
            raise RuntimeError("离线区域入口未保留矩形闭合边界")
        batch = [{"cmd": "arm", "task_id": "self-check", "step_id": 1, "params": {}},
                 {"cmd": "takeoff", "task_id": "self-check", "step_id": 2, "params": {"altitude": 1.2}}]
        validation = invoke("skills/flight-control/scripts/submit_queue_batch.py", "--json", json.dumps(batch),
                            "--dry-run", json_output=True)
        if validation.get("validated") is not True or validation.get("submitted") is not False:
            raise RuntimeError("动作dry-run入口状态错误")
    return checked


def check_multi_step():
    """Exercise mixed patrol/search steps, per-step parameters and order."""
    lon, lat = 120.0, 24.0
    candidates = [{"uav_id": "CHECK-A", "origin": {"longitude": lon, "latitude": lat},
                   "range_budget": 10000.0, "time_budget": 10000.0,
                   "flight_remain": 10000.0, "battery": 100.0,
                   "max_altitude": 500.0, "max_speed": 5.0},
                  {"uav_id": "CHECK-B", "origin": {"longitude": lon, "latitude": lat},
                   "range_budget": 10000.0, "time_budget": 10000.0,
                   "flight_remain": 10000.0, "battery": 100.0,
                   "max_altitude": 500.0, "max_speed": 5.0}]
    jobs = [{"job_id": "J1", "name": "patrol", "center": [lon, lat],
             "shape": "rectangle", "length": 10, "width": 10,
             "task_type": "patrol", "laps": 2, "fixed_uav": "CHECK-A"},
            {"job_id": "J2", "name": "search", "center": [lon, lat],
             "shape": "rectangle", "length": 10, "width": 10,
             "task_type": "search", "search_timeout": 300, "fixed_uav": "CHECK-B"}]
    capability = check_task({"steps": [{"job_id": "J1", "task_type": "patrol", "laps": 2},
                                       {"job_id": "J2", "task_type": "search",
                                        "search_timeout": 300}], "ordered": True})
    if capability["route"] != "full" or capability["gaps"]:
        raise RuntimeError("多步骤能力检查未通过")
    estimate = costs({"operation": "costs", "candidates": candidates, "jobs": jobs,
                      "ordered": True})
    allocation = compute({"candidates": estimate["candidates"], "jobs": estimate["jobs"],
                          "costs": estimate["costs"], "constraints": estimate["constraints"]})
    plan = build({"operation": "build", "cost_estimate": estimate,
                  "allocation": allocation})
    steps = [(step["type"], step.get("laps"), step.get("timeout_sec"))
             for assignment in plan["assignments"] for step in assignment["steps"]
             if step["type"] in ("search", "patrol")]
    if sorted(step[0] for step in steps) != ["patrol", "search"]:
        raise RuntimeError("混合步骤未按类型生成")
    if not any(step[0] == "patrol" and step[1] == 2 for step in steps):
        raise RuntimeError("巡逻圈数未按步骤保留")
    if not any(step[0] == "search" and step[2] == 300 for step in steps):
        raise RuntimeError("搜索时限未按步骤保留")


def check_alternatives():
    """Default speed may fail while a bounded speed makes the task feasible."""
    def candidate(uid, max_speed, radius=None):
        return {"uav_id": uid, "origin": {"longitude": 0.0, "latitude": 0.0},
                "range_budget": 10000.0, "time_budget": 1000.0,
                "flight_remain": 10000.0, "battery": 100.0,
                "max_altitude": 500.0, "max_speed": max_speed,
                "max_flight_radius": radius}

    job = {"job_id": "J1", "name": "alternatives", "center": [0.01, 0.0],
           "shape": "rectangle", "length": 10, "width": 10,
           "task_type": "patrol", "laps": 1}
    try:
        costs({"operation": "costs", "candidates": [candidate("CHECK-SLOW", 10.0)],
               "jobs": [job]})
        raise RuntimeError("默认速度应当不可行")
    except AtomicError as exc:
        if exc.code != "NO_FEASIBLE_ALLOCATION":
            raise
        alternatives = exc.details["candidate_screening"]["alternatives"]
        if alternatives["feasible_count"] != 1:
            raise RuntimeError("默认速度失败时未给出可用速度备选")
        top = alternatives["items"][0]
        if not 2.0 < top["suggested_speed_mps"] < 3.0 or not top["feasible_with_speed"]:
            raise RuntimeError("速度备选的建议值不正确")
    try:
        costs({"operation": "costs", "candidates": [candidate("CHECK-MAX", 2.0)],
               "jobs": [job]})
        raise RuntimeError("最大速度不足时应当不可行")
    except AtomicError as exc:
        alternatives = exc.details["candidate_screening"]["alternatives"]
        if alternatives["feasible_count"] != 0:
            raise RuntimeError("最大速度不足时不应给出可行速度备选") from exc
    try:
        costs({"operation": "costs", "candidates": [candidate("CHECK-RADIUS", 10.0, 500.0)],
               "jobs": [job]})
        raise RuntimeError("超出最大飞行半径时应当不可行")
    except AtomicError as exc:
        if "最大飞行半径" not in str(exc):
            raise RuntimeError("最大飞行半径失败未给出明确原因") from exc

    # A circular search is executed as its axis-aligned box, so costing must
    # use the same geometry.
    circle = {**job, "shape": "circle", "radius": 5}
    circle.pop("length")
    circle.pop("width")
    box = region_route(circle, 1.2, "search")
    rectangle = region_route({**job, "shape": "rectangle", "length": 10, "width": 10}, 1.2, "search")
    if abs(box["path_length"] - rectangle["path_length"]) > 1e-6:
        raise RuntimeError("圆形搜索成本与执行搜索框不一致")

    fleet = [{"uav_id": "CHECK-RESERVE", "mission_status": "空闲", "battery_level": 80.0,
              "health_score": 90.0, "flight_remain": 10000.0, "max_altitude": 500.0,
              "max_endurance": 100.0, "max_speed": 10.0, "return_reserve": 25.0,
              "payload_status": "", "origin": {"longitude": 0.0, "latitude": 0.0}}]
    selected = select_candidates({"fleet": fleet, "flight_alt": 1.2})["candidates"][0]
    if abs(selected["range_budget"] - 6875.0) > 1e-6:
        raise RuntimeError("逐机返航预留未进入航程预算")
    if abs(selected["time_budget"] - 3300.0) > 1e-6:
        raise RuntimeError("逐机返航预留未进入时间预算")

    cruise = candidate("CHECK-CRUISE", 10.0)
    cruise["cruise_speed"] = 5.0
    result = costs({"operation": "costs", "candidates": [cruise], "jobs": [job]})
    if result["candidates"][0]["planning_speed"] != 5.0:
        raise RuntimeError("未指定速度时未使用 cruise_speed")
    overridden = costs({"operation": "costs", "candidates": [cruise], "jobs": [job],
                        "speed": 3.0})
    if overridden["candidates"][0]["planning_speed"] != 3.0:
        raise RuntimeError("用户指定速度未覆盖 cruise_speed")

    # The figure's example: 500x300, lane 75 m, 4 lanes, coverage 2,225 m.
    fov = math.degrees(2 * math.atan(75 / (2 * 50)))
    if abs(search_lane_m(50, fov) - 75) > 1e-6:
        raise RuntimeError("航带公式与图示不一致")
    if abs(search_coverage_m(500, 300, 75) - 2225) > 1e-6:
        raise RuntimeError("覆盖航线公式与图示不一致")
    if abs(search_lane_m(1.2, 89) - 2.358473431477656) > 1e-6:
        raise RuntimeError("任务航高下的航带计算不正确")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", action="store_true")
    parser.add_argument("--environment", action="store_true", help="只检查本地路径与在线配置，不连接服务")
    args = parser.parse_args()
    if args.environment:
        print(json.dumps({"status": "OK", "environment": environment_report()}, ensure_ascii=False))
        return 0
    entrypoints = check_entrypoints()
    route = direct_route((0, 0), (0.001, 0), 1.2)
    if len(route["go_waypoints"]) != 5 or route["one_way_distance"] <= 0:
        raise RuntimeError("mock 点对点航线校验失败")
    area = query_area(SimpleNamespace(center_lon=0, center_lat=0, center_alt=0, flight_alt=1.2,
                                     shape="circle", radius=5, lane=2, mode="coverage"))
    if len(area["waypoints"]) < 2:
        raise RuntimeError("mock 区域航线校验失败")
    result = compute({"candidates": [{"uav_id": "CHECK", "range_budget": 100, "time_budget": 100}],
                      "jobs": [{"job_id": "CHECK"}],
                      "costs": {"transfer_range": {"CHECK": [[0, 5], [5, 0]]},
                                "transfer_time": {"CHECK": [[0, 5], [5, 0]]},
                                "work_range": [10], "work_time": [10], "reachable": {"CHECK": ["CHECK"]}}})
    if not result["assignments"] or not result["solver_result"]["validation"]["passed"]:
        raise RuntimeError("数值分配求解校验失败")
    check_multi_step()
    check_alternatives()
    facts = {}
    if args.data:
        required = {
            "geo_library": {"region_name", "longitude", "latitude"},
            "uav_status": {"uav_id", "longitude", "latitude", "mission_status", "battery_level",
                           "health_score", "flight_remain", "payload_status"},
            "uav_resource": {"uav_id", "max_endurance", "max_altitude", "max_speed"},
        }
        for table, fields in required.items():
            records = rows(table)
            facts[table] = len(records)
            if records and fields - records[0].keys():
                raise ValueError(f"{table} 缺少字段 {sorted(fields - records[0].keys())}")
        if not all(facts.values()):
            raise ValueError("三个库都需要有可用记录")
    print(json.dumps({"status": "OK", "mock": True, "solver": result["solver"],
                      "multi_step": "ok", "database_rows": facts,
                      "entrypoints_checked": entrypoints, "environment": environment_report()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "CHECK_FAILED", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
