"""Tests for run classification and guard analysis (hookdoctor.analyze)."""

import unittest

from hookdoctor import analyze, config
from hookdoctor.runner import RunResult


def _result(exit_code=0, stdout="", stderr="", timed_out=False,
            spawn_error=None):
    return RunResult(exit_code, stdout, stderr, 0.1, timed_out,
                     spawn_error=spawn_error)


def _hook(event="PreToolUse", handler=None):
    return config.Hook(event, "", handler or {"type": "command",
                                              "command": "guard.sh"},
                       "/tmp/settings.json", "settings")


class TestClassifyRun(unittest.TestCase):
    def test_exit_0_plain_text_is_allow(self):
        out = analyze.classify_run("PreToolUse", _result(0, "ok\n"))
        self.assertEqual(out["outcome"], "allow")

    def test_exit_0_valid_json_applies(self):
        out = analyze.classify_run(
            "PreToolUse",
            _result(0, '{"hookSpecificOutput": {"permissionDecision": "deny"}}'))
        self.assertEqual(out["outcome"], "allow")
        self.assertEqual(out["decision"]["hookSpecificOutput"]
                         ["permissionDecision"], "deny")

    def test_exit_0_bad_json_is_non_blocking_error(self):
        # Starts and ends with braces but is not valid JSON: a parse
        # failure. (A string that merely starts with "{" is plain text
        # per the docs, and plain text on exit 0 is success.)
        out = analyze.classify_run("PreToolUse", _result(0, '{"a": }'))
        self.assertEqual(out["outcome"], "non-blocking-error")

    def test_exit_0_brace_prefix_is_plain_text(self):
        out = analyze.classify_run("PreToolUse", _result(0, "{bad"))
        self.assertEqual(out["outcome"], "allow")

    def test_exit_2_blocks(self):
        out = analyze.classify_run("PreToolUse",
                                   _result(2, "", "not allowed\n"))
        self.assertEqual(out["outcome"], "block")

    def test_exit_2_cannot_block_on_posttooluse(self):
        out = analyze.classify_run("PostToolUse", _result(2, "", "x\n"))
        self.assertEqual(out["outcome"], "block-ignored")

    def test_exit_1_with_valid_json_decides(self):
        out = analyze.classify_run(
            "PreToolUse",
            _result(1, '{"hookSpecificOutput": {"permissionDecision": "deny"}}'))
        self.assertEqual(out["outcome"], "allow")  # fields decide, not code
        self.assertIsNotNone(out["decision"])

    def test_exit_1_plain_text_is_non_blocking_error(self):
        out = analyze.classify_run("PreToolUse", _result(1, "", "boom\n"))
        self.assertEqual(out["outcome"], "non-blocking-error")

    def test_timeout(self):
        out = analyze.classify_run("PreToolUse",
                                   _result(None, "", "", timed_out=True))
        self.assertEqual(out["outcome"], "timeout")

    def test_spawn_error(self):
        out = analyze.classify_run("PreToolUse", _result(
            spawn_error="cannot start hook: boom"))
        self.assertEqual(out["outcome"], "spawn-error")

    def test_worktree_create_any_nonzero_fails(self):
        out = analyze.classify_run("WorktreeCreate", _result(1, "", "x"))
        self.assertEqual(out["outcome"], "fail")

    def test_stop_missing_file_exit_2_is_non_blocking(self):
        out = analyze.classify_run(
            "Stop", _result(2, "", "No such file or directory\n"))
        self.assertEqual(out["outcome"], "non-blocking-error")


class TestDecisionAllows(unittest.TestCase):
    def test_deny(self):
        self.assertFalse(analyze.decision_allows(
            {"hookSpecificOutput": {"permissionDecision": "deny"}}))
        self.assertTrue(analyze.decision_allows(
            {"hookSpecificOutput": {"permissionDecision": "allow"}}))
        self.assertFalse(analyze.decision_allows({"continue": False}))
        self.assertFalse(analyze.decision_allows({"decision": "block"}))
        self.assertIsNone(analyze.decision_allows({}))
        self.assertIsNone(analyze.decision_allows(None))


class TestDecisionPlacement(unittest.TestCase):
    def test_top_level_permission_decision_is_ignored(self):
        findings = analyze.check_decision_placement(
            "PreToolUse", {"permissionDecision": "deny"})
        self.assertTrue(any(f["code"] == "decision-misplaced"
                            for f in findings))

    def test_nested_decision_is_fine(self):
        findings = analyze.check_decision_placement(
            "PreToolUse",
            {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                    "permissionDecision": "deny"}})
        self.assertEqual(findings, [])

    def test_wrong_event_name(self):
        findings = analyze.check_decision_placement(
            "PreToolUse",
            {"hookSpecificOutput": {"hookEventName": "Stop",
                                    "permissionDecision": "deny"}})
        self.assertTrue(any(f["code"] == "decision-wrong-event"
                            for f in findings))


class TestFailOpen(unittest.TestCase):
    def test_onfailure_block_is_fail_closed(self):
        hook = _hook(handler={"type": "command", "command": "g.sh",
                              "onFailure": "block"})
        self.assertEqual(analyze.fail_open_assessment(hook, "timeout"), [])

    def test_crash_fails_open(self):
        hook = _hook()
        findings = analyze.fail_open_assessment(hook, "non-blocking-error")
        self.assertTrue(any(f["code"] == "fails-open" for f in findings))

    def test_healthy_hook_is_only_a_risk_warning(self):
        hook = _hook()
        findings = analyze.fail_open_assessment(hook, "allow")
        self.assertTrue(any(f["code"] == "fails-open-risk" for f in findings))
        self.assertFalse(any(f["severity"] == "error" for f in findings))

    def test_non_blocking_event_has_no_fail_open(self):
        hook = _hook("PostToolUse")
        self.assertEqual(analyze.fail_open_assessment(hook, "timeout"), [])


class TestStopLoop(unittest.TestCase):
    def _outcomes(self, n_blocks):
        return [{"outcome": "block"}] * n_blocks

    def test_looping_hook_flagged(self):
        findings = analyze.stop_loop_assessment(self._outcomes(8))
        self.assertTrue(any(f["code"] == "stop-loop" for f in findings))

    def test_respecting_hook_ok(self):
        outcomes = [{"outcome": "block"}, {"outcome": "allow"}]
        findings = analyze.stop_loop_assessment(outcomes)
        self.assertTrue(any(f["code"] == "stop-loop-ok" for f in findings))
        self.assertFalse(any(f["severity"] == "error" for f in findings))


class TestGuardShaped(unittest.TestCase):
    def test_guard_words(self):
        hook = _hook(handler={"type": "command",
                              "command": "block-rm.sh",
                              "if": "Bash(rm *)"})
        is_guard, reasons = analyze.looks_guard_shaped(hook)
        self.assertTrue(is_guard)
        self.assertTrue(reasons)

    def test_observational_hook(self):
        hook = _hook("PostToolUse", {"type": "command",
                                     "command": "log-usage.sh"})
        is_guard, _ = analyze.looks_guard_shaped(hook)
        self.assertFalse(is_guard)


if __name__ == "__main__":
    unittest.main()
