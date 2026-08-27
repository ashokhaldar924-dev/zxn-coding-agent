from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self.data = data or {}
        self.text = text

    def json(self):
        return self.data


class FakeRequests(types.SimpleNamespace):
    class RequestException(Exception):
        pass


class TestLLM(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.get("AGENT_API_KEY")
        self.old_model = config.MODEL_NAME
        self.old_url = config.API_BASE_URL
        os.environ["AGENT_API_KEY"] = "test-secret"
        config.MODEL_NAME = "test-model"
        config.API_BASE_URL = "https://example.test/v1"

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop("AGENT_API_KEY", None)
        else:
            os.environ["AGENT_API_KEY"] = self.old_key
        config.MODEL_NAME = self.old_model
        config.API_BASE_URL = self.old_url

    def install_fake(self, post):
        fake = FakeRequests(post=post)
        return mock.patch.dict(sys.modules, {"requests": fake})

    def test_success_payload_and_usage(self):
        captured = {}
        def post(url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse(data={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            })

        with self.install_fake(post):
            message, usage = llm.call([{"role": "user", "content": "hi"}], [{"type": "function"}])
        self.assertEqual(message["content"], "ok")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["json"]["tool_choice"], "auto")
        self.assertEqual(set(captured["json"]), {"model", "messages", "tools", "tool_choice"})

    def test_retries_transient_status_twice(self):
        responses = [FakeResponse(429), FakeResponse(503), FakeResponse(data={
            "choices": [{"message": {"content": "recovered"}}],
            "usage": {"input_tokens": 4, "output_tokens": 1},
        })]
        calls = 0
        def post(url, **kwargs):
            nonlocal calls
            calls += 1
            return responses.pop(0)

        with self.install_fake(post), mock.patch("time.sleep"):
            message, usage = llm.call([], [])
        self.assertEqual(calls, 3)
        self.assertEqual(message["content"], "recovered")
        self.assertEqual(usage["total_tokens"], 5)

    def test_invalid_response_is_clear_and_redacted(self):
        def post(url, **kwargs):
            return FakeResponse(data={"bad": "shape"}, text="test-secret malformed")

        with self.install_fake(post):
            with self.assertRaises(llm.LLMError) as caught:
                llm.call([], [])
        self.assertIn("Invalid model response", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
