"""Additive planning-only orchestration over the existing numeric mission pipeline.

Conditional graphs are not flattened into a queue_batch. Unknown target locations
remain references; route evidence only certifies the static prefix actually built.
"""
from copy import deepcopy
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

from .atomic.common import AtomicError, finite, require
from .atomic.query import compute as query
from .atomic.allocation import compute as allocate
from .direct_path import direct_route
from .mission import constraints_for
from .mission_models import digest
from .planning_geometry import lon_lat, region_route, search_box
from .seven_inputs import prepare_candidates, resolve_jobs
from .seven_geometry import costs, build
from .task_graph import (validate, scenarios, bind, references, GEOMETRY_ACTIONS,
                         FLIGHT_ACTIONS, LABELS, EVENT_LABELS)


def _issue(code, message, node_id=None):
    result = {"code": code, "message": message}
    if node_id:
        result["node_id"] = node_id
    return result


def _haversine_m(lon1, lat1, lon2, lat2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _show_actions_flag():
    scripts = Path(__file__).resolve().parents[1] / "skills/flight-control/scripts"
    spec = importlib.util.spec_from_file_location("_graph_assemble_flag", scripts / "assemble_plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts))
    try:
        spec.loader.exec_module(module)
        return bool(getattr(module, "SHOW_ACTIONS", False))
    except Exception:
        return False
    finally:
        if str(scripts) in sys.path:
            sys.path.remove(str(scripts))


def _compact_issue(issue):
    out = {k: issue[k] for k in ("code", "message", "node_id") if k in issue}
    if "max_distance_m" in issue:
        out["max_distance_m"] = issue["max_distance_m"]
    if "coordinate_filter" in issue and isinstance(issue["coordinate_filter"], dict):
        out["coordinate_filter"] = issue["coordinate_filter"]
    if "available_count" in issue:
        out["available_count"] = issue["available_count"]
    if "max_recorded_altitude" in issue:
        out["max_recorded_altitude"] = issue["max_recorded_altitude"]
    screening = issue.get("candidate_screening")
    if isinstance(screening, dict):
        cs = {}
        for k in ("input_count", "total_candidates", "retained_count", "retained_uav_ids",
                  "reachable_count", "cost_excluded_count",
                  "diagnosis", "reasons", "range_ok_but_time_fail"):
            if k in screening:
                cs[k] = screening[k]
        if isinstance(screening.get("alternatives"), dict):
            alt = screening["alternatives"]
            cs["alternatives"] = {
                "feasible_count": alt.get("feasible_count", 0),
                "items": (alt.get("items") or [])[:5],
            }
        if isinstance(screening.get("altitude_alternatives"), dict):
            aalt = screening["altitude_alternatives"]
            cs["altitude_alternatives"] = {
                "default_altitude_m": aalt.get("default_altitude_m"),
                "items": (aalt.get("items") or [])[:5],
            }
        if cs:
            out["candidate_screening"] = cs
    return out


def _compact_waypoints(waypoints):
    return {
        "count": len(waypoints),
        "first": waypoints[0] if waypoints else None,
        "last": waypoints[-1] if waypoints else None,
    }


def _resolve_transit_dest(node, places):
    """Extract (lon, lat) for a transit node from params text or geo_library."""
    params = node.get("params") or {}
    texts = [str(params.get(k) or "") for k in ("description", "target")]
    for text in texts:
        m = re.search(r"(-?\d{1,3}\.\d+)\s*[,，]\s*(-?\d{1,2}\.\d+)", text)
        if m:
            lon, lat = float(m.group(1)), float(m.group(2))
            if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
                return (lon, lat)
    target = str(params.get("target") or "").strip()
    if target and places:
        for p in places:
            name = str(p.get("region_name") or "").strip()
            aliases = [a.strip() for a in str(p.get("alias") or "").split(";") if a.strip()]
            if target == name or target in aliases:
                lon = p.get("region_center_longitude") if p.get("region_center_longitude") is not None else p.get("longitude")
                lat = p.get("region_center_latitude") if p.get("region_center_latitude") is not None else p.get("latitude")
                if lon is not None and lat is not None:
                    return (float(lon), float(lat))
        # Substring fallback when target carries suffix like "上空" or "处"
        for p in places:
            name = str(p.get("region_name") or "").strip()
            if name and name in target:
                lon = p.get("region_center_longitude") if p.get("region_center_longitude") is not None else p.get("longitude")
                lat = p.get("region_center_latitude") if p.get("region_center_latitude") is not None else p.get("latitude")
                if lon is not None and lat is not None:
                    return (float(lon), float(lat))
    return None


def _uav_origin(record):
    if not isinstance(record, dict):
        return None
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("longitude") is not None and origin.get("latitude") is not None:
        return (float(origin["longitude"]), float(origin["latitude"]))
    if record.get("longitude") is not None and record.get("latitude") is not None:
        return (float(record["longitude"]), float(record["latitude"]))
    return None


def _synthesize_scenario_actions(request, graph, variant, plan, geometry_actions, actors, fleet, places, altitude_order):
    """Assemble standardized compact action JSON per UAV for every planned scenario."""
    if variant.get("planning_status") in ("INFEASIBLE", "BLOCKED"):
        return {}
    geom_reports = {
        p["uav_id"]: p["actions"]
        for p in (((geometry_actions or {}).get("_summary") or {}).get("participants") or [])
        if isinstance(p, dict) and "uav_id" in p and "actions" in p
    } if isinstance(geometry_actions, dict) else {}
    active = variant.get("active") or []
    nodes = graph["indexed"]
    bindings = request.get("runtime_bindings", {})
    plan_task_key = (plan or {}).get("task_key")
    if not plan_task_key:
        sig = digest({"nodes": [nodes[n] for n in active], "scenario": variant.get("scenario_id")})[:4]
        plan_task_key = f"TG{datetime.now().strftime('%m%d%H%M%S')}{sig}"

    uav_nodes = {}
    for nid in active:
        for uid in actors.get(nid) or []:
            uav_nodes.setdefault(uid, []).append(nid)

    synthesized = {}
    base_alt = float(graph.get("flight_alt", 1.2))
    order = list(altitude_order or [])
    for uid, nids in uav_nodes.items():
        if uid not in order:
            order.append(uid)
        flight_alt = round(base_alt + 0.3 * order.index(uid), 3)
        rec = fleet.get(uid) or {}
        speed = request.get("speed")
        if speed is None:
            speed = rec.get("planning_speed") or rec.get("cruise_speed")
        if speed is not None:
            speed = round(float(speed), 3)

        # Reuse exact geometry_actions if this UAV only has geometry nodes already assembled.
        if uid in geom_reports and all(nodes[nid]["action"] in GEOMETRY_ACTIONS for nid in nids):
            synthesized[uid] = {
                "uav_id": uid,
                "flight_alt": flight_alt,
                "speed": speed,
                "nodes": nids,
                "actions": deepcopy(geom_reports[uid]),
            }
            continue

        task_id = f"{plan_task_key}-{uid}"
        raw = []

        def add(cmd, params):
            p = deepcopy(params)
            if speed is not None and cmd in ("takeoff", "follow_path", "patrol_move", "fuel_explore"):
                p.setdefault("max_vel" if cmd == "fuel_explore" else "speed", speed)
            raw.append({"cmd": cmd, "task_id": task_id, "step_id": len(raw) + 1, "params": p})

        origin = _uav_origin(rec)
        current_pos = origin
        did_return = False
        did_land = False
        total_range_m = 0.0

        add("arm", {})
        add("takeoff", {"altitude": flight_alt})

        for nid in nids:
            node = nodes[nid]
            kind = node["action"]
            params = bind(node.get("params") or {}, bindings)
            if kind == "transit":
                dest = _resolve_transit_dest(node, places)
                if dest is not None and current_pos is not None:
                    leg = direct_route(current_pos, dest, flight_alt)
                    total_range_m += leg["one_way_distance"]
                    add("follow_path", {"waypoints": _compact_waypoints(leg["go_waypoints"]), "skip_unreachable": False})
                    current_pos = dest
                else:
                    add("follow_path", {"target": params.get("target") or params.get("description") or nid, "skip_unreachable": False})
            elif kind in GEOMETRY_ACTIONS:
                job = deepcopy(node.get("job") or {})
                has_unbound = bool(list(references(bind(job, bindings))))
                if not has_unbound:
                    if "center_from" in job:
                        pos = bind(job.pop("center_from"), bindings)
                        if isinstance(pos, dict) and "longitude" in pos and "latitude" in pos:
                            job.update(center_lon=pos["longitude"], center_lat=pos["latitude"])
                    job.update(job_id=nid, task_type=kind)
                    try:
                        zone = resolve_jobs({"jobs": [job], "places": places})["jobs"][0]
                        route = region_route(zone, flight_alt, kind, zone.get("search_lane_m"))
                        entry = lon_lat(route["waypoints"][0])
                        if current_pos is not None:
                            leg = direct_route(current_pos, entry, flight_alt)
                            total_range_m += leg["one_way_distance"]
                            add("follow_path", {"waypoints": _compact_waypoints(leg["go_waypoints"]), "skip_unreachable": False})
                        if kind == "search":
                            bmin, bmax = search_box(zone, flight_alt)
                            timeout_sec = float(job.get("search_timeout") or request.get("search_timeout") or 1200)
                            total_range_m += route["path_length"]
                            add("fuel_explore", {"box_min": bmin, "box_max": bmax, "timeout_sec": timeout_sec})
                        else:
                            laps = int(zone.get("laps") or 1)
                            total_range_m += route["path_length"] * laps
                            wview = _compact_waypoints(route["waypoints"])
                            for _ in range(laps):
                                add("patrol_move", {"waypoints": wview, "skip_unreachable": False})
                        current_pos = lon_lat(route["waypoints"][-1])
                    except Exception:
                        add("fuel_explore" if kind == "search" else "patrol_move", {"job_id": nid, "job": job})
                else:
                    add("fuel_explore" if kind == "search" else "patrol_move", {"job_id": nid, "job": job})
            elif kind in ("hover", "wait"):
                # 与 submit_queue_batch 校验契约保持一致：hover.params 必须为空对象
                add("hover", {})
            elif kind == "return":
                if current_pos is not None and origin is not None and current_pos != origin:
                    leg = direct_route(current_pos, origin, flight_alt)
                    total_range_m += leg["one_way_distance"]
                    add("follow_path", {"waypoints": _compact_waypoints(leg["go_waypoints"]), "skip_unreachable": False})
                    current_pos = origin
                did_return = True
            elif kind == "land":
                add("land", {})
                did_land = True
            else:
                add(kind, params)

        explicit_rth = request.get("return_to_home") is True
        if did_return and not did_land:
            add("land", {})
        elif not did_land and not did_return:
            if explicit_rth:
                if current_pos is not None and origin is not None and current_pos != origin:
                    leg = direct_route(current_pos, origin, flight_alt)
                    total_range_m += leg["one_way_distance"]
                    add("follow_path", {"waypoints": _compact_waypoints(leg["go_waypoints"]), "skip_unreachable": False})
                add("land", {})
            else:
                # 盘旋/悬停/待命及未指定结束动作：不返航、只算单程，任务结束后原地悬停
                if not raw or raw[-1]["cmd"] != "hover":
                    add("hover", {})

        synthesized[uid] = {
            "uav_id": uid,
            "flight_alt": flight_alt,
            "speed": speed,
            "nodes": nids,
            "estimated_range_m": round(total_range_m, 3) if total_range_m > 0 else None,
            "return_to_home": bool(did_return or explicit_rth),
            "actions": raw,
        }
    return synthesized


def _scenario_summary(variant):
    plan = variant.get("geometry_plan") or {}
    actions = variant.get("geometry_actions")
    synthesized = variant.get("synthesized_actions") or {}
    budgets = {b["uav_id"]: b for b in variant.get("aircraft_budgets") or [] if isinstance(b, dict) and "uav_id" in b}
    action_reports = {
        p["uav_id"]: p["actions"]
        for p in ((actions.get("_summary") or {}).get("participants") or [])
        if isinstance(p, dict) and "uav_id" in p and "actions" in p
    } if isinstance(actions, dict) else {}
    for uid, info in synthesized.items():
        action_reports[uid] = info["actions"]
    job_centers = {j["job_id"]: j.get("center") for j in plan.get("jobs") or [] if isinstance(j, dict) and "job_id" in j}
    assignments = []
    seen_uavs = set()
    for row in plan.get("assignments") or []:
        uid = row["uav_id"]
        seen_uavs.add(uid)
        b = budgets.get(uid, {})
        origin = row.get("origin") or {}
        dists = {}
        if "longitude" in origin and "latitude" in origin:
            for jid in row.get("jobs") or []:
                c = job_centers.get(jid)
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    dists[jid] = round(_haversine_m(origin["longitude"], origin["latitude"], c[0], c[1]), 1)
        item = {
            "uav_id": uid,
            "flight_alt": row.get("flight_alt"),
            "speed": row.get("speed"),
            "jobs": row.get("jobs"),
            "battery_level": row.get("battery_level"),
            "range_budget_m": row.get("range_budget"),
            "time_budget_s": row.get("time_budget"),
            "estimated_range_m": row.get("estimated_range_m"),
            "estimated_battery_after": row.get("estimated_battery_after"),
            "reserved_airborne_time_s": b.get("reserved_airborne_time_s"),
            "within_time_budget": b.get("within_time_budget"),
        }
        if dists:
            item["takeoff_to_jobs_m"] = dists
        if uid in action_reports:
            item["actions"] = action_reports[uid]
        assignments.append(item)
    for uid, info in synthesized.items():
        if uid in seen_uavs:
            continue
        b = budgets.get(uid, {})
        item = {
            "uav_id": uid,
            "flight_alt": info.get("flight_alt"),
            "speed": info.get("speed"),
            "jobs": info.get("nodes"),
            "time_budget_s": b.get("time_budget_s"),
            "return_to_home": info.get("return_to_home"),
            "actions": info["actions"],
        }
        if info.get("estimated_range_m") is not None:
            item["estimated_range_m"] = info["estimated_range_m"]
        assignments.append(item)
    return {
        "scenario_id": variant.get("scenario_id"),
        "events": variant.get("events"),
        "planning_status": variant.get("planning_status"),
        "task_key": plan.get("task_key"),
        "reserved_makespan_s": variant.get("reserved_makespan_s"),
        "within_deadline": variant.get("within_deadline"),
        "actions_validated": actions.get("validated") if isinstance(actions, dict) else None,
        "assignments": assignments,
        "violations": [_compact_issue(v) for v in variant.get("violations") or []],
        "pending": [_compact_issue(p) for p in variant.get("pending") or []],
    }


def _compact_scenarios(variants):
    """Compact multi-branch summaries to stay under tool-result size limits."""
    items = [_scenario_summary(v) for v in variants]
    if len(items) <= 3:
        return items
    seen = {field: {} for field in ("assignments", "violations", "pending")}
    for item in items:
        for field in ("assignments", "violations", "pending"):
            val = item.get(field)
            if not val:
                continue
            sig = json.dumps(val, ensure_ascii=False, sort_keys=True)
            if sig in seen[field]:
                item[f"same_{field}_as"] = seen[field][sig]
                item.pop(field, None)
            else:
                seen[field][sig] = item["scenario_id"]
    return items


def _assemble(plan):
    # Reuse the existing dry-run validator, without teaching flight control any
    # new commands or publishing interface/conditional nodes as a flat queue.
    scripts = Path(__file__).resolve().parents[1] / "skills/flight-control/scripts"
    spec = importlib.util.spec_from_file_location("_graph_assemble_plan", scripts / "assemble_plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts))
    try:
        spec.loader.exec_module(module)
        return module.compute({"plan": plan})
    finally:
        sys.path.remove(str(scripts))


def _routes(request, graph, active, locked_owners=None, altitude_order=None):
    """Plan all known, connected jobs jointly; preserve uncertain continuations."""
    jobs, pending, blocked, terminal_actors, moved_actors = [], [], set(), set(), set()
    nodes = graph["indexed"]
    bindings = request.get("runtime_bindings", {})
    for nid in active:
        node = nodes[nid]
        bound = bind(node, bindings)
        uncertain_parent = any(e["from"] in blocked for e in graph["incoming"][nid])
        unbound = bool(list(references(bound)))
        # A moving target, point flight or intervening landing can change the
        # next leg's origin. Never connect later jobs as if this step vanished.
        changes_origin = node["action"] in {"track", "transit"}
        if node["action"] in {"return", "land"}:
            terminal_actors.add(node.get("uav_id"))
        affected = terminal_actors | moved_actors
        if node["action"] in GEOMETRY_ACTIONS and (None in affected or node.get("uav_id") in affected
                                                   or (node.get("uav_id") is None and affected)):
            uncertain_parent = True
        if unbound or uncertain_parent:
            if node["action"] in FLIGHT_ACTIONS or uncertain_parent:
                blocked.add(nid)
                moved_actors.add(node.get("uav_id"))
            pending.append(_issue("RUNTIME_GEOMETRY", "需要触发结果或前序实际位置后继续计算", nid))
            continue
        if node["action"] in GEOMETRY_ACTIONS:
            job = deepcopy(node["job"])
            if "center_from" in job:
                position = bind(job.pop("center_from"), bindings)
                if set(position) != {"longitude", "latitude"}:
                    raise AtomicError("INVALID_ARGUMENT", "center_from须引用目标经纬度", node_id=nid)
                if "center_lon" in job or "center_lat" in job or "place" in job:
                    raise AtomicError("INVALID_ARGUMENT", "center_from不能与其他位置同时指定", node_id=nid)
                job.update(center_lon=position["longitude"], center_lat=position["latitude"])
            job.update(job_id=nid, task_type=node["action"])
            if node.get("uav_id"):
                job["uav_id"] = node["uav_id"]
            jobs.append(job)
        if changes_origin:
            blocked.add(nid)
            moved_actors.add(node.get("uav_id"))
            if node["action"] in {"track", "transit"}:
                pending.append(_issue("RUNTIME_TRAJECTORY", "保留该飞行步骤，实际路径确定后补算航程", nid))
    if not jobs:
        if "required_drones" in request.get("constraints", {}):
            pending.append(_issue("DEFERRED_FLEET_COUNT", "完整出动数量约束已保留，尚无可联合分配的已知区域作业"))
        return None, None, None, pending
    candidate_args = {k: request[k] for k in ("flight_alt", "uav_ids", "exclude_uavs", "payload") if k in request}
    prepared = prepare_candidates(candidate_args)
    places = query({"entity": "places"})["places"] if any("place" in j for j in jobs) else []
    resolved = resolve_jobs({"jobs": jobs, "places": places})
    parameters = {k: request[k] for k in ("flight_alt", "speed") if k in request}
    estimate = costs(dict(parameters, ordered=True, candidates=prepared["candidates"], jobs=resolved["jobs"]))
    supplied = deepcopy(request.get("constraints", {}))
    require(supplied, "constraints", dict)
    known = {j["job_id"] for j in jobs}
    if "required_drones" in supplied and any(graph["indexed"][nid]["action"] in GEOMETRY_ACTIONS
                                            and nid not in known for nid in active):
        supplied.pop("required_drones")
        pending.append(_issue("DEFERRED_FLEET_COUNT", "完整出动数量约束已保留，待动态作业补齐后联合核验"))
    for key in ("fixed_assignments", "allowed_assignments"):
        if key in supplied:
            require(supplied[key], key, dict)
            supplied[key] = {j: value for j, value in supplied[key].items() if j in known}
    if "single_owner_jobs" in supplied:
        require(supplied["single_owner_jobs"], "single_owner_jobs", list)
        supplied["single_owner_jobs"] = [j for j in supplied["single_owner_jobs"] if j in known]
    constraints = constraints_for(estimate["constraints"], supplied)
    locks = {nid: owners for nid, owners in (locked_owners or {}).items() if nid in known}
    for nid, owners in locks.items():
        if len(owners) == 1:
            fixed = constraints.setdefault("fixed_assignments", {})
            if nid in fixed and fixed[nid] != owners[0]:
                raise AtomicError("INVALID_ARGUMENT", "公共步骤机号与明确归属冲突", node_id=nid)
            fixed[nid] = owners[0]
        else:
            allowed = constraints.setdefault("allowed_assignments", {})
            allowed[nid] = sorted(set(allowed.get(nid, owners)) & set(owners))
    args = {k: estimate[k] for k in ("candidates", "jobs", "costs")}
    args["constraints"] = constraints
    if "solver_options" in request:
        args["solver_options"] = request["solver_options"]
    try:
        allocation = allocate(args)
    except AtomicError as exc:
        if locks and exc.code == "NO_FEASIBLE_ALLOCATION":
            raise AtomicError("PREFIX_ALLOCATION_UNCONFIRMED", "固定公共步骤的机号后，本分支尚未找到联合方案；不能据此判定所有分配均无解",
                              cause=_failure(exc)) from exc
        raise
    for nid, owners in allocation["owners"].items():
        if nid in locks and set(owners) != set(locks[nid]):
            pending.append(_issue("SHARED_PREFIX_ALLOCATION", "分支间公共步骤的共担机组不同，尚需联合分配；不能按不同机组执行同一公共步骤", nid))
    order = altitude_order if altitude_order is not None else []
    for uid in sorted(row["uav_id"] for row in allocation["assignments"]):
        if uid not in order:
            order.append(uid)
    plan = build({"cost_estimate": estimate, "allocation": allocation,
                  "search_timeout": request.get("search_timeout", 1200), "step_metrics": True,
                  "altitude_order": order})
    if locked_owners is not None:
        for nid, owners in allocation["owners"].items():
            locked_owners.setdefault(nid, list(owners))
    try:
        actions = _assemble(plan)
    except AtomicError as exc:
        # Keep successful route/allocation evidence when the old action
        # validator rejects the preview (for example its 10 km limit).
        actions = {"status": exc.code, "validated": False, "submitted": False,
                   "message": str(exc), "details": exc.details}
    return plan, actions, estimate, pending


def _actors(graph, active, plan):
    actors = {nid: ([graph["indexed"][nid]["uav_id"]] if graph["indexed"][nid].get("uav_id") else [])
              for nid in active}
    for row in (plan or {}).get("assignments", []):
        for nid in row["jobs"]:
            if row["uav_id"] not in actors[nid]:
                actors[nid].append(row["uav_id"])
    # Infer interface executor only when unique. An unresolved cross-UAV
    # executor must never silently inherit an arbitrary aircraft.
    for nid in active:
        if actors[nid] or graph["indexed"][nid]["action"] == "report":
            continue
        parents = {uid for e in graph["incoming"][nid] for uid in actors.get(e["from"], [])}
        if len(parents) == 1:
            actors[nid] = sorted(parents)
    return actors


def _timing(request, graph, variant, plan, fleet, pending):
    active, nodes = variant["active"], graph["indexed"]
    actors = _actors(graph, active, plan)
    metrics, rows, tail, climb, descent = {}, {}, {}, {}, {}
    for row in (plan or {}).get("assignments", []):
        uid = row["uav_id"]
        rows[uid] = row
        transit = 0.0
        for step in row["steps"]:
            if step["type"] == "transit":
                transit = step["estimated_time_s"]
                if step["to"] == "home":
                    tail[uid] = transit
            else:
                duration = step["estimated_time_s"]
                # Reserve the full search timeout, instead of treating a short
                # strip estimate as a guarantee about autonomous search time.
                if step["type"] == "search":
                    duration = max(duration, step["timeout_sec"])
                metrics[(step["job_id"], uid)] = transit + duration
        for target, field in ((climb, "max_up_speed"), (descent, "max_down_speed")):
            speed = fleet[uid].get(field)
            if type(speed) in (int, float) and speed > 0:
                target[uid] = row["flight_alt"] / speed
            else:
                target[uid] = None
                pending.append(_issue("MISSING_VERTICAL_SPEED", f"{uid}缺少有效{field}，不能算完整起降时间"))
    clock, resource, first_flight, last_flight, timeline = {}, {}, {}, {}, []
    has_takeoff, resource_edges, violations = set(), [], []
    for nid in active:
        node, kind = nodes[nid], nodes[nid]["action"]
        edges = [e for e in graph["incoming"][nid] if e["from"] in active and
                 (e["on"] == "completed" or variant["events"].get(e["from"]) == e["on"])]
        times = [clock[e["from"]] for e in edges]
        if not times:
            ready = 0.0
        elif node["join"] == "any":
            # Unknown event time can precede a known one; avoid presenting a
            # known clock as the chosen winner of an unresolved race.
            ready = None if None in times else min(times)
        else:
            ready = None if None in times else max(times)
        durations, starts, finishes = [], [], []
        who = actors[nid]
        if not who and kind != "report":
            pending.append(_issue("NEEDS_EXECUTOR", "该步骤的执行机尚未唯一确定", nid))
        for uid in who or [None]:
            duration = node["params"].get("duration_s")
            if kind in GEOMETRY_ACTIONS:
                duration = metrics.get((nid, uid))
            elif kind == "return":
                duration = tail.get(uid)
            elif kind == "land":
                duration = descent.get(uid)
            elif duration is None and kind in {"track", "observe", "hover", "wait", "scan", "transit"}:
                pending.append(_issue("NEEDS_DURATION", "该步骤需要持续时间或实际路径耗时", nid))
            elif duration is None:
                duration = 0.0  # Control-interface latency is not calibrated.
            if duration is None:
                pending.append(_issue("RUNTIME_TIMING", "该步骤的完整耗时尚待计算", nid))
            start = ready
            if uid and kind in FLIGHT_ACTIONS:
                if uid in resource:
                    last, available = resource[uid]
                    resource_edges.append({"from": last, "to": nid, "uav_id": uid})
                    start = None if start is None or available is None else max(start, available)
                if kind in GEOMETRY_ACTIONS and uid not in has_takeoff:
                    first_flight[uid] = start
                    duration = None if duration is None or climb.get(uid) is None else duration + climb[uid]
                    has_takeoff.add(uid)
                end = None if start is None or duration is None else start + duration
                resource[uid] = (nid, end)
                last_flight[uid] = end
            else:
                end = None if start is None or duration is None else start + duration
            starts.append(start)
            finishes.append(end)
            durations.append(duration)
        clock[nid] = None if None in finishes else max(finishes)
        timeline.append({"node_id": nid, "action": kind, "uav_ids": who,
                         "start_s": None if None in starts else min(starts), "finish_s": clock[nid],
                         "duration_s": None if None in durations else max(durations),
                         "depends_on": edges, "params": bind(node["params"], request.get("runtime_bindings", {}))})
    budgets, finish_times = [], list(clock.values())
    for uid, row in rows.items():
        actor_nodes = [nid for nid in active if uid in actors[nid]]
        ends = [clock[nid] for nid in actor_nodes]
        final = None if None in ends else max(ends)
        explicit_return = any(nodes[nid]["action"] == "return" for nid in actor_nodes)
        explicit_land = any(nodes[nid]["action"] == "land" for nid in actor_nodes)
        extras = (0 if explicit_return else tail.get(uid, 0))
        down = 0 if explicit_land else descent.get(uid)
        final = None if final is None or down is None else final + extras + down
        start = first_flight.get(uid)
        duration = None if final is None or start is None else final - start
        within = None if duration is None else duration <= row["time_budget"] + 1e-6
        mandatory_duration = sum(nodes[nid]["params"].get("duration_s", 0) for nid in actor_nodes
                                 if nodes[nid]["action"] in FLIGHT_ACTIONS - GEOMETRY_ACTIONS)
        if within is False:
            if mandatory_duration > row["time_budget"] + 1e-6:
                violations.append(_issue("TIME_BUDGET", f"{uid}仅明确要求的持续时间已超过预留后预算"))
            else:
                # Exceeding a reserved search upper bound does not prove the
                # aircraft cannot finish sooner. Preserve that distinction.
                pending.append(_issue("TIME_RESERVATION", f"{uid}按搜索时限、起降及等待预留的时间超预算，尚不能确认整任务时长；可另算缩短搜索时限的备选"))
        budgets.append({"uav_id": uid, "flight_alt": row["flight_alt"],
                        "horizontal_range_m": row["estimated_range_m"], "range_budget_m": row["range_budget"],
                        "reserved_airborne_time_s": duration, "time_budget_s": row["time_budget"],
                        "within_time_budget": within, "whole_mission_energy_complete": False})
        finish_times.append(final)
    # Interface-only actors still consume endurance. Even when their route is
    # unknown, an explicit tracking/hover duration can already exceed budget.
    other_airborne = {uid for nid in active if nodes[nid]["action"] in FLIGHT_ACTIONS
                      for uid in actors[nid]} - set(rows)
    for uid in sorted(other_airborne):
        actor_nodes = [nid for nid in active if uid in actors[nid] and nodes[nid]["action"] in FLIGHT_ACTIONS]
        duration_lower_bound = sum(nodes[nid]["params"].get("duration_s", 0) for nid in actor_nodes)
        budget = fleet[uid].get("time_budget")
        within = False if budget is not None and duration_lower_bound > budget + 1e-6 else None
        if within is False:
            violations.append(_issue("TIME_BUDGET", f"{uid}仅明确要求的持续时间已超过预留后预算"))
        pending.append(_issue("RUNTIME_GEOMETRY", f"{uid}的完整往返路径和起降时间尚待计算"))
        budgets.append({"uav_id": uid, "known_duration_lower_bound_s": duration_lower_bound,
                        "time_budget_s": budget, "within_time_budget": within,
                        "horizontal_range_m": None, "whole_mission_energy_complete": False})
    mission_time = None if None in finish_times else max(finish_times, default=0)
    deadline = request.get("max_duration_s")
    deadline_ok = None if deadline is None or mission_time is None else mission_time <= deadline + 1e-6
    if deadline_ok is False:
        mandatory_lower = max([0] + [(climb.get(uid) or 0) + (descent.get(uid) or 0) for uid in rows] +
                              [sum(nodes[nid]["params"].get("duration_s", 0) for nid in active
                                   if uid in actors[nid] and nodes[nid]["action"] in FLIGHT_ACTIONS - GEOMETRY_ACTIONS)
                               for uid in {u for values in actors.values() for u in values}])
        if mandatory_lower > deadline + 1e-6:
            violations.append(_issue("MISSION_DEADLINE", "仅必要起降或明确持续时间已超过用户总时限"))
        else:
            pending.append(_issue("DEADLINE_RESERVATION", "组合任务的时间预留超过总时限，当前方案不能保证按时完成；不能据此断言所有方案都不可行"))
    return {"timeline": timeline, "resource_edges": resource_edges, "aircraft_budgets": budgets,
            "reserved_makespan_s": mission_time, "within_deadline": deadline_ok,
            "violations": violations, "actors": actors}


def _failure(exc):
    """Lead with the requested aircraft's evidence; alternatives change intent."""
    result = {"code": exc.code, "message": str(exc), **exc.details}
    fixed = exc.details.get("fixed_assignments_infeasible", [])
    if fixed:
        messages = []
        for row in fixed:
            failed = []
            if not row.get("radius_ok", True):
                failed.append(f"最远作业点{row['farthest_m']:.1f}米超过飞行半径{row['max_flight_radius_m']:g}米")
            if not row.get("range_ok", True):
                failed.append(f"往返下界{row['round_trip_lower_bound_m']:.1f}米超过航程预算{row['range_budget']:.1f}米")
            if not row.get("time_ok", True):
                failed.append(f"时间下界{row['time_lower_bound_s']:.1f}秒超过时间预算{row['time_budget']:.1f}秒")
            if not row.get("speed_ok", True):
                failed.append("指定速度超过机型限制")
            messages.append(row["uav_id"] + "执行" + row["job_id"] + "时，" + "，".join(failed))
        result.update(tool_message=str(exc), message="；".join(messages) + "。其他机型只作为改变指定机要求的备选，尚未采用。")
    return result


def _reply(graph, variants, state, request):
    lines = [{"PLANNED": "任务编排方案已生成，尚未下发。", "CONDITIONAL": "条件任务方案已生成，尚未下发。",
              "NEEDS_DATA": "任务要求已保留，部分步骤还需补齐信息。",
              "BLOCKED": "任务要求已保留，规划或动作校验遇到阻塞，尚未下发。",
              "INFEASIBLE": "原要求下存在不能通过的任务分支，尚未下发。"}[state]]
    for node in graph["nodes"]:
        nid, kind = node["id"], node["action"]
        conditions = [e["from"] + EVENT_LABELS[e["on"]] for e in graph["incoming"][nid]]
        prefix = ("；".join(conditions) if node["join"] == "all" else "任一满足：" + "；".join(conditions)) if conditions else "开始时"
        actor = node.get("uav_id") or "按分支分配执行机"
        details = []
        if node.get("job"):
            job = node["job"]
            details.append(job.get("name") or job.get("place") or ("目标发现位置周围" if "center_from" in job else "指定区域"))
            if job.get("shape") == "circle" and "radius" in job:
                details.append(f"半径{job['radius']:g}米")
            elif job.get("shape") == "rectangle" and all(k in job for k in ("length", "width")):
                details.append(f"东西{job['length']:g}米、南北{job['width']:g}米的矩形")
            if kind == "patrol":
                details.append(str(job.get("laps", 1)) + "圈")
            if kind == "search":
                details.append(f"搜索时限{job.get('search_timeout', request.get('search_timeout', 1200)):g}秒")
        params = node["params"]
        for key in ("mode", "description", "targets", "target"):
            if key in params:
                value = params[key]
                if list(references(value)):
                    details.append("使用前序发现的目标位置")
                elif isinstance(value, list):
                    details.append("、".join(str(v) for v in value))
                else:
                    details.append(str(value))
        if "duration_s" in params:
            details.append(f"持续{params['duration_s']:g}秒")
        elif kind in {"track", "observe", "hover", "wait", "scan"}:
            details.append("持续时间未指定，待补")
        if "until_step" in params:
            details.append("持续到" + params["until_step"] + "完成")
        lines.append(f"{nid}：{prefix}，{actor}{LABELS[kind]}" + ("，" + "，".join(details) if details else "") + "。")
    lines.append(f"任务基础航高{graph['flight_alt']:g}米；多机按任务固定顺序间隔0.3米错高，公共步骤跨分支保持一致。")
    if "speed" in request:
        lines.append(f"指定速度{request['speed']:g}米/秒，按机型上限核验。")
    else:
        lines.append("未指定速度，沿用逐机巡航速度。")
    if "max_duration_s" in request:
        lines.append(f"全任务总时限{request['max_duration_s']:g}秒。")
    if not any(n["action"] in {"return", "land"} for n in graph["nodes"]):
        if request.get("return_to_home") is True:
            lines.append("各分支结束后按要求返航降落；未指定结束点的录像持续到该分支任务结束。")
        else:
            lines.append("未明确要求返航，任务结束后默认原地悬停（不返航、按单程计算）；未指定结束点的录像持续到该分支任务结束。")
    for variant in variants:
        assignments = (variant.get("geometry_plan") or {}).get("assignments", [])
        if assignments:
            lines.append(variant["scenario_id"] + "已知航段分配：" + "；".join(
                row["uav_id"] + "执行" + "→".join(row["jobs"]) + f"，航高{row['flight_alt']:g}米" for row in assignments) + "。")
    messages = sorted({issue["message"] for v in variants for issue in v.get("pending", []) + v.get("violations", [])})
    lines.extend(messages)
    if any(n["action"] not in GEOMETRY_ACTIONS for n in graph["nodes"]):
        lines.append("录像、模式切换等已作为能力步骤保留；当前交付编排方案，尚未实际调用这些动作。")
    lines.append("时间按搜索时限、起降和已知等待预留；电量仍为水平航迹估计，未认证完整能耗与现场障碍。")
    return "\n".join(lines)


def compute(request):
    """规划任务图与可达分支，复用原计算链；不注册、不下发、不启动感知。"""
    graph = validate(request)
    supplied = require(request.get("constraints", {}), "constraints", dict)
    if set(supplied) - {"fixed_assignments", "allowed_assignments", "single_owner_jobs", "required_drones"}:
        raise AtomicError("INVALID_ARGUMENT", "任务图分配约束含不支持的字段")
    if "required_drones" in supplied and (type(supplied["required_drones"]) is not int
                                          or not 1 <= supplied["required_drones"] <= 64):
        raise AtomicError("INVALID_ARGUMENT", "required_drones须为1..64的整数")
    for key in ("fixed_assignments", "allowed_assignments", "single_owner_jobs"):
        if key in supplied:
            if not isinstance(supplied[key], dict if key != "single_owner_jobs" else list):
                raise AtomicError("INVALID_ARGUMENT", key + "格式无效")
            if any(j not in graph["indexed"] or graph["indexed"][j]["action"] not in GEOMETRY_ACTIONS
                   for j in supplied[key]):
                raise AtomicError("INVALID_ARGUMENT", "分配约束必须引用区域作业节点")
    variants = scenarios(graph, request.get("observed_events"))
    # Validate all explicit executors, even if they appear only in an interface
    # node or an as-yet inactive branch.
    explicit = sorted({n["uav_id"] for n in graph["nodes"] if n.get("uav_id")})
    selected = request.get("uav_ids")
    if selected is not None:
        require(selected, "uav_ids", list)
        if not set(explicit) <= set(selected):
            raise AtomicError("INVALID_ARGUMENT", "步骤指定机不在任务候选名单中")
    selected_records = []
    if explicit:
        args = {k: request[k] for k in ("flight_alt", "exclude_uavs", "payload") if k in request}
        selected_records = prepare_candidates(dict(args, uav_ids=explicit))["candidates"]
    fleet = {r["uav_id"]: r for r in query({"entity": "fleet"})["fleet"]}
    fleet.update({r["uav_id"]: r for r in selected_records})
    places_cache = query({"entity": "places"})["places"]
    output, locked_owners, altitude_order = [], {}, list(explicit)
    for index, variant in enumerate(variants, 1):
        result = dict(variant, scenario_id=f"B{index}")
        try:
            plan, actions, estimate, pending = _routes(request, graph, variant["active"], locked_owners, altitude_order)
            result.update(_timing(request, graph, variant, plan, fleet, pending))
            result.update(pending=pending, geometry_plan=plan, geometry_actions=actions,
                          cost_estimate=estimate, geometry_scope="known_connected_prefix",
                          validated_for_execution=False)
            result["interface_actions"] = [
                {"node_id": nid, "action": graph["indexed"][nid]["action"],
                 "uav_ids": result["actors"][nid], "params": bind(graph["indexed"][nid]["params"], request.get("runtime_bindings", {})),
                 "trigger": graph["incoming"][nid], "assumed_available": True,
                 "record_until": graph["indexed"][nid]["params"].get("until_step", "mission_end")
                                  if graph["indexed"][nid]["action"] == "record_start" else None}
                for nid in variant["active"] if graph["indexed"][nid]["action"] not in GEOMETRY_ACTIONS]
            physical_failure = bool(result["violations"])
            action_failure = actions is not None and not actions.get("validated")
            if action_failure:
                result["violations"].append(_issue("ACTION_VALIDATION", actions["message"]))
            result["planning_status"] = ("INFEASIBLE" if physical_failure else "BLOCKED" if action_failure
                                         else "CONDITIONAL" if pending else "PLANNED")
            if _show_actions_flag():
                result["synthesized_actions"] = _synthesize_scenario_actions(
                    request, graph, result, plan, actions, result["actors"], fleet, places_cache, altitude_order
                )
        except AtomicError as exc:
            result.update(planning_status="CONDITIONAL" if exc.code == "PREFIX_ALLOCATION_UNCONFIRMED" else "INFEASIBLE" if exc.code in {
                "NO_FEASIBLE_ALLOCATION", "BUDGET_EXCEEDED", "ALTITUDE_INFEASIBLE", "NO_CANDIDATE", "CANDIDATE_EXCLUDED"
            } else "NEEDS_DATA" if exc.code in {"MISSING_PARAMETER", "NOT_FOUND", "AMBIGUOUS"} else "BLOCKED",
                          pending=[_failure(exc)] if exc.code == "PREFIX_ALLOCATION_UNCONFIRMED" else [],
                          violations=[] if exc.code == "PREFIX_ALLOCATION_UNCONFIRMED" else [_failure(exc)])
        output.append(result)
    statuses = {v["planning_status"] for v in output}
    state = ("INFEASIBLE" if "INFEASIBLE" in statuses else "BLOCKED" if "BLOCKED" in statuses else "NEEDS_DATA" if "NEEDS_DATA" in statuses
             else "CONDITIONAL" if len(output) > 1 or "CONDITIONAL" in statuses else "PLANNED")
    result = {"status": "OK", "kind": "task_graph_plan", "planning_status": state,
              "submitted": False, "validated_for_execution": False,
              "whole_mission_budget_complete": False,
              "graph": {k: graph[k] for k in ("nodes", "edges", "order", "flight_alt")},
              "scenarios": output,
              "planning_bindings": {"owners": locked_owners, "altitude_order": altitude_order},
              "execution_contract": {"conditional_graph_backend_required": True,
                                     "reserved_interfaces_assumed_available": True,
                                     "flatten_to_legacy_queue": False,
                                     "unbounded_loops_allowed": False},
              "reply": _reply(graph, output, state, request)}
    scenarios_summary = _compact_scenarios(output)
    task_keys = sorted({s["task_key"] for s in scenarios_summary if s.get("task_key")})
    result["_summary"] = {k: result[k] for k in ("planning_status", "submitted", "validated_for_execution",
                                                "whole_mission_budget_complete", "reply")}
    result["_summary"].update(
        node_count=len(graph["nodes"]),
        scenario_count=len(output),
        show_actions=_show_actions_flag(),
        task_keys=task_keys,
        budget_scope="水平转场和覆盖/巡逻长度估计；时间按搜索时限、起降、已知等待及返航预留，未认证完整能耗与现场障碍",
        scenarios=scenarios_summary,
    )
    return result
