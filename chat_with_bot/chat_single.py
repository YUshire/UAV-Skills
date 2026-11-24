import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# An explicit source override is optional; otherwise use the installed package.
NANOBOT_SOURCE = Path(os.environ.get("NANOBOT_SOURCE", Path(__file__).resolve().parents[1] / "nanobot-source"))
if NANOBOT_SOURCE.is_dir():
    sys.path.insert(0, str(NANOBOT_SOURCE))

from nanobot import Nanobot
from nanobot.utils.helpers import safe_filename
from nanobot.providers.api_logger import install_api_logger, set_session_key, get_session_messages

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_llm_calls import _extract_think

WORKSPACE = Path(os.environ.get("UAV_WORKSPACE", Path(__file__).resolve().parents[1])).resolve()
NANOBOT_SESSIONS_DIR = WORKSPACE / "sessions"
OUTPUT_PATH = Path(__file__).resolve().parent / "chat_output.jsonl"
TRACES_PATH = Path(__file__).resolve().parent / "chat_output_traces.jsonl"
API_LOG_PATH = Path(__file__).resolve().parent / "chat_api_calls.jsonl"

# config.json 的改写/恢复必须串行，批量并发时不能交叉
_CONFIG_LOCK = asyncio.Lock()

# Portable settings: credentials and model endpoints come from the environment.
MODEL_OVERRIDE = os.environ.get("UAV_MODEL", "Qwen3.8-27B")
PROVIDER_OVERRIDE = os.environ.get("UAV_PROVIDER", "vllm")
API_BASE_OVERRIDE = os.environ.get("UAV_API_BASE", "http://127.0.0.1:7696/v1")
API_KEY_OVERRIDE = os.environ.get("UAV_API_KEY", "EMPTY")
REASONING_EFFORT_OVERRIDE = None
MAX_TOKENS_OVERRIDE = int(os.environ.get("UAV_MAX_TOKENS", "8192"))
THINKING_BUDGET_OVERRIDE = None
TEMPERATURE_OVERRIDE = None
SHOW_REASONING = False
CONFIG_PATH = Path(os.environ.get("NANOBOT_CONFIG", WORKSPACE / "runtime-config" / "local.json"))


def deep_decode(obj):
    """递归把 JSON 字符串 decode 成 Python 对象，避免多重编码"""
    if isinstance(obj, str):
        try:
            decoded = json.loads(obj)
            return deep_decode(decoded)
        except (json.JSONDecodeError, TypeError, ValueError):
            return obj
    elif isinstance(obj, dict):
        return {k: deep_decode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [deep_decode(item) for item in obj]
    else:
        return obj


def extract_skills_from_messages(messages):
    skills = set()
    if not messages:
        return []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            continue
        content = ""
        val = msg.get("content")
        if isinstance(val, str):
            content += val
        elif isinstance(val, dict):
            content += json.dumps(val, ensure_ascii=False)
        tcs = msg.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict):
                    func = tc.get("function")
                    if isinstance(func, dict):
                        args = func.get("arguments")
                        if isinstance(args, str):
                            content += " " + args
        for m in re.finditer(r"skills/([^/\s\"]+)", content):
            skill_name = m.group(1)
            skill_name = re.sub(r"[`'\".,;:!?[\](){}]", "", skill_name)
            if skill_name and (WORKSPACE / "skills" / skill_name).exists():
                skills.add(skill_name)
    return sorted(list(skills))


def _last_assistant_content(messages):
    """从 messages 末尾找最近一条 role==assistant 且 content 非空的文本内容。
    content 可能是 str 或 list（如 anthropic 的 [{type,text}, ...]），都统一拍成字符串。"""
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str):
            if c.strip():
                return c
        elif isinstance(c, list):
            parts = []
            for blk in c:
                if isinstance(blk, dict):
                    t = blk.get("text") or blk.get("content") or ""
                    if isinstance(t, str) and t:
                        parts.append(t)
                elif isinstance(blk, str):
                    parts.append(blk)
            joined = "".join(parts)
            if joined.strip():
                return joined
    return None


async def main(Q, session_key, install_logger=True):
    # 安装 API 调用日志记录器（直接在 provider 层拦截，避免转义问题）
    # 批量并发时由入口安装一次，避免重复开关同一个日志文件句柄
    if install_logger:
        install_api_logger(str(API_LOG_PATH), mode="a")

    async with _CONFIG_LOCK:
        patched = False
        original = None
        if MODEL_OVERRIDE or PROVIDER_OVERRIDE or API_BASE_OVERRIDE or API_KEY_OVERRIDE or MAX_TOKENS_OVERRIDE or REASONING_EFFORT_OVERRIDE is not None or THINKING_BUDGET_OVERRIDE is not None:
            import copy
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            original = copy.deepcopy(raw)
            if MODEL_OVERRIDE:
                raw["agents"]["defaults"]["model"] = MODEL_OVERRIDE
            if PROVIDER_OVERRIDE:
                raw["agents"]["defaults"]["provider"] = PROVIDER_OVERRIDE
            # 覆盖 maxTokens（每轮生成长度）
            if MAX_TOKENS_OVERRIDE:
                raw["agents"]["defaults"]["maxTokens"] = MAX_TOKENS_OVERRIDE
            if THINKING_BUDGET_OVERRIDE is not None:
                raw["agents"]["defaults"]["thinkingBudget"] = THINKING_BUDGET_OVERRIDE
            # 覆盖 temperature
            if TEMPERATURE_OVERRIDE is not None:
                raw["agents"]["defaults"]["temperature"] = TEMPERATURE_OVERRIDE
            # 覆盖 reasoningEffort（"off" 转成 None，避免传给不支持的 vLLM）
            if REASONING_EFFORT_OVERRIDE is not None:
                if isinstance(REASONING_EFFORT_OVERRIDE, str) and REASONING_EFFORT_OVERRIDE.lower() in ("off", "none", "disable", "disabled"):
                    raw["agents"]["defaults"]["reasoningEffort"] = None
                else:
                    raw["agents"]["defaults"]["reasoningEffort"] = REASONING_EFFORT_OVERRIDE
            # 同时覆盖 provider 的 apiBase 和 apiKey
            if PROVIDER_OVERRIDE and (API_BASE_OVERRIDE or API_KEY_OVERRIDE):
                if PROVIDER_OVERRIDE not in raw["providers"]:
                    raw["providers"][PROVIDER_OVERRIDE] = {}
                if API_BASE_OVERRIDE:
                    raw["providers"][PROVIDER_OVERRIDE]["apiBase"] = API_BASE_OVERRIDE
                if API_KEY_OVERRIDE:
                    raw["providers"][PROVIDER_OVERRIDE]["apiKey"] = API_KEY_OVERRIDE
            CONFIG_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            patched = True

        try:
            bot = Nanobot.from_config(config_path=CONFIG_PATH, workspace=WORKSPACE)
        finally:
            if patched:
                CONFIG_PATH.write_text(json.dumps(original, indent=2, ensure_ascii=False), encoding="utf-8")

    if patched:
        print(f"模型覆盖: {bot._loop.model} @ {API_BASE_OVERRIDE or '默认地址'}", file=sys.stderr)
        if MAX_TOKENS_OVERRIDE:
            print(f"生成长度: maxTokens={MAX_TOKENS_OVERRIDE}", file=sys.stderr)
        if THINKING_BUDGET_OVERRIDE is not None:
            print(f"推理预算: thinkingBudget={THINKING_BUDGET_OVERRIDE}", file=sys.stderr)

    # 设置当前 session_key（供 api_logger 使用）
    set_session_key(session_key)

    # 清掉旧 session 文件，避免复用历史对话
    old_session = NANOBOT_SESSIONS_DIR / f"{safe_filename(session_key.replace(':', '_'))}.jsonl"
    if old_session.exists():
        old_session.unlink()

    result = await bot.run(Q, session_key=session_key)

    # 从 API logger 获取本次对话的完整 messages
    messages = get_session_messages(session_key)
    skills_used = extract_skills_from_messages(messages) if messages else []

    # reply 兜底：bot.run() 返回的 result.content 偶尔为空，但 messages 里最后一条 assistant 实际有内容
    reply_content = result.content
    if reply_content is None or (isinstance(reply_content, str) and not reply_content.strip()):
        fallback = _last_assistant_content(messages)
        if fallback:
            reply_content = fallback

    # 提取最后一条 assistant 的 reasoning_content 作为 think
    think = None
    if messages:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                think = _extract_think(m)
                break

    if SHOW_REASONING and think:
        print('=' * 25 + '推理记录（调试）' + '=' * 25)
        print(think)
    print('=' * 25 + '最终回复' + '=' * 25)
    print(reply_content or "")

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "session_key": session_key,
        "query": Q,
        "think": think,
        "output": reply_content,
        "skills_used": skills_used,
    }
    with open(OUTPUT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\n[saved] {OUTPUT_PATH}")

    if messages:
        trace_record = {
            "session_key": session_key,
            "q": Q,
            "reply": reply_content,
            "msg_len": sum(len(json.dumps(m, ensure_ascii=False)) for m in messages),
            "skill_used_in_this_tra": skills_used,
            "messages": messages,  # 直接来自 API logger 内存缓存，无转义问题
        }
        with open(TRACES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(trace_record, ensure_ascii=False) + "\n")
        print(f"[saved] {TRACES_PATH}")

    # 等待所有子进程清理完成，避免 event loop 关闭警告
    await asyncio.sleep(0.1)


Q = """
灰色行李箱要找的有这几处：258号机、73号机以桃园龟山陆军装甲兵战备基地东南50米处为中心、东西12米、南北8米的矩形找灰色行李箱；273号机、219号机以埔里核心指挥所东南50米处为中心、东西8米、南北6米的矩形找灰色行李箱；129号机、335号机以金门料罗湾海军补给港口东南50米处为中心、东西6米、南北4米的矩形找灰色行李箱。
""".strip()

# 时间戳保证每次运行 session_key 唯一，不复用历史 session
if __name__ == "__main__":
    session_key = f"{time.strftime('%m%d-%H%M%S')}-{Q[:15]}"
    # 子进程沿用同一会话标识；导入模块进行测试时不启动任务。
    os.environ["CLAUDE_CODE_SESSION_ID"] = session_key
    asyncio.run(main(Q, session_key))

"""
1. 同一个session_key对应到一场对话，Q就是query
2. session数据在/root/.nanobot/workspace/sessions
3. 具体轨迹数据在/root/trace_data/serverlog/1.logl
4. 模型产物默认在/root/.nanobot/workspace
"""
