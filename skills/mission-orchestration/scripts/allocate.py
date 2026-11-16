#!/usr/bin/env python3
"""Solve UAV ownership and visit order from supplied numerical costs."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from uav_runtime.atomic.allocation import compute
from uav_runtime.atomic.common import run


if __name__ == "__main__":
    raise SystemExit(run(compute, "allocation"))
