#!/usr/bin/env python3
"""Check step types, parameters and order before planning a combined task."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import run
from uav_runtime.capabilities import check_task

if __name__ == "__main__":
    raise SystemExit(run(check_task, "task_capability"))
