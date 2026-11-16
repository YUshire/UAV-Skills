#!/usr/bin/env python3
"""Numerical planning costs or final routes, without solving assignments."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import run
from uav_runtime.seven_geometry import compute

if __name__ == "__main__":
    raise SystemExit(run(compute, "planning_routes"))
