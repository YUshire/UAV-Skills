#!/usr/bin/env python3
"""Run a skill relative to the workspace, using its own virtual environment."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--pipeline":
        sys.path.insert(0, str(ROOT))
        from uav_runtime.pipeline import main as pipeline_main
        return pipeline_main(sys.argv[2:])
    if len(sys.argv) < 2:
        print("Usage: python uav_runtime/run.py skills/<name>/scripts/<file>.py [arguments]", file=sys.stderr)
        return 2
    script = (ROOT / sys.argv[1]).resolve()
    if not script.is_relative_to(ROOT / "skills") or script.suffix != ".py" or not script.is_file():
        print("请指定 workspace/skills 内的 Python 脚本", file=sys.stderr)
        return 2
    env_python = ROOT / "uav_runtime/.venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    interpreter = str(env_python) if env_python.is_file() else sys.executable
    environment = os.environ.copy()
    environment["PLAN_AGENT_WORKSPACE"] = str(ROOT)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), *filter(None, [environment.get("PYTHONPATH")])]
    )
    return subprocess.run([interpreter, "-B", "-X", "utf8", str(script), *sys.argv[2:]],
                          cwd=ROOT, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
