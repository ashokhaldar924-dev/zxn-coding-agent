from __future__ import annotations

import unittest

from zxn_agent.verification import (
    FULL_SUITE,
    TARGETED,
    UNKNOWN,
    failure_fingerprint,
    normalize_failure_output,
    task_requires_full_suite,
    verifier_scope,
)


class TestVerificationScope(unittest.TestCase):
    def test_explicit_full_suite_requests_are_detected(self):
        tasks = [
            "确保全部现有测试继续通过",
            "最后运行所有测试",
            "Run the full test suite before finishing",
            "Make sure all existing tests pass",
        ]
        for task in tasks:
            with self.subTest(task=task):
                self.assertTrue(task_requires_full_suite(task))

    def test_targeted_request_does_not_invent_full_suite_requirement(self):
        self.assertFalse(task_requires_full_suite("运行相关测试并修复失败"))
        self.assertFalse(task_requires_full_suite("run the parser tests"))
        self.assertFalse(task_requires_full_suite("不要运行全部测试，只跑相关测试"))
        self.assertFalse(task_requires_full_suite("Do not run the full test suite"))
        self.assertTrue(
            task_requires_full_suite("不要每次都运行全部测试；最后确保全部测试通过")
        )

    def test_pytest_scope_distinguishes_repository_and_file_checks(self):
        self.assertEqual(verifier_scope("python -m pytest tests -q"), FULL_SUITE)
        self.assertEqual(verifier_scope("pytest -q"), FULL_SUITE)
        self.assertEqual(
            verifier_scope("python -m pytest tests/test_gradebook.py -q"),
            TARGETED,
        )
        self.assertEqual(verifier_scope("pytest tests -k average -q"), TARGETED)

    def test_other_common_full_suite_commands_are_conservative(self):
        self.assertEqual(verifier_scope("python -m unittest discover -s tests"), FULL_SUITE)
        self.assertEqual(verifier_scope("npm test"), FULL_SUITE)
        self.assertEqual(verifier_scope("go test ./..."), FULL_SUITE)
        self.assertEqual(verifier_scope("python -m compileall ."), UNKNOWN)
        self.assertEqual(verifier_scope("pytest -q && echo done"), UNKNOWN)

    def test_failure_fingerprint_removes_volatile_paths_times_and_lines(self):
        first = (
            "2026-08-30T10:20:30 C:\\tmp\\run-a\\tests\\test_parser.py:41: "
            "FAILED tests/test_parser.py::test_value - AssertionError: expected 2\n"
            "1 failed in 1.23s"
        )
        second = (
            "2026-08-30T10:21:55 D:\\other\\run-b\\tests\\test_parser.py:99: "
            "FAILED tests/test_parser.py::test_value - AssertionError: expected 2\n"
            "1 failed in 9.87s"
        )
        self.assertEqual(failure_fingerprint(first)[0], failure_fingerprint(second)[0])
        self.assertIn("AssertionError: expected 2", normalize_failure_output(first))

    def test_failure_fingerprint_changes_with_failure_identity(self):
        first = "FAILED tests/test_parser.py::test_value - AssertionError: expected 2"
        second = "FAILED tests/test_parser.py::test_value - TypeError: value is None"
        self.assertNotEqual(failure_fingerprint(first)[0], failure_fingerprint(second)[0])


if __name__ == "__main__":
    unittest.main()
