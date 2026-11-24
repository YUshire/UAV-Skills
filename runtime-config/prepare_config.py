"""Create a private, portable agent config without changing ~/.nanobot/config.json."""
import argparse
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime-config/local.json")
    parser.add_argument("--model", default=os.environ.get("UAV_MODEL", "Qwen3.8-27B"))
    parser.add_argument("--provider", default=os.environ.get("UAV_PROVIDER", "vllm"))
    parser.add_argument("--api-base", default=os.environ.get("UAV_API_BASE", "http://127.0.0.1:7696/v1"))
    args = parser.parse_args()
    config = json.loads((ROOT / "runtime-config/config.example.json").read_text())
    defaults = config["agents"]["defaults"]
    defaults.update(workspace=str(args.workspace.resolve()), model=args.model, provider=args.provider)
    config["providers"] = {args.provider: {"apiBase": args.api_base,
                                          "apiKey": os.environ.get("UAV_API_KEY", "EMPTY")}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite an existing config, and never print its credentials.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(config, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"Created config: {args.output.resolve()}")


if __name__ == "__main__":
    main()
