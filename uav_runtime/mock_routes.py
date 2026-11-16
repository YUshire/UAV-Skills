"""Generate synthetic routes from coordinates and dimensions supplied by callers."""
import json
import sys

from . import area_route, direct_path


def main():
    parser = direct_path.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    direct_path.add_arguments(sub.add_parser("query-path"))
    area_route.add_arguments(sub.add_parser("query-area"))
    try:
        args = parser.parse_args()
        result = direct_path.query_path(args) if args.operation == "query-path" else area_route.query_area(args)
        result.update(mock=True, map_used=False, submitted=False, validated_for_execution=False,
                      map_context={"backend": "mock", "mock": True, "obstacle_checked": False})
        for route in result.get("results", []):
            route.update(mock=True, map_used=False, submitted=False, validated_for_execution=False)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, TypeError, OverflowError) as exc:
        print(json.dumps({"status": "INVALID_ARGUMENT", "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
