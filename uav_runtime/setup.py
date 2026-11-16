#!/usr/bin/env python3
"""Install planning dependencies inside uav_runtime and check this workspace."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import venv

RUNTIME = Path(__file__).resolve().parent
WORKSPACE = RUNTIME.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅用当前解释器检查环境")
    parser.add_argument("--data", action="store_true", help="同时检查 data 中的三个库")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.exit(2, "需要 Python 3.11 或以上版本。\n")
    if args.check:
        interpreter = sys.executable
    else:
        directory = RUNTIME / ".venv"
        interpreter = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if not interpreter.is_file():
            venv.EnvBuilder(with_pip=True).create(directory)
        pip_ready = subprocess.run([str(interpreter), "-c", "import pip"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not pip_ready:
            subprocess.run([str(interpreter), "-m", "ensurepip", "--upgrade"], check=True)
        subprocess.run([str(interpreter), "-m", "pip", "install", "-r", str(RUNTIME / "requirements.txt")], check=True)
    command = [str(interpreter), "-B", "-X", "utf8", str(RUNTIME / "self_check.py")]
    if args.data:
        command.append("--data")
    subprocess.run(command, check=True)
    fragment = {"agents": {"defaults": {"workspace": str(WORKSPACE)}},
                "tools": {"exec": {"timeout": 180}}}
    (WORKSPACE / "nanobot-workspace.fragment.json").write_text(
        json.dumps(fragment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("工作区依赖检查通过，nanobot 配置片段已生成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
