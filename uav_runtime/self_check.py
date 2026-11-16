"""Check mock geometry, the real numerical solver and optionally the three databases."""
import argparse
import json
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
from uav_runtime.atomic.query import rows


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
                       "target-detection/scripts/validate_targets.py"):
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
    print(json.dumps({"status": "OK", "mock": True, "solver": result["solver"], "database_rows": facts,
                      "entrypoints_checked": entrypoints, "environment": environment_report()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "CHECK_FAILED", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
