"""Opt-in live regression with ordinary text tasks; never loads agent tools.

Run: python check_generation_budget.py --live --output-dir <new directory>
Reads credentials from nanobot's existing config without writing that config.
"""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import itertools
import json
import os
import sys
from pathlib import Path
import re
import time

_source = Path(os.environ.get("NANOBOT_SOURCE", Path(__file__).resolve().parents[1] / "nanobot-source"))
if _source.is_dir():
    sys.path.insert(0, str(_source))

from nanobot.config.loader import load_config
from nanobot.nanobot import _make_provider
from nanobot.providers.api_logger import close_logger, install_api_logger, set_session_key


def cases():
    schedules = [list(p) for p in itertools.permutations("甲乙丙丁")
                 if p[0] != "甲" and p.index("乙") < p.index("丙")
                 and p.index("丁") in (1, 3) and p[3] != "丙"]
    rows = [{"id": i, "group": "ABC"[i % 3], "qty": i % 7 + 1, "price": i % 11 + 2}
            for i in range(1, 91)]
    totals = {g: sum(r["qty"] * r["price"] for r in rows if r["group"] == g) for g in "ABC"}
    return [
        ("arithmetic", "计算17+25，只输出JSON对象，键为sum。", {"sum": 42}),
        ("schedule", "有甲乙丙丁四人安排周一至周四值班，每人恰好一天。甲不在周一，乙必须比丙早，丁在周二或周四，丙不在周四。列出所有合法排班，只输出JSON数组，每项按周一到周四列出姓名。", schedules),
        ("orders", "按group汇总qty乘price的金额，只输出以A、B、C为键的JSON对象。订单：" + json.dumps(rows, ensure_ascii=False), totals),
        ("orders_from_tool", [
            {"role": "user", "content": "读取工具已核对的订单汇总，只输出以A、B、C为键的金额JSON对象，不重新手算。"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_totals", "type": "function", "function": {"name": "sum_orders", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_totals", "content": json.dumps({"status": "OK", "verified": True, "totals": totals})},
        ], totals),
    ]


def parse_answer(text):
    text = (text or "").strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    return json.loads(text)


def correct(name, content, expected):
    try:
        actual = parse_answer(content)
        if name == "schedule":
            return (len(actual) == len(expected)
                    and {tuple(x) for x in actual} == {tuple(x) for x in expected})
        return actual == expected
    except (ValueError, TypeError, KeyError):
        return False


def repetition_fraction(text):
    tokens = re.findall(r"[A-Za-z]+|[\u4e00-\u9fff]|[0-9]+", (text or "").lower())
    grams = Counter(tuple(tokens[i:i + 8]) for i in range(max(0, len(tokens) - 7)))
    count = sum(grams.values())
    return round(sum(v - 1 for v in grams.values()) / count, 4) if count else 0


async def run(output_dir, budgets=(0, 2048), selected=None):
    output_dir.mkdir(parents=True, exist_ok=False)
    config = load_config(Path(os.environ.get("NANOBOT_CONFIG", Path(__file__).resolve().parents[1] / "runtime-config/local.json")))
    provider = _make_provider(config)
    initial = provider.generation
    install_api_logger(str(output_dir / "api_calls.jsonl"), mode="w")
    results = []
    try:
        for configured_budget in budgets:
            budget = configured_budget or None
            profile = "baseline" if budget is None else f"bounded_{budget}"
            provider.generation = replace(initial, max_tokens=2048, thinking_budget=budget)
            for name, prompt, expected in cases():
                if selected and name not in selected:
                    continue
                set_session_key(f"generation-check-{profile}-{name}")
                started = time.monotonic()
                try:
                    messages = [{"role": "system", "content": "完成必要核对后直接交付，遵循用户指定的输出格式。"}]
                    messages.extend(prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}])
                    response = await asyncio.wait_for(provider.chat_with_retry(
                        messages=messages,
                    ), timeout=90)
                    row = {"profile": profile, "case": name, "thinking_budget": budget,
                           "finish_reason": response.finish_reason, "usage": response.usage,
                           "reasoning_chars": len(response.reasoning_content or ""),
                           "repeated_8gram_fraction": repetition_fraction(response.reasoning_content),
                           "content": response.content,
                           "correct": response.finish_reason == "stop" and correct(name, response.content, expected)}
                except asyncio.TimeoutError:
                    row = {"profile": profile, "case": name, "correct": False, "error": "timeout"}
                row["seconds"] = round(time.monotonic() - started, 3)
                results.append(row)
                (output_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({k: v for k, v in row.items() if k != "content"}, ensure_ascii=False), flush=True)
    finally:
        close_logger()
        await provider._client.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly allow calls to the configured model")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--budgets", type=int, nargs="+", default=[0, 2048], help="0 means provider default")
    parser.add_argument("--cases", nargs="+", choices=[name for name, _, _ in cases()])
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run the paid model checks")
    if any(budget < 0 for budget in args.budgets):
        parser.error("Budgets must be nonnegative")
    asyncio.run(run(args.output_dir, args.budgets, args.cases))
