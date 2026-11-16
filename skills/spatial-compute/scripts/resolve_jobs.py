#!/usr/bin/env python3
"""Resolve geographic jobs from supplied records without database reads."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import run
from uav_runtime.seven_inputs import resolve_jobs

if __name__ == '__main__':
    raise SystemExit(run(resolve_jobs, 'regions'))
