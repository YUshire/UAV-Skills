#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from uav_runtime.snapshot_sql import SnapshotError, select

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def emit(payload, code=0):
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


def find_database(name):
    candidate = Path(__file__).resolve().parents[3] / "data" / name
    if candidate.is_file():
        return candidate
    emit({"status": "ERROR", "message": f"cannot locate {candidate}"}, 1)


def run(database_name, table_name):
    parser = argparse.ArgumentParser(
        description=f"Run one read-only SQLite query against {database_name} ({table_name})"
    )
    parser.add_argument("--sql", help="one SELECT or WITH ... SELECT; omitted reads stdin")
    args = parser.parse_args()

    sql = args.sql if args.sql is not None else sys.stdin.read()
    if not sql.strip():
        emit({"status": "INVALID_ARGUMENT", "message": "SQL cannot be empty"}, 2)
    try:
        data, truncated = select(find_database(database_name), table_name, sql)
        status = "OK" if data else "NOT_FOUND"
        emit({"status": status, "count": len(data), "truncated": truncated, "data": data})
    except SnapshotError as exc:
        emit({"status": exc.code, "message": str(exc)}, 2)
    except ValueError as exc:
        emit({"status": "INVALID_ARGUMENT", "message": str(exc)}, 2)
    except OSError as exc:
        emit({"status": "IO_ERROR", "message": str(exc)}, 2)
