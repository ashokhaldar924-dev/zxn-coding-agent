import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from context_manager import ConversationContext  # noqa: E402


class TestContextManager(unittest.TestCase):
    def setUp(self):
        self._old_limit = config.MAX_HISTORY_MESSAGES
        config.MAX_HISTORY_MESSAGES = 6

    def tearDown(self):
        config.MAX_HISTORY_MESSAGES = self._old_limit

    def test_no_trim_when_under_limit(self):
        ctx = ConversationContext("sys", "task")
        ctx.add_user_message("hi")
        self.assertEqual(len(ctx.get_messages()), 3)

    def test_trim_keeps_anchor_and_recent(self):
        ctx = ConversationContext("system prompt", "original task")
        for i in range(10):
            ctx.add_user_message(f"msg {i}")

        messages = ctx.get_messages()
        self.assertLessEqual(len(messages), config.MAX_HISTORY_MESSAGES)
        # 第一条必须还是系统提示，第二条必须还是最初任务，保证 agent 不"失忆"
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "system prompt")
        self.assertEqual(messages[1]["content"], "original task")
        # 最后一条应该是最近追加的消息
        self.assertEqual(messages[-1]["content"], "msg 9")


if __name__ == "__main__":
    unittest.main()
