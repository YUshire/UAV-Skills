"""Shell execution tool."""

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.sandbox import wrap_command
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.agent.tools.tmp_remap import remap_command, remap_path, set_redirect
from nanobot.config.paths import get_media_dir

_IS_WINDOWS = sys.platform == "win32"


@tool_parameters(
    tool_parameters_schema(
        command=StringSchema("The shell command to execute"),
        working_dir=StringSchema("Optional working directory for the command"),
        timeout=IntegerSchema(
            60,
            description=(
                "Timeout in seconds. Increase for long-running commands "
                "like compilation or installation (default 60, max 600)."
            ),
            minimum=1,
            maximum=600,
        ),
        required=["command"],
    )
)
class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        sandbox: str = "",
        path_append: str = "",
        tmpdir: str = "",
        allowed_env_keys: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.sandbox = sandbox
        self.tmpdir = tmpdir
        # Drive the /tmp redirect (used by both exec and filesystem tools) from
        # the same configured dir, so all tools land in one place.
        if tmpdir:
            set_redirect(tmpdir)
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            # Block writes to nanobot internal state files (#2989).
            # history.jsonl / .dream_cursor are managed by append_history();
            # direct writes corrupt the cursor format and crash /dream.
            r">>?\s*\S*(?:history\.jsonl|\.dream_cursor)",            # > / >> redirect
            r"\btee\b[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",     # tee / tee -a
            r"\b(?:cp|mv)\b(?:\s+[^\s|;&<>]+)+\s+\S*(?:history\.jsonl|\.dream_cursor)",  # cp/mv target
            r"\bdd\b[^|;&<>]*\bof=\S*(?:history\.jsonl|\.dream_cursor)",  # dd of=
            r"\bsed\s+-i[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",  # sed -i
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.allowed_env_keys = allowed_env_keys or []

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output. "
            "Prefer read_file/write_file/edit_file over cat/echo/sed, "
            "and grep/glob over shell find/grep. "
            "Use -y or --yes flags to avoid interactive prompts. "
            "Output is truncated at 10 000 chars; timeout defaults to 60s."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, **kwargs: Any,
    ) -> str:
        # Redirect hardcoded /tmp references to the configured dir, matching the
        # filesystem tools' rewrite so a /tmp write and later read/exec agree.
        command = remap_command(command)
        if working_dir:
            working_dir = remap_path(working_dir)
        cwd = working_dir or self.working_dir or os.getcwd()

        # Prevent an LLM-supplied working_dir from escaping the configured
        # workspace when restrict_to_workspace is enabled (#2826). Without
        # this, a caller can pass working_dir="/etc" and then all absolute
        # paths under /etc would pass the _guard_command check that anchors
        # on cwd.
        if self.restrict_to_workspace and self.working_dir:
            try:
                requested = Path(cwd).expanduser().resolve()
                workspace_root = Path(self.working_dir).expanduser().resolve()
            except Exception:
                return "Error: working_dir could not be resolved"
            if requested != workspace_root and workspace_root not in requested.parents:
                return "Error: working_dir is outside the configured workspace"

        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        if self.sandbox:
            if _IS_WINDOWS:
                logger.warning(
                    "Sandbox '{}' is not supported on Windows; running unsandboxed",
                    self.sandbox,
                )
            else:
                workspace = self.working_dir or cwd
                command = wrap_command(self.sandbox, command, workspace, cwd, tmpdir=self.tmpdir)
                cwd = str(Path(workspace).resolve())

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)
        env = self._build_env()

        if self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = env.get("PATH", "") + ";" + self.path_append
            else:
                command = f'export PATH="$PATH:{self.path_append}"; {command}'

        # Rewrite hardcoded /tmp inside scripts that will run under our redirect.
        # Agent-generated scripts often have open('/tmp/...') which won't see our
        # TMPDIR env; we rewrite the script source before exec so they land in the
        # same place as write_file output. Only touches scripts in our tmpdir.
        if self.tmpdir:
            self._rewrite_script_tmp_refs(command, cwd)

        try:
            process = await self._spawn(command, cwd, env)

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(process)
                return f"Error: Command timed out after {effective_timeout} seconds"
            except asyncio.CancelledError:
                await self._kill_process(process)
                raise

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    @staticmethod
    async def _spawn(
        command: str, cwd: str, env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        """Launch *command* in a platform-appropriate shell."""
        if _IS_WINDOWS:
            comspec = env.get("COMSPEC", os.environ.get("COMSPEC", "cmd.exe"))
            return await asyncio.create_subprocess_exec(
                comspec, "/c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        bash = shutil.which("bash") or "/bin/bash"
        return await asyncio.create_subprocess_exec(
            bash, "-l", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """Kill a subprocess and reap it to prevent zombies."""
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        finally:
            if not _IS_WINDOWS:
                try:
                    os.waitpid(process.pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError) as e:
                    logger.debug("Process already reaped or not found: {}", e)

    def _build_env(self) -> dict[str, str]:
        """Build a minimal environment for subprocess execution.

        On Unix, only HOME/LANG/TERM are passed; ``bash -l`` sources the
        user's profile which sets PATH and other essentials.

        On Windows, ``cmd.exe`` has no login-profile mechanism, so a curated
        set of system variables (including PATH) is forwarded.  API keys and
        other secrets are still excluded.

        自动注入 CLAUDE_CODE_SESSION_ID：从 ContextVar 读取当前协程的 session_key
        并传给子进程，确保并发场景下每个任务的 subprocess 拿到正确的 session_key。
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
            for key in self.allowed_env_keys:
                val = os.environ.get(key)
                if val is not None:
                    env[key] = val
            return env
        home = os.environ.get("HOME", "/tmp")
        env = {
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PATH": "/root/miniforge3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        # 约束子进程临时文件目录：让 python tempfile / mkstemp / /tmp 类写入落到配置目录
        # 而不是系统默认 /tmp。TMPDIR(POSIX) / TMP / TEMP 三个都设，覆盖不同库的取值习惯。
        if self.tmpdir:
            try:
                os.makedirs(self.tmpdir, exist_ok=True)
            except OSError as e:
                logger.warning("Failed to create exec tmpdir {}: {}", self.tmpdir, e)
            env["TMPDIR"] = self.tmpdir
            env["TMP"] = self.tmpdir
            env["TEMP"] = self.tmpdir
        for key in self.allowed_env_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

        # 从 ContextVar 读取当前协程的 session_key，注入到子进程环境
        # （并发安全：每个协程的 ContextVar 值独立，不会互相覆盖）
        try:
            from nanobot.providers.api_logger import get_session_key
            session_key = get_session_key()
            if session_key:
                env["CLAUDE_CODE_SESSION_ID"] = session_key
        except ImportError:
            pass  # api_logger 未安装时不影响正常功能

        return env

    def _rewrite_script_tmp_refs(self, command: str, cwd: str) -> None:
        """Rewrite hardcoded /tmp paths inside scripts that will execute.

        Agent-generated scripts often have ``open('/tmp/foo')`` which won't see
        our ``TMPDIR`` env. We detect ``python /path/to/script.py`` / ``bash ...``
        commands where the script is under our tmpdir, read it, replace ``'/tmp/``
        with the configured tmpdir, and write back so the script's internal paths
        land in the same place as write_file output.
        """
        if not self.tmpdir or "/tmp" not in command:
            return
        # Match: python [opts] /data02/.../tmp/script.py [args]
        # or:    bash /data02/.../tmp/script.sh
        # Common interpreters; ignore if script path doesn't contain our tmpdir.
        m = re.search(
            r'\b(?:python3?|bash|sh)\b(?:\s+[^\s/]+)*\s+((?:[^\s]+/)?[^\s]+\.(?:py|sh))',
            command
        )
        if not m:
            return
        script_path_arg = m.group(1)
        # Resolve relative to cwd
        if not script_path_arg.startswith('/'):
            script_path_arg = str(Path(cwd) / script_path_arg)
        script_path = Path(script_path_arg).resolve()
        # Only rewrite scripts that live in our tmpdir (agent wrote them there).
        # Don't touch system scripts or workspace scripts.
        tmpdir_resolved = Path(self.tmpdir).resolve()
        try:
            script_path.relative_to(tmpdir_resolved)
        except ValueError:
            return  # not under tmpdir
        if not script_path.is_file():
            return
        try:
            original = script_path.read_text(encoding='utf-8')
        except Exception:
            return  # binary or unreadable
        # Replace hardcoded '/tmp/' with our target, but not '/var/tmp/' etc.
        # Use the same regex as tmp_remap.remap_command for consistency.
        from nanobot.agent.tools.tmp_remap import _TMP_RE
        rewritten = _TMP_RE.sub(self.tmpdir, original)
        if rewritten != original:
            try:
                script_path.write_text(rewritten, encoding='utf-8')
                logger.debug("Rewrote /tmp refs in script: {}", script_path)
            except Exception as e:
                logger.warning("Failed to rewrite script {}: {}", script_path, e)

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url
        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                media_path = get_media_dir().resolve()
                if (p.is_absolute()
                    and cwd_path not in p.parents
                    and p != cwd_path
                    and media_path not in p.parents
                    and p != media_path
                ):
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # Windows: match drive-root paths like `C:\` as well as `C:\path\to\file`
        # NOTE: `*` is required so `C:\` (nothing after the slash) is still extracted.
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]*", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths
