from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guards import RepetitionGuard  # noqa: E402


class TestRepetitionGuard(unittest.TestCase):
    def test_blocks_third_identical_call_and_resets_on_change(self):
        guard = RepetitionGuard(limit=3)
        self.assertIsNone(guard.check("read_file", {"path": "a.py"}))
        self.assertIsNone(guard.check("read_file", {"path": "a.py"}))
        self.assertIn("Stagnation guard", guard.check("read_file", {"path": "a.py"}))
        self.assertIsNone(guard.check("read_file", {"path": "b.py"}))
        self.assertEqual(guard.count, 1)

    def test_argument_order_does_not_change_fingerprint(self):
        guard = RepetitionGuard(limit=2)
        self.assertIsNone(guard.check("search_text", {"query": "x", "path": "."}))
        self.assertIn(
            "Stagnation guard",
            guard.check("search_text", {"path": ".", "query": "x"}),
        )


if __name__ == "__main__":
    unittest.main()
