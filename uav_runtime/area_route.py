#!/usr/bin/env python3
"""Generate a boundary or parallel sweep route from a geographic area."""

import argparse
import json
import math


EARTH_RADIUS_M = 6_371_008.8
MAX_WAYPOINTS = 10_000
# Accept boundary waypoints exported by older clients at eight decimal places.
ENTRY_TOLERANCE_M = 0.001


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def coordinates(lon, lat):
    if not (-180 <= finite(lon, "longitude") <= 180 and -90 <= finite(lat, "latitude") <= 90):
        raise ValueError("longitude/latitude outside WGS84 range")


class LocalFrame:
    """Spherical azimuthal equidistant coordinates, in metres east and north."""

    def __init__(self, lon, lat):
        coordinates(lon, lat)
        self.lon, self.lat = lon, lat
        self.sin_lat = math.sin(math.radians(lat))
        self.cos_lat = 0.0 if abs(lat) == 90 else math.cos(math.radians(lat))

    def to_xy(self, lon, lat):
        coordinates(lon, lat)
        delta = math.radians(math.remainder(lon - self.lon, 360.0))
        s = math.sin(math.radians(lat))
        c = 0.0 if abs(lat) == 90 else math.cos(math.radians(lat))
        east = c * math.sin(delta)
        north = self.cos_lat * s - self.sin_lat * c * math.cos(delta)
        up = self.sin_lat * s + self.cos_lat * c * math.cos(delta)
        horizontal = math.hypot(east, north)
        if up < 0 and horizontal < 1e-15:
            raise ValueError("the antipode has no unique local direction")
        if horizontal == 0:
            return (0.0, 0.0)
        scale = EARTH_RADIUS_M * math.atan2(horizontal, up) / horizontal
        return (east * scale, north * scale)

    def to_gps(self, x, y):
        distance = math.hypot(finite(x, "x"), finite(y, "y"))
        if distance == 0:
            return (self.lon, self.lat)
        angle = distance / EARTH_RADIUS_M
        if angle >= math.pi:
            raise ValueError("area extends beyond the local projection's antipode")
        east = math.sin(angle) * (x / distance)
        north = math.sin(angle) * (y / distance)
        up = math.cos(angle)
        radial = self.cos_lat * up - self.sin_lat * north
        z = self.sin_lat * up + self.cos_lat * north
        lon = math.remainder(self.lon + math.degrees(math.atan2(east, radial)), 360.0)
        lat = math.degrees(math.atan2(z, math.hypot(east, radial)))
        return (lon, lat)


def positive(value, name):
    if finite(value, name) <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def segment_count(distance, spacing):
    if distance > MAX_WAYPOINTS * spacing:
        raise ValueError(f"route exceeds {MAX_WAYPOINTS} waypoints; increase lane spacing")
    return max(1, math.ceil(distance / spacing))


VALID_ENTRY_SIDES = ("south", "north", "east", "west")
VALID_DIRECTIONS = ("ccw", "cw")


def _side_anchor(half_x, half_y, entry_side):
    if entry_side == "south":
        return (0.0, -half_y)
    if entry_side == "north":
        return (0.0, half_y)
    if entry_side == "west":
        return (-half_x, 0.0)
    if entry_side == "east":
        return (half_x, 0.0)
    raise ValueError(f"entry_side must be one of {VALID_ENTRY_SIDES}")


def boundary(half_x, half_y, circle, spacing, direction="ccw", entry_side=None):
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {VALID_DIRECTIONS}")
    if circle:
        count = max(16, segment_count(2 * math.pi * half_x, spacing))
        if count + 1 > MAX_WAYPOINTS:
            raise ValueError(f"boundary exceeds {MAX_WAYPOINTS} waypoints")
        ring = [(half_x * math.cos(math.tau * i / count), half_x * math.sin(math.tau * i / count))
                for i in range(count)]
    else:
        nx = segment_count(2 * half_x, spacing)
        ny = segment_count(2 * half_y, spacing)
        if 2 * (nx + ny) + 1 > MAX_WAYPOINTS:
            raise ValueError(f"boundary exceeds {MAX_WAYPOINTS} waypoints; increase lane spacing")
        corners = [(-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)]
        ring = []
        for a, b, count in zip(corners, corners[1:] + corners[:1], (nx, ny, nx, ny)):
            ring.extend((a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count)
                        for i in range(count))
    if direction == "cw":
        ring = [ring[0]] + list(reversed(ring[1:]))
    if entry_side is not None:
        anchor = _side_anchor(half_x, half_y, entry_side)
        start_idx = min(range(len(ring)), key=lambda i: math.hypot(ring[i][0] - anchor[0], ring[i][1] - anchor[1]))
        ring = ring[start_idx:] + ring[:start_idx]
    return ring + [ring[0]]


def sweep(half_x, half_y, circle, spacing, entry_side=None):
    # Each track bisects a strip. A circle smaller than one strip has a full diameter track.
    if entry_side not in (None, *VALID_ENTRY_SIDES):
        raise ValueError(f"entry_side must be one of {VALID_ENTRY_SIDES}")
    vertical = entry_side in ("west", "east")
    span = 2 * half_x if vertical else 2 * half_y
    count = segment_count(span, spacing)
    if 2 * count + 1 > MAX_WAYPOINTS:
        raise ValueError(f"coverage exceeds {MAX_WAYPOINTS} waypoints; increase lane spacing")
    indices = range(count - 1, -1, -1) if entry_side in ("north", "east") else range(count)
    points = []
    for step_idx, i in enumerate(indices):
        if vertical:
            x = half_x * ((2 * i + 1) / count - 1)
            extent = math.sqrt(max(0.0, (half_x - x) * (half_x + x))) if circle else half_y
            start_y = extent if step_idx % 2 else -extent
            points.extend([(x, start_y), (x, -start_y)])
        else:
            y = half_y * ((2 * i + 1) / count - 1)
            extent = math.sqrt(max(0.0, (half_y - y) * (half_y + y))) if circle else half_x
            start_x = extent if step_idx % 2 else -extent
            points.extend([(start_x, y), (-start_x, y)])
    return points


def horizontal_length(waypoints):
    """Sum spherical distances between successive geographic waypoints, in metres."""
    length = 0.0
    for a, b in zip(waypoints, waypoints[1:]):
        dlat = math.radians(b["latitude"] - a["latitude"])
        dlon = math.radians(math.remainder(b["longitude"] - a["longitude"], 360.0))
        v = (math.sin(dlat / 2)**2 + math.cos(math.radians(a["latitude"]))
             * math.cos(math.radians(b["latitude"])) * math.sin(dlon / 2)**2)
        length += 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1, max(0, v))))
    return length


def query_area(args):
    frame = LocalFrame(args.center_lon, args.center_lat)
    finite(args.center_alt, "center-alt")
    if finite(args.flight_alt, "flight-alt") <= 0:
        raise ValueError("flight-alt must be positive")
    spacing = positive(args.lane, "lane")
    circle = args.shape == "circle"
    if circle:
        half_x = half_y = positive(args.radius, "radius")
        extent = half_x
    elif args.shape == "rectangle":
        half_x, half_y = positive(args.length, "length") / 2, positive(args.width, "width") / 2
        extent = math.hypot(half_x, half_y)
    else:
        raise ValueError("shape must be rectangle or circle")
    if extent >= math.pi * EARTH_RADIUS_M:
        raise ValueError("area extends beyond the local projection's antipode")
    if half_x == 0 or half_y == 0:
        raise ValueError("area dimensions are too small to represent")

    lon, lat = getattr(args, "start_lon", None), getattr(args, "start_lat", None)
    entry_side = getattr(args, "entry_side", None)
    direction = getattr(args, "direction", "ccw") or "ccw"
    if (lon is None) != (lat is None):
        raise ValueError("start-lon and start-lat must be supplied together")
    if lon is not None:
        entry = frame.to_xy(lon, lat)
    elif entry_side is not None:
        entry = _side_anchor(half_x, half_y, entry_side)
    else:
        entry = (0.0, 0.0)
    if circle:
        overflow = math.hypot(*entry) - half_x
    else:
        overflow = max(abs(entry[0]) - half_x, abs(entry[1]) - half_y)
    if overflow > ENTRY_TOLERANCE_M:
        raise ValueError("start point is outside the requested area")

    def waypoint(point):
        longitude, latitude = frame.to_gps(*point)
        return {"longitude": longitude, "latitude": latitude, "altitude": args.flight_alt}

    entry_waypoint = ({"longitude": lon, "latitude": lat, "altitude": args.flight_alt}
                      if lon is not None else waypoint(entry))
    result = {"status": "OK", "map_used": False, "shape": args.shape, "region": "给定区域",
              "bounds_xy": [-half_x, -half_y, half_x, half_y],
              "area_m2": math.pi * half_x**2 if circle else 4 * half_x * half_y,
              "coverage_certified": False}
    if entry_side is not None:
        result["entry_side"] = entry_side
    if args.mode == "boundary":
        points = boundary(half_x, half_y, circle, spacing, direction=direction, entry_side=entry_side)
        approach = [entry_waypoint, waypoint(points[0])]
        result.update(approach_waypoints=approach, approach_length=round(horizontal_length(approach), 3),
                      closed=True, boundary_complete=True, direction=direction)
    elif args.mode == "coverage":
        samples = sweep(half_x, half_y, circle, spacing, entry_side=entry_side)
        points = samples if math.dist(entry, samples[0]) < 1e-8 else [entry, *samples]
        result.update(reachable_samples=len(samples), candidate_samples=len(samples))
    else:
        raise ValueError("mode must be boundary or coverage")
    waypoints = [waypoint(p) for p in points]
    if args.mode == "coverage":
        waypoints[0] = entry_waypoint
    distance = horizontal_length(waypoints)
    if distance == 0:
        raise ValueError("area is too small for distinct geographic waypoints")
    return dict(result, path_length=round(distance, 3), waypoint_count=len(waypoints), waypoints=waypoints)


def add_arguments(parser):
    parser.add_argument("--center-lon", type=float, required=True)
    parser.add_argument("--center-lat", type=float, required=True)
    parser.add_argument("--center-alt", type=float, default=0)
    parser.add_argument("--shape", choices=("rectangle", "circle"), required=True)
    parser.add_argument("--length", type=float)
    parser.add_argument("--width", type=float)
    parser.add_argument("--radius", type=float)
    parser.add_argument("--mode", choices=("boundary", "coverage"), default="coverage")
    parser.add_argument("--flight-alt", type=float, default=50.0)
    parser.add_argument("--lane", type=float, default=2.0)
    parser.add_argument("--start-lon", type=float)
    parser.add_argument("--start-lat", type=float)
    parser.add_argument("--entry-side", choices=VALID_ENTRY_SIDES)
    parser.add_argument("--direction", choices=VALID_DIRECTIONS, default="ccw")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    try:
        print(json.dumps(query_area(args), ensure_ascii=False, allow_nan=False))
    except ValueError as exc:
        print(json.dumps({"status": "INVALID_ARGUMENT", "message": str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
