"""Runtime-specific helper functions and constants."""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "本次回复生成因输出长度耗尽或模型响应为空而中断，未能整理出最终答复。"
    "此前并非所有任务步骤都已完成，请重试或缩小任务范围。"
)

FINALIZATION_RETRY_PROMPT = (
    "请直接基于上面已有的工具返回结果，用简洁日常中文回答用户："
    "只报告已经完成并有证据支撑的内容，如实说明尚未完成或受阻的环节；"
    "不要调用任何工具，不要输出 <tool_call> 标签，也不要声称未执行的步骤已完成。"
)

EMPTY_LENGTH_STEERING_PROMPT = (
    "上一轮推理耗尽单次输出长度且未输出正文或工具调用。"
    "请立即停止冗长推演，直接发起下一步最关键的工具调用，或基于已有工具结果用简洁中文直接回答用户。"
)

LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return content


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_empty_length_steering_message() -> dict[str, str]:
    """A short steering prompt after empty reasoning-length exhaustion."""
    return {"role": "user", "content": EMPTY_LENGTH_STEERING_PROMPT}


def looks_like_raw_tool_call_leak(content: str | None) -> bool:
    """Detect raw tool-call XML/markup leaked as plain text when tools are disabled."""
    if not content:
        return False
    stripped = content.lstrip()
    return stripped.startswith("<tool_call>") or "<tool_call>" in stripped[:200]


def build_length_recovery_message() -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit."""
    return {"role": "user", "content": LENGTH_RECOVERY_PROMPT}


def external_lookup_signature(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Stable signature for repeated external lookups we want to throttle."""
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    return None


def repeated_external_lookup_error(
    tool_name: str,
    arguments: dict[str, Any],
    seen_counts: dict[str, int],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )
