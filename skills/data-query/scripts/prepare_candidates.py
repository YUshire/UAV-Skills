#!/usr/bin/env python3
"""Read eligible fleet records and calculate available numerical budgets."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import run
from uav_runtime.seven_inputs import prepare_candidates

if __name__ == '__main__':
    raise SystemExit(run(prepare_candidates, 'candidates'))
