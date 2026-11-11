"""Tests for config loading and validation (hookdoctor.config)."""

import json
import os
import tempfile
import unittest

from hookdoctor import config


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestLoadSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_hooks(self):
        path = os.path.join(self.dir, "settings.json")
        _write(path, json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "guard.sh"}]},
                    {"matcher": "Edit", "hooks": [
                        {"type": "command", "command": "lint.sh",
                         "timeout": 30}]},
                ],
                "Stop": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": "check.sh"}]},
                ],
            }}))
        hooks, errors = config.load_settings_file(path)
        self.assertEqual(errors, [])
        self.assertEqual(len(hooks), 3)
        self.assertEqual(hooks[0].event, "PreToolUse")
        self.assertEqual(hooks[0].matcher, "Bash")
        self.assertEqual(hooks[0].type, "command")
        self.assertEqual(hooks[2].matcher, "")

    def test_no_hooks_key(self):
        path = os.path.join(self.dir, "settings.json")
        _write(path, json.dumps({"permissions": {"allow": ["Bash(git *)"]}}))
        hooks, errors = config.load_settings_file(path)
        self.assertEqual(hooks, [])
        self.assertEqual(errors, [])

    def test_bad_json(self):
        path = os.path.join(self.dir, "settings.json")
        _write(path, "{not json")
        hooks, errors = config.load_settings_file(path)
        self.assertEqual(hooks, [])
        self.assertEqual(len(errors), 1)

    def test_hooks_not_object(self):
        path = os.path.join(self.dir, "settings.json")
        _write(path, json.dumps({"hooks": ["PreToolUse"]}))
        hooks, errors = config.load_settings_file(path)
        self.assertEqual(hooks, [])
        self.assertEqual(len(errors), 1)


class TestFrontmatter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_skill_frontmatter_hooks(self):
        path = os.path.join(self.dir, ".claude", "skills", "review",
                            "SKILL.md")
        _write(path, """---
name: review
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: guard.sh
---

# Review skill
""")
        hooks, errors = config.parse_frontmatter_hooks(path)
        self.assertEqual(errors, [])
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].event, "PreToolUse")
        self.assertEqual(hooks[0].matcher, "Bash")
        self.assertEqual(hooks[0].source_kind, "skill")

    def test_no_frontmatter(self):
        path = os.path.join(self.dir, "SKILL.md")
        _write(path, "# Just a skill\n")
        hooks, errors = config.parse_frontmatter_hooks(path)
        self.assertEqual(hooks, [])
        self.assertEqual(errors, [])

    def test_block_scalar_rejected(self):
        path = os.path.join(self.dir, "SKILL.md")
        _write(path, "---\nname: x\nhooks:\n  Stop: |\n    blocked\n---\n")
        hooks, errors = config.parse_frontmatter_hooks(path)
        self.assertEqual(hooks, [])
        self.assertEqual(len(errors), 1)


class TestValidateHandler(unittest.TestCase):
    def _hook(self, event, handler, kind="settings"):
        return config.Hook(event, "", handler, "/tmp/settings.json", kind)

    def test_missing_type(self):
        findings = config.validate_handler(
            self._hook("PreToolUse", {"command": "x.sh"}))
        self.assertTrue(any(f["code"] == "missing-type" for f in findings))

    def test_unknown_type(self):
        findings = config.validate_handler(
            self._hook("PreToolUse", {"type": "carrier-pigeon"}))
        self.assertTrue(any(f["code"] == "unknown-type" for f in findings))

    def test_missing_command(self):
        findings = config.validate_handler(
            self._hook("PreToolUse", {"type": "command"}))
        self.assertTrue(any(f["code"] == "missing-field" for f in findings))

    def test_http_needs_url(self):
        findings = config.validate_handler(
            self._hook("PreToolUse", {"type": "http"}))
        self.assertTrue(any(f["code"] == "missing-field" for f in findings))

    def test_unknown_event(self):
        findings = config.validate_handler(
            self._hook("BeforeBreakfast", {"type": "command",
                                           "command": "x.sh"}))
        self.assertTrue(any(f["code"] == "unknown-event" for f in findings))

    def test_if_on_non_tool_event_never_runs(self):
        findings = config.validate_handler(
            self._hook("Stop", {"type": "command", "command": "x.sh",
                                "if": "Bash(git *)"}))
        self.assertTrue(any(f["code"] == "if-never-runs" for f in findings))

    def test_async_on_blocking_event(self):
        findings = config.validate_handler(
            self._hook("PreToolUse", {"type": "command", "command": "x.sh",
                                      "async": True}))
        self.assertTrue(any(f["code"] == "async-blocking" for f in findings))

    def test_once_outside_skill_ignored(self):
        findings = config.validate_handler(
            self._hook("Stop", {"type": "command", "command": "x.sh",
                                "once": True}))
        self.assertTrue(any(f["code"] == "once-ignored" for f in findings))

    def test_bad_timeout(self):
        findings = config.validate_handler(
            self._hook("Stop", {"type": "command", "command": "x.sh",
                                "timeout": -5}))
        self.assertTrue(any(f["code"] == "bad-timeout" for f in findings))

    def test_onfailure_on_prompt_hook(self):
        findings = config.validate_handler(
            self._hook("Stop", {"type": "prompt", "prompt": "check",
                                "onFailure": "block"}))
        self.assertTrue(any(f["code"] == "onfailure-unsupported"
                            for f in findings))

    def test_healthy_handler_no_findings(self):
        findings = config.validate_handler(
            self._hook("PreToolUse", {"type": "command",
                                      "command": "guard.sh",
                                      "timeout": 10,
                                      "onFailure": "block"}))
        self.assertEqual(findings, [])


class TestCollectHooks(unittest.TestCase):
    def test_collects_project_settings(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, ".claude", "settings.json"), json.dumps({
                "hooks": {"Stop": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "a.sh"}]}]}}))
            _write(os.path.join(d, ".claude", "settings.local.json"),
                   json.dumps({
                       "hooks": {"PreToolUse": [{"matcher": "Bash",
                                                 "hooks": [{"type": "command",
                                                            "command": "b.sh"}]}]}}))
            hooks, errors, sources = config.collect_hooks(
                d, include_user=False)
            self.assertEqual(errors, [])
            self.assertEqual(len(hooks), 2)
            self.assertEqual(len(sources), 2)


if __name__ == "__main__":
    unittest.main()
