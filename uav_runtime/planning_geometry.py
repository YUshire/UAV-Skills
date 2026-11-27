"""Geometric costs and route steps assembled from explicit areas and WGS84 points."""
import math
from types import SimpleNamespace

from uav_runtime.area_route import LocalFrame, query_area
from uav_runtime.direct_path import direct_route


GEOMETRY_MODEL = "mock_area_v1"


def lon_lat(waypoint):
    return waypoint["longitude"], waypoint["latitude"]


def job_radius(job):
    """Farthest horizontal distance from the job centre to its action area."""
    if job["shape"] == "circle":
        return job["radius"]
    return (job["length"] ** 2 + job["width"] ** 2) ** 0.5 / 2


def search_lane_m(flight_alt, fov_deg):
    """Ground strip width: lane = 2 * height * tan(FOV / 2)."""
    return 2 * flight_alt * math.tan(math.radians(fov_deg / 2))


def search_coverage_m(length, width, lane):
    """Boustrophedon length using the long side as the strip direction.

    Matches the data-construction figure: n = ceil(short / lane),
    coverage = n * long + (n - 1) * lane.
    """
    long_side, short_side = max(length, width), min(length, width)
    lanes = max(1, math.ceil(round(short_side / lane, 9)))
    return lanes * long_side + (lanes - 1) * lane


def search_circle_coverage_m(radius, lane):
    """Circle coverage chord-sum length, matching data-construction scenario_geometry.py."""
    diameter = 2.0 * radius
    if diameter <= 0:
        return 0.0
    lanes = max(1, math.ceil(round(diameter / lane, 9)))
    spacing = diameter / lanes
    chords = []
    for i in range(lanes):
        low = -radius + i * spacing
        high = -radius + (i + 1) * spacing
        y = 0.0 if low <= 0.0 <= high else min(abs(low), abs(high))
        chords.append(2.0 * math.sqrt(max(0.0, radius * radius - y * y)))
    return sum(chords) + (lanes - 1) * spacing


def candidate_fov_deg(candidate, fallback=89.0):
    fov = candidate.get("fov") if isinstance(candidate, dict) else None
    if isinstance(fov, (int, float)) and not isinstance(fov, bool) and math.isfinite(fov) and fov > 0:
        return float(fov)
    return float(fallback)


def job_search_work_m(job, lane):
    """Nominal coverage length for a search job using data-construction formulas."""
    if job["shape"] == "rectangle":
        return search_coverage_m(job["length"], job["width"], lane) * job["laps"]
    return search_circle_coverage_m(job["radius"], lane) * job["laps"]


def job_path_mode(job, default_task_type=None):
    """Orthogonal geometry mode: coverage (parallel sweep) vs boundary (closed loop)."""
    if job.get("path_mode") in ("coverage", "boundary"):
        return job["path_mode"]
    tt = job.get("task_type") or default_task_type or "search"
    return "boundary" if tt == "patrol" else "coverage"


def region_route(area, altitude, task_type, lane=None):
    """Use the public area query for both costing and final trajectories."""
    mode = area.get("path_mode") or ("boundary" if task_type == "patrol" else "coverage")
    # Only autonomous box search (task_type=search, mode=coverage) uses an axis-aligned
    # bounding square for fuel_explore. Circular coverage patrol (task_type=patrol,
    # mode=coverage) and all boundary loops preserve true circle geometry.
    if task_type == "search" and mode == "coverage" and area["shape"] == "circle":
        side = 2 * area["radius"]
        area = {**area, "shape": "rectangle", "length": side, "width": side}
    center = area["center"]
    entry_side = area.get("entry_side")
    direction = area.get("direction") or "ccw"
    start = None if entry_side is not None else center
    # A registered entrance can be outside this job (especially after a split).
    entrance = area.get("entrance")
    if entry_side is None and entrance is not None and mode != "boundary":
        east, north = LocalFrame(*center).to_xy(*entrance)
        if area["shape"] == "rectangle":
            inside = abs(east) <= area["length"] / 2 and abs(north) <= area["width"] / 2
        else:
            inside = east * east + north * north <= area["radius"] ** 2
        if inside:
            start = entrance

    args = SimpleNamespace(center_lon=center[0], center_lat=center[1], center_alt=0.0,
                           flight_alt=altitude, shape=area["shape"], length=area.get("length"),
                           width=area.get("width"), radius=area.get("radius"),
                           lane=2.0 if lane is None else lane,
                           mode=mode,
                           start_lon=start[0] if start is not None else None,
                           start_lat=start[1] if start is not None else None,
                           entry_side=entry_side,
                           direction=direction)
    return query_area(args)


def cost_geometry(prepared, routes=None, work_range=None):
    altitude = prepared["flight_alt"]
    if routes is None:
        routes = [region_route(job, altitude, job["task_type"], job.get("search_lane_m"))
                  for job in prepared["jobs"]]
    if work_range is None:
        work_range = []
        for route, job in zip(routes, prepared["jobs"]):
            if job_path_mode(job) == "coverage":
                work_range.append(job_search_work_m(job, job.get("search_lane_m") or 2.0))
            else:
                work_range.append(route["path_length"] * job["laps"])
    job_ids = [job["job_id"] for job in prepared["jobs"]]
    req_work_speed = prepared.get("requested_work_speed")
    costs = {"work_range": work_range,
             "work_time": [d / job["work_speed"] for d, job in zip(work_range, prepared["jobs"])],
             "work_range_by_uav": {}, "work_time_by_uav": {},
             "transfer_range": {}, "transfer_time": {}, "reachable": {}}
    return_to_home = prepared.get("return_to_home", True)
    for candidate in prepared["candidates"]:
        uid = candidate["uav_id"]
        cand_alt = candidate.get("planning_alt", altitude)
        cand_fov = candidate_fov_deg(candidate, prepared["jobs"][0].get("fov_deg") or 89.0)
        cand_work_speed = req_work_speed if req_work_speed is not None else candidate["planning_speed"]
        cand_work = []
        for route, job in zip(routes, prepared["jobs"]):
            if job_path_mode(job) == "coverage":
                lane = search_lane_m(cand_alt, cand_fov)
                cand_work.append(job_search_work_m(job, lane))
            else:
                cand_work.append(route["path_length"] * job["laps"])
        costs["work_range_by_uav"][uid] = cand_work
        costs["work_time_by_uav"][uid] = [w / cand_work_speed for w in cand_work]
        home = lon_lat(candidate["origin"])
        entries = [home] + [lon_lat(route["waypoints"][0]) for route in routes]
        exits = [home] + [lon_lat(route["waypoints"][-1]) for route in routes]
        distances = []
        for i, end_of_previous_job in enumerate(exits):
            row = []
            for j, start_of_next_job in enumerate(entries):
                if i == j or (not return_to_home and j == 0):
                    row.append(0.0)
                else:
                    leg = direct_route(end_of_previous_job, start_of_next_job, altitude, points=0)
                    row.append(leg["one_way_distance"])
            distances.append(row)
        times = [[d / candidate["planning_speed"] for d in row] for row in distances]
        # Per-job physical reachability: the aircraft must be able to fly from
        # its takeoff point to this job and back within its range and time
        # budgets. Job work is intentionally excluded here because a shareable
        # job can be split between up to two owners; the allocation solver
        # enforces the actual work share. Recording all jobs here (the previous
        # behaviour) incorrectly marked far jobs reachable for aircraft that
        # only passed screening for a different, nearer job.
        reachable = []
        radius_limit = candidate.get("max_flight_radius")
        for index, job in enumerate(prepared["jobs"]):
            job_id = job["job_id"]
            round_range = distances[0][index + 1] + distances[index + 1][0]
            round_time = times[0][index + 1] + times[index + 1][0]
            center_distance = direct_route(home, (job["center"][0], job["center"][1]),
                                           altitude, points=0)["one_way_distance"]
            radius_ok = radius_limit is None or (
                center_distance + job_radius(job) <= radius_limit + 1e-6)
            if (round_range <= candidate["range_budget"] + 1e-6
                    and round_time <= candidate["time_budget"] + 1e-6 and radius_ok):
                reachable.append(job_id)
        costs["reachable"][uid] = reachable
        # Unreachable jobs must be null arcs, not merely large costs. The MILP
        # uses per-aircraft endurance as a big-M; keeping a transfer time that
        # exceeds endurance would make an idle aircraft's constraint infeasible.
        unreachable = {index + 1 for index, job_id in enumerate(job_ids)
                       if job_id not in reachable}

        def mask(matrix):
            return [[0.0 if row_index == column_index else
                     (None if row_index in unreachable or column_index in unreachable else value)
                     for column_index, value in enumerate(row)]
                    for row_index, row in enumerate(matrix)]

        costs["transfer_range"][uid] = mask(distances)
        costs["transfer_time"][uid] = mask(times)
    return {"job_ids": job_ids, "costs": costs}


def search_box(zone, altitude):
    frame = LocalFrame(*zone["center"])
    east = zone["radius"] if zone["shape"] == "circle" else zone["length"] / 2
    north = zone["radius"] if zone["shape"] == "circle" else zone["width"] / 2
    # The existing search action takes an axis-aligned WGS84 box. Unwrap
    # longitudes around the job before finding its western and eastern edges.
    samples = [frame.to_gps(x, y) for x in (-east, 0, east) for y in (-north, 0, north)]
    center_lon = zone["center"][0]
    offsets = [(lon - center_lon + 180) % 360 - 180 for lon, _ in samples]
    west = (center_lon + min(offsets) + 180) % 360 - 180
    east_lon = (center_lon + max(offsets) + 180) % 360 - 180
    return [west, min(lat for _, lat in samples), 0.3], [east_lon, max(lat for _, lat in samples), round(altitude + 0.5, 3)]


def build_steps(prepared, ordered, origin, altitude, timeout, work_speed=None, include_metrics=False, candidate_fov=None, transit_speed=None):
    """Connect the actual first/last waypoints of consecutive jobs and the home point."""
    home = previous = lon_lat(origin)
    steps = []
    transit_range = work_range = work_time = 0.0
    t_speed = transit_speed or work_speed or prepared["speeds"]["transit"]
    for zone in ordered:
        task_type = zone["task_type"]
        mode = job_path_mode(zone, task_type)
        w_speed = prepared.get("requested_work_speed") or work_speed or zone["work_speed"]
        if mode == "coverage" and candidate_fov is not None:
            lane = search_lane_m(altitude, candidate_fov)
        else:
            lane = zone.get("search_lane_m")
        route = region_route(zone, altitude, task_type, lane)
        waypoints = route["waypoints"]
        transfer = direct_route(previous, lon_lat(waypoints[0]), altitude)
        transit_range += transfer["one_way_distance"]
        job_id = zone["job_id"]
        transit = {"type": "transit", "to": job_id, "waypoints": transfer["go_waypoints"]}
        if include_metrics:
            transit.update(estimated_range_m=transfer["one_way_distance"],
                           estimated_time_s=transfer["one_way_distance"] / t_speed)
        steps.append(transit)
        if mode == "coverage":
            lap_length = job_search_work_m(zone, lane or 2.0)
        else:
            lap_length = route["path_length"] * zone["laps"]
        work_range += lap_length
        work_time += lap_length / w_speed
        operation = {"type": task_type, "path_mode": mode, "job_id": job_id,
                     "name": zone.get("name", job_id), "zone": zone["label"]}
        if prepared.get("requested_work_speed") is not None:
            operation["work_speed"] = w_speed
        if zone.get("entry_side"):
            operation["entry_side"] = zone["entry_side"]
        if zone.get("direction"):
            operation["direction"] = zone["direction"]
        if task_type == "patrol" or mode == "boundary":
            operation.update(waypoints=waypoints, laps=zone["laps"])
            if task_type == "search":
                operation["timeout_sec"] = zone.get("search_timeout", timeout)
        else:
            low, high = search_box(zone, altitude)
            operation.update(box_min=low, box_max=high,
                             timeout_sec=zone.get("search_timeout", timeout), waypoints=waypoints)
        if include_metrics:
            operation.update(estimated_range_m=lap_length,
                             estimated_time_s=lap_length / w_speed)
        steps.append(operation)
        previous = lon_lat(waypoints[-1])
    if not prepared.get("return_to_home", True):
        steps.append({"type": "hover"})
        return steps, transit_range, work_range, work_time
    transfer = direct_route(previous, home, altitude)
    transit = {"type": "transit", "to": "home", "waypoints": transfer["go_waypoints"]}
    if include_metrics:
        transit.update(estimated_range_m=transfer["one_way_distance"],
                       estimated_time_s=transfer["one_way_distance"] / t_speed)
    steps.append(transit)
    return steps, transit_range + transfer["one_way_distance"], work_range, work_time
