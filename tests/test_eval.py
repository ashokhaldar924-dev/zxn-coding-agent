from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals.cases import CASES  # noqa: E402
from evals.run_eval import _hash_tests, _run_verifier, materialize  # noqa: E402


class TestEvalFixtures(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_eight_real_repair_fixtures_are_initially_failing_and_bounded(self):
        self.assertEqual(len(CASES), 8)
        for case in CASES:
            workspace = Path(self.tmpdir, case["name"])
            materialize(case, workspace)
            self.assertNotEqual(_run_verifier(workspace).returncode, 0, case["name"])
            hashes = _hash_tests(workspace)
            self.assertTrue(hashes)
            self.assertTrue((workspace / ".agent-verifier").is_file())


if __name__ == "__main__":
    unittest.main()
