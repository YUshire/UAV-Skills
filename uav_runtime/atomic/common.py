"""Artifact transport and value validation, without planning orchestration."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid
from uav_runtime.paths import ROOT, ARTIFACTS


class AtomicError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def finite(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AtomicError("INVALID_ARGUMENT", f"{name} 必须是有限数值")
    if positive and value <= 0:
        raise AtomicError("INVALID_ARGUMENT", f"{name} 必须大于 0")
    return float(value)


def require(value, name, cls):
    if not isinstance(value, cls):
        raise AtomicError("INVALID_ARGUMENT", f"{name} 类型不正确")
    return value


def required(data, key):
    if key not in data or data[key] is None:
        raise AtomicError("MISSING_PARAMETER", f"缺少 {key}", missing=[key])
    return data[key]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def checked_input_path(value):
    if not isinstance(value, str) or not value.strip():
        raise AtomicError("INVALID_REFERENCE", "文件路径须为非空字符串")
    path = Path(value)
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    allowed = [ROOT]
    if not any(path.is_relative_to(root) for root in allowed):
        raise AtomicError("INVALID_REFERENCE", "数据引用须位于本次工作区或产物目录")
    if not path.is_file() or path.stat().st_size > 20_000_000:
        raise AtomicError("INVALID_REFERENCE", "数据文件不存在或超过 20 MB")
    return path


def pointer(value, select):
    if not isinstance(select, str) or (select and not select.startswith("/")):
        raise AtomicError("INVALID_REFERENCE", "select 使用 JSON Pointer，例如 /data/candidates")
    if not select:
        return value
    try:
        for token in select[1:].split("/"):
            if re.search(r"~(?![01])", token):
                raise ValueError("invalid JSON Pointer escape")
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise ValueError("invalid array index")
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise TypeError("cannot select from a scalar")
        return value
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AtomicError("INVALID_REFERENCE", f"引用字段不存在: {select}") from exc


def load_json(text, code="INVALID_ARGUMENT"):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON 字段重复: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(text, object_pairs_hook=object_pairs)
        canonical(document)  # Reject NaN, Infinity and overflowing JSON numbers.
        return document
    except (ValueError, TypeError, RecursionError) as exc:
        raise AtomicError(code, "JSON 数据无效: " + str(exc)) from exc


def resolve_references(value, dependencies=None, depth=0):
    if depth > 32:
        raise AtomicError("INVALID_REFERENCE", "数据引用层级过深")
    dependencies = dependencies if dependencies is not None else []
    if isinstance(value, dict) and "ref" in value:
        if set(value) - {"ref", "select"}:
            raise AtomicError("INVALID_REFERENCE", "引用对象仅包含 ref 和可选 select")
        path = checked_input_path(value["ref"])
        raw = path.read_bytes()
        try:
            document = load_json(raw.decode("utf-8-sig"), "INVALID_REFERENCE")
        except UnicodeError as exc:
            raise AtomicError("INVALID_REFERENCE", "引用文件须为 UTF-8 JSON") from exc
        dependencies.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        selected = pointer(document, value.get("select", "/data"))
        return resolve_references(selected, dependencies, depth + 1)
    if isinstance(value, dict):
        return {k: resolve_references(v, dependencies, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_references(v, dependencies, depth + 1) for v in value]
    return value


def publish_artifact(path, serialized):
    """Publish already serialized JSON only after a complete temporary write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(compute, kind):
    parser = argparse.ArgumentParser(description=compute.__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="JSON 对象；大数据使用 ref/select 文件引用")
    source.add_argument("--input-file", help="本次工作区内的 JSON 输入文件")
    args = parser.parse_args()
    stage = "input"
    try:
        raw = load_json(args.input if args.input is not None else checked_input_path(args.input_file).read_text(encoding="utf-8-sig"))
        require(raw, "input", dict)
        stage = "references"
        dependencies = []
        value = resolve_references(raw, dependencies)
        input_digest = digest(value)
        stage = "compute"
        result = compute(value)
        require(result, "result", dict)
        result = dict(result)
        summary = result.pop("_summary", {"fields": sorted(result)})
        result.setdefault("status", "OK")
        stage = "output"
        output_dir = ARTIFACTS / "atomic"
        artifact_id = kind + "-" + uuid.uuid4().hex
        path = output_dir / (artifact_id + ".json")
        envelope = {"schema_version": 1, "kind": kind, "artifact_id": artifact_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "input_digest": input_digest, "input": raw, "dependencies": dependencies, "data": result}
        serialized = json.dumps(envelope, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        response = {"status": result["status"], "kind": kind, "artifact_file": str(path.resolve()),
                    "data_fields": sorted(result), "summary": summary, "artifact_created": True}
        for flag in ("mock", "submitted", "validated"):
            if flag in result:
                response[flag] = result[flag]
        response_json = json.dumps(response, ensure_ascii=False, allow_nan=False)
        publish_artifact(path, serialized)
        print(response_json)
        return 0
    except AtomicError as exc:
        error = {"status": exc.code, "message": str(exc), **exc.details}
    except OSError as exc:
        error = {"status": "IO_ERROR", "message": str(exc)}
    except (ValueError, TypeError, KeyError) as exc:
        error = {"status": "OUTPUT_SERIALIZATION_FAILED" if stage == "output" else "INVALID_ARGUMENT",
                 "message": str(exc)}
    except Exception as exc:
        error = {"status": "INTERNAL_ERROR", "message": str(exc), "error_type": type(exc).__name__}
    print(json.dumps({**error, "stage": stage, "artifact_created": False}, ensure_ascii=False))
    return 2
