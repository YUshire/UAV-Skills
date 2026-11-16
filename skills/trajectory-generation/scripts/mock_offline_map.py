#!/usr/bin/env python3
"""Workspace-local synthetic route previews; no map or execution backend."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.mock_routes import main


if __name__ == "__main__":
    raise SystemExit(main())
