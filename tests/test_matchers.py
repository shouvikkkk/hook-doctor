"""Tests for matcher and `if` semantics (hookdoctor.matchers)."""

import unittest

from hookdoctor import matchers


class TestMatcherKind(unittest.TestCase):
    def test_all(self):
        self.assertEqual(matchers.matcher_kind("", "PreToolUse"), "all")
        self.assertEqual(matchers.matcher_kind("*", "PreToolUse"), "all")
        self.assertEqual(matchers.matcher_kind(None, "PreToolUse"), "all")

    def test_exact(self):
        self.assertEqual(matchers.matcher_kind("Bash", "PreToolUse"), "exact")
        self.assertEqual(matchers.matcher_kind("Edit|Write", "PreToolUse"),
                         "exact")
        self.assertEqual(matchers.matcher_kind("Edit, Write", "PreToolUse"),
                         "exact")

    def test_regex(self):
        self.assertEqual(matchers.matcher_kind("mcp__.*", "PreToolUse"),
                         "regex")
        self.assertEqual(matchers.matcher_kind("^Notebook", "PreToolUse"),
                         "regex")

    def test_narrow_events(self):
        # StopFailure: hyphen keeps it on the regex path.
        self.assertEqual(matchers.matcher_kind("rate-limit", "StopFailure"),
                         "regex")
        self.assertEqual(matchers.matcher_kind("rate_limit", "StopFailure"),
                         "exact")
        # FileChanged: only | separates alternatives.
        self.assertEqual(matchers.matcher_kind(".env|.envrc", "FileChanged"),
                         "exact")


class TestEvaluateMatcher(unittest.TestCase):
    def test_exact_list(self):
        self.assertTrue(matchers.evaluate_matcher("Edit|Write", "Edit",
                                                  "PreToolUse")[0])
        self.assertFalse(matchers.evaluate_matcher("Edit|Write", "Bash",
                                                   "PreToolUse")[0])
        self.assertTrue(matchers.evaluate_matcher("Edit, Write", "Write",
                                                  "PreToolUse")[0])

    def test_regex_unanchored(self):
        # Edit.* matches NotebookEdit (unanchored, per the docs).
        self.assertTrue(matchers.evaluate_matcher("Edit.*", "NotebookEdit",
                                                  "PreToolUse")[0])
        self.assertFalse(matchers.evaluate_matcher("^Edit$", "NotebookEdit",
                                                   "PreToolUse")[0])

    def test_bad_regex(self):
        matches, note = matchers.evaluate_matcher("([", "Bash", "PreToolUse")
        self.assertIsNone(matches)
        self.assertIn("not evaluable", note)


class TestEvaluateIf(unittest.TestCase):
    def test_bare_tool(self):
        runs, _ = matchers.evaluate_if("Bash", "Bash",
                                       {"command": "git status"})
        self.assertTrue(runs)
        runs, _ = matchers.evaluate_if("Bash", "Edit",
                                       {"file_path": "x.py"})
        self.assertFalse(runs)

    def test_bash_wildcard(self):
        runs, _ = matchers.evaluate_if("Bash(git *)", "Bash",
                                       {"command": "git push origin main"})
        self.assertTrue(runs)
        runs, _ = matchers.evaluate_if("Bash(git *)", "Bash",
                                       {"command": "npm test"})
        self.assertFalse(runs)

    def test_leading_assignments_stripped(self):
        runs, _ = matchers.evaluate_if("Bash(git *)", "Bash",
                                       {"command": "FOO=bar git push"})
        self.assertTrue(runs)

    def test_subcommands_checked(self):
        runs, _ = matchers.evaluate_if("Bash(git *)", "Bash",
                                       {"command": "npm test && git push"})
        self.assertTrue(runs)

    def test_command_substitution_checked(self):
        runs, _ = matchers.evaluate_if("Bash(rm *)", "Bash",
                                       {"command": "echo $(rm -rf /)"})
        self.assertTrue(runs)
        runs, _ = matchers.evaluate_if("Bash(rm *)", "Bash",
                                       {"command": "echo $(date)"})
        self.assertFalse(runs)

    def test_file_tool(self):
        runs, _ = matchers.evaluate_if("Edit(*.ts)", "Edit",
                                       {"file_path": "src/app.ts"})
        self.assertTrue(runs)
        runs, _ = matchers.evaluate_if("Edit(*.ts)", "Edit",
                                       {"file_path": "src/app.py"})
        self.assertFalse(runs)

    def test_malformed(self):
        runs, note = matchers.evaluate_if("Bash(git", "Bash",
                                          {"command": "git status"})
        self.assertIsNone(runs)
        self.assertIn("malformed", note)

    def test_hostile_bash_matches_rm_pattern(self):
        runs, _ = matchers.evaluate_if("Bash(rm *)", "Bash",
                                       {"command": "rm -rf /tmp/x"})
        self.assertTrue(runs)


if __name__ == "__main__":
    unittest.main()
