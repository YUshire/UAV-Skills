"""Read-only JSON fence check for saved replies; never invokes the model."""
import argparse
import json
from pathlib import Path
import re


JSON_BLOCK = re.compile(r"^[ \t]*```json[ \t]*\r?\n(.*?)(^[ \t]*```[ \t]*$|\Z)",
                        re.MULTILINE | re.DOTALL | re.IGNORECASE)


def check_reply(reply):
    errors = []
    for index, match in enumerate(JSON_BLOCK.finditer(reply), 1):
        if not match.group(2):
            errors.append({"block": index, "error": "unclosed JSON fence"})
            continue
        try:
            json.loads(match.group(1))
        except ValueError as exc:
            errors.append({"block": index, "error": str(exc)})
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", type=Path)
    parser.add_argument("--last", type=int, help="check only the last N trace rows")
    args = parser.parse_args()
    if args.last is not None and args.last < 1:
        parser.error("--last must be positive")
    lines = args.traces.read_text(encoding="utf-8").splitlines()
    start = max(0, len(lines) - args.last) if args.last is not None else 0
    failures = []
    for index in range(start, len(lines)):
        row = json.loads(lines[index])
        errors = check_reply(row["reply"])
        if errors:
            failures.append({"line": index + 1, "session_key": row["session_key"], "errors": errors})
    print(json.dumps({"checked_rows": len(lines) - start, "failures": failures}, ensure_ascii=False))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
