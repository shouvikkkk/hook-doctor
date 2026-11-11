"""End-to-end tests for the hook-doctor CLI."""

import json
import os
import unittest

from hookdoctor import cli

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BROKEN = os.path.join(ROOT, "examples", "broken-hooks")
HEALTHY = os.path.join(ROOT, "examples", "healthy-hooks")


def _codes(entry):
    return {f["code"] for f in entry["findings"]}


class TestBrokenExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results, cls.summary = cli.check(BROKEN, include_user=False)

    def test_finds_all_hooks(self):
        # 4 PreToolUse + 1 SessionEnd + 2 Stop + 1 Notification = 8
        self.assertEqual(self.summary["hooks"], 8)

    def test_missing_guard_is_a_dead_guard(self):
        # With `sh <script>` a missing script is caught statically:
        # dash exits 2 on a file it cannot open, so without this check
        # the hook would look like it blocks everything.
        entry = [e for e in self.results
                 if e["hook"].handler.get("command")
                 == "sh .claude/hooks/missing-guard.sh"][0]
        self.assertEqual(entry["verdict"], "error")
        self.assertIn("script-not-found", _codes(entry))

    def test_always_allow_guard_fails(self):
        entry = [e for e in self.results
                 if e["hook"].event == "PreToolUse"
                 and e["hook"].matcher == "Bash"
                 and e["hook"].handler.get("if") == "Bash(rm *)"][0]
        self.assertEqual(entry["verdict"], "fail")
        self.assertIn("always-allow-guard", _codes(entry))
        # The hook ran fine and allowed -- so the fail-open finding is
        # the risk warning (no onFailure: block), not a crash.
        self.assertIn("fails-open-risk", _codes(entry))

    def test_stop_loop_fails(self):
        entry = [e for e in self.results
                 if e["hook"].event == "Stop"
                 and not e["hook"].handler.get("if")][0]
        self.assertEqual(entry["verdict"], "fail")
        self.assertIn("stop-loop", _codes(entry))

    def test_if_on_stop_never_runs(self):
        entry = [e for e in self.results
                 if e["hook"].event == "Stop"
                 and e["hook"].handler.get("if")][0]
        self.assertIn("if-never-runs", _codes(entry))

    def test_if_matcher_contradiction(self):
        entry = [e for e in self.results
                 if e["hook"].event == "PreToolUse"
                 and e["hook"].matcher == "Edit"][0]
        self.assertIn("if-matcher-contradiction", _codes(entry))

    def test_misplaced_decision_flagged(self):
        entry = [e for e in self.results
                 if e["hook"].handler.get("command")
                 == "sh .claude/hooks/misplaced.sh"][0]
        self.assertIn("decision-misplaced", _codes(entry))

    def test_sessionend_timeout(self):
        entry = [e for e in self.results
                 if e["hook"].event == "SessionEnd"][0]
        self.assertIn("hook-unhealthy", _codes(entry))

    def test_prompt_hook_is_skipped(self):
        entry = [e for e in self.results
                 if e["hook"].event == "Notification"][0]
        self.assertEqual(entry["verdict"], "skip")
        self.assertIn("not-executable-offline", _codes(entry))

    def test_overall_exit_code_is_2(self):
        self.assertGreater(self.summary["fail"], 0)


class TestHealthyExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results, cls.summary = cli.check(HEALTHY, include_user=False)

    def test_all_hooks_pass(self):
        self.assertEqual(self.summary["hooks"], 3)
        self.assertEqual(self.summary["fail"], 0)
        self.assertEqual(self.summary["warn"], 0)
        for entry in self.results:
            self.assertEqual(entry["verdict"], "pass", entry["hook"].label())

    def test_guard_blocks_hostile_allows_benign(self):
        entry = [e for e in self.results
                 if e["hook"].event == "PreToolUse"][0]
        self.assertIn("guard-works", _codes(entry))

    def test_stop_hook_respects_flag(self):
        entry = [e for e in self.results
                 if e["hook"].event == "Stop"][0]
        self.assertIn("stop-loop-ok", _codes(entry))


class TestNoRunMode(unittest.TestCase):
    def test_no_run_skips_execution(self):
        results, summary = cli.check(BROKEN, include_user=False, run=False)
        # Static findings still fire (missing command, contradictions),
        # but no runtime findings appear.
        self.assertEqual(summary["hooks"], 8)
        for entry in results:
            self.assertNotIn("always-allow-guard", _codes(entry))
            self.assertIsNone(entry["runtime"])

    def test_json_output(self):
        results, summary = cli.check(HEALTHY, include_user=False)
        from hookdoctor import report
        text = report.render_json(results, summary)
        parsed = json.loads(text)
        self.assertEqual(parsed["summary"]["hooks"], 3)
        self.assertEqual(len(parsed["hooks"]), 3)

    def test_main_exit_codes(self):
        self.assertEqual(cli.main(["check", HEALTHY, "--no-user"]), 0)
        self.assertEqual(cli.main(["check", BROKEN, "--no-user"]), 2)
        self.assertEqual(cli.main(["check", "/nonexistent-dir-xyz"]), 3)


class TestEmptyProject(unittest.TestCase):
    def test_no_hooks_found(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            results, summary = cli.check(d, include_user=False)
            self.assertEqual(results, [])
            self.assertEqual(summary["hooks"], 0)

    def test_bare_missing_command_is_static_error(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(d, ".claude", "settings.json"),
                      "w") as fh:
                json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash",
                                                     "hooks": [{"type": "command",
                                                                "command": "hook-doctor-definitely-missing-xyz"}]}]}},
                          fh)
            results, summary = cli.check(d, include_user=False)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["verdict"], "error")
            codes = {f["code"] for f in results[0]["findings"]}
            self.assertIn("command-not-found", codes)

    def test_missing_script_behind_interpreter(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(d, ".claude", "settings.json"),
                      "w") as fh:
                json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash",
                                                     "hooks": [{"type": "command",
                                                                "command": "sh .claude/hooks/nope.sh"}]}]}},
                          fh)
            results, summary = cli.check(d, include_user=False)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["verdict"], "error")
            codes = {f["code"] for f in results[0]["findings"]}
            self.assertIn("script-not-found", codes)


if __name__ == "__main__":
    unittest.main()
