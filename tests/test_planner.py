from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from zxn_agent import agent, config, tools
from zxn_agent.planner import PLANNER_POLICY_PROMPT, PlanState, plan_policy_issue
from zxn_agent.session import SessionStore, restore_state
from zxn_agent.state import State


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_workspace = config.WORKSPACE_DIR
        config.WORKSPACE_DIR = self.tmpdir

    def tearDown(self):
        config.WORKSPACE_DIR = self.old_workspace
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_plan_is_compact_and_rejects_ambiguous_progress(self):
        plan = PlanState()
        plan.replace([
            {"step": "Inspect the failing parser", "status": "completed"},
            {"step": "Implement the focused fix", "status": "in_progress"},
            {"step": "Run the verifier", "status": "pending"},
        ], explanation="Keep the repair scoped")

        self.assertEqual(plan.completed, 1)
        self.assertIn("1/3 completed", plan.progress_text())
        self.assertIn("→ Implement the focused fix", plan.compact())
        self.assertIn(plan.compact(), State(plan=plan).runtime_context())

        with self.assertRaisesRegex(ValueError, "at most one"):
            plan.replace([
                {"step": "First", "status": "in_progress"},
                {"step": "Second", "status": "in_progress"},
            ])
        with self.assertRaisesRegex(ValueError, "status"):
            plan.replace([{"step": "First", "status": "skipped"}])
        self.assertEqual(plan.revision, 1)

    def test_verified_finish_closes_navigation_without_changing_step_text(self):
        plan = PlanState()
        plan.replace([
            {"step": "实现事务语义", "status": "in_progress"},
            {"step": "运行全量回归验证", "status": "pending"},
        ])

        self.assertTrue(plan.complete_for_verified_finish())
        self.assertEqual(
            [(item.step, item.status) for item in plan.items],
            [
                ("实现事务语义", "completed"),
                ("运行全量回归验证", "completed"),
            ],
        )
        self.assertFalse(plan.complete_for_verified_finish())

    def test_update_plan_does_not_change_workspace_or_verification_state(self):
        Path(self.tmpdir, "a.py").write_text("value = 1\n", encoding="utf-8")
        st = State(rev=3, changed=True)
        st.initialize_workspace_tracking(self.tmpdir)
        st.mark_verified()
        before = (st.rev, st.ok_rev, st.ok_workspace_fingerprint)

        result = tools.run_tool("update_plan", {
            "plan": [
                {"step": "Inspect", "status": "completed"},
                {"step": "Verify", "status": "in_progress"},
            ]
        }, st)

        self.assertTrue(result.ok)
        self.assertTrue(result.plan_updated)
        self.assertEqual((st.rev, st.ok_rev, st.ok_workspace_fingerprint), before)
        self.assertTrue(st.verification_current())

        unchanged = tools.run_tool("update_plan", {
            "plan": [
                {"step": "Inspect", "status": "completed"},
                {"step": "Verify", "status": "in_progress"},
            ]
        }, st)
        self.assertFalse(unchanged.plan_updated)
        self.assertEqual(st.plan.revision, 1)

    def test_plan_round_trips_through_session_and_old_state_stays_compatible(self):
        store = SessionStore.create(self.tmpdir, "model-a", "task")
        st = State(session_id=store.session_id)
        st.plan.replace([
            {"step": "Inspect", "status": "completed"},
            {"step": "Repair", "status": "in_progress"},
        ])
        store.record_state(st, "plan_update")

        loaded = store.load("system")
        restored = restore_state(loaded.state, store.session_id)

        self.assertEqual(restored.plan.to_data(), st.plan.to_data())
        self.assertEqual(restore_state({}, "old-session").plan.to_data(), PlanState().to_data())

    def test_policy_prompt_defines_timing_quality_granularity_and_live_updates(self):
        policy = PLANNER_POLICY_PROMPT.casefold()
        for concept in (
            "existing repository",
            "3-7",
            "in_progress",
            "high-quality plan",
            "low-quality plan",
            "same natural language",
            "never replaces runtime verification",
        ):
            self.assertIn(concept, policy)
        self.assertIn(PLANNER_POLICY_PROMPT, agent.system_prompt())

    def test_existing_repository_requires_read_only_evidence_before_first_plan(self):
        Path(self.tmpdir, "scheduler.py").write_text(
            "class Scheduler:\n    pass\n", encoding="utf-8"
        )
        st = State()
        st.initialize_workspace_tracking(self.tmpdir)
        st.begin_turn(task="Build a durable local task scheduler with retries")
        technical_plan = [
            {"step": "Map existing job states and storage boundaries", "status": "in_progress"},
            {"step": "Define lifecycle transitions and the SQLite schema", "status": "pending"},
            {"step": "Persist restart recovery and duplicate-execution guards", "status": "pending"},
            {"step": "Add cancellation and bounded retry transitions", "status": "pending"},
            {"step": "Run the full regression verifier", "status": "pending"},
        ]

        early = tools.run_tool("update_plan", {"plan": technical_plan}, st)
        self.assertFalse(early.ok)
        self.assertEqual(st.plan.items, [])

        self.assertTrue(tools.run_tool("list_dir", {"path": "."}, st).ok)
        self.assertTrue(tools.run_tool("read_file", {"path": "scheduler.py"}, st).ok)
        accepted = tools.run_tool("update_plan", {"plan": technical_plan}, st)

        self.assertTrue(accepted.ok)
        self.assertTrue(accepted.plan_updated)
        self.assertEqual(st.planner_observations, {"list_dir", "read_file"})

    def test_empty_project_can_plan_directly_but_generic_template_is_rejected(self):
        st = State()
        st.initialize_workspace_tracking(self.tmpdir)
        st.begin_turn(task="Create a durable local task scheduler")
        generic = [
            {"step": "Implement feature", "status": "in_progress"},
            {"step": "Write tests", "status": "pending"},
            {"step": "Write README", "status": "pending"},
            {"step": "Run tests", "status": "pending"},
        ]

        result = tools.run_tool("update_plan", {"plan": generic}, st)

        self.assertFalse(result.ok)
        self.assertEqual(st.plan.items, [])

    def test_readme_is_not_a_default_milestone(self):
        plan = [
            {"step": "Define scheduler states and persistence keys", "status": "in_progress"},
            {"step": "Implement restart recovery and retry transitions", "status": "pending"},
            {"step": "Update README", "status": "pending"},
            {"step": "Run the full regression verifier", "status": "pending"},
        ]
        issue = plan_policy_issue(
            plan,
            task="Implement durable task scheduling",
            existing_workspace=False,
            investigation_tools=set(),
            initial_plan=True,
        )
        allowed_when_requested = plan_policy_issue(
            plan,
            task="Implement durable task scheduling and update the README",
            existing_workspace=False,
            investigation_tools=set(),
            initial_plan=True,
        )

        self.assertIsNotNone(issue)
        self.assertIsNone(allowed_when_requested)

    def test_policy_accepts_distinct_domain_milestones_not_one_shared_template(self):
        scheduler = [
            {"step": "Define task lifecycle states and SQLite schema", "status": "in_progress"},
            {"step": "Persist restart recovery and execution leases", "status": "pending"},
            {"step": "Add cancellation and bounded retry transitions", "status": "pending"},
            {"step": "Cover recovery and invalid-transition boundaries", "status": "pending"},
            {"step": "Run the full regression verifier", "status": "pending"},
        ]
        grades = [
            {"step": "Locate grade models and statistics entry points", "status": "in_progress"},
            {"step": "Define score buckets and distribution contracts", "status": "pending"},
            {"step": "Connect distribution queries through service and CLI", "status": "pending"},
            {"step": "Cover boundary scores, empty courses, and missing courses", "status": "pending"},
            {"step": "Run the full regression verifier", "status": "pending"},
        ]

        issues = [
            plan_policy_issue(
                candidate,
                task=task,
                existing_workspace=False,
                investigation_tools=set(),
                initial_plan=True,
            )
            for candidate, task in (
                (scheduler, "Build a local task scheduler"),
                (grades, "Add grade distribution queries"),
            )
        ]

        self.assertEqual(issues, [None, None])
        self.assertNotEqual(
            {item["step"] for item in scheduler[:-1]},
            {item["step"] for item in grades[:-1]},
        )

    def test_completed_plan_cannot_satisfy_verification(self):
        Path(self.tmpdir, "a.py").write_text("value = 1\n", encoding="utf-8")
        st = State(changed=True)
        st.initialize_workspace_tracking(self.tmpdir)
        st.plan.replace([
            {"step": "Trace the value flow", "status": "completed"},
            {"step": "Correct the boundary behavior", "status": "completed"},
            {"step": "Run the full regression verifier", "status": "completed"},
        ])

        self.assertFalse(st.verification_current())
        self.assertFalse(st.verification_satisfied())


if __name__ == "__main__":
    unittest.main()
