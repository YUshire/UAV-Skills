"""Shared execution loop for tool-using agents."""

from __future__ import annotations
import os
import copy
import asyncio
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from typing import Any
import json
from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.utils.prompt_templates import render_template
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, ToolCallRequest
from nanobot.utils.helpers import (
    build_assistant_message,
    debug_dump,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    maybe_persist_tool_result,
    truncate_text,
)
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_empty_length_steering_message,
    build_finalization_retry_message,
    build_length_recovery_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    looks_like_raw_tool_call_leak,
    repeated_external_lookup_error,
)

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_EMPTY_LENGTH_RETRIES = 1
_MAX_LENGTH_RECOVERIES = 0  # 0 = disable length-truncation recovery (single-turn, no continuation prompt)
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_SNIP_SAFETY_BUFFER = 1024
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 1500000
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "exec", "grep", "glob",
    "web_search", "web_fetch", "list_dir",
})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"

logger.enable("nanobot")  # 强制启用
# 日志落到 NANOBOT_LOG_PATH；不设就不加 sink（不写任何地方）
_log_path = os.environ.get("NANOBOT_LOG_PATH", "").strip()
if _log_path:
    os.makedirs(os.path.dirname(os.path.abspath(_log_path)) or ".", exist_ok=True)
    logger.add(_log_path, level="INFO", enqueue=True, rotation="1 GB", retention=5)
print("nanobot logger loaddone")

@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False

def save_to_js(obj):
    if isinstance(obj, dict):
        debug_dump("tmp.txt", json.dumps(obj, ensure_ascii=False, indent=4))
    else:
        debug_dump("tmp.txt", str(obj))
    print('写入Done')
class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            return False, injection_cycles
        injections = await self._drain_injections(spec)
        if not injections:
            return False, injection_cycles
        injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        logger.info(
            "Injected {} follow-up message(s) {} ({}/{})",
            len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
        )
        return True, injection_cycles

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                injected_messages.append(item)
                continue
            text = getattr(item, "content", str(item))
            if text.strip():
                injected_messages.append({"role": "user", "content": text})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_empty_retries = 0
        length_recovery_count = 0
        pending_steering: dict[str, Any] | None = None
        pending_temp_bump = False
        had_injections = False
        injection_cycles = 0

        for iteration in range(spec.max_iterations):
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                messages_for_model = self._drop_orphan_tool_results(messages)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                messages_for_model = self._microcompact(messages_for_model)
                messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
                messages_for_model = self._snip_history(spec, messages_for_model)
                # Snipping may have created new orphans; clean them up.
                messages_for_model = self._drop_orphan_tool_results(messages_for_model)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
            except Exception as exc:
                logger.warning(
                    "Context governance failed on turn {} for {}: {}; applying minimal repair",
                    iteration,
                    spec.session_key or "default",
                    exc,
                )
                try:
                    messages_for_model = self._drop_orphan_tool_results(messages)
                    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                except Exception:
                    messages_for_model = messages
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            # import pdb;pdb.set_trace()
            call_messages = messages_for_model
            temp_override = None
            pp_override = None
            if pending_steering is not None:
                call_messages = list(messages_for_model)
                if call_messages and call_messages[-1].get("role") == "user":
                    call_messages.append({"role": "assistant", "content": ""})
                call_messages.append(pending_steering)
                if pending_temp_bump:
                    base_temp = spec.temperature if spec.temperature is not None else 0.1
                    temp_override = max(base_temp, 0.3)
                    base_pp = spec.presence_penalty if spec.presence_penalty is not None else 0.0
                    pp_override = max(base_pp, 0.8)
                pending_steering = None
                pending_temp_bump = False
            response = await self._request_model(
                spec, call_messages, hook, context,
                temperature_override=temp_override,
                presence_penalty_override=pp_override,
            )
            # 模型返回的toolcall是什么样的文本，框架如何解析该toolcall并生成结果并返回给模型？这里的步骤需要搞清楚
            # 分析response可知，LLMResponse已经包含了结构化的toolcall，因此要继续往回找，看哪里把文本解析为结构化toolcall
            # LLMResponse(content='\n\nNow I have sufficient materials. Let me generate the outline and write the full report.\n\n', tool_calls=[ToolCallRequest(id='NvDBoSDoY', name='write_file', arguments={'content': '中欧关系分析报告：习主席2024年访欧的战略考量与地缘影响\n\n一、访欧背景与战略考量：五年首访的时机选择\n（一）中欧关系现状与访问时机\n（二）三国选择的战略逻辑\n（三）与2019年访欧的氛围对比\n\n二、访欧成果与协议签署：务实合作的多维展开\n（一）中法合作：18项协议与战略自主对话\n（二）中塞合作：一带一路在巴尔干的深化\n（三）中匈合作：黄金航道与新能源投资\n\n三、中欧关系深层矛盾：贸易摩擦与地缘分歧\n（一）电动汽车关税争端与贸易博弈\n（二）乌克兰危机立场分歧\n（三）欧洲对华认知转变与"去风险"战略\n\n四、各方反应与博弈态势：美欧中三角互动\n（一）欧洲内部对华立场分化\n（二）美国视角下的中欧关系\n（三）中国"挑拨美欧"战略的成效评估\n\n五、未来展望与风险研判：中欧关系的走向\n（一）短期情景：合作与摩擦并存\n（二）中长期趋势：战略自主的机遇与挑战\n\n结语\n', 'path': '/root/.nanobot/workspace/reports/outline_习主席访欧.txt'}, extra_content=None, provider_specific_fields=None, function_provider_specific_fields=None)], finish_reason='tool_calls', usage={'prompt_tokens': 15654, 'completion_tokens': 523, 'total_tokens': 16177}, retry_after=None, reasoning_content='I now have sufficient material to generate a comprehensive analysis report. Let me compile the outline and then write the full report.\n\nLet me organize the materials and create the report following the style guidelines from the skill file.\n\nKey themes from the materials:\n1. Xi\'s visit to France, Serbia, and Hungary (May 2024) - first visit in 5 years\n2. Strategic selection of countries - France (strategic autonomy), Serbia (Belt and Road in Balkans), Hungary (closest EU ally)\n3. Changed atmosphere compared to 2019 Italy visit\n4. Trade tensions - EV tariffs, anti-subsidy investigation\n5. Ukraine war position differences\n6. Results - 18 agreements with France, various projects with Serbia and Hungary\n\n\n7. EU\'s "de-risking" stance and the broader geopolitical context of US-China competition\n\nNow I\'ll draft the report following the required format with proper section numbering and structure.\n', thinking_blocks=None, error_status_code=None, error_kind=None, error_type=None, error_code=None, error_retry_after_s=None, error_should_retry=None)
            # 2个todo：保证轨迹数据中没有被省略和压缩的部分，比如"content": "[read_file result omitted from context]"；工具调用的定义写到system prompt中
            # logger.warning(f"单轮对话session-{spec.session_key}调用完成，详情:messages_for_model 长度{len(str(messages_for_model))}:{messages_for_model}\nresp 长度{len(str(response))}:{response}")
            # **resaoning_content ->reasoning的openai协议字段变化，确保字段变化不会导致拿错或者拿空字段。**
            print(f"msgdone!{len(messages_for_model)},session:{spec.session_key},model:{spec.model}")
            tmp = copy.deepcopy(messages_for_model)
            # 补上最后一条 assistant msg 的思考过程（reasoning_content / thinking_blocks / finish_reason）
            tmp.append({"model":spec.model,"this_session_used_tool":spec.tools.get_definitions(),"role":"assistant", "content":response.content, "tool_calls":response.tool_calls,"reasoning_content":response.reasoning_content,"thinking_blocks":response.thinking_blocks,"finish_reason":response.finish_reason})
            # 排查用：把原始 response 落盘（写入 ~/.nanobot/debug_tmp，失败不影响运行）
            debug_dump("processed_resp.log", str(response))

            logger.warning(f"单轮对话session-{spec.session_key}调用完成，详情:messages_for_model 长度{len(str(tmp))}:{tmp}")
            # import pdb;pdb.set_trace()
            raw_usage = self._usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self._accumulate_usage(usage, raw_usage)

            if response.should_execute_tools:
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                tools_used.extend(tc.name for tc in response.tool_calls)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                )
                tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_empty_retries = 0
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            raw_tc_leak = (
                response.finish_reason != "error"
                and not response.has_tool_calls
                and looks_like_raw_tool_call_leak(clean)
            )
            if response.finish_reason != "error" and (is_blank_text(clean) or raw_tc_leak):
                # 【修复】空 content + 有 tool_calls 是正常工具链，不应计入空转
                # skill-based workflow 经常连续读多个文件（SKILL.md → references/*.md → 执行脚本）
                if response.has_tool_calls and not raw_tc_leak:
                    # 有工具调用，重置空响应计数（这是正常的多步工具链，不是空转）
                    empty_content_retries = 0
                    length_empty_retries = 0
                    logger.debug(
                        "Empty content with tool_calls on turn {} for {} (normal tool chain, not empty loop)",
                        iteration,
                        spec.session_key or "default",
                    )
                    # 继续正常处理 tool_calls（下一个 iteration 开始时会执行工具）
                elif response.finish_reason == "length" and not raw_tc_leak:
                    # 输出长度耗尽且正文/工具均为空（常见于思维链失控）：绝不原样重复 5 次，
                    # 仅做 1 次带纠偏提示和轻微升温的重试，仍失败则关闭思考直接整理已有结果。
                    length_empty_retries += 1
                    if length_empty_retries <= _MAX_EMPTY_LENGTH_RETRIES:
                        logger.warning(
                            "Empty length-exhausted response on turn {} for {} ({}/{}); steering retry",
                            iteration,
                            spec.session_key or "default",
                            length_empty_retries,
                            _MAX_EMPTY_LENGTH_RETRIES,
                        )
                        pending_steering = build_empty_length_steering_message()
                        pending_temp_bump = True
                        if hook.wants_streaming():
                            await hook.on_stream_end(context, resuming=False)
                        await hook.after_iteration(context)
                        continue
                    logger.warning(
                        "Empty length-exhausted response persisted on turn {} for {}; finalizing with thinking disabled",
                        iteration,
                        spec.session_key or "default",
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    response = await self._request_finalization_retry(
                        spec, messages_for_model, disable_thinking=True
                    )
                    retry_usage = self._usage_dict(response.usage)
                    self._accumulate_usage(usage, retry_usage)
                    raw_usage = self._merge_usage(raw_usage, retry_usage)
                    context.response = response
                    context.usage = dict(raw_usage)
                    context.tool_calls = list(response.tool_calls)
                    clean = hook.finalize_content(context, response.content)
                    if looks_like_raw_tool_call_leak(clean) or (response.has_tool_calls and is_blank_text(clean)):
                        clean = ""
                else:
                    # 普通空响应或裸 <tool_call> 泄漏
                    empty_content_retries += 1
                    if not raw_tc_leak and empty_content_retries < _MAX_EMPTY_RETRIES:
                        logger.warning(
                            "Empty response on turn {} for {} ({}/{}); retrying",
                            iteration,
                            spec.session_key or "default",
                            empty_content_retries,
                            _MAX_EMPTY_RETRIES,
                        )
                        if hook.wants_streaming():
                            await hook.on_stream_end(context, resuming=False)
                        await hook.after_iteration(context)
                        continue
                    logger.warning(
                        "Empty or malformed response on turn {} for {} (raw_tc_leak={}); finalizing with thinking disabled",
                        iteration,
                        spec.session_key or "default",
                        raw_tc_leak,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    response = await self._request_finalization_retry(
                        spec, messages_for_model, disable_thinking=True
                    )
                    retry_usage = self._usage_dict(response.usage)
                    self._accumulate_usage(usage, retry_usage)
                    raw_usage = self._merge_usage(raw_usage, retry_usage)
                    context.response = response
                    context.usage = dict(raw_usage)
                    context.tool_calls = list(response.tool_calls)
                    clean = hook.finalize_content(context, response.content)
                    if looks_like_raw_tool_call_leak(clean) or (response.has_tool_calls and is_blank_text(clean)):
                        clean = ""

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    await hook.after_iteration(context)
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            if spec.max_iterations_message:
                final_content = spec.max_iterations_message.format(
                    max_iterations=spec.max_iterations,
                )
            else:
                final_content = render_template(
                    "agent/max_iterations_message.md",
                    strip=True,
                    max_iterations=spec.max_iterations,
                )
            self._append_final_message(messages, final_content)
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We ignore should_continue here because the for-loop has already
            # exhausted all iterations.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.top_p is not None:
            kwargs["top_p"] = spec.top_p
        if spec.top_k is not None:
            kwargs["top_k"] = spec.top_k
        if spec.min_p is not None:
            kwargs["min_p"] = spec.min_p
        if spec.presence_penalty is not None:
            kwargs["presence_penalty"] = spec.presence_penalty
        if spec.repetition_penalty is not None:
            kwargs["repetition_penalty"] = spec.repetition_penalty
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        *,
        temperature_override: float | None = None,
        presence_penalty_override: float | None = None,
    ):
        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
        if temperature_override is not None:
            kwargs["temperature"] = temperature_override
        if presence_penalty_override is not None:
            kwargs["presence_penalty"] = presence_penalty_override
        if hook.wants_streaming():
            async def _stream(delta: str) -> None:
                await hook.on_stream(context, delta)

            return await self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
            )
        return await self.provider.chat_with_retry(**kwargs)    # vllm走这里

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        disable_thinking: bool = False,
    ):
        retry_messages = list(messages)
        # 检查最后一条消息的角色，避免连续两个 user 消息
        if retry_messages and retry_messages[-1].get("role") == "user":
            # 如果最后一条已经是 user，插入一条空的 assistant 消息
            retry_messages.append({"role": "assistant", "content": ""})
        retry_messages.append(build_finalization_retry_message())
        kwargs = self._build_request_kwargs(spec, retry_messages, tools=None)
        if disable_thinking:
            kwargs["reasoning_effort"] = "none"
        return await self.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                tool_results.extend(await asyncio.gather(*(
                    self._run_tool(spec, tool_call, external_lookup_counts)
                    for tool_call in batch
                )))
            else:
                for tool_call in batch:
                    tool_results.append(await self._run_tool(spec, tool_call, external_lookup_counts))

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        _HINT = ""
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error, event, RuntimeError(lookup_error)
            return lookup_error, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            try:
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
            except Exception:
                pass
        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            return prep_error, event, RuntimeError(prep_error) if spec.fail_on_tool_error else None
        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            if spec.fail_on_tool_error:
                return f"Error: {type(exc).__name__}: {exc}", event, exc
            return f"Error: {type(exc).__name__}: {exc}", event, None

        if isinstance(result, str) and result.startswith("Error"):
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            if spec.fail_on_tool_error:
                return result, event, RuntimeError(result)
            return result, event, None

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception as exc:
            logger.warning(
                "Tool result persist failed for {} in {}: {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
                exc,
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            content = truncate_text(content, spec.max_tool_result_chars)

        # 为特定工具添加中文思考钩子
        # hint = '\n\n注意：思维链使用中文进行清晰、精确的思考。在调用工具前，请简要说明：(1)为什么选择该工具而非其他工具；(2)明确写出将传入的参数值（例如：调用get_weather，地点参数为"Beijing"）。思考过程控制字数。'
        # if isinstance(content, str) and hint not in content:
        #     content = content + hint
        return content

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop tool results that have no matching assistant tool_call earlier in the history."""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    @staticmethod
    def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace old compactable tool results with one-line summaries."""
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
                compactable_indices.append(idx)

        if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
            return messages

        stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
                continue
            name = msg.get("name", "tool")
            summary = f"[{name} result omitted from context]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else messages

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else (
            provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        estimate, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            messages,
            spec.tools.get_definitions(),
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        remaining_budget = max(128, budget - system_tokens)
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
