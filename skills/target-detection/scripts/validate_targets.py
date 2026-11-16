#!/usr/bin/env python3
"""Validate target descriptions and UAV snapshot membership without starting perception."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.atomic.common import AtomicError, load_json
from uav_runtime.atomic.query import compute as query
from uav_runtime.search_targets import validate_targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uav-id", required=True)
    parser.add_argument("--query-json", required=True, help="search_id and targets object")
    args = parser.parse_args()
    try:
        value = load_json(args.query_json)
        if not isinstance(value, dict) or set(value) != {"search_id", "targets"}:
            raise ValueError("query-json requires search_id and targets")
        if not isinstance(value["search_id"], str) or not value["search_id"].strip():
            raise ValueError("search_id must be non-empty")
        targets = validate_targets(value["targets"])
        facts = query({"entity": "fleet", "uav_ids": [args.uav_id]})
        record = facts["fleet"][0]
        if not record["has_status"] or not record["has_resource"]:
            raise ValueError("UAV requires both status and resource records")
        print(json.dumps({"status": "OK", "validated": True, "submitted": False,
                          "validation_scope": "target_format_and_snapshot_membership",
                          "perception_started": False, "backend_verified": False,
                          "uav_id": args.uav_id, "search_id": value["search_id"], "targets": targets},
                         ensure_ascii=False))
        return 0
    except (AtomicError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": getattr(exc, "code", "INVALID_ARGUMENT"), "message": str(exc),
                          "submitted": False, "perception_started": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
