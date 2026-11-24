"""Pass exact artifact references between local numeric skills; stop on failure."""
import argparse
import json
import re
import subprocess
import sys

from .atomic.common import AtomicError, checked_input_path, load_json
from .paths import ROOT


SCRIPTS = {
    "skills/data-query/scripts/prepare_candidates.py",
    "skills/spatial-compute/scripts/resolve_jobs.py",
    "skills/trajectory-generation/scripts/planning_routes.py",
    "skills/mission-orchestration/scripts/allocate.py",
    "skills/flight-control/scripts/assemble_plan.py",
}


def bind(value, outputs):
    if isinstance(value, dict):
        if "ref_step" in value:
            source = value["ref_step"]
            if (set(value) - {"ref_step", "select"} or not isinstance(source, str)
                    or source not in outputs):
                raise AtomicError("INVALID_REFERENCE", "ref_step 须引用本次串联中已成功的步骤")
            return {"ref": outputs[source]["artifact_file"],
                    "select": value.get("select", "/data")}
        return {key: bind(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [bind(item, outputs) for item in value]
    return value


def compute(request):
    if not isinstance(request, dict) or set(request) != {"steps"}:
        raise AtomicError("INVALID_ARGUMENT", "串联输入须为 steps 数组")
    steps = request["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise AtomicError("INVALID_ARGUMENT", "steps 数量须为 1..64")
    # Check the whole manifest before starting any computation.
    known = {}
    for step in steps:
        if (not isinstance(step, dict) or set(step) != {"id", "script", "input"}
                or not isinstance(step["id"], str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", step["id"])
                or step["id"] in known or not isinstance(step["script"], str)
                or step["script"] not in SCRIPTS or not isinstance(step["input"], dict)):
            raise AtomicError("INVALID_ARGUMENT", "每步须有唯一 id、支持的本地计算 script 和 input 对象")
        bind(step["input"], known)
        known[step["id"]] = {"artifact_file": ""}

    outputs, completed = {}, {}
    last_ok_step, last_ok_summary = None, None
    for step in steps:
        sid = step["id"]
        value = bind(step["input"], outputs)
        process = subprocess.run(
            [sys.executable, "-B", str(ROOT / "uav_runtime/run.py"), step["script"],
             "--input", json.dumps(value, ensure_ascii=False, allow_nan=False)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        try:
            result = load_json(process.stdout)
            if not isinstance(result, dict):
                raise AtomicError("INVALID_ARGUMENT", "返回值不是 JSON 对象")
        except AtomicError as exc:
            raise AtomicError("PIPELINE_STEP_FAILED", "计算步骤未返回有效 JSON",
                              failed_step=sid, pipeline_steps=completed,
                              returncode=process.returncode, detail=str(exc),
                              stderr=process.stderr[-2000:]) from exc
        if process.returncode != 0 or result.get("status") != "OK":
            failure_payload = {
                **result,
                "status": result.get("status") if result.get("status") not in (None, "OK")
                else "PIPELINE_STEP_FAILED",
                "failed_step": sid,
                "pipeline_steps": completed,
            }
            if last_ok_summary is not None:
                failure_payload["last_ok_step"] = last_ok_step
                failure_payload["last_ok_summary"] = last_ok_summary
            return failure_payload
        if result.get("artifact_created") is not True or not isinstance(result.get("artifact_file"), str):
            raise AtomicError("PIPELINE_STEP_FAILED", "成功步骤未返回产物路径",
                              failed_step=sid, pipeline_steps=completed)
        outputs[sid] = result
        completed[sid] = {key: result[key] for key in ("status", "kind", "artifact_file") if key in result}
        if "summary" in result:
            last_ok_step = sid
            last_ok_summary = result["summary"]
    return {**result, "pipeline_steps": completed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--input-file")
    args = parser.parse_args(argv)
    try:
        request = load_json(args.input if args.input is not None else
                            checked_input_path(args.input_file).read_text(encoding="utf-8-sig"))
        result = compute(request)
    except AtomicError as exc:
        result = {"status": exc.code, "message": str(exc), **exc.details, "artifact_created": False}
    except (OSError, ValueError, TypeError) as exc:
        result = {"status": "PIPELINE_ERROR", "message": str(exc), "artifact_created": False}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") == "OK" else 2
