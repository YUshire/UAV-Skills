"""
API 调用日志记录器 - 直接在 provider 层拦截记录模型输入输出。

用法：
    from nanobot.providers.api_logger import install_api_logger

    # 在 nanobot 初始化后安装
    install_api_logger("/path/to/api_calls.jsonl")
"""
import json
import time
import contextvars
import inspect
from pathlib import Path
from typing import Any
from functools import wraps

_log_file = None
_log_fp = None
_logger_installed = False
_wire_context = contextvars.ContextVar("api_logger_wire_parameters", default=None)
_GENERATION_KEYS = (
    "max_tokens", "temperature", "reasoning_effort", "top_p", "top_k",
    "min_p", "presence_penalty", "repetition_penalty", "thinking_budget",
)
_WIRE_KEYS = (*_GENERATION_KEYS, "max_completion_tokens", "enable_thinking", "tool_choice", "model")


def record_wire_parameters(parameters):
    """Capture only generation settings, never headers, credentials or message bodies."""
    captured = _wire_context.get()
    if captured is None:
        return
    captured.update({k: parameters[k] for k in _WIRE_KEYS if k in parameters})
    extra = parameters.get("extra_body") or {}
    selected = {k: extra[k] for k in _WIRE_KEYS if k in extra}
    if selected:
        captured["extra_body"] = selected


# 用 ContextVar 跨调用传递 session_key：asyncio 并发时每个协程链路有独立的值，
# 不会像普通全局变量那样被其它并发协程覆盖（否则 messages 会串台到别的 session）。
_session_key_context = contextvars.ContextVar("session_key", default=None)
_session_messages = {}  # session_key -> 最后一次调用的 messages（供外部提取 skills）


def get_session_messages(session_key: str):
    """获取指定 session 最后一次 API 调用的 messages（供外部提取 skills 等）。"""
    return _session_messages.get(session_key, [])


def set_session_key(session_key: str):
    """设置当前的 session_key（由外部调用者在 run() 前设置）。
    基于 ContextVar，设置只在当前协程上下文生效，并发安全。"""
    _session_key_context.set(session_key)


def get_session_key():
    """获取当前的 session_key。"""
    return _session_key_context.get()


def install_api_logger(log_path: str, mode: str = "a"):
    """
    安装 API 日志记录器，拦截所有 provider 的 chat() 和 chat_stream() 调用。

    Args:
        log_path: 日志文件路径（JSONL 格式）
        mode: 文件打开模式，"a"=追加（断点续跑），"w"=覆盖（从头跑）
    """
    global _log_file, _log_fp, _logger_installed

    _log_file = Path(log_path)
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    if _log_fp is not None:
        _log_fp.close()
    _log_fp = open(_log_file, mode, encoding="utf-8")
    if _logger_installed:
        return

    # 猴子补丁：拦截 LLMProvider 的 chat_with_retry() 和 chat_stream_with_retry()
    # 这两个方法只在基类定义、子类不重写，且 runner 实际调用的就是它们，
    # 所以补丁一定生效（不像 chat()/chat_stream() 会被 AnthropicProvider 重写覆盖）。
    from nanobot.providers.base import LLMProvider

    original_chat = LLMProvider.chat_with_retry
    original_chat_stream = LLMProvider.chat_stream_with_retry

    def _build_request_data(self, kwargs):
        data = {
            "messages": kwargs.get("messages"),
            "tools": kwargs.get("tools"),
            "model": kwargs.get("model") or getattr(self, "default_model", None),
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
            "reasoning_effort": kwargs.get("reasoning_effort"),
            "tool_choice": kwargs.get("tool_choice"),
            "top_p": kwargs.get("top_p"),
            "top_k": kwargs.get("top_k"),
            "min_p": kwargs.get("min_p"),
            "presence_penalty": kwargs.get("presence_penalty"),
            "repetition_penalty": kwargs.get("repetition_penalty"),
        }
        generation = getattr(self, "generation", None)
        for key in _GENERATION_KEYS:
            if key not in kwargs or (key in ("max_tokens", "temperature") and kwargs[key] is None):
                data[key] = getattr(generation, key, None)
            else:
                data[key] = kwargs[key]
        return {k: v for k, v in data.items() if v is not None}

    def _build_response_data(response):
        # tool_calls 必须用 to_openai_tool_call() 转成标准格式，
        # 否则最后一轮 assistant 的 tool_calls 格式与中间轮（来自 request.messages）不一致
        data = {
            "content": response.content,
            "finish_reason": response.finish_reason,
            "tool_calls": [tc.to_openai_tool_call() for tc in (response.tool_calls or [])] if response.tool_calls else None,
            # 补充思考链、usage、错误信息（若有）
            "reasoning_content": getattr(response, "reasoning_content", None),
            "thinking_blocks": getattr(response, "thinking_blocks", None),
            "usage": getattr(response, "usage", None) or None,
            "error_kind": getattr(response, "error_kind", None),
            "error_type": getattr(response, "error_type", None),
            "error_code": getattr(response, "error_code", None),
            "error_status_code": getattr(response, "error_status_code", None),
        }
        return {k: v for k, v in data.items() if v is not None}

    @wraps(original_chat)
    async def logged_chat(self, **kwargs):
        """拦截 chat_with_retry() 调用并记录输入输出。"""
        t0 = time.time()
        request_data = _build_request_data(self, kwargs)
        wire = {}
        token = _wire_context.set(wire)
        try:
            response = await original_chat(self, **kwargs)
        finally:
            _wire_context.reset(token)
        request_data["wire_parameters"] = wire
        request_data["provider_source"] = str(inspect.getfile(type(self)))
        elapsed = time.time() - t0
        response_data = _build_response_data(response)
        _write_log_entry(
            session_key=get_session_key(),
            provider=self.__class__.__name__,
            method="chat",
            request=request_data,
            response=response_data,
            elapsed=elapsed,
            status="ok" if response.finish_reason != "error" else "error",
        )
        return response

    @wraps(original_chat_stream)
    async def logged_chat_stream(self, **kwargs):
        """拦截 chat_stream_with_retry() 调用并记录输入输出。"""
        t0 = time.time()
        request_data = _build_request_data(self, kwargs)
        wire = {}
        token = _wire_context.set(wire)
        try:
            response = await original_chat_stream(self, **kwargs)
        finally:
            _wire_context.reset(token)
        request_data["wire_parameters"] = wire
        request_data["provider_source"] = str(inspect.getfile(type(self)))
        elapsed = time.time() - t0
        response_data = _build_response_data(response)
        _write_log_entry(
            session_key=get_session_key(),
            provider=self.__class__.__name__,
            method="chat_stream",
            request=request_data,
            response=response_data,
            elapsed=elapsed,
            status="ok" if response.finish_reason != "error" else "error",
        )
        return response

    # 替换原始方法
    LLMProvider.chat_with_retry = logged_chat
    LLMProvider.chat_stream_with_retry = logged_chat_stream
    _logger_installed = True


def _write_log_entry(
    session_key: str | None,
    provider: str,
    method: str,
    request: dict[str, Any],
    response: dict[str, Any],
    elapsed: float,
    status: str,
):
    """写入一条日志记录。"""
    if _log_fp is None:
        return

    # 保存这个 session 的完整 messages（request messages + 本次 response），供外部提取 skills
    # 多次调用时，保留 messages 最长（最完整）的那次
    if session_key:
        req_messages = request.get("messages", []) or []
        # 把本次 response 作为 assistant 消息追加，构成完整对话
        full_messages = list(req_messages)
        resp_content = response.get("content")
        resp_tool_calls = response.get("tool_calls")
        resp_reasoning = response.get("reasoning_content")
        resp_thinking = response.get("thinking_blocks")
        if resp_content or resp_tool_calls or resp_reasoning:
            assistant_msg = {"role": "assistant"}
            if resp_content:
                assistant_msg["content"] = resp_content
            if resp_tool_calls:
                # tool_calls 已经是标准 OpenAI 格式（_build_response_data 里调用了 to_openai_tool_call）
                assistant_msg["tool_calls"] = resp_tool_calls
            # 最后一轮 assistant 只能靠这里从 response 拼出，
            # 必须带上思考链（reasoning_content / thinking_blocks），
            # 否则每条对话的收尾 assistant 会丢失思考过程。
            if resp_reasoning:
                assistant_msg["reasoning_content"] = resp_reasoning
            if resp_thinking:
                assistant_msg["thinking_blocks"] = resp_thinking
            full_messages.append(assistant_msg)

        prev = _session_messages.get(session_key)
        if prev is None or len(full_messages) >= len(prev):
            _session_messages[session_key] = full_messages

    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_key": session_key or "",
        "provider": provider,
        "method": method,
        "status": status,
        "elapsed": round(elapsed, 3),
        "request": request,
        "response": response,
    }

    _log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    _log_fp.flush()


def close_logger():
    """关闭日志文件。"""
    global _log_fp
    if _log_fp is not None:
        _log_fp.close()
        _log_fp = None
