"""批量测试入口：queries.txt 每行一条 query，空行忽略。

用法：改下面两个数，然后运行 python chat_multi.py
像 Excel 一样粘贴：一行一条，直接覆盖 queries.txt。
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import chat_single
from nanobot.providers.api_logger import install_api_logger

# ===== 改这里 =====
CONCURRENCY = 20                 # 同时跑几条会话
QUERIES_FILE = "queries.txt"
# ==================


async def run_one(sem, i, total, q):
    async with sem:
        session_key = f"{time.strftime('%m%d-%H%M%S')}-batch-{i:03d}-{q[:15]}"
        print(f"\n{'=' * 20} [{i}/{total}] {q[:40]} {'=' * 20}", file=sys.stderr)
        try:
            await chat_single.main(q, session_key, install_logger=False)
            return True
        except Exception as e:
            # 单条失败不中断整批；失败也写进 chat_output.jsonl
            print(f"[batch] 第 {i} 条失败: {e}", file=sys.stderr)
            record = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "session_key": session_key,
                "query": q,
                "think": None,
                "output": None,
                "skills_used": [],
                "error": str(e),
            }
            with open(chat_single.OUTPUT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return False


async def main():
    src = Path(__file__).with_name(QUERIES_FILE)
    queries = [line.strip() for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"[batch] {src} 共 {len(queries)} 条，并发 {CONCURRENCY}", file=sys.stderr)

    install_api_logger(str(chat_single.API_LOG_PATH), mode="a")
    sem = asyncio.Semaphore(max(1, CONCURRENCY))
    results = await asyncio.gather(*(run_one(sem, i, len(queries), q) for i, q in enumerate(queries, 1)))
    print(f"\n[batch] 全部完成: 成功 {sum(results)} 条，失败 {len(results) - sum(results)} 条", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
