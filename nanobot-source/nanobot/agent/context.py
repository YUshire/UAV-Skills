"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import os
import platform
import sqlite3
from datetime import datetime
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import build_assistant_message, current_time_str, detect_image_mime
from nanobot.utils.prompt_templates import render_template


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        # 不再注入 Recent History 到 system prompt
        # entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        # if entries:
        #     capped = entries[-self._MAX_RECENT_HISTORY:]
        #     parts.append("# Recent History\n\n" + "\n".join(
        #         f"- [{e['timestamp']}] {e['content']}" for e in capped
        #     ))

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None, chat_id: str | None, timezone: str | None = None,
        session_summary: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if session_summary:
            lines += ["", "[Resumed Session]", session_summary]
        # return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END
        # return "\n".join(lines)+'\n只允许使用中文进行思考与回复。'
        # return '首先使用中文进行思考，再使用中文进行回复。'
        return ''

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                if filename == "USER.md":
                    content = self._fill_user_md_placeholders(content)
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    # -- USER.md runtime placeholders ----------------------------------------
    _PLOT_SQL_QUERY_PY = Path(
        "/root/.nanobot/workspace/skills/plot_instance_query/scripts/plot_sql_query.py"
    )
    _MAP_CONTROL_DB_FALLBACK = (
        "/root/.nanobot/workspace/skills/plot_instance_query/scripts/标绘库_0914.db"
    )

    @classmethod
    def _resolve_map_control_db(cls) -> str:
        """从 plot_sql_query.py 读取 DB 路径，保持两处一致。"""
        try:
            import re as _re
            text = cls._PLOT_SQL_QUERY_PY.read_text(encoding="utf-8")
            m = _re.search(r"""^p\s*=\s*r?['"](.+?)['"]""", text, _re.MULTILINE)
            if m:
                return m.group(1)
        except Exception:
            pass
        return cls._MAP_CONTROL_DB_FALLBACK

    @classmethod
    def _fill_user_md_placeholders(cls, content: str) -> str:
        """运行时替换 USER.md 中的 {current_time}/{layers}/{current_layer} 占位符。

        - {current_time}: 当前日期（%Y-%m-%d）
        - {layers} / {current_layer}: 实时查询 map_control 表
        任一查询失败都不影响其余占位符，保持原始占位符不变。
        """
        if "{current_time}" in content:
            content = content.replace("{current_time}", datetime.now().strftime("%Y-%m-%d"))

        if "{layers}" in content or "{current_layer}" in content:
            layers, current_layer = cls._query_map_control()
            if layers is not None:
                content = content.replace("{layers}", layers)
            if current_layer is not None:
                content = content.replace("{current_layer}", current_layer)

        return content

    @classmethod
    def _query_map_control(cls) -> tuple[str | None, str | None]:
        """查询 map_control 表的 layers 与 current_layer，返回字符串（失败返回 None）。"""
        db_path = os.environ.get("NANOBOT_MAP_CONTROL_DB", cls._resolve_map_control_db())
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT layers, current_layer FROM map_control LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return None, None
        if not row:
            return None, None
        layers, current_layer = row
        return (
            str(layers) if layers is not None else None,
            str(current_layer) if current_layer is not None else None,
        )

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        try:
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        session_summary: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id, self.timezone, session_summary=session_summary)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}" if runtime_ctx else user_content
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content
        messages = [
            {"role": "system", "content": self.build_system_prompt(skill_names, channel=channel)},
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
