"""Offline checks for reasoning budgets, real request logging and channel separation."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nanobot.config.schema import AgentDefaults, Config
from nanobot.nanobot import _make_provider
from nanobot.providers import api_logger
from nanobot.providers.base import GenerationSettings
from nanobot.providers.openai_compat_provider import OpenAICompatProvider


def response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"sum":42}', tool_calls=None,
                                                        reasoning_content="internal reasoning"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=15, total_tokens=20,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=7)),
    )


class GenerationBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "calls.jsonl"

    def tearDown(self):
        api_logger.close_logger()
        self.temp.cleanup()

    async def test_opt_in_budget_and_normalized_logging(self):
        with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
            provider = OpenAICompatProvider(default_model="test-model")
        provider.generation = GenerationSettings(max_tokens=123, thinking_budget=77, temperature=0.4)
        provider._client.chat.completions.create = AsyncMock(return_value=response())
        api_logger.install_api_logger(str(self.path), "w")
        api_logger.set_session_key("budget-test")
        result = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])
        sent = provider._client.chat.completions.create.call_args.kwargs
        record = json.loads(self.path.read_text())
        self.assertEqual(sent["max_tokens"], 123)
        self.assertEqual(sent["extra_body"]["thinking_budget"], 77)
        self.assertEqual(record["request"]["max_tokens"], 123)
        self.assertEqual(record["request"]["temperature"], 0.4)
        self.assertEqual(record["request"]["wire_parameters"]["extra_body"], {"thinking_budget": 77})
        self.assertNotIn("messages", record["request"]["wire_parameters"])
        self.assertNotIn("api_key", record["request"]["wire_parameters"])
        self.assertEqual(result.content, '{"sum":42}')
        self.assertEqual(record["response"]["reasoning_content"], "internal reasoning")
        self.assertEqual(result.usage["reasoning_tokens"], 7)

    async def test_default_and_explicit_overrides(self):
        with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
            provider = OpenAICompatProvider(default_model="test-model")
        provider._client.chat.completions.create = AsyncMock(return_value=response())
        api_logger.install_api_logger(str(self.path), "w")
        await provider.chat_with_retry(messages=[], max_tokens=99, temperature=0.2, reasoning_effort=None)
        sent = provider._client.chat.completions.create.call_args.kwargs
        self.assertNotIn("thinking_budget", sent.get("extra_body", {}))
        record = json.loads(self.path.read_text())
        self.assertEqual(record["request"]["max_tokens"], 99)
        self.assertEqual(record["request"]["temperature"], 0.2)
        self.assertNotIn("reasoning_effort", record["request"])

    async def test_reinstall_does_not_duplicate_logs(self):
        with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
            provider = OpenAICompatProvider(default_model="test-model")
        provider._client.chat.completions.create = AsyncMock(return_value=response())
        api_logger.install_api_logger(str(self.path), "w")
        api_logger.install_api_logger(str(self.path), "a")
        await provider.chat_with_retry(messages=[])
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    async def test_streaming_budget_and_channels(self):
        with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
            provider = OpenAICompatProvider(default_model="test-model")
        provider.generation = replace(provider.generation, thinking_budget=77)

        async def chunks():
            yield {"choices": [{"delta": {"reasoning_content": "internal"}, "finish_reason": None}]}
            yield {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}

        provider._client.chat.completions.create = AsyncMock(return_value=chunks())
        api_logger.install_api_logger(str(self.path), "w")
        result = await provider.chat_stream_with_retry(messages=[])
        record = json.loads(self.path.read_text())
        self.assertEqual(record["request"]["wire_parameters"]["extra_body"]["thinking_budget"], 77)
        self.assertEqual(result.content, "answer")
        self.assertEqual(result.reasoning_content, "internal")

    async def test_concurrent_wire_parameters_stay_with_each_call(self):
        providers = []
        for budget in (55, 88):
            with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
                provider = OpenAICompatProvider(default_model="test-model")
            provider.generation = replace(provider.generation, thinking_budget=budget)
            provider._client.chat.completions.create = AsyncMock(return_value=response())
            providers.append(provider)
        api_logger.install_api_logger(str(self.path), "w")
        await asyncio.gather(*(p.chat_with_retry(messages=[]) for p in providers))
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual({r["request"]["wire_parameters"]["extra_body"]["thinking_budget"] for r in records}, {55, 88})

    def test_config_validation_and_factory_propagation(self):
        self.assertEqual(AgentDefaults(thinkingBudget=2048).thinking_budget, 2048)
        with self.assertRaises(ValueError):
            AgentDefaults(thinkingBudget=0)
        config = Config()
        config.agents.defaults.provider = "custom"
        config.agents.defaults.thinking_budget = 2048
        config.providers.custom.api_key = "test-only"
        with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
            provider = _make_provider(config)
        self.assertEqual(provider.generation.thinking_budget, 2048)


if __name__ == "__main__":
    unittest.main()
