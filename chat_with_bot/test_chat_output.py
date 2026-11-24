"""Exercise the chat entrypoint with a fake bot, never the configured model."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import chat_single


class ChatOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_display_separate_from_preserved_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            original = '{"agents":{"defaults":{}},"providers":{}}'
            config.write_text(original)
            bot = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(content="final text")),
                                  _loop=SimpleNamespace(model="test-model"))
            messages = [{"role": "assistant", "content": "final text", "reasoning_content": "private reasoning"}]
            stdout = io.StringIO()
            with patch.multiple(chat_single, CONFIG_PATH=config, OUTPUT_PATH=root / "output.jsonl",
                                TRACES_PATH=root / "traces.jsonl", NANOBOT_SESSIONS_DIR=root,
                                SHOW_REASONING=False), \
                    patch.object(chat_single.Nanobot, "from_config", return_value=bot), \
                    patch.object(chat_single, "install_api_logger"), \
                    patch.object(chat_single, "get_session_messages", return_value=messages), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                await chat_single.main("test query", "display-test")
            bot.run.assert_awaited_once_with("test query", session_key="display-test")
            self.assertIn("final text", stdout.getvalue())
            self.assertNotIn("private reasoning", stdout.getvalue())
            record = json.loads((root / "output.jsonl").read_text())
            self.assertEqual(record["output"], "final text")
            self.assertEqual(record["think"], "private reasoning")
            trace = json.loads((root / "traces.jsonl").read_text())
            self.assertEqual(trace["messages"], messages)
            self.assertEqual(json.loads(config.read_text()), json.loads(original))

    async def test_constructor_failure_restores_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            original = '{"agents":{"defaults":{}},"providers":{}}'
            config.write_text(original)
            with patch.object(chat_single, "CONFIG_PATH", config), \
                    patch.object(chat_single, "install_api_logger"), \
                    patch.object(chat_single.Nanobot, "from_config", side_effect=ValueError("test failure")):
                with self.assertRaises(ValueError):
                    await chat_single.main("test query", "constructor-failure-test")
            self.assertEqual(json.loads(config.read_text()), json.loads(original))


if __name__ == "__main__":
    unittest.main()
