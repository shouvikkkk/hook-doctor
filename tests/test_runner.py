"""Tests for the hook runner (hookdoctor.runner)."""

import os
import tempfile
import unittest

from hookdoctor import runner


class TestRunner(unittest.TestCase):
    def test_runs_hook_and_captures_output(self):
        handler = {"type": "command",
                   "command": "echo '{\"hookSpecificOutput\": {}}'"}
        result = runner.run_command_hook(
            handler, {"hook_event_name": "Stop"}, timeout_s=5)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hookSpecificOutput", result.stdout)
        self.assertFalse(result.timed_out)
        self.assertGreaterEqual(result.duration_s, 0.0)

    def test_stdin_receives_payload(self):
        handler = {"type": "command", "command": "cat"}
        result = runner.run_command_hook(
            handler, {"hook_event_name": "Stop", "x": 1}, timeout_s=5)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('"hook_event_name": "Stop"', result.stdout)

    def test_timeout(self):
        handler = {"type": "command", "command": "sleep 30"}
        result = runner.run_command_hook(handler, {}, timeout_s=0.2)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)

    def test_missing_executable(self):
        handler = {"type": "command",
                   "command": "/nonexistent/hook-doctor-nope.sh"}
        result = runner.run_command_hook(handler, {}, timeout_s=5)
        # sh -c reports 127; the resolver flags it statically before
        # execution, but the runner itself must not crash.
        self.assertEqual(result.exit_code, 127)

    def test_exec_form(self):
        handler = {"type": "command", "command": "/bin/echo",
                   "args": ["hello"]}
        result = runner.run_command_hook(handler, {}, timeout_s=5)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_env_is_scrubbed(self):
        os.environ["HOOK_DOCTOR_SECRET_TEST"] = "should-not-leak"
        try:
            handler = {"type": "command",
                       "command": "env | grep HOOK_DOCTOR_SECRET_TEST || true"}
            result = runner.run_command_hook(handler, {}, timeout_s=5)
            self.assertNotIn("should-not-leak", result.stdout)
        finally:
            del os.environ["HOOK_DOCTOR_SECRET_TEST"]

    def test_placeholders_point_inside_sandbox(self):
        handler = {"type": "command",
                   "command": "echo $CLAUDE_PROJECT_DIR"}
        result = runner.run_command_hook(handler, {}, timeout_s=5)
        self.assertIn("hook-doctor-", result.stdout)

    def test_stdout_json_parsing(self):
        handler = {"type": "command",
                   "command": "printf '{\"a\": 1}'"}
        result = runner.run_command_hook(handler, {}, timeout_s=5)
        self.assertEqual(result.stdout_json, {"a": 1})
        self.assertIsNone(result.stdout_json_error)

    def test_stdout_json_error(self):
        handler = {"type": "command", "command": "printf '{bad json}'"}
        result = runner.run_command_hook(handler, {}, timeout_s=5)
        self.assertIsNone(result.stdout_json)
        self.assertIn("not valid JSON", result.stdout_json_error)

    def test_plain_text_stdout(self):
        handler = {"type": "command", "command": "echo hello"}
        result = runner.run_command_hook(handler, {}, timeout_s=5)
        self.assertIsNone(result.stdout_json)
        self.assertIn("plain text", result.stdout_json_error)


class TestResolveCommandPath(unittest.TestCase):
    def test_builtin(self):
        kind, _ = runner.resolve_command_path("echo hi", "/tmp")
        self.assertEqual(kind, "builtin")

    def test_on_path(self):
        kind, _ = runner.resolve_command_path("sh -c 'true'", "/tmp")
        self.assertEqual(kind, "path")

    def test_missing(self):
        kind, _ = runner.resolve_command_path(
            "hook-doctor-definitely-missing-xyz", "/tmp")
        self.assertEqual(kind, "missing")

    def test_file_not_executable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "guard.sh")
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            kind, _ = runner.resolve_command_path("./guard.sh", d)
            self.assertEqual(kind, "file-not-executable")
            os.chmod(path, 0o755)
            kind, _ = runner.resolve_command_path("./guard.sh", d)
            self.assertEqual(kind, "file")


if __name__ == "__main__":
    unittest.main()
