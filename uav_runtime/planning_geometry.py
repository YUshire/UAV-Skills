"""Geometric costs and route steps assembled from explicit areas and WGS84 points."""
from types import SimpleNamespace

from uav_runtime.area_route import LocalFrame, query_area
from uav_runtime.direct_path import direct_route


GEOMETRY_MODEL = "mock_area_v1"


def lon_lat(waypoint):
    return waypoint["longitude"], waypoint["latitude"]


def region_route(area, altitude, task_type):
    """Use the public area query for both costing and final trajectories."""
    center = area["center"]
    start = center
    # A registered entrance can be outside this job (especially after a split).
    entrance = area.get("entrance")
    if entrance is not None and task_type != "patrol":
        east, north = LocalFrame(*center).to_xy(*entrance)
        if area["shape"] == "rectangle":
            inside = abs(east) <= area["length"] / 2 and abs(north) <= area["width"] / 2
        else:
            inside = east * east + north * north <= area["radius"] ** 2
        if inside:
            start = entrance

    args = SimpleNamespace(center_lon=center[0], center_lat=center[1], center_alt=0.0,
                           flight_alt=altitude, shape=area["shape"], length=area.get("length"),
                           width=area.get("width"), radius=area.get("radius"), lane=2.0,
                           mode="boundary" if task_type == "patrol" else "coverage",
                           start_lon=start[0], start_lat=start[1])
    return query_area(args)


def cost_geometry(prepared):
    task_type, altitude = prepared["task_type"], prepared["flight_alt"]
    routes = [region_route(job, altitude, task_type) for job in prepared["jobs"]]
    work_range = [r["path_length"] * prepared["laps"] for r in routes]
    speeds = prepared["speeds"]
    job_ids = [job["job_id"] for job in prepared["jobs"]]
    costs = {"work_range": work_range, "work_time": [d / speeds["work"] for d in work_range],
             "transfer_range": {}, "transfer_time": {}, "reachable": {}}
    for candidate in prepared["candidates"]:
        uid = candidate["uav_id"]
        home = lon_lat(candidate["origin"])
        entries = [home] + [lon_lat(route["waypoints"][0]) for route in routes]
        exits = [home] + [lon_lat(route["waypoints"][-1]) for route in routes]
        distances = []
        for i, end_of_previous_job in enumerate(exits):
            row = []
            for j, start_of_next_job in enumerate(entries):
                if i == j:
                    row.append(0.0)
                else:
                    leg = direct_route(end_of_previous_job, start_of_next_job, altitude, points=0)
                    row.append(leg["one_way_distance"])
            distances.append(row)
        times = [[d / speeds["transit"] for d in row] for row in distances]
        # Per-job physical reachability: the aircraft must be able to fly from
        # its takeoff point to this job and back within its range and time
        # budgets. Job work is intentionally excluded here because a shareable
        # job can be split between up to two owners; the allocation solver
        # enforces the actual work share. Recording all jobs here (the previous
        # behaviour) incorrectly marked far jobs reachable for aircraft that
        # only passed screening for a different, nearer job.
        reachable = []
        for index, job_id in enumerate(job_ids):
            round_range = distances[0][index + 1] + distances[index + 1][0]
            round_time = times[0][index + 1] + times[index + 1][0]
            if (round_range <= candidate["range_budget"] + 1e-6
                    and round_time <= candidate["time_budget"] + 1e-6):
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


def build_steps(prepared, ordered, origin, altitude, timeout):
    """Connect the actual first/last waypoints of consecutive jobs and the home point."""
    task_type = prepared["task_type"]
    home = previous = lon_lat(origin)
    steps = []
    transit_range = work_range = 0.0
    for zone in ordered:
        route = region_route(zone, altitude, task_type)
        waypoints = route["waypoints"]
        transfer = direct_route(previous, lon_lat(waypoints[0]), altitude)
        transit_range += transfer["one_way_distance"]
        job_id = zone["job_id"]
        steps.append({"type": "transit", "to": job_id, "waypoints": transfer["go_waypoints"]})
        work_range += route["path_length"] * prepared["laps"]
        operation = {"type": task_type, "job_id": job_id,
                     "name": zone.get("name", job_id), "zone": zone["label"]}
        if task_type == "patrol":
            operation.update(waypoints=waypoints, laps=prepared["laps"])
        else:
            low, high = search_box(zone, altitude)
            operation.update(box_min=low, box_max=high, timeout_sec=timeout, waypoints=waypoints)
        steps.append(operation)
        previous = lon_lat(waypoints[-1])
    transfer = direct_route(previous, home, altitude)
    steps.append({"type": "transit", "to": "home", "waypoints": transfer["go_waypoints"]})
    return steps, transit_range + transfer["one_way_distance"], work_range
