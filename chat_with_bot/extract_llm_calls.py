"""
从 nanobot serverlog 中抽取每次 LLM 调用的完整 messages_for_model 快照，
按 session_key 拆成一个 jsonl 文件，每行一次调用。

用法:
    python extract_llm_calls.py /data02/shengyang6/data/serverlog/1.log -o ./llm_calls/
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s*\|\s*WARNING\s*\|\s*"
    r"nanobot\.agent\.runner:run:\d+\s*-\s*"
    r"单轮对话session-(?P<session>.+?)调用完成，"
    r"详情:messages_for_model 长度(?P<length>\d+):"
    r"(?P<payload>.*)$"
)

# 仅消息体（loguru sink 拿到的 record["message"]，没有 ts/level 前缀）
MESSAGE_RE = re.compile(
    r"^单轮对话session-(?P<session>.+?)调用完成，"
    r"详情:messages_for_model 长度(?P<length>\d+):"
    r"(?P<payload>.*)$",
    re.DOTALL,
)


def _eval_node(node):
    """把 ast 节点求值成 Python 对象。
    plain literal 走 ast.literal_eval 语义；遇到 Call(name=X, ...) 时把它转成 dict
    {"_type": "X", **kwargs}，兼容 ToolCallRequest(...) 这类对象 repr。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return +_eval_node(node.operand)
    if isinstance(node, ast.List) or isinstance(node, ast.Tuple):
        return [_eval_node(e) for e in node.elts]
    if isinstance(node, ast.Set):
        return [_eval_node(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {_eval_node(k): _eval_node(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Name):
        return {"True": True, "False": False, "None": None}.get(node.id, f"<name:{node.id}>")
    if isinstance(node, ast.Call):
        func_name = ast.unparse(node.func) if hasattr(ast, "unparse") else getattr(node.func, "id", "<call>")
        out = {"_type": func_name}
        for kw in node.keywords:
            if kw.arg is None:
                continue
            out[kw.arg] = _eval_node(kw.value)
        if node.args:
            out["_args"] = [_eval_node(a) for a in node.args]
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval_node(node.left) + _eval_node(node.right)
    raise ValueError(f"unsupported AST node: {type(node).__name__}")


def parse_payload(text):
    tree = ast.parse(text, mode="eval")
    return _eval_node(tree.body)


def _normalize_tool_call(tc):
    """OpenAI dict({id,type,function:{name,arguments}}) 与 ToolCallRequest dict
    （我们 AST 解析出的 {_type, id, name, arguments, ...}）统一成 {name, arguments}。"""
    if not isinstance(tc, dict):
        return {"name": None, "arguments": str(tc)}
    if "function" in tc and isinstance(tc["function"], dict):
        fn = tc["function"]
        name = fn.get("name")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
    else:
        name = tc.get("name")
        args = tc.get("arguments")
    return {"name": name, "arguments": args}


def _content_to_str(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _extract_think(m):
    """从 assistant 消息里抽 think 文本：优先 reasoning_content（字符串），
    否则用 thinking_blocks（[{type:'thinking', thinking:'...'}, ...]，AST 解析后
    可能形如 {_type:'ThinkingBlock', type:'thinking', thinking:'...'}，都兼容）。"""
    rc = m.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return rc
    tbs = m.get("thinking_blocks")
    if isinstance(tbs, list):
        parts = []
        for tb in tbs:
            if not isinstance(tb, dict):
                continue
            if tb.get("type") != "thinking":
                continue
            t = tb.get("thinking")
            if isinstance(t, str) and t.strip():
                parts.append(t)
        if parts:
            return "\n".join(parts)
    return None


def _wrap_with_think(text, think):
    if not think:
        return text
    # 如果 text 已经包含了 think 的内容，说明模型已经输出了，不要重复包装
    if text and think in text:
        return text
    block = f"<think>\n{think}\n</think>"
    if text:
        return f"{block}\n\n{text}"
    return block


def flatten_messages(msgs):
    """把 OpenAI 风格的 messages 摊平成 {role, content} 列表：
    - assistant 带 tool_calls → 拆成 (assistant 文本 + N 条 tool_call)
    - assistant 带 reasoning_content / thinking_blocks → 文本前面加 <think>...</think>
    - tool 角色 → 改名 tool_result，content 仍为字符串"""
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            out.append({"role": "tool_result", "content": _content_to_str(m.get("content"))})
            continue
        if role == "assistant":
            text = _content_to_str(m.get("content"))
            # 提取并包装 think 内容
            think = _extract_think(m)
            wrapped_text = _wrap_with_think(text, think)
            if wrapped_text:
                out.append({"role": "assistant", "content": wrapped_text})
            for tc in m.get("tool_calls") or []:
                payload = _normalize_tool_call(tc)
                out.append({"role": "tool_call", "content": payload})
            continue
        out.append({"role": role, "content": _content_to_str(m.get("content"))})
    return out


def simplify_tools(tools):
    """OpenAI tool-def 列表 → [{name, description}, ...]，再转 JSON 字符串。"""
    simplified = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict):
            continue
        simplified.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
        })
    return json.dumps(simplified, ensure_ascii=False)


def build_record_from_message(message_text, timestamp):
    """给定一条 nanobot.agent.runner WARNING 的消息体（不含 loguru 前缀）和 timestamp 字符串，
    返回扁平化后的 record dict。如果消息不是目标格式或解析失败，返回 None。"""
    m = MESSAGE_RE.match(message_text)
    if not m:
        return None
    session_key = m.group("session")
    payload = m.group("payload")
    try:
        messages = parse_payload(payload)
    except Exception:
        return None

    tools_raw = []
    response_tool_calls = []
    response_content = None
    response_synth = None
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict) \
            and "this_session_used_tool" in messages[-1]:
        synth = messages.pop()
        response_synth = synth
        tools_raw = synth.get("this_session_used_tool") or []
        response_content = synth.get("content")
        response_tool_calls = synth.get("tool_calls") or []

    flat_messages = flatten_messages(messages)
    if response_content:
        think_content = _extract_think(response_synth or {})
        text = _wrap_with_think(_content_to_str(response_content), think_content)
        flat_messages.append({"role": "assistant", "content": text})
    for tc in response_tool_calls:
        flat_messages.append({
            "role": "tool_call",
            "content": _normalize_tool_call(tc),
        })

    return {
        "timestamp": timestamp,
        "session_key": session_key,
        "tools": simplify_tools(tools_raw),
        "messages": flat_messages,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="serverlog 路径")
    ap.add_argument("-o", "--out-dir", default="./llm_calls",
                    help="输出目录，每个 session_key 一个 jsonl 文件")
    ap.add_argument("--mode", choices=["call", "session"], default="call",
                    help="call=每次LLM调用一行（默认）；session=每个session只保留最后一次调用（最完整）")
    ap.add_argument("--keep-tool-defs", action="store_true",
                    help="保留 this_session_used_tool 字段（默认会保留）")
    ap.add_argument("--unified-output", help="输出到单个统一的 jsonl 文件（类似 batch_chat.py 的 _llm_calls.jsonl）")
    args = ap.parse_args()

    log_path = Path(args.log)

    # 如果指定了 unified-output，则输出到单个文件；否则按 session 分文件
    if args.unified_output:
        unified_path = Path(args.unified_output)
        unified_path.parent.mkdir(parents=True, exist_ok=True)
        out_dir = None
    else:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        unified_path = None

    session_files = {}
    last_record_per_session = {}
    n_total = n_ok = n_fail = 0
    fail_samples = []

    def _safe_filename(name):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()[:200]

    unified_fp = None
    if unified_path:
        unified_fp = open(unified_path, "w", encoding="utf-8")

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                m = LINE_RE.match(line)
                if not m:
                    continue
                n_total += 1
                session_key = m.group("session")
                ts = m.group("ts")
                length = int(m.group("length"))
                payload = m.group("payload")

                try:
                    messages = parse_payload(payload)
                except Exception as e:
                    n_fail += 1
                    if len(fail_samples) < 3:
                        fail_samples.append((session_key, ts, str(e)[:120]))
                    continue

                tools_raw = []
                response_tool_calls = []
                response_content = None
                response_synth = None
                if isinstance(messages, list) and messages and isinstance(messages[-1], dict) \
                        and "this_session_used_tool" in messages[-1]:
                    synth = messages.pop()
                    response_synth = synth
                    tools_raw = synth.get("this_session_used_tool") or []
                    response_content = synth.get("content")
                    response_tool_calls = synth.get("tool_calls") or []

                flat_messages = flatten_messages(messages)
                if response_content:
                    text = _wrap_with_think(_content_to_str(response_content),
                                            _extract_think(response_synth or {}))
                    flat_messages.append({"role": "assistant", "content": text})
                for tc in response_tool_calls:
                    flat_messages.append({
                        "role": "tool_call",
                        "content": _normalize_tool_call(tc),
                    })

                record = {
                    "timestamp": ts,
                    "session_key": session_key,
                    "tools": simplify_tools(tools_raw),
                    "messages": flat_messages,
                }
                fname = _safe_filename(session_key) + ".jsonl"

                if args.mode == "session":
                    last_record_per_session[fname] = record
                    n_ok += 1
                    continue

                fp = session_files.get(fname)
                if fp is None:
                    fp = open(out_dir / fname, "w", encoding="utf-8")
                    session_files[fname] = fp
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                n_ok += 1
    finally:
        for fp in session_files.values():
            fp.close()

    if args.mode == "session":
        for fname, record in last_record_per_session.items():
            with open(out_dir / fname, "w", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"日志条目（messages_for_model）: {n_total}")
    print(f"  成功解析: {n_ok}")
    print(f"  解析失败: {n_fail}")
    if fail_samples:
        print("  失败样本:")
        for sk, ts, err in fail_samples:
            print(f"    [{ts}] session={sk}: {err}")
    out_count = len(last_record_per_session) if args.mode == "session" else len(session_files)
    print(f"模式: {args.mode}（{'每 session 一行' if args.mode == 'session' else '每次 LLM 调用一行'}）")
    print(f"输出文件数: {out_count}")
    print(f"输出目录: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
