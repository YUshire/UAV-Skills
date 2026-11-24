#!/usr/bin/env python3
"""
统计 traces 文件中 assistant 消息的 reasoning_content 和 content 重复情况。
"""
import json
import sys
from pathlib import Path
from collections import defaultdict


def analyze_trace_file(file_path):
    """分析单个 trace 文件的重复情况"""
    stats = {
        'total_records': 0,
        'total_assistant_msgs': 0,
        'tool_call_msgs': 0,
        'final_answer_msgs': 0,
        'tool_call_duplicated': 0,
        'tool_call_not_duplicated': 0,
        'final_answer_duplicated': 0,
        'final_answer_not_duplicated': 0,
    }

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            stats['total_records'] += 1
            msgs = rec.get('messages', [])

            for msg in msgs:
                if msg.get('role') != 'assistant':
                    continue

                stats['total_assistant_msgs'] += 1

                content = msg.get('content', '')
                reasoning = msg.get('reasoning_content', '')
                has_tool_calls = bool(msg.get('tool_calls'))

                # 判断是否重复（都非空且相等）
                is_duplicated = (
                    content and reasoning and content == reasoning
                )

                if has_tool_calls:
                    stats['tool_call_msgs'] += 1
                    if is_duplicated:
                        stats['tool_call_duplicated'] += 1
                    else:
                        stats['tool_call_not_duplicated'] += 1
                else:
                    stats['final_answer_msgs'] += 1
                    if is_duplicated:
                        stats['final_answer_duplicated'] += 1
                    else:
                        stats['final_answer_not_duplicated'] += 1

    return stats


def print_stats(file_path, stats):
    """打印统计结果"""
    print(f"\n{'='*70}")
    print(f"文件: {file_path.name}")
    print(f"{'='*70}")

    print(f"\n📊 总体统计:")
    print(f"  对话记录数: {stats['total_records']}")
    print(f"  assistant 消息总数: {stats['total_assistant_msgs']}")

    print(f"\n🔧 工具调用轮 (有 tool_calls):")
    total_tool = stats['tool_call_msgs']
    dup_tool = stats['tool_call_duplicated']
    print(f"  总数: {total_tool}")
    print(f"  重复: {dup_tool} ({dup_tool/total_tool*100:.1f}%)" if total_tool > 0 else "  重复: 0")
    print(f"  不重复: {stats['tool_call_not_duplicated']}")

    print(f"\n✅ 最终回答轮 (无 tool_calls):")
    total_final = stats['final_answer_msgs']
    dup_final = stats['final_answer_duplicated']
    print(f"  总数: {total_final}")
    print(f"  重复: {dup_final} ({dup_final/total_final*100:.1f}%)" if total_final > 0 else "  重复: 0")
    print(f"  不重复: {stats['final_answer_not_duplicated']}")

    print(f"\n📈 汇总:")
    if stats['total_assistant_msgs'] > 0:
        total_dup = dup_tool + dup_final
        ratio = total_dup / stats['total_assistant_msgs'] * 100
        print(f"  总重复率: {total_dup}/{stats['total_assistant_msgs']} ({ratio:.1f}%)")

    # 关键结论
    if total_tool > 0:
        tool_dup_ratio = dup_tool / total_tool * 100
        if tool_dup_ratio > 50:
            print(f"\n⚠️  工具调用轮重复率高达 {tool_dup_ratio:.1f}%，这是 nanobot 的兜底逻辑导致的。")
        else:
            print(f"\n✓ 工具调用轮重复率较低 ({tool_dup_ratio:.1f}%)。")


def main():
    if len(sys.argv) > 1:
        file_paths = [Path(p) for p in sys.argv[1:]]
    else:
        # 默认分析这几个文件
        base_dir = Path('/data02/shengyang6/data/数据分析test')
        file_paths = [
            base_dir / 'test_new_130_27B_traces.jsonl',
            base_dir / 'test_new_130_4b_sft_traces.jsonl',
            base_dir / 'test_new_130_27B_sft_traces.jsonl',
        ]

    for file_path in file_paths:
        if not file_path.exists():
            print(f"⚠️  文件不存在: {file_path}")
            continue

        stats = analyze_trace_file(file_path)
        print_stats(file_path, stats)

    print(f"\n{'='*70}\n")


if __name__ == '__main__':
    main()
