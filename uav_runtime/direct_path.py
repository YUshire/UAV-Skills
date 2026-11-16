#!/usr/bin/env python3
"""Query horizontal great-circle distance and sample points on the same arc."""

import argparse
import json
import math
import random
import sys


EARTH_RADIUS_M = 6_371_008.8
MAX_WAYPOINTS = 10_000


def finite(value, name):
    if type(value) not in (int, float) or not -sys.float_info.max <= value <= sys.float_info.max:
        raise ValueError(f"{name} must be a finite number")


def unit_vector(point):
    lon, lat = point
    finite(lon, "longitude")
    finite(lat, "latitude")
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError("longitude/latitude outside WGS84 range")
    if abs(lat) == 90:
        return (0.0, 0.0, math.copysign(1.0, lat))
    lon = math.radians(-180 if lon == 180 else lon)
    lat = math.radians(lat)
    return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sample_fractions(count, distance, threshold, rng):
    # Sample once per bin, in order, keeping a margin from both endpoints.
    fractions = [(i + 0.25 + 0.5 * rng.random()) / count for i in range(count)]
    if threshold is None:
        return fractions
    if distance > threshold * (MAX_WAYPOINTS - 1):
        raise ValueError(f"threshold would exceed {MAX_WAYPOINTS} waypoints")
    result = []
    previous = 0.0
    for stop in fractions + [1.0]:
        segments = max(1, math.ceil((stop - previous) * distance / threshold))
        if len(result) + segments + 1 > MAX_WAYPOINTS:
            raise ValueError(f"threshold and points would exceed {MAX_WAYPOINTS} waypoints")
        result.extend(previous + (stop - previous) * j / segments for j in range(1, segments))
        if stop < 1.0:
            result.append(stop)
        previous = stop
    return result


def direct_route(start, end, altitude, points=3, threshold=None, rng=None):
    """Return horizontal distance in metres and a route at constant relative altitude."""
    finite(altitude, "flight-alt")
    if altitude <= 0:
        raise ValueError("flight-alt must be positive")
    if type(points) is not int or not 0 <= points <= MAX_WAYPOINTS - 2:
        raise ValueError(f"points must be an integer within 0..{MAX_WAYPOINTS - 2}")
    if threshold is not None:
        finite(threshold, "threshold")
        if threshold <= 0:
            raise ValueError("threshold must be positive")

    a, b = unit_vector(start), unit_vector(end)
    normal = cross(a, b)
    sine = math.hypot(*normal)
    cosine = sum(x * y for x, y in zip(a, b))
    angle = math.atan2(sine, cosine)
    distance = angle * EARTH_RADIUS_M
    go = [{"longitude": start[0], "latitude": start[1], "altitude": altitude}]

    if angle:
        # Antipodes admit many shortest arcs. Pick a stable plane for this case.
        opposite = start[1] == -end[1] and (
            abs(start[1]) == 90 or abs(end[0] - start[0]) == 180
        )
        if opposite or sine == 0:
            axis = (0, 0, 1) if abs(a[2]) <= abs(a[0]) else (1, 0, 0)
            normal = cross(a, axis)
        tangent = cross(normal, a)
        length = math.hypot(*tangent)
        tangent = tuple(x / length for x in tangent)
        for t in sample_fractions(points, distance, threshold, rng or random):
            c, s = math.cos(t * angle), math.sin(t * angle)
            x, y, z = (c * p + s * q for p, q in zip(a, tangent))
            go.append({"longitude": math.degrees(math.atan2(y, x)),
                       "latitude": math.degrees(math.atan2(z, math.hypot(x, y))),
                       "altitude": altitude, "pass_through": True})
    go.append({"longitude": end[0], "latitude": end[1], "altitude": altitude})
    one_way = round(distance, 3)
    return {"status": "OK", "route_model": "great_circle", "map_used": False,
            "one_way_distance": one_way, "total_distance": round(2 * one_way, 3),
            "go_waypoints": go, "back_waypoints": list(reversed(go))}


def add_arguments(parser):
    parser.add_argument("--start-lon", type=float)
    parser.add_argument("--start-lat", type=float)
    parser.add_argument("--start-alt", type=float, help="ground elevation, excluded from horizontal distance")
    parser.add_argument("--starts-json", help="array of UAV origins instead of a single start")
    parser.add_argument("--end-lon", type=float, required=True)
    parser.add_argument("--end-lat", type=float, required=True)
    parser.add_argument("--end-alt", type=float, default=0, help="ground elevation, excluded from horizontal distance")
    parser.add_argument("--flight-alt", type=float, default=1.2, help="relative altitude in metres")
    parser.add_argument("--points", type=int, default=3, help="number of random intermediate points")
    parser.add_argument("--seed", type=int, help="repeat the same sample with this seed")
    parser.add_argument("--threshold", type=float, help="add points to limit spacing in metres")


def query_path(args):
    finite(args.end_alt, "end-alt")
    goal = (args.end_lon, args.end_lat)
    rng = random.Random(args.seed)
    if args.starts_json is None:
        if args.start_lon is None or args.start_lat is None:
            raise ValueError("single origin requires start-lon and start-lat")
        if args.start_alt is not None:
            finite(args.start_alt, "start-alt")
        return direct_route((args.start_lon, args.start_lat), goal, args.flight_alt, args.points, args.threshold, rng)

    if any(v is not None for v in (args.start_lon, args.start_lat, args.start_alt)):
        raise ValueError("use either starts-json or single start arguments")
    starts = json.loads(args.starts_json)
    if not isinstance(starts, list) or not starts:
        raise ValueError("starts-json must be a non-empty array")
    results, seen = [], set()
    total_points = 0
    for start in starts:
        if not isinstance(start, dict) or set(start) != {"uav_id", "longitude", "latitude", "altitude"}:
            raise ValueError("each origin requires uav_id, longitude, latitude and altitude only")
        uid = start["uav_id"]
        if not isinstance(uid, str) or not uid.strip() or uid in seen:
            raise ValueError("uav_id must be unique and non-empty")
        seen.add(uid)
        finite(start["altitude"], "start altitude")
        route = direct_route((start["longitude"], start["latitude"]), goal,
                             args.flight_alt, args.points, args.threshold, rng)
        total_points += len(route["go_waypoints"])
        if total_points > MAX_WAYPOINTS:
            raise ValueError(f"combined routes would exceed {MAX_WAYPOINTS} outbound waypoints")
        results.append({"uav_id": uid, **route})
    return {"status": "OK", "route_model": "great_circle", "map_used": False, "results": results}


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main():
    parser = ArgumentParser(description=__doc__)
    add_arguments(parser)
    try:
        print(json.dumps(query_path(parser.parse_args()), ensure_ascii=False, allow_nan=False))
        return 0
    except ValueError as exc:
        print(json.dumps({"status": "INVALID_ARGUMENT", "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
